"""LinkedIn background scanner.

Architecture:
  LinkedInScanner    — runs all configured queries sequentially, persists each
                       job to LinkedInStagingStorage as it is discovered.
                       The scanner never participates in the normal BaseCollector
                       execution path and never makes the pipeline wait for it.

Compliance notes:
  - This scanner uses python-jobspy to access LinkedIn's public job search.
  - It does NOT bypass authentication, CAPTCHAs, or access controls.
  - Configurable request delays (rate_limit_seconds) must be respected.
  - Users are responsible for compliance with LinkedIn's Terms of Service.
  - 429 / 503 responses cause the current query to abort gracefully; already
    persisted jobs are safe. The scan continues with the next query.

Pagination stopping conditions (per query):
  1. JobSpy returns an empty DataFrame → query exhausted.
  2. max_pages_per_query safety limit reached (detected via for-else).
  Conditions that do NOT stop pagination:
  - A page where all jobs are duplicates (already in seen_ids).
  - A page where all jobs are rejected by the hard filter.
  - A page where all jobs are already in the database.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.linkedin import scan_progress
from src.linkedin.staging import LinkedInStagingStorage
from src.requirements.principal_skill import extract_known_technologies
from src.scan_control import (
    STATE_PAUSED,
    STATE_RUNNING,
    StopSignal,
    config_fingerprint,
    interrupted_checkpoint_id,
    persist_pause,
    record_shutdown_stop,
    resumable_scan_id,
    scan_state,
    wait_for_stop,
)

logger = logging.getLogger(__name__)


class _MessageCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _jobspy_errors() -> Iterator[list[str]]:
    """ERROR messages python-jobspy logs during the block.

    jobspy does not raise on an HTTP error (429 when rate limited), a network
    error or a blocked page: it logs an ERROR on its "JobSpy:LinkedIn" logger
    and returns the rows it has, possibly none. Its end-of-results cases log
    nothing above WARNING, so an ERROR here is what tells a failed page apart
    from an exhausted query.
    """
    collector = _MessageCollector()
    jobspy_logger = logging.getLogger("JobSpy:LinkedIn")
    jobspy_logger.addHandler(collector)
    try:
        yield collector.messages
    finally:
        jobspy_logger.removeHandler(collector)

# ---------------------------------------------------------------------------
# Location / field helpers
# ---------------------------------------------------------------------------

_LOCATION_COUNTRY_MAP: dict[str, str] = {
    # France
    "france": "France", "paris": "France", "lyon": "France", "marseille": "France",
    "toulouse": "France", "nantes": "France", "bordeaux": "France", "lille": "France",
    "île-de-france": "France", "ile-de-france": "France",
    # Belgium
    "belgium": "Belgium", "belgique": "Belgium", "bruxelles": "Belgium", "brussels": "Belgium",
    # Luxembourg
    "luxembourg": "Luxembourg",
    # Switzerland
    "switzerland": "Switzerland", "suisse": "Switzerland", "zürich": "Switzerland",
    "zurich": "Switzerland", "genève": "Switzerland", "geneva": "Switzerland",
    # Germany
    "germany": "Germany", "deutschland": "Germany", "berlin": "Germany",
    "munich": "Germany", "münchen": "Germany",
    # United Kingdom
    "united kingdom": "United Kingdom", "great britain": "United Kingdom",
    "london": "United Kingdom", "england": "United Kingdom",
    # Netherlands
    "netherlands": "Netherlands", "pays-bas": "Netherlands", "amsterdam": "Netherlands",
    # Spain
    "spain": "Spain", "espagne": "Spain", "madrid": "Spain", "barcelona": "Spain",
    # Italy
    "italy": "Italy", "italie": "Italy", "rome": "Italy", "milan": "Italy",
    # Canada
    "canada": "Canada",
    # United States
    "united states": "United States", "usa": "United States",
}


def _extract_country(location: str | None) -> list[str]:
    if not location:
        return []
    loc_lower = location.lower()
    for keyword, code in _LOCATION_COUNTRY_MAP.items():
        if keyword in loc_lower:
            return [code]
    return []


def _extract_work_mode(is_remote: bool, job_type: str | None) -> str | None:
    if is_remote:
        return "remote"
    if job_type:
        jt = str(job_type).lower()
        if "hybrid" in jt:
            return "hybrid"
        if "on_site" in jt or "onsite" in jt:
            return "on_site"
        if "remote" in jt:
            return "remote"
    return None


# Work-mode signals found in job titles and location strings.
# Bounded by parentheses, brackets, or delimiters to avoid false positives
# from technology names containing "remote" (e.g. "Kafka Remote Procedure
# Call", "Prometheus Remote Write").
_TITLE_REMOTE_MARKERS: tuple[str, ...] = (
    "(remote)", "[remote]",
    "- remote", "| remote", "/ remote",
    "(télétravail)", "[télétravail]", "(teletravail)", "[teletravail]",
    "- télétravail", "| télétravail",
)
_TITLE_HYBRID_MARKERS: tuple[str, ...] = (
    "(hybrid)", "[hybrid]", "(hybride)", "[hybride]",
    "- hybrid", "| hybrid", "- hybride", "| hybride",
    "(télétravail partiel)", "(teletravail partiel)",
)
_TITLE_ONSITE_MARKERS: tuple[str, ...] = (
    "(on-site)", "[on-site]", "(on site)", "[on site]",
    "(présentiel)", "[présentiel]", "(presentiel)", "[presentiel]",
    "- on-site", "| on-site", "- présentiel", "- presentiel",
)


def _extract_work_mode_from_title(title: str | None) -> str | None:
    """Extract work mode from a job title using conservative parenthetical/delimiter patterns.

    Returns None when no clear signal is found, preserving unknown-is-unknown semantics.
    Does NOT match bare 'remote' in a title to avoid false positives from technology
    names like 'Kafka Remote Procedure Call' or 'Prometheus Remote Write'.
    """
    if not title:
        return None
    t = title.lower()
    for marker in _TITLE_REMOTE_MARKERS:
        if marker in t:
            return "remote"
    for marker in _TITLE_HYBRID_MARKERS:
        if marker in t:
            return "hybrid"
    for marker in _TITLE_ONSITE_MARKERS:
        if marker in t:
            return "on_site"
    return None


def _extract_work_mode_from_location(location: str | None) -> str | None:
    """Extract a work-mode hint from the LinkedIn location string.

    LinkedIn location values such as 'Remote', 'France (Hybrid)', or
    'Paris, Île-de-France, France (Remote)' carry direct work-mode signals.
    Returns None when no unambiguous signal is found.
    """
    if not location:
        return None
    loc = location.lower().strip()
    if loc == "remote":
        return "remote"
    for marker in ("(remote)", "(télétravail)", "(teletravail)"):
        if marker in loc:
            return "remote"
    for marker in ("(hybrid)", "(hybride)"):
        if marker in loc:
            return "hybrid"
    for marker in ("(on-site)", "(présentiel)", "(presentiel)"):
        if marker in loc:
            return "on_site"
    return None


# Strong description-based work-mode signals.  Listed from most specific to
# least specific; checked in order so "100% télétravail" wins over "hybride"
# if both appear (pathological case).
_REMOTE_DESC_SIGNALS: tuple[str, ...] = (
    "100% télétravail", "100% teletravail",
    "100% remote", "fully remote", "full remote",
    "télétravail complet", "teletravail complet",
    "remote first", "remote-first",
    "100% en télétravail", "100% en teletravail",
    "télétravail 5j", "teletravail 5j",
    "full télétravail", "full teletravail",
)
_HYBRID_DESC_SIGNALS: tuple[str, ...] = (
    "télétravail partiel", "teletravail partiel",
    "hybrid", "hybride",
    "partial remote", "semi-remote", "semi remote",
)
_ONSITE_DESC_SIGNALS: tuple[str, ...] = (
    "présentiel", "presentiel",
    "en présentiel", "en presentiel",
    "sur site", "sur place",
)


def _extract_work_mode_from_text(description: str) -> str | None:
    """Infer work mode from free-form description text.

    Conservative: only returns a value on clear, unambiguous signals.
    Does NOT match bare "télétravail" to avoid false-positives from
    negative contexts like "pas de télétravail".
    """
    if not description:
        return None
    desc = description.lower()
    for signal in _REMOTE_DESC_SIGNALS:
        if signal in desc:
            return "remote"
    for signal in _HYBRID_DESC_SIGNALS:
        if signal in desc:
            return "hybrid"
    for signal in _ONSITE_DESC_SIGNALS:
        if signal in desc:
            return "on_site"
    return None


def _extract_employment_type(job_type: str | None, title: str | None = None) -> str | None:
    """Extract employment type from JobSpy structured field and/or job title.

    Checks the structured job_type field first (highest confidence), then falls
    back to title-based signals for French employment types (alternance, stage,
    CDI, CDD) that LinkedIn often encodes in the title rather than metadata.

    Uses conservative delimited patterns in titles to reduce false positives:
    parentheses, brackets, or common delimiters (dash, pipe, slash, space-bounded).
    """
    # -- Structured field (highest confidence) --------------------------------
    if job_type:
        jt = str(job_type).lower()
        if any(k in jt for k in ("fulltime", "full_time", "full-time")):
            return "full_time"
        if any(k in jt for k in ("parttime", "part_time", "part-time")):
            return "part_time"
        if any(k in jt for k in ("alternance", "alternant", "apprentissage", "apprenti")):
            return "alternance"
        if "intern" in jt:
            return "internship"
        if "freelance" in jt:
            return "freelance"
        if "contract" in jt:
            return "contract"
        if "temporar" in jt:
            return "contract"

    # -- Title-based fallback (common in French LinkedIn postings) ------------
    if title:
        t = title.lower()
        # Alternance / apprentissage — word-boundary matching handles "(alternance/CDI)" etc.
        if re.search(r"\balternance\b", t) or re.search(r"\balternant\b", t):
            return "alternance"
        if re.search(r"\bapprentissage\b", t) or re.search(r"\bapprenti\b", t):
            return "alternance"
        # Stage / internship — word-boundary is safe for "stage" in IT job titles
        if re.search(r"\bstagiaire\b", t) or re.search(r"\bstage\b", t):
            return "internship"
        # CDI / CDD — use word-boundary regex to catch "(CDI/FREELANCE)", "CDI CDI", etc.
        # \b ensures we don't match "cdi" inside longer words (none exist in French IT titles).
        if re.search(r"\bcdi\b", t):
            return "full_time"
        if re.search(r"\bcdd\b", t):
            return "contract"
        # Freelance — word-boundary safe
        if re.search(r"\bfreelance\b", t) or re.search(r"\bindépendant\b", t) or re.search(r"\bindependant\b", t):
            return "freelance"

    return None


# ---------------------------------------------------------------------------
# Title-based skill extraction
# ---------------------------------------------------------------------------

# Tech skill tokens safe for word-boundary extraction from job titles.
# (token_lower, canonical_display) — token must be specific enough that
# false-positives in normal French/English job titles are essentially zero.
_TITLE_SKILL_VOCAB: tuple[tuple[str, str], ...] = (
    # Cloud platforms
    ("aws", "AWS"), ("azure", "Azure"), ("gcp", "GCP"),
    ("google cloud", "Google Cloud"),
    # Container / orchestration
    ("kubernetes", "Kubernetes"), ("k8s", "Kubernetes"),
    ("docker", "Docker"), ("helm", "Helm"), ("openshift", "OpenShift"),
    # Infrastructure-as-code
    ("terraform", "Terraform"), ("ansible", "Ansible"),
    ("puppet", "Puppet"), ("packer", "Packer"),
    # CI/CD
    ("jenkins", "Jenkins"), ("gitlab", "GitLab"), ("github", "GitHub"),
    ("argocd", "ArgoCD"), ("ci/cd", "CI/CD"),
    ("github actions", "GitHub Actions"), ("gitlab ci", "GitLab CI"),
    # Observability
    ("prometheus", "Prometheus"), ("grafana", "Grafana"),
    ("datadog", "Datadog"), ("splunk", "Splunk"), ("elastic", "Elasticsearch"),
    # Languages
    ("python", "Python"), ("golang", "Go"), ("rust", "Rust"),
    ("java", "Java"), ("scala", "Scala"), ("kotlin", "Kotlin"),
    ("javascript", "JavaScript"), ("typescript", "TypeScript"),
    ("php", "PHP"), ("ruby", "Ruby"), ("swift", "Swift"),
    # Web frameworks
    ("django", "Django"), ("flask", "Flask"), ("fastapi", "FastAPI"),
    ("spring", "Spring"), ("react", "React"), ("angular", "Angular"),
    ("vue.js", "Vue.js"), ("nodejs", "Node.js"), ("node.js", "Node.js"),
    ("laravel", "Laravel"), ("symfony", "Symfony"),
    # Data / databases
    ("postgresql", "PostgreSQL"), ("mysql", "MySQL"),
    ("mongodb", "MongoDB"), ("redis", "Redis"),
    ("elasticsearch", "Elasticsearch"), ("kafka", "Kafka"),
    ("spark", "Spark"), ("airflow", "Airflow"), ("dbt", "dbt"),
    ("hadoop", "Hadoop"), ("flink", "Flink"),
    # AI / ML
    ("machine learning", "Machine Learning"), ("deep learning", "Deep Learning"),
    ("tensorflow", "TensorFlow"), ("pytorch", "PyTorch"),
    ("scikit-learn", "scikit-learn"), ("mlops", "MLOps"),
    # Security (specific enough to be unambiguous in IT job titles)
    ("siem", "SIEM"), ("soc", "SOC"), ("iam", "IAM"),
    ("pentest", "Pentest"), ("pentesting", "Pentest"),
    ("devsecops", "DevSecOps"), ("owasp", "OWASP"),
    ("vulnerability", "Vulnerability Management"),
    # Systems / networking
    ("linux", "Linux"), ("bash", "Bash"), ("git", "Git"),
    ("active directory", "Active Directory"), ("ldap", "LDAP"),
    ("vmware", "VMware"), ("proxmox", "Proxmox"),
    # .NET / Microsoft
    (".net", ".NET"), ("c#", "C#"), ("powershell", "PowerShell"),
    # Automation
    ("n8n", "n8n"), ("zapier", "Zapier"),
)

# Pre-compiled word-boundary patterns for each skill token.
_TITLE_SKILL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(r"(?<![a-z0-9\.])" + re.escape(token) + r"(?![a-z0-9\.])", re.IGNORECASE), label)
    for token, label in _TITLE_SKILL_VOCAB
)


def _extract_title_skills(title: str | None) -> list[str]:
    """Extract known tech skill names from a job title.

    Uses word-boundary matching so that e.g. 'Java' in 'JavaScript' is not
    extracted and 'node' alone is not extracted.  Returns an empty list when
    no skills are found or the title is absent.  Skills are deduplicated and
    returned in discovery order.
    """
    if not title:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for pattern, label in _TITLE_SKILL_PATTERNS:
        if label not in seen and pattern.search(title):
            seen.add(label)
            result.append(label)
    return result


def _safe_str(val: Any) -> str | None:
    if val is None:
        return None
    try:
        if math.isnan(float(val)):
            return None
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    return s if s and s.lower() not in ("nan", "none", "null", "nat", "") else None


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _safe_date_str(val: Any) -> str | None:
    """Convert a date/datetime/string value to ISO-format string, or None."""
    if val is None:
        return None
    # pandas NaT and float NaN
    try:
        import math
        if math.isnan(float(val)):  # type: ignore[arg-type]
            return None
    except (TypeError, ValueError):
        pass
    # Objects with isoformat (date, datetime, pandas Timestamp)
    if hasattr(val, "isoformat"):
        try:
            iso: str = val.isoformat()
            return iso
        except Exception:  # noqa: BLE001, S110
            pass
    s = str(val).strip()
    return s if s and s.lower() not in ("nan", "none", "nat", "null") else None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class LinkedInQueryConfig:
    search_term: str
    location: str = ""
    results_wanted: int = 50


@dataclass
class LinkedInConfig:
    enabled: bool = False
    schedule_hour: int = 8
    schedule_minute: int = 0
    queries: list[LinkedInQueryConfig] = field(default_factory=list)
    rate_limit_seconds: float = 15.0
    fetch_description: bool = True
    # Whether to perform targeted description fetches for filter survivors.
    # Separate from description_fetch_delay (rate-limiting only).
    enrich_descriptions: bool = False
    # Seconds to wait between post-filter description requests.  Controls
    # pacing only; does not enable or disable enrichment.
    description_fetch_delay: float = 3.0
    hours_old: int = 72
    max_results_per_query: int = 50
    max_pages_per_query: int = 50

    @classmethod
    def load(cls, config_dir: str) -> LinkedInConfig:
        from pathlib import Path

        import yaml

        path = Path(config_dir) / "linkedin.yaml"
        if not path.exists():
            return cls()
        with path.open() as f:
            raw = yaml.safe_load(f) or {}
        schedule = raw.get("schedule") or {}
        queries = [
            LinkedInQueryConfig(
                search_term=q.get("search_term", ""),
                location=q.get("location", ""),
                results_wanted=int(q.get("results_wanted", 50)),
            )
            for q in (raw.get("queries") or [])
            if q.get("search_term")
        ]
        return cls(
            enabled=bool(raw.get("enabled", False)),
            schedule_hour=int(schedule.get("hour", 8)),
            schedule_minute=int(schedule.get("minute", 0)),
            queries=queries,
            rate_limit_seconds=float(raw.get("rate_limit_seconds", 15.0)),
            fetch_description=bool(raw.get("fetch_description", True)),
            enrich_descriptions=bool(raw.get("enrich_descriptions", False)),
            description_fetch_delay=float(raw.get("description_fetch_delay", 3.0)),
            hours_old=int(raw.get("hours_old", 72)),
            max_results_per_query=int(raw.get("max_results_per_query", 50)),
            max_pages_per_query=int(raw.get("max_pages_per_query", 50)),
        )


# ---------------------------------------------------------------------------
# Per-query statistics
# ---------------------------------------------------------------------------

@dataclass
class _QueryStats:
    """Statistics collected during a single query execution."""
    search_term: str
    location: str
    pages_fetched: int = 0
    jobs_raw: int = 0                       # rows returned by JobSpy (before any dedup)
    jobs_unique: int = 0                    # after cross-query/cross-page dedup (scan_seen_ids)
    jobs_cross_query_dups: int = 0          # jobs this query found that a prior query already saw
    jobs_filtered_out: int = 0              # rejected by hard filter
    jobs_already_known: int = 0             # passed filter but already in staging DB
    jobs_staged: int = 0                    # newly inserted into staging
    jobs_unique_to_query: int = 0           # staged exclusively by this query (set after all queries)
    jobs_description_attempted: int = 0    # post-filter description fetch attempts
    jobs_description_fetched: int = 0      # post-filter description fetches that succeeded
    stopped_by: str = "exhaustion"          # "exhaustion" | "safety_limit" | "error" | "resumed_skip"


# Per-query counters kept in the checkpoint's stats_json (issue #86) so a
# logical scan spanning several processes reports every attempt.
# jobs_unique_to_query is derived from staging at completion, not stored.
_PERSISTED_COUNTERS = (
    "pages_fetched",
    "jobs_raw",
    "jobs_unique",
    "jobs_cross_query_dups",
    "jobs_filtered_out",
    "jobs_already_known",
    "jobs_staged",
    "jobs_description_attempted",
    "jobs_description_fetched",
)


def _stats_to_dict(q_stats: _QueryStats) -> dict[str, Any]:
    saved: dict[str, Any] = {name: getattr(q_stats, name) for name in _PERSISTED_COUNTERS}
    saved["stopped_by"] = q_stats.stopped_by
    return saved


def _stats_from_dict(query: LinkedInQueryConfig, saved: dict[str, Any]) -> _QueryStats:
    q_stats = _QueryStats(search_term=query.search_term, location=query.location)
    for name in _PERSISTED_COUNTERS:
        setattr(q_stats, name, int(saved.get(name) or 0))
    q_stats.stopped_by = str(saved.get("stopped_by") or q_stats.stopped_by)
    return q_stats


def _load_scan_stats(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    """The checkpoint's running statistics; empty for a legacy/NULL value."""
    try:
        saved = json.loads((checkpoint or {}).get("stats_json") or "{}")
    except (TypeError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    queries = saved.get("queries")
    return {
        "active_seconds": float(saved.get("active_seconds") or 0.0),
        "queries": dict(queries) if isinstance(queries, dict) else {},
    }


def _query_key(query: LinkedInQueryConfig) -> str:
    """Stable identity for a query, used as the resumable-checkpoint key.

    "\x1f" (ASCII unit separator) is used rather than a printable delimiter
    like "|" since search terms/locations are free-form user config and could
    plausibly contain one.
    """
    return f"{query.search_term}\x1f{query.location or ''}"


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

_LINKEDIN_JOB_ID_RE = re.compile(r"/jobs/view/(\d+)")

# Login-page markers used by _fetch_description (French and English).
_LOGIN_MARKERS: tuple[str, ...] = (
    "S'identifier", "Identifiez-vous", "Inscrivez-vous", "Mot de passe",
    "Sign in", "Sign In", "Join now",
)


def fetch_linkedin_description_classified(
    linkedin_job_id: str,
) -> tuple[str | None, str]:
    """Fetch a LinkedIn job description and return (text, status).

    Status values:
      "success"       — description text returned
      "login_gated"   — HTTP 200 with a sign-in page (retryable later)
      "deleted"       — HTTP 404 / 410 (permanently gone; do not retry)
      "rate_limited"  — HTTP 429 (retryable later)
      "network_error" — connection / timeout failure (retryable)
      "empty"         — response too short to be a real description

    The "li-" prefix on linkedin_job_id (e.g. "li-4457992613") is stripped
    automatically; the guest API needs the bare numeric ID.
    """
    import re as _re
    from html.parser import HTMLParser

    try:
        import requests as _req
    except ImportError:
        return None, "network_error"

    numeric_id_match = _re.search(r"(\d+)$", linkedin_job_id)
    numeric_id = numeric_id_match.group(1) if numeric_id_match else linkedin_job_id
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{numeric_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    try:
        resp = _req.get(url, headers=headers, timeout=15)
    except Exception as exc:
        logger.debug("LinkedIn: request failed for %s: %s", linkedin_job_id, exc)
        return None, "network_error"

    if resp.status_code in (404, 410):
        logger.debug("LinkedIn: job %s is gone (HTTP %d).", linkedin_job_id, resp.status_code)
        return None, "deleted"
    if resp.status_code == 429:
        logger.warning(
            "LinkedIn: rate-limited on description fetch for job %s — backing off.",
            linkedin_job_id,
        )
        return None, "rate_limited"
    if resp.status_code != 200:
        logger.debug("LinkedIn: HTTP %d for job %s.", resp.status_code, linkedin_job_id)
        return None, f"http_{resp.status_code}"

    if "linkedin.com/login" in resp.url or "linkedin.com/signup" in resp.url:
        return None, "login_gated"

    class _TextCollector(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self._parts: list[str] = []
            self._skip = False

        def handle_starttag(self, tag: str, attrs: list) -> None:  # type: ignore[override]
            if tag in ("script", "style"):
                self._skip = True
            elif tag in ("p", "li", "br"):
                self._parts.append("\n")

        def handle_endtag(self, tag: str) -> None:
            if tag in ("script", "style"):
                self._skip = False
            elif tag in ("div", "section"):
                self._parts.append("\n")

        def handle_data(self, data: str) -> None:
            if not self._skip:
                stripped = data.strip()
                if stripped:
                    self._parts.append(stripped)

        def get_text(self) -> str:
            raw = " ".join(self._parts)
            return _re.sub(r"\n{3,}", "\n\n", _re.sub(r" +", " ", raw)).strip()

    collector = _TextCollector()
    try:
        collector.feed(resp.text)
    except Exception:
        return None, "parse_error"

    text = collector.get_text()
    if len(text) < 50:
        return None, "empty"

    if sum(m in text for m in _LOGIN_MARKERS) >= 2:
        logger.debug(
            "LinkedIn: description for job %s is a login-gated page — discarding.",
            linkedin_job_id,
        )
        return None, "login_gated"

    return text, "success"


def fetch_linkedin_description(linkedin_job_id: str) -> str | None:
    """Fetch the full description for one LinkedIn job from the guest API.

    Thin wrapper around fetch_linkedin_description_classified that returns
    the description text or None (for backward compatibility with callers
    that don't need the failure reason).
    """
    text, _status = fetch_linkedin_description_classified(linkedin_job_id)
    return text


class LinkedInScanner:
    """Collects LinkedIn jobs and persists them to the staging storage.

    Public interface:
      run_scan()    — run synchronously in the calling thread (used by scheduler)
      start_scan()  — spawn a daemon thread; returns scan_id (used by API)
      is_scanning() — thread-safe check
    """

    def __init__(
        self,
        config: LinkedInConfig,
        staging: LinkedInStagingStorage,
        config_dir: str = "config",
        shutdown_event: threading.Event | None = None,
        db_path: str | None = None,
    ) -> None:
        self.config = config
        self.staging = staging
        self.config_dir = config_dir
        # jobs.db, read only to resolve the human-approved active strategy for the pre-staging filter;
        # None (tests, standalone use) filters with config/ as before.
        self.db_path = db_path
        self._scan_lock = threading.Lock()
        self._current_scan_id: str | None = None
        # Set at the end of _do_scan to reflect whether the most recent
        # run_scan() call genuinely finished (vs. stopped for shutdown,
        # already-running, or an internally-caught failure). Read by
        # LinkedInHandler.dispatch() (src/scheduler/service.py) so
        # SchedulerService can record schedule_executions.status accurately
        # instead of assuming "dispatch() didn't raise" means "completed".
        # Does not change run_scan()'s own return value/contract at all.
        self._last_scan_completed = False
        # Checked between pages/queries so a backend shutdown (uvicorn
        # --reload restart, or a real stop) can let an in-flight scan finish
        # its current page cleanly and exit at a checkpointed boundary,
        # instead of being abandoned mid-request when the process dies.
        # Does not change checkpoint save/load semantics at all — it only
        # decides *when* the scan stops looping.
        self._shutdown_event = shutdown_event if shutdown_event is not None else threading.Event()
        # User pause (issue #81): private to this scanner, honoured at exactly
        # the same checkpointed boundaries as a shutdown. Cleared whenever a
        # new scan claims the lock, so a stale request never pauses a resume.
        self._pause_event = threading.Event()
        self._stop = StopSignal(self._shutdown_event, self._pause_event)
        self._last_scan_paused = False
        # Running statistics of the logical scan (issue #86): restored from
        # the checkpoint's stats_json when a scan resumes and saved with
        # every page, so they cover all attempts. Active time is measured
        # per process attempt, so downtime between attempts is excluded.
        self._scan_stats: dict[str, Any] = _load_scan_stats(None)
        self._attempt_started = time.monotonic()
        self._attempt_pages = 0

    def is_scanning(self) -> bool:
        with self._scan_lock:
            return self._current_scan_id is not None

    def current_scan_id(self) -> str | None:
        with self._scan_lock:
            return self._current_scan_id

    def last_scan_completed(self) -> bool:
        """Whether the most recently finished run_scan() genuinely completed.

        False for a cooperative shutdown stop, an internally-caught failure,
        or if no scan has run yet in this process.
        """
        return self._last_scan_completed

    def shutdown_requested(self) -> bool:
        """Whether the backend asked running scans to stop (the scheduler
        records such a scan as interrupted, not failed — issue #68)."""
        return self._shutdown_event.is_set()

    # -- pause / resume (issue #81) ---------------------------------------------

    def request_pause(self) -> bool:
        """Ask the running scan to stop at its next checkpoint boundary.

        Returns False (and does nothing) when no scan is running, so a pause
        racing a completion, or a repeated pause, is a harmless no-op.
        """
        with self._scan_lock:
            if self._current_scan_id is None:
                return False
            if not self._pause_event.is_set():
                self._pause_event.set()
                logger.info(
                    "LinkedIn scan %s: pause requested — stopping at the next page boundary.",
                    self._current_scan_id,
                )
            return True

    def pause_requested(self) -> bool:
        return self.is_scanning() and self._pause_event.is_set()

    def last_scan_paused(self) -> bool:
        """Whether the most recently finished run_scan() stopped for a user pause."""
        return self._last_scan_paused

    def scan_state(self) -> str:
        """idle | running | pausing | paused | interrupted (see src/scan_control.py)."""
        return scan_state(
            self.is_scanning(), self._pause_event.is_set(), self.staging.get_scan_checkpoint()
        )

    def is_paused(self) -> bool:
        return self.scan_state() == STATE_PAUSED

    def interrupted_checkpoint_id(self) -> str | None:
        """Identity of an interrupted checkpoint the scheduler may resume
        (see src/scan_control.py); None while scanning or when there is none."""
        if self.is_scanning():
            return None
        return interrupted_checkpoint_id(self.staging.get_scan_checkpoint())

    def has_interrupted_checkpoint(self) -> bool:
        return self.interrupted_checkpoint_id() is not None

    def progress(self) -> dict[str, Any] | None:
        """Durable, work-based progress (issue #86): None when idle with no
        checkpoint. Everything is derived from the checkpoint, the config
        and past completed runs (see src/linkedin/scan_progress.py), so a
        restart never resets it. ETA only while actively running."""
        cp = self.staging.get_scan_checkpoint()
        scanning = self.is_scanning()
        if cp is None and not scanning:
            return None
        keys = [_query_key(q) for q in self.config.queries]
        completed = set(json.loads((cp or {}).get("completed_queries_json") or "[]")) & set(keys)
        current_key = (cp or {}).get("current_query_key")
        current = next((q for q in self.config.queries if _query_key(q) == current_key), None)
        current_page = ((cp or {}).get("current_page") or 0) if current else 0

        try:
            runs = self.staging.list_scan_runs(limit=20)
        except Exception as exc:  # noqa: BLE001 - history only refines the estimate
            logger.debug("LinkedIn: scan history unavailable for progress: %s", exc)
            runs = []
        stats = _load_scan_stats(cp)
        actual_pages = {
            k: int(q.get("pages_fetched") or 0)
            for k, q in stats["queries"].items()
            if k in completed and isinstance(q, dict)
        }
        estimate = scan_progress.estimate_progress(
            keys, completed, current_key if current else None, current_page,
            scan_progress.expected_pages(keys, runs, self.config.max_pages_per_query),
            actual_pages,
        )
        eta = None
        basis = None
        if scan_state(scanning, self._pause_event.is_set(), cp) == STATE_RUNNING:
            rate, basis = scan_progress.seconds_per_page(
                time.monotonic() - self._attempt_started, self._attempt_pages, stats, runs,
            )
            eta = scan_progress.eta_seconds(estimate, rate)
        return {
            "queries_completed": len(completed),
            "queries_total": len(keys),
            "current_query": current.search_term if current else None,
            "current_page": current_page,
            "updated_at": (cp or {}).get("updated_at"),
            **estimate,
            "eta_seconds": eta,
            "eta_basis": basis if eta is not None else None,
        }

    def _stats_snapshot(
        self, current_key: str | None = None, current: _QueryStats | None = None
    ) -> dict[str, Any]:
        """Running statistics to persist with the checkpoint: completed
        queries, the in-progress query so far, and active seconds of all
        attempts (this process's time added to what earlier ones saved)."""
        queries = dict(self._scan_stats["queries"])
        if current_key is not None and current is not None:
            queries[current_key] = _stats_to_dict(current)
        active = self._scan_stats["active_seconds"] + (time.monotonic() - self._attempt_started)
        return {"active_seconds": round(active, 1), "queries": queries}

    def _config_fingerprint(self) -> str:
        """Settings that give a saved page number its meaning: the page size
        of every query (offset = page * page_size) and the time window."""
        return config_fingerprint({
            "hours_old": self.config.hours_old,
            "page_sizes": {
                _query_key(q): min(q.results_wanted, self.config.max_results_per_query)
                for q in self.config.queries
            },
        })

    def _next_scan_id(self) -> str:
        """The resumable logical scan's id when there is one (its history row
        is reopened, issue #86), else a fresh id for a new run."""
        try:
            checkpoint = self.staging.get_scan_checkpoint()
            run = self.staging.get_scan_run(checkpoint["scan_id"]) if checkpoint else None
            resumed = resumable_scan_id(checkpoint, run, self._config_fingerprint())
        except Exception as exc:  # noqa: BLE001
            logger.warning("LinkedIn: could not read the checkpoint to reuse its scan run: %s", exc)
            resumed = None
        return resumed or str(uuid.uuid4())

    def run_scan(self) -> str | None:
        """Run a full scan synchronously.  Returns scan_id or None if already scanning."""
        next_id = self._next_scan_id()
        with self._scan_lock:
            if self._current_scan_id is not None:
                logger.warning(
                    "LinkedIn: scan already in progress (%s), skipping.",
                    self._current_scan_id,
                )
                return None
            scan_id = next_id
            self._current_scan_id = scan_id
            self._pause_event.clear()
        try:
            self._do_scan(scan_id)
        finally:
            with self._scan_lock:
                self._current_scan_id = None
        return scan_id

    def start_scan(self) -> str:
        """Trigger a scan in a daemon thread.  Raises RuntimeError if already scanning."""
        next_id = self._next_scan_id()
        with self._scan_lock:
            if self._current_scan_id is not None:
                raise RuntimeError("A LinkedIn scan is already in progress")
            scan_id = next_id
            self._current_scan_id = scan_id
            self._pause_event.clear()
        t = threading.Thread(
            target=self._run_and_unlock,
            args=(scan_id,),
            daemon=True,
            name=f"linkedin-scan-{scan_id[:8]}",
        )
        t.start()
        return scan_id

    def _run_and_unlock(self, scan_id: str) -> None:
        try:
            self._do_scan(scan_id)
        finally:
            with self._scan_lock:
                self._current_scan_id = None

    # -- scan implementation --------------------------------------------------

    def _load_filters_config(self) -> dict[str, Any] | None:
        """Load the hard-filter config from profile.yaml + filters.yaml.

        Returns None if the filter module is not yet loaded, if either config
        file is missing, or if the config cannot be parsed.  In all these
        cases pre-staging filtering is silently skipped and all jobs are staged.

        The sys.modules guard is intentional: src.pipeline.__init__.py eagerly
        imports SentenceTransformers (~25 s cold start).  That import must not
        happen inside the background scan thread.  In production the pipeline
        module is always loaded by the FastAPI lifespan before any scan starts,
        so the guard is a no-op there.  In tests that want filter behaviour the
        module is pre-populated in sys.modules via a mock.
        """
        import os
        import sys

        if "src.pipeline.filter" not in sys.modules:
            logger.debug(
                "LinkedIn: src.pipeline.filter not yet imported — "
                "pre-staging filter inactive (pipeline not initialised)."
            )
            return None

        from src.config_files import resolve_config_path

        # Personal config/profile.local.yaml wins over the tracked default.
        profile_path = str(resolve_config_path(self.config_dir, "profile.yaml"))
        # The vocabulary of the strategy the next pipeline run will use (an approved, effective bundle or
        # config/; see src/decisions/active_strategy.py), so staging never drops a job the run would keep.
        engine_path = os.path.join(self.config_dir, "filters.yaml")
        if self.db_path is not None:
            from src.decisions.active_strategy import resolve_active_strategy

            active = resolve_active_strategy(self.db_path, self.config_dir)
            engine_path = str(active.path("filters.yaml"))
            logger.info("LinkedIn: pre-staging filter uses strategy %s (%s).", active.status, active.reason)
        try:
            from src.pipeline.filter import (
                load_filter_config,  # deferred, already cached
            )
            cfg = load_filter_config(profile_path, engine_path)
            logger.info("LinkedIn: loaded filter config — pre-staging hard filter active.")
            return cfg
        except FileNotFoundError:
            logger.warning(
                "LinkedIn: profile.yaml or filters.yaml not found — "
                "skipping pre-staging hard filter."
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LinkedIn: could not load filter config: %s — "
                "skipping pre-staging hard filter.",
                exc,
            )
            return None

    def _do_scan(self, scan_id: str) -> None:
        self._last_scan_paused = False
        # A previous scan's attempt counters must never feed this scan's
        # live ETA, even before its checkpoint is loaded below.
        self._attempt_started = time.monotonic()
        self._attempt_pages = 0
        # create_scan_run() and everything below it must be inside this try:
        # a transient "database is locked" from a concurrent writer (pipeline,
        # API request, or the sibling scanner) previously propagated straight
        # out of this method, uncaught, when it happened on this first call.
        # That killed the background thread silently: _run_and_unlock()'s
        # finally still cleared _current_scan_id (releasing the scan lock)
        # but no scan_run row was ever created/failed, so the very next
        # trigger (scheduler tick or a manual "Scan Now") started a brand
        # new scan_id from scratch — indistinguishable from the scan having
        # "reset to the beginning". Catching it here lets the scan retry
        # cleanly next time with a properly recorded failure instead.
        try:
            queries_json = json.dumps(
                [{"search_term": q.search_term, "location": q.location} for q in self.config.queries]
            )
            # A resumable logical scan reopens its own history row (issue #86).
            resumed_run = self.staging.reopen_scan_run(scan_id)
            if not resumed_run:
                self.staging.create_scan_run(scan_id, queries_json)
            logger.info(
                "LinkedIn scan %s %s (%d queries, fetch_description=%s, max_pages=%d).",
                scan_id,
                "resumed (same logical scan)" if resumed_run else "started",
                len(self.config.queries),
                self.config.fetch_description,
                self.config.max_pages_per_query,
            )

            if not self.config.queries:
                logger.info("LinkedIn: no queries configured — scan complete (0 results).")
                self.staging.complete_scan_run(scan_id, 0, 0)
                self._last_scan_completed = True
                return

            try:
                from jobspy import scrape_jobs  # type: ignore[import-untyped]
            except ImportError as exc:
                raise RuntimeError(
                    "python-jobspy is not installed. "
                    "Install the LinkedIn extra: pip install 'find-your-job-with-ai[linkedin]'"
                ) from exc

            filters_config = self._load_filters_config()

            # Resume support: if a previous scan was interrupted, its
            # checkpoint records which queries are already fully done and
            # which page the in-progress query had reached. Skipping done
            # queries and resuming the in-progress one from its saved page
            # avoids redundant re-fetching on top of the job-level dedup
            # insert_job() already provides. A checkpoint only exists when
            # the last scan never reached a clean finish — see
            # clear_scan_checkpoint() below.
            fingerprint = self._config_fingerprint()
            checkpoint = self.staging.get_scan_checkpoint()
            if checkpoint and checkpoint.get("config_fingerprint") not in (None, fingerprint):
                # Saved page numbers no longer mean the same offsets (page
                # size or time window changed): resuming could skip results.
                # Start over instead — re-fetches are absorbed by insert_job's
                # upsert (issue #81).
                logger.warning(
                    "LinkedIn scan %s: checkpoint was taken under a different "
                    "configuration — discarding it and starting from the first query.",
                    scan_id,
                )
                if checkpoint["scan_id"] != scan_id:
                    self.staging.abandon_scan_run(checkpoint["scan_id"])
                self.staging.clear_scan_checkpoint()
                checkpoint = None
            completed_query_keys: set[str] = set()
            resume_query_key: str | None = None
            resume_page = 0
            if checkpoint:
                self.staging.set_checkpoint_state(scan_id, "running", create_if_missing=False)
                completed_query_keys = set(json.loads(checkpoint.get("completed_queries_json") or "[]"))
                resume_query_key = checkpoint.get("current_query_key")
                resume_page = checkpoint.get("current_page") or 0
                if completed_query_keys or resume_query_key:
                    logger.info(
                        "LinkedIn scan %s resuming — %d quer(y/ies) already completed in a "
                        "previous interrupted attempt; resuming %s at page %d.",
                        scan_id, len(completed_query_keys),
                        resume_query_key or "(none)", resume_page,
                    )
            # Continue the logical scan's statistics from where the
            # checkpoint left them (empty for a new scan or a legacy row).
            self._scan_stats = _load_scan_stats(checkpoint)
            self._attempt_started = time.monotonic()
            self._attempt_pages = 0

            # scan_seen_ids maps job_id → first search_term that discovered it.
            # Shared across all queries to dedup within a scan and attribute each job
            # to the query that discovered it first.
            scan_seen_ids: dict[str, str] = {}
            query_stats_list: list[_QueryStats] = []

            stopped_for_shutdown = False
            for i, query in enumerate(self.config.queries):
                if self._stop.is_set():
                    logger.info(
                        "LinkedIn scan %s: %s requested — stopping before "
                        "query %r (checkpoint already reflects last completed "
                        "page; will resume here).",
                        scan_id, self._stop_reason(), query.search_term,
                    )
                    stopped_for_shutdown = True
                    break
                key = _query_key(query)
                if key in completed_query_keys:
                    # Completed by an earlier attempt of this logical scan:
                    # report its saved statistics (a checkpoint written
                    # before stats_json existed has none — zeros, as before).
                    saved = self._scan_stats["queries"].get(key)
                    query_stats_list.append(
                        _stats_from_dict(query, saved) if saved else _QueryStats(
                            search_term=query.search_term,
                            location=query.location,
                            stopped_by="resumed_skip",
                        )
                    )
                    continue

                if i > 0 and self.config.rate_limit_seconds > 0:
                    logger.debug(
                        "LinkedIn: waiting %.1fs before next query.",
                        self.config.rate_limit_seconds,
                    )
                    if wait_for_stop(self._stop, self.config.rate_limit_seconds):
                        stopped_for_shutdown = True
                        break
                start_page = resume_page if key == resume_query_key else 0
                try:
                    q_stats = self._run_query(
                        scan_id, query, scrape_jobs, filters_config, scan_seen_ids,
                        start_page=start_page,
                        completed_query_keys=completed_query_keys,
                    )
                except Exception as exc:  # noqa: BLE001
                    # Do NOT mark this query complete: a caught transient
                    # failure (network error, jobspy/scraper exception, a DB
                    # error not already isolated per-row inside
                    # _run_query) means the query was not actually finished.
                    # Marking it "done" here would make a future resume skip
                    # it forever — permanently losing whatever pages it
                    # hadn't reached yet — until the next fully clean scan
                    # happens to clear the whole checkpoint. Leaving it
                    # unmarked means the next scan (scheduled retry or a
                    # crash-recovery resume) retries this query, picking up
                    # from whatever page-level checkpoint _run_query already
                    # persisted before the failure (or page 0, if it failed
                    # before completing any page) — bounded by the scan's own
                    # normal cadence, not an unbounded retry loop, since a
                    # persistently-failing query still lets every other
                    # query in this scan proceed normally.
                    logger.error(
                        "LinkedIn: query %r/%r failed: %s — continuing with remaining "
                        "queries; this query remains resumable, not marked complete.",
                        query.search_term,
                        query.location,
                        exc,
                    )
                    query_stats_list.append(
                        _QueryStats(
                            search_term=query.search_term,
                            location=query.location,
                            stopped_by="error",
                        )
                    )
                else:
                    query_stats_list.append(q_stats)
                    if q_stats.stopped_by in ("shutdown", "paused"):
                        # Stopped mid-query, between pages — do NOT mark
                        # complete (would make a future resume skip whatever
                        # pages remain). _run_query already persisted the
                        # page-level checkpoint before returning, so nothing
                        # further to save here.
                        stopped_for_shutdown = True
                        break
                    # This query completed in full for this scan (exhausted
                    # or hit its safety limit) — mark it done so a resume
                    # never reprocesses it, and clear the in-query page
                    # pointer since there is no longer an in-progress query.
                    completed_query_keys.add(key)
                    self._scan_stats["queries"][key] = _stats_to_dict(q_stats)
                    try:
                        self.staging.save_scan_checkpoint(
                            scan_id, sorted(completed_query_keys), None, 0,
                            fingerprint=fingerprint, stats=self._stats_snapshot(),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "LinkedIn: could not persist scan checkpoint after query %r: %s",
                            query.search_term, exc,
                        )

            if stopped_for_shutdown and self._pause_event.is_set():
                # A user pause wins over a concurrent shutdown: the intent to
                # stay paused must survive the restart (issue #81).
                self._record_pause(scan_id)
                return

            if stopped_for_shutdown:
                logger.info(
                    "LinkedIn scan %s stopped cleanly for backend shutdown — "
                    "checkpoint preserved, will resume on next scan.",
                    scan_id,
                )
                record_shutdown_stop(self.staging, scan_id, "LinkedIn")
                self._set_checkpoint_state_quietly(scan_id, "interrupted")
                self._last_scan_completed = False
                return

            total_unique = sum(q.jobs_unique for q in query_stats_list)
            total_staged = sum(q.jobs_staged for q in query_stats_list)

            # Back-fill unique_to_query counts now that all queries are done.
            for q in query_stats_list:
                if q.stopped_by != "error":
                    q.jobs_unique_to_query = self.staging.count_unique_to_query(
                        scan_id, q.search_term
                    )

            stats = {
                "total_raw": sum(q.jobs_raw for q in query_stats_list),
                "total_unique": total_unique,
                "total_cross_query_dups": sum(q.jobs_cross_query_dups for q in query_stats_list),
                "total_filtered_out": sum(q.jobs_filtered_out for q in query_stats_list),
                "total_already_known": sum(q.jobs_already_known for q in query_stats_list),
                "total_staged": total_staged,
                "total_pages_fetched": sum(q.pages_fetched for q in query_stats_list),
                # Processing time across every attempt of this logical scan,
                # excluding downtime between them (issue #86) — unlike
                # started_at → completed_at.
                "active_seconds": self._stats_snapshot()["active_seconds"],
                "total_description_attempted": sum(
                    q.jobs_description_attempted for q in query_stats_list
                ),
                "total_description_fetched": sum(
                    q.jobs_description_fetched for q in query_stats_list
                ),
                "queries_completed": len(query_stats_list),
                "queries_stopped_by_exhaustion": sum(
                    1 for q in query_stats_list if q.stopped_by == "exhaustion"
                ),
                "queries_stopped_by_limit": sum(
                    1 for q in query_stats_list if q.stopped_by == "safety_limit"
                ),
                "queries_failed": sum(
                    1 for q in query_stats_list if q.stopped_by == "error"
                ),
                "queries": [
                    {
                        "search_term": q.search_term,
                        "location": q.location,
                        "pages_fetched": q.pages_fetched,
                        "jobs_raw": q.jobs_raw,
                        "jobs_unique": q.jobs_unique,
                        "jobs_cross_query_dups": q.jobs_cross_query_dups,
                        "jobs_filtered_out": q.jobs_filtered_out,
                        "jobs_already_known": q.jobs_already_known,
                        "jobs_staged": q.jobs_staged,
                        "jobs_unique_to_query": q.jobs_unique_to_query,
                        "jobs_description_attempted": q.jobs_description_attempted,
                        "jobs_description_fetched": q.jobs_description_fetched,
                        "stopped_by": q.stopped_by,
                    }
                    for q in query_stats_list
                ],
            }

            self.staging.complete_scan_run(
                scan_id, total_unique, total_staged, stats_json=json.dumps(stats)
            )
            # Full success — every query was attempted, nothing left to
            # resume. Clears the checkpoint so the next *scheduled* scan
            # starts from page 0 of the first query again, rather than
            # skipping everything because of stale progress.
            self.staging.clear_scan_checkpoint()
            self._last_scan_completed = True
            logger.info(
                "LinkedIn scan %s completed: %d unique fetched, %d staged "
                "(%d filtered, %d already known).",
                scan_id,
                total_unique,
                total_staged,
                stats["total_filtered_out"],
                stats["total_already_known"],
            )

        except Exception as exc:
            logger.exception("LinkedIn scan %s failed.", scan_id)
            try:
                self.staging.fail_scan_run(scan_id, str(exc))
            except Exception:  # noqa: BLE001
                # DB still contended, or the run row was never created (e.g.
                # create_scan_run() itself is what failed) — nothing more to
                # record; the scan lock is still released via the finally in
                # run_scan()/_run_and_unlock() so the next trigger can retry.
                pass
            self._set_checkpoint_state_quietly(scan_id, "failed")
            self._last_scan_completed = False
            # Checkpoint intentionally left in place (not cleared) so the
            # next attempt — manual retry or the next scheduler tick — can
            # resume from whatever queries/pages were already completed.

    def _stop_reason(self) -> str:
        """Why the loop is stopping, for log lines."""
        return "pause" if self._pause_event.is_set() else "shutdown"

    def _stopped_by(self) -> str:
        """The _QueryStats.stopped_by value for a cooperative stop."""
        return "paused" if self._pause_event.is_set() else "shutdown"

    def _set_checkpoint_state_quietly(self, scan_id: str, state: str) -> None:
        """Label an existing checkpoint; never creates one, never raises."""
        try:
            self.staging.set_checkpoint_state(scan_id, state, create_if_missing=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LinkedIn: could not mark checkpoint %s for scan %s: %s", state, scan_id, exc
            )

    def _record_pause(self, scan_id: str) -> None:
        """Persist a user pause: checkpoint 'paused' (created if the scan had
        no progress yet, so the pause itself survives a restart) and the scan
        run 'paused'. Progress columns are left exactly as last saved. The
        pause is reported only once it is durable (see persist_pause)."""
        self._last_scan_completed = False
        self._last_scan_paused = persist_pause(self.staging, scan_id, "LinkedIn")
        if not self._last_scan_paused:
            return
        # The pause is durable from here on: the log line is best effort and
        # must not reach _do_scan's error path, which would relabel it failed.
        try:
            progress = self.progress() or {}
        except Exception as exc:  # noqa: BLE001
            logger.info("LinkedIn scan %s paused (progress unavailable: %s).", scan_id, exc)
            return
        logger.info(
            "LinkedIn scan %s paused — checkpoint: %d/%d queries done, next %r page %d.",
            scan_id,
            progress.get("queries_completed", 0),
            progress.get("queries_total", len(self.config.queries)),
            progress.get("current_query"),
            progress.get("current_page", 0),
        )

    def _run_query(
        self,
        scan_id: str,
        query: LinkedInQueryConfig,
        scrape_jobs: Any,
        filters_config: dict[str, Any] | None,
        scan_seen_ids: dict[str, str],
        start_page: int = 0,
        completed_query_keys: set[str] | frozenset[str] = frozenset(),
    ) -> _QueryStats:
        """Fetch pages for one query, applying filter and dedup before staging.

        Stops only when JobSpy returns an empty result set, or the safety limit
        (max_pages_per_query) is reached.  A page returning only duplicates or
        only filtered-out jobs does NOT cause early termination.

        ``start_page`` resumes a query interrupted mid-pagination in a
        previous scan attempt (offset = page * page_size, so starting at a
        later page is a plain continuation, not a special case). After every
        page is fully processed, the resumable checkpoint is updated with
        this query's key and the next page to fetch — so a crash mid-query
        loses at most one page's worth of position, not the whole query.

        Returns a _QueryStats describing what happened.
        """
        page_size = min(query.results_wanted, self.config.max_results_per_query)
        max_pages = self.config.max_pages_per_query
        query_key = _query_key(query)
        # A query resumed mid-pagination continues the counters its earlier
        # attempt saved with the checkpoint (issue #86).
        saved = self._scan_stats["queries"].get(query_key) if start_page > 0 else None
        q_stats = (
            _stats_from_dict(query, saved) if saved
            else _QueryStats(search_term=query.search_term, location=query.location)
        )

        logger.info(
            "LinkedIn: querying %r in %r (page_size=%d, max_pages=%d, start_page=%d, filter=%s).",
            query.search_term,
            query.location or "any",
            page_size,
            max_pages,
            start_page,
            "active" if filters_config is not None else "off",
        )

        fingerprint = self._config_fingerprint()
        for page in range(start_page, max_pages):
            if self._stop.is_set():
                logger.info(
                    "LinkedIn: query %r stopping before page %d — %s "
                    "requested; checkpoint stays at page %d.",
                    query.search_term, page, self._stop_reason(), page,
                )
                q_stats.stopped_by = self._stopped_by()
                break
            if page > 0 and self.config.rate_limit_seconds > 0:
                logger.debug(
                    "LinkedIn: waiting %.1fs before page %d.",
                    self.config.rate_limit_seconds,
                    page,
                )
                if wait_for_stop(self._stop, self.config.rate_limit_seconds):
                    q_stats.stopped_by = self._stopped_by()
                    break

            kwargs: dict[str, Any] = {
                "site_name": ["linkedin"],
                "search_term": query.search_term,
                "results_wanted": page_size,
                "offset": page * page_size,
                "linkedin_fetch_description": self.config.fetch_description,
            }
            if query.location:
                kwargs["location"] = query.location
            if self.config.hours_old > 0:
                kwargs["hours_old"] = self.config.hours_old

            with _jobspy_errors() as jobspy_errors:
                df = scrape_jobs(**kwargs)

            if (df is None or df.empty) and jobspy_errors:
                # Rate limited or blocked, not exhausted: fail the query so it is neither
                # reported nor checkpointed as done (the caller keeps it resumable).
                raise RuntimeError(f"python-jobspy returned no rows and reported: {jobspy_errors[0]}")
            if jobspy_errors:
                logger.warning(
                    "LinkedIn: query %r page %d returned %d row(s) but python-jobspy reported: %s",
                    query.search_term, page, len(df), "; ".join(jobspy_errors),
                )

            if df is None or df.empty:
                logger.info(
                    "LinkedIn: query %r page %d returned 0 results — query exhausted.",
                    query.search_term,
                    page,
                )
                q_stats.stopped_by = "exhaustion"
                break

            raw_count = len(df)
            q_stats.pages_fetched += 1
            q_stats.jobs_raw += raw_count
            now_iso = datetime.now(UTC).isoformat()
            staged_this_page = 0
            page_aborted = False

            for _, row in df.iterrows():
                try:
                    record = self._build_record(row, scan_id, now_iso, query_location=query.location)
                    if record is None:
                        continue

                    job_id = record["linkedin_job_id"]
                    if job_id in scan_seen_ids:
                        # Cross-query duplicate — record attribution, then skip.
                        q_stats.jobs_cross_query_dups += 1
                        self.staging.record_also_seen_by(job_id, query.search_term, scan_id)
                        continue
                    scan_seen_ids[job_id] = query.search_term
                    q_stats.jobs_unique += 1
                    record["discovered_by_query"] = query.search_term

                    # Pre-staging hard filter
                    if filters_config is not None:
                        from src.linkedin.staging import staging_row_to_job  # deferred
                        from src.pipeline.filter import filter_jobs  # deferred
                        job_obj = staging_row_to_job(record)
                        valid, _ = filter_jobs([job_obj], filters_config)
                        if not valid:
                            q_stats.jobs_filtered_out += 1
                            continue

                    # Post-filter description enrichment.
                    # Runs when enrich_descriptions=True (explicit opt-in),
                    # the job still has no description (covers both the case
                    # where fetch_description=False and the case where
                    # fetch_description=True but the bulk fetch returned nothing),
                    # and the staging DB doesn't already hold a fetched
                    # description for it. Unlike a plain "already known" check,
                    # this also re-attempts jobs staged under the previously
                    # broken enrichment path (description_fetched=0), including
                    # ones stuck in a terminal 'transferred'/'processed' state —
                    # insert_job() below reopens those for the pipeline once a
                    # real description comes back.
                    if (
                        self.config.enrich_descriptions
                        and not record.get("description")
                        and self.staging.needs_description(record.get("linkedin_job_id", ""))
                    ):
                        jid = record.get("linkedin_job_id", "")
                        q_stats.jobs_description_attempted += 1
                        if self.config.description_fetch_delay > 0 and wait_for_stop(
                            self._stop, self.config.description_fetch_delay
                        ):
                            # Stop requested mid-page: abandon the rest of this
                            # page WITHOUT checkpointing it, so a resume
                            # re-fetches the whole page (at-least-once; rows
                            # already staged are upserted, issue #81).
                            page_aborted = True
                            break
                        fetched_desc, fetch_status = fetch_linkedin_description_classified(jid)
                        if fetched_desc:
                            record["description"] = fetched_desc
                            record["description_fetched"] = 1
                            q_stats.jobs_description_fetched += 1
                            # Re-derive skills now that a real description is
                            # available — the initial extraction at build time
                            # only saw the title (no description yet).
                            raw_payload = json.loads(record["raw_payload"])
                            extracted_skills = list(dict.fromkeys(
                                _extract_title_skills(record.get("title"))
                                + extract_known_technologies(fetched_desc)
                            ))
                            if extracted_skills:
                                raw_payload["extracted_title_skills"] = extracted_skills
                                record["raw_payload"] = json.dumps(raw_payload)
                            # Re-derive work_mode from description when unknown.
                            if not record.get("work_mode"):
                                wm = _extract_work_mode_from_text(fetched_desc)
                                if wm:
                                    record["work_mode"] = wm
                                    if wm == "remote":
                                        record["is_remote"] = 1
                        elif fetch_status == "deleted":
                            # HTTP 404/410 — the job is permanently gone.  Mark
                            # description_fetched=2 (terminal) so needs_description()
                            # never retries it again; the record itself is kept
                            # (jobs are never deleted from staging).
                            record["description_fetched"] = 2

                    if self.staging.insert_job(record):
                        q_stats.jobs_staged += 1
                        staged_this_page += 1
                    else:
                        q_stats.jobs_already_known += 1

                except Exception as exc:  # noqa: BLE001
                    logger.warning("LinkedIn: could not process job row: %s", exc)

            if page_aborted:
                logger.info(
                    "LinkedIn: query %r page %d interrupted by %s — page will be "
                    "re-fetched on resume.",
                    query.search_term, page, self._stop_reason(),
                )
                q_stats.stopped_by = self._stopped_by()
                break

            logger.info(
                "LinkedIn: query %r page %d → raw=%d unique=%d "
                "filtered=%d known=%d staged=%d (total unique=%d staged=%d).",
                query.search_term,
                page,
                raw_count,
                q_stats.jobs_unique,
                q_stats.jobs_filtered_out,
                q_stats.jobs_already_known,
                staged_this_page,
                q_stats.jobs_unique,
                q_stats.jobs_staged,
            )

            # This page is fully staged — persist the resumable checkpoint
            # now, before fetching the next one. A crash right after this
            # point resumes at page+1 for this query, not from page 0. The
            # running statistics are written in the same row write, so they
            # always match the saved position (issue #86).
            self._attempt_pages += 1
            try:
                self.staging.save_scan_checkpoint(
                    scan_id, sorted(completed_query_keys), query_key, page + 1,
                    fingerprint=fingerprint,
                    stats=self._stats_snapshot(query_key, q_stats),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "LinkedIn: could not persist scan checkpoint after page %d: %s",
                    page, exc,
                )

        else:
            # for-loop exhausted max_pages without a break → safety limit reached
            q_stats.stopped_by = "safety_limit"
            logger.info(
                "LinkedIn: query %r reached max_pages_per_query=%d safety limit.",
                query.search_term,
                max_pages,
            )

        return q_stats

    def _fetch_description(self, linkedin_job_id: str) -> str | None:
        """Delegate to the module-level fetch_linkedin_description function."""
        return fetch_linkedin_description(linkedin_job_id)

    def _build_record(
        self, row: Any, scan_id: str, now_iso: str, query_location: str = ""
    ) -> dict[str, Any] | None:
        import hashlib

        # -- LinkedIn job ID --------------------------------------------------
        linkedin_job_id = _safe_str(row.get("id"))
        if not linkedin_job_id:
            job_url_raw = _safe_str(row.get("job_url"))
            if job_url_raw:
                m = _LINKEDIN_JOB_ID_RE.search(job_url_raw)
                if m:
                    linkedin_job_id = m.group(1)
        if not linkedin_job_id:
            logger.debug("LinkedIn: skipping row with no job ID.")
            return None

        # -- Canonical URL ----------------------------------------------------
        canonical_url = _safe_str(row.get("job_url"))
        if not canonical_url:
            canonical_url = f"https://www.linkedin.com/jobs/view/{linkedin_job_id}"

        # -- Stable primary key (same formula as normalize.py) ---------------
        stable_id = hashlib.sha256(
            f"linkedin:{linkedin_job_id}".encode()
        ).hexdigest()[:32]

        # -- Core fields ------------------------------------------------------
        title = _safe_str(row.get("title")) or ""
        company = _safe_str(row.get("company"))
        location = _safe_str(row.get("location"))
        description = _safe_str(row.get("description"))
        description_fetched = 1 if description else 0

        # -- Boolean / enum fields -------------------------------------------
        is_remote_raw = row.get("is_remote")
        is_remote = False
        if is_remote_raw is not None:
            try:
                if not math.isnan(float(is_remote_raw)):
                    is_remote = bool(is_remote_raw)
            except (TypeError, ValueError):
                is_remote = bool(is_remote_raw)

        job_type = _safe_str(row.get("job_type"))
        work_mode = _extract_work_mode(is_remote, job_type)
        # Fall back to title/location signals when structured fields give nothing.
        if work_mode is None:
            derived = _extract_work_mode_from_title(title) or _extract_work_mode_from_location(location)
            if derived:
                work_mode = derived
                if derived == "remote":
                    is_remote = True
        employment_type = _extract_employment_type(job_type, title=title)
        countries = _extract_country(location)
        # When the job's own location string yields no country (e.g. "Remote",
        # None, or a city not in our map), fall back to the scanner query's
        # configured location so that jobs collected by the France scanner are
        # attributed to France.  Only applies when the query has a non-empty
        # location and the job itself carried no geographic signal.
        if not countries and query_location:
            countries = _extract_country(query_location)

        # -- Salary -----------------------------------------------------------
        salary_min = _safe_float(row.get("min_amount"))
        salary_max = _safe_float(row.get("max_amount"))
        # Use the currency LinkedIn provides; default to EUR (not USD) for French
        # searches.  When salary is absent (min=max=None) the currency is stored
        # as EUR to match the Job model default — the API/UI must not show salary
        # information when both salary fields are NULL regardless of currency.
        currency = _safe_str(row.get("currency")) or "EUR"

        # -- Date posted ------------------------------------------------------
        published_at = _safe_date_str(row.get("date_posted"))

        # -- Raw payload (audit trail; never discarded) ----------------------
        raw: dict[str, Any] = {}
        for field_name in (
            "id", "site", "job_url", "title", "company", "location",
            "job_type", "date_posted", "interval", "min_amount", "max_amount",
            "currency", "is_remote", "job_level", "company_url", "company_logo",
        ):
            val = row.get(field_name)
            if val is None:
                continue
            try:
                if math.isnan(float(val)):  # type: ignore[arg-type]
                    continue
            except (TypeError, ValueError):
                pass
            raw[field_name] = (
                str(val) if not isinstance(val, (str, int, float, bool)) else val
            )
        # description is stored in its own column; also include in raw_payload
        if description:
            raw["description"] = description

        # Extract skills from the title, plus from the description when one is
        # already available at build time (bulk fetch). Title-only extraction
        # under-populates skills for jobs whose title has no recognisable tech
        # keyword — other sources fall back to scanning title+description via
        # extract_known_technologies() in src/pipeline/normalize.py; LinkedIn
        # needs the same description-based fallback, not title-only matching.
        # These are persisted in raw_payload so staging_row_to_job can populate
        # the Job.skills field for the scoring layer.
        extracted_skills = list(dict.fromkeys(
            _extract_title_skills(title) + extract_known_technologies(description or "")
        ))
        if extracted_skills:
            raw["extracted_title_skills"] = extracted_skills

        return {
            "id": stable_id,
            "linkedin_job_id": linkedin_job_id,
            "canonical_url": canonical_url,
            "title": title.strip() or None,
            "company": company,
            "location": location,
            "countries": json.dumps(countries),
            "is_remote": 1 if is_remote else 0,
            "work_mode": work_mode,
            "employment_type": employment_type,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "description": description,
            "description_fetched": description_fetched,
            "published_at": published_at,
            "raw_payload": json.dumps(raw),
            "scan_run_id": scan_id,
            "discovered_at": now_iso,
            "last_seen_at": now_iso,
        }
