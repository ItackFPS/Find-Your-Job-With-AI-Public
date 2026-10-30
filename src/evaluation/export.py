"""Read-only export of a real-job evaluation dataset + manual annotation tooling.

Safety properties (each tested):
  * the source database is opened ``mode=ro`` with ``query_only`` — the file is
    never modified;
  * the output directory must be git-ignored (or outside any repository) so
    real jobs can never be committed;
  * by default text is anonymised (company alias, e-mails/phones/URLs scrubbed,
    URLs and raw payload dropped, ids hashed) — best effort, the dataset stays
    local;
  * the sample is deterministic for a given (database content, seed).

SURVIVAL BIAS: ``jobs.db`` only contains jobs that were accepted at the time
(rejected jobs keep no description), so an exported dataset can reveal false
positives but cannot measure false negatives (see ADR 0007).
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import re
import secrets
import shutil
import sqlite3
import subprocess
import zlib
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.evaluation.dataset import (
    JOBS, LABELS, MANIFEST, DatasetError, load_dataset, write_dataset,
)
from src.evaluation.models import LabelRecord

PIPELINE_REJECTED_NOTE = "pipeline-rejected sample"   # manifest-notes marker read by the runner's warnings
TIER_THRESHOLDS = (60, 65, 72, 80)
NEAR_THRESHOLD_POINTS = 2.0
ANNOTATIONS = "annotations.csv"
EXPORT_META = "PRIVATE_export_meta.json"   # strata + id map + salt: never share this file
_OUTPUT_FILES = ("jobs.jsonl", "labels.jsonl", "manifest.json", "annotations.csv", EXPORT_META, "profile.yaml")
_EXPORT_COLUMNS = (
    "id", "source", "company", "title", "description", "location", "countries", "department",
    "remote", "work_mode", "employment_type", "salary_min", "salary_max", "salary_currency",
    "published_at", "skills", "status",
)
# Bounded local part + "not preceded by a local-part character": linear time on long unbroken tokens.
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+")
_EMAIL_OBFUSCATED = re.compile(
    r"(?<![\w.+-])[\w.+-]{1,64}\s?[\[(]\s?(?:at|arobase)\s?[\])]\s?[\w-]+"
    r"(?:\s?[\[(]\s?(?:dot|point)\s?[\])]\s?[\w-]+|\.[\w-]+)+", re.IGNORECASE)
_URL = re.compile(r"https?://\S+|www\.\S+")
# Phone numbers. Conservative on purpose (dates, salary ranges, year ranges, versions and plain
# thousands-grouped numbers must survive) but covers French numbers (0X XX XX XX XX, +33 / 0033,
# irregular grouping), E.164-style "+<cc> ...", "+<cc> (0) ...", 00-prefixed international numbers,
# UK national numbers, 0800-style freephone and North-American "(555) 123-4567" / "555-123-4567".
# Every alternative must not start in the middle of a longer number ("2023-11-05 2023-12" is not a phone).
_NOT_MID = r"(?<![\w+])(?<!\d[\s.-])"
_NOT_THOUSANDS = r"(?!\d{1,3}(?:[\s.]\d{3}){2,}(?![\s.]?\d))"
_PHONE = re.compile(
    rf"{_NOT_MID}(?:(?:\+33|0033)[\s.-]?\(?0?\)?[1-9]|0[1-9])(?:[\s.-]?\d{{2}}){{4}}(?!\d)"
    rf"|{_NOT_MID}0[\s.-]?\d(?:[\s.-]?\d){{8,10}}(?![\s.-]?\d)"
    rf"|{_NOT_MID}\+\s?{_NOT_THOUSANDS}\d{{1,3}}(?:[\s.-]?\d){{8,12}}(?!\d)"
    rf"|{_NOT_MID}\+\s?\d{{1,3}}[\s.-]?\(\d{{1,4}}\)(?:[\s.-]?\d){{6,12}}(?!\d)"
    rf"|{_NOT_MID}00[1-9]\d{{0,2}}(?:[\s.-]?\d){{8,12}}(?!\d)"
    rf"|{_NOT_MID}\(?[2-9]\d{{2}}\)[\s.-]?\d{{3}}[\s.-]\d{{4}}(?!\d)"
    rf"|{_NOT_MID}[2-9]\d{{2}}([.-])\d{{3}}\1\d{{4}}(?!\d)"
)
_LEGAL_SUFFIX = re.compile(
    r"[\s,]+(?:sas|sasu|sarl|sa|eurl|inc\.?|ltd\.?|llc|gmbh|corp\.?|group|groupe|ag|bv)[\s.]*$", re.I)
_ALIAS_ANY = re.compile(r"Company-[0-9a-f]{8}")
_LEGAL_SUFFIX = re.compile(r"\s+(?:sas|sasu|sarl|sa|eurl|inc\.?|ltd\.?|llc|gmbh|corp\.?|group|groupe|ag|bv)\s*$", re.I)


class ExportError(ValueError):
    pass


def _company_variants(company: str | None) -> list[str]:
    """Name variants to scrub/detect: the full name, the name without (stacked) legal suffixes and the
    comma-less form. Short names (1-2 characters, e.g. "HP", "3M") are kept: they are matched
    case-sensitively as whole words (see ``_company_patterns``)."""
    if not company:
        return []
    full = company.strip()
    variants = {full, full.replace(",", "")}
    stripped = full
    for _ in range(4):                         # "Acme Group SAS" -> "Acme Group" -> "Acme"
        nxt = _LEGAL_SUFFIX.sub("", stripped).strip()
        if nxt == stripped or not nxt:
            break
        stripped = nxt
        variants.update({stripped, stripped.replace(",", "")})
    return sorted((v for v in variants if len(v) >= 1), key=len, reverse=True)


_INVISIBLE = dict.fromkeys(map(ord, "\u00ad\u200b\u200c\u200d\u2060\ufeff"))


def _fold_chars(text: str) -> tuple[str, list[int]]:
    """Fold ``text`` for matching and return ``(folded, origin)`` where ``origin[i]`` is the index in
    ``text`` of folded char ``i``: accents removed, case folded, typographic apostrophes/dashes and
    ``&amp;`` normalised, fullwidth forms mapped to ASCII, invisible characters dropped."""
    import unicodedata

    out: list[str] = []
    origin: list[int] = []
    i = 0
    while i < len(text):
        if text.startswith("&amp;", i):
            out.append("&")
            origin.append(i)
            i += 5
            continue
        c = text[i]
        if ord(c) in _INVISIBLE:
            i += 1
            continue
        for d in unicodedata.normalize("NFKD", c):
            if unicodedata.category(d) == "Mn":
                continue
            d = {"\u2019": "'", "\u2018": "'", "\u2013": "-", "\u2014": "-", "\u2011": "-"}.get(d, d)
            for f in d.casefold():
                out.append(f)
                origin.append(i)
        i += 1
    return "".join(out), origin


def _fold(text: str) -> str:
    return _fold_chars(text)[0]


def _company_patterns(company: str | None) -> list[tuple[re.Pattern[str], bool]]:
    """``(pattern, case_sensitive)`` per name variant, to be run on FOLDED text. Whole-word, separator-
    insensitive (space, hyphen, slash, underscore). Names of 1-2 characters are matched on the original
    text, case-sensitively. Shared by the scrubber and the leak detector."""
    out = []
    for v in _company_variants(company):
        short = len(_fold(v)) <= 2
        base = v if short else _fold(v)
        parts = [p for p in re.split(r"[\s\-/_]+", base) if p]
        if not parts:
            continue
        pattern = r"[\s\-/_]+".join(re.escape(part) for part in parts)
        out.append((re.compile(rf"(?<!\w){pattern}(?!\w)"), short))
    return out


def _company_spans(text: str, patterns: list[tuple[re.Pattern[str], bool]]) -> list[tuple[int, int]]:
    """Spans (in ``text``) of every mention matched by ``patterns``."""
    folded, origin = _fold_chars(text)
    spans: list[tuple[int, int]] = []
    for rx, short in patterns:
        if short:
            spans += [m.span() for m in rx.finditer(text)]
        else:
            for m in rx.finditer(folded):
                start = origin[m.start()]
                end = origin[m.end() - 1] + 1
                if text.startswith("&amp;", origin[m.end() - 1]):
                    end = origin[m.end() - 1] + 5
                spans.append((start, end))
    return sorted(set(spans), key=lambda ab: (ab[0], -ab[1]))


def _replace_spans(text: str, spans: list[tuple[int, int]], repl: str) -> str:
    out, last = [], 0
    for a, b in spans:
        if a < last:
            continue                    # overlapping variant: already replaced
        out.append(text[last:a])
        out.append(repl)
        last = b
    out.append(text[last:])
    return "".join(out)


def _scrub_phones_outside_alias(text: str) -> str:
    """Phone-scrub everything except the aliases themselves: an alias made only of digits
    (``Company-06123456``) followed by more digits would otherwise be read as a phone number."""
    out, last = [], 0
    for m in _ALIAS_ANY.finditer(text):
        out.append(_PHONE.sub("[phone]", text[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_PHONE.sub("[phone]", text[last:]))
    return "".join(out)


def _fold_digits(text: str) -> str:
    """Map fullwidth ASCII (digits, '@', letters) to ASCII and drop invisible characters so that
    obfuscated identifiers are seen by the regexes; other characters are untouched."""
    return "".join(
        chr(ord(c) - 0xFEE0) if "\uff01" <= c <= "\uff5e" else c for c in text if ord(c) not in _INVISIBLE)


def scrub_text(text: str | None, company: str | None = None, alias: str | None = None) -> str | None:
    """Best-effort anonymisation: e-mails (also "[at]" forms), URLs, phones, company name (whole words,
    accent/case/apostrophe-insensitive, with and without legal suffixes, separator-insensitive).
    Numbers that are not phones are preserved. A bare short form of a multi-word name ("Acme" for
    "Acme Corp") is NOT scrubbed: it cannot be told apart from ordinary words without corrupting text.
    Phones are scrubbed again after the alias substitution because substituting can make neighbouring
    digits form a new number (aliases themselves are protected)."""
    if text is None:
        return None
    out = _fold_digits(text)
    out = _EMAIL_OBFUSCATED.sub("[email]", _EMAIL.sub("[email]", out))
    out = _URL.sub("[url]", out)
    out = _PHONE.sub("[phone]", out)
    if alias:
        out = _replace_spans(out, _company_spans(out, _company_patterns(company)), alias)
        out = _scrub_phones_outside_alias(out)
    return out


_PLAIN_FIELDS = frozenset({"id", "company", "status"})


def _map_strings(value: Any, fn: Any) -> Any:
    """Apply ``fn`` to every string inside ``value`` (nested lists/tuples/dicts)."""
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, (list, tuple)):
        return [_map_strings(x, fn) for x in value]
    if isinstance(value, dict):
        return {k: _map_strings(v, fn) for k, v in value.items()}
    return value


def _iter_strings(value: Any) -> Any:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for x in value:
            yield from _iter_strings(x)
    elif isinstance(value, dict):
        for x in value.values():
            yield from _iter_strings(x)


def scrub_record(rec: dict[str, Any], company: str | None, alias: str | None) -> dict[str, Any]:
    """Scrub EVERY string value of an exported job (including nested containers), not a fixed field
    list: identifiers can sit in any text-bearing field. ``id``/``company``/``status`` are set by the
    exporter."""
    return {k: (v if k in _PLAIN_FIELDS else _map_strings(v, lambda t: scrub_text(t, company, alias)))
            for k, v in rec.items()}


def _shape(s: str) -> str:
    """Content-free shape of a matched string: digits -> '#', letters -> 'a'/'A'."""
    return "".join("#" if c.isdigit() else ("A" if c.isupper() else "a") if c.isalpha() else c for c in s)


def find_leaks(jobs: list[dict[str, Any]], companies: dict[str, str | None]) -> list[dict[str, str]]:
    """Residual identifiers in an anonymised dataset, one entry per occurrence:
    ``{job_id, field, kind, shape}`` (shape never contains content). Every string value is scanned on
    its own (never joined with another, so a number cannot be assembled across fields); aliases are
    masked first; unsupported value types fail closed. ``companies`` maps anonymised job id -> the REAL
    company name."""
    leaks: list[dict[str, str]] = []
    for job in jobs:
        jid = str(job.get("id", ""))
        patterns = _company_patterns(companies.get(jid))
        for field, value in job.items():
            if field in ("id", "status"):
                continue
            if value is not None and not isinstance(value, (str, int, float, bool, list, tuple, dict)):
                leaks.append({"job_id": jid, "field": field, "kind": "unsupported_type", "shape": type(value).__name__})
                continue
            for item in _iter_strings(value):
                if field == "company":
                    if not re.fullmatch(r"Company-[0-9a-f]{8}", item):
                        leaks.append({"job_id": jid, "field": field, "kind": "company_not_alias",
                                      "shape": f"len={len(item)}"})
                    continue
                masked = _ALIAS_ANY.sub("ALIAS", _fold_digits(item))
                for kind, rx in (("email", _EMAIL), ("email", _EMAIL_OBFUSCATED), ("url", _URL), ("phone", _PHONE)):
                    for m in rx.finditer(masked):
                        leaks.append({"job_id": jid, "field": field, "kind": kind, "shape": _shape(m.group(0))[:40]})
                for a, b in _company_spans(masked, patterns):
                    leaks.append({"job_id": jid, "field": field, "kind": "company", "shape": f"len={b - a}"})
    return leaks


def summarise_leaks(leaks: list[dict[str, str]]) -> str:
    from collections import Counter

    c = Counter((x["kind"], x["field"]) for x in leaks)
    return "; ".join(f"{k}@{f}: {n}" for (k, f), n in sorted(c.items()))


def assert_not_committable(path: Path) -> None:
    """Refuse an output path whose files git would track. Fails CLOSED on any git error
    except "git is not installed" / "not a git repository"."""
    target = path.resolve()
    probe = target
    while not probe.exists():
        probe = probe.parent
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=probe, capture_output=True,
                             text=True, timeout=15)
    except FileNotFoundError:
        return                                  # git not installed: nothing can be committed via git
    except subprocess.SubprocessError as exc:
        raise ExportError(f"cannot determine git status of {target}: {exc}") from exc
    if top.returncode != 0:
        if "not a git repository" in top.stderr.lower():
            return
        raise ExportError(f"cannot determine git status of {target}: {top.stderr.strip()}")
    for name in _OUTPUT_FILES:
        try:
            ignored = subprocess.run(["git", "check-ignore", "-q", str(target / name)],
                                     cwd=top.stdout.strip(), timeout=15)
        except subprocess.SubprocessError as exc:
            raise ExportError(f"cannot determine git status of {target / name}: {exc}") from exc
        if ignored.returncode == 1:
            raise ExportError(
                f"{target / name} is inside a git repository and not git-ignored; real jobs must "
                "never be committed. Export under data/benchmarks/ (ignored) or outside the repository."
            )
        if ignored.returncode not in (0, 1):
            raise ExportError(f"git check-ignore failed for {target / name} (exit {ignored.returncode})")


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    if not p.is_file():
        raise ExportError(f"database not found: {p}")
    conn = sqlite3.connect(f"file:{p.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _latest_scores(conn: sqlite3.Connection) -> dict[str, float]:
    """job_id → latest final_score on a 0-100 scale (``final_score`` is stored as 0-1).

    Deterministic: ordered by run start, run id, row id — the last row per job wins."""
    try:
        rows = conn.execute(
            """SELECT js.job_id AS job_id, js.final_score AS s FROM job_scores js
               JOIN pipeline_runs pr ON js.run_id = pr.id
               WHERE js.final_score IS NOT NULL
               ORDER BY pr.started_at, pr.id, js.rowid"""
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {}                            # a database that never ran the pipeline
        raise
    return {r["job_id"]: float(r["s"]) * 100.0 for r in rows}


def _stratify(
    candidates: list[dict[str, Any]], scores: dict[str, float], n: int, seed: int
) -> dict[str, str]:
    """Deterministic stratified sample → ``{job_id: stratum}``, never more than ``n``.

    The unbiased ``random_control`` stratum always keeps a reserved share (≥ n//4, ≥ 1 when
    n ≥ 2); the other strata share the rest and are capped by the remaining budget."""
    rng = random.Random(seed)
    ordered = sorted(candidates, key=lambda c: c["id"])
    chosen: dict[str, str] = {}
    reserve = max(1, n // 4) if n >= 2 else 0
    budget = n - reserve

    def take(stratum: str, pool: list[dict[str, Any]], k: int, limit: int) -> None:
        pool = [c for c in pool if c["id"] not in chosen]
        rng.shuffle(pool)
        k = max(0, min(k, limit - len(chosen)))
        for c in pool[:k]:
            chosen[c["id"]] = stratum

    quarter = max(1, n // 4)
    near = [c for c in ordered if c["id"] in scores
            and min(abs(scores[c["id"]] - t) for t in TIER_THRESHOLDS) <= NEAR_THRESHOLD_POINTS]
    take("near_tier_threshold", near, quarter, budget)
    take("user_actioned", [c for c in ordered if c.get("application_status")], quarter, budget)
    by_source: dict[str, list[dict[str, Any]]] = {}
    for c in ordered:
        by_source.setdefault(c["source"] or "", []).append(c)
    start = len(chosen)
    sources = sorted(by_source)
    per_source = max(1, quarter // max(1, len(sources)))
    for src in sources:
        take("per_source", by_source[src], per_source, min(budget, start + quarter))
    take("random_control", list(ordered), n - len(chosen), n)
    return chosen


# Journal stages whose rejection can hide a wanted job (validation/dedup drops are not opportunities).
_OPPORTUNITY_STAGES = ("filter", "requirements_gate", "llm_eligibility", "score", "ranking")
REJECTED_STRATUM = "pipeline_rejected"


def _read_snapshot(conn: sqlite3.Connection, h: str | None) -> dict[str, Any] | None:
    if not h:
        return None
    row = conn.execute("SELECT text_blob, compression FROM job_snapshots WHERE snapshot_hash = ?", (h,)).fetchone()
    if row is None or row["text_blob"] is None:
        return None
    raw = zlib.decompress(row["text_blob"]) if row["compression"] == "zlib" else bytes(row["text_blob"])
    return dict(json.loads(raw.decode("utf-8")))


def _rejected_candidates(
    conn: sqlite3.Connection, stored: set[str], stored_hashes: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Jobs whose LATEST journal decision is a rejection at an opportunity stage, never stored in ``jobs``,
    with snapshot text still available → export rows (same columns as ``jobs``) + ``_stage``.

    Duplicates are not missed opportunities: a rejected copy whose ``content_hash`` (the deduplication
    key: title + description) equals a stored job's was surfaced under that other id, and several rejected
    copies of one listing count once (the smallest job id is kept)."""
    # NOT INDEXED + only the needed columns: walking idx_decisions_job fetches every journal row at a random
    # position of a multi-GB file (measured: 517 s cold, 1.6 s warm, on a 3.3 GB jobs.db); a scan in file order
    # reads it sequentially and the window sorts narrow rows in memory
    sql = f"""SELECT job_id, deciding_stage, snapshot_hash, content_hash FROM (
                  SELECT job_id, deciding_stage, snapshot_hash, {{h}} AS content_hash, outcome,
                         ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY decided_at DESC, rowid DESC) AS rn
                  FROM job_decisions NOT INDEXED)
                WHERE rn = 1 AND outcome = 'rejected'
                  AND deciding_stage IN ({",".join("?" * len(_OPPORTUNITY_STAGES))})
                ORDER BY job_id"""
    try:
        try:
            rows = conn.execute(sql.format(h="content_hash"), _OPPORTUNITY_STAGES).fetchall()
        except sqlite3.OperationalError as exc:
            if "content_hash" not in str(exc):
                raise
            rows = conn.execute(sql.format(h="NULL"), _OPPORTUNITY_STAGES).fetchall()   # journal predating the key
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []                           # no decision journal yet
        raise
    out = []
    seen_hashes: set[str] = set()
    for r in rows:
        if r["job_id"] in stored:
            continue
        h = r["content_hash"] or None
        if h is not None and (h in stored_hashes or h in seen_hashes):
            continue                            # a duplicate of a stored listing, or of one already in the pool
        payload = _read_snapshot(conn, r["snapshot_hash"])
        if payload is None or len(payload.get("description") or "") <= 200:
            continue                            # text purged by retention, or too short (same rule as jobs)
        if h is not None:
            seen_hashes.add(h)
        rec = {k: payload.get(k) for k in _EXPORT_COLUMNS if k != "id"}
        out.append({**rec, "id": r["job_id"], "status": "active", "_stage": r["deciding_stage"]})
    return out


def _first_seen_before(raw: str | None, as_of: datetime) -> str | None:
    """A stored ``first_seen_at`` as UTC ISO text when it is a valid date not after ``as_of``, else ``None``."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    dt = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    ref = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds") if dt <= ref else None


def export_real_dataset(
    db_path: str | Path, out_dir: str | Path, *, n: int = 200, seed: int = 20260917,
    dataset_id: str = "real-v1", as_of: str | None = None, anonymize: bool = True,
    salt: str | None = None, rejected: int = 0, first_seen: bool = True,
) -> dict[str, Any]:
    """Export ``n`` jobs. ``seed`` drives the sample; ``salt`` drives the anonymised ids/aliases
    (random by default and stored only in the private sidecar; pass one to reproduce an export
    byte for byte together with a fixed ``as_of``).

    ``rejected`` adds up to that many jobs the pipeline REJECTED (latest journal decision; never stored
    in ``jobs``), drawn uniformly with ``seed`` and rebuilt from their journal snapshot. They go through
    the same anonymisation and leak check; their stratum and stage stay in the private sidecar only, so
    the annotation sheet remains blind. Without a journal the pool is empty and nothing is added.

    ``first_seen``: stored jobs carry ``first_seen_at`` (when not after ``as_of``) so a replay ages an
    undated listing from its first sighting, as production does (ADR 0012); pipeline-rejected jobs were
    never stored and carry none, as in production. ``False`` reproduces exports made before this field
    existed (``audit`` passes the value recorded in the sidecar)."""
    if n < 1:
        raise ExportError("--n must be >= 1")
    if rejected < 0:
        raise ExportError("--rejected must be >= 0")
    out = Path(out_dir)
    assert_not_committable(out)
    if (out / MANIFEST).exists():
        raise ExportError(f"{out} already contains a dataset; refusing to overwrite")
    salt = salt if salt is not None else secrets.token_hex(16)
    conn = open_readonly(db_path)
    try:
        base = ("SELECT {cols} FROM jobs WHERE status = 'active' AND description IS NOT NULL "
                "AND length(description) > 200 ORDER BY id")
        try:
            rows = conn.execute(base.format(cols=", ".join(_EXPORT_COLUMNS + ("application_status",)))).fetchall()
        except sqlite3.OperationalError as exc:
            if "application_status" not in str(exc):
                raise
            rows = conn.execute(base.format(cols=", ".join(_EXPORT_COLUMNS))).fetchall()
        scores = _latest_scores(conn)
        first_seen_at: dict[str, str] = {}
        if first_seen:
            first_seen_at = {r[0]: r[1] for r in conn.execute(
                "SELECT id, first_seen_at FROM jobs WHERE first_seen_at IS NOT NULL").fetchall()}
        rejected_pool: list[dict[str, Any]] = []
        if rejected:
            stored_rows = conn.execute("SELECT id, content_hash FROM jobs").fetchall()
            rejected_pool = _rejected_candidates(conn, {r[0] for r in stored_rows},
                                                 {r[1] for r in stored_rows if r[1]})
    finally:
        conn.close()
    candidates = [dict(r) for r in rows]
    if not candidates:
        raise ExportError("no eligible active jobs with a description in the database")
    strata = _stratify(candidates, scores, min(n, len(candidates)), seed)
    by_id = {c["id"]: c for c in candidates}
    picked = random.Random(f"{seed}:{REJECTED_STRATUM}").sample(rejected_pool, min(rejected, len(rejected_pool)))
    rejected_stage: dict[str, str] = {}
    for c in picked:
        by_id[c["id"]] = c
        strata[c["id"]] = REJECTED_STRATUM
        rejected_stage[c["id"]] = c["_stage"]

    def digest(label: str, value: str, size: int) -> str:
        return hashlib.sha256(f"{salt}:{label}:{value}".encode()).hexdigest()[:size]

    now = datetime.now(UTC).isoformat(timespec="seconds")
    as_of = as_of or now
    as_of_dt = datetime.fromisoformat(as_of)
    jobs: list[dict[str, Any]] = []
    id_map: dict[str, str] = {}
    real_companies: dict[str, str | None] = {}
    for jid in sorted(strata):
        c = by_id[jid]
        new_id = f"j-{digest('id', jid, 12)}" if anonymize else jid
        company = c.get("company")
        alias = f"Company-{digest('company', company.strip().lower(), 8)}" if (anonymize and company) else None
        rec: dict[str, Any] = {k: c[k] for k in _EXPORT_COLUMNS if k != "id"}
        for k in ("countries", "skills"):
            rec[k] = json.loads(rec[k]) if isinstance(rec[k], str) else (rec[k] or [])
        rec["remote"] = bool(rec["remote"])
        rec["id"] = new_id
        if anonymize:
            rec["company"] = alias
            rec = scrub_record(rec, company, alias)
        rec = {k: v for k, v in rec.items() if v is not None}
        seen = _first_seen_before(first_seen_at.get(jid), as_of_dt)
        if seen is not None:
            rec["first_seen_at"] = seen
        rec["status"] = "active"
        jobs.append(rec)
        id_map[new_id] = jid
        real_companies[new_id] = company

    if anonymize:
        leaks = find_leaks(jobs, real_companies)
        if leaks:           # enforce the contract at the source: never write a leaking "anonymised" dataset
            raise ExportError(
                f"anonymisation left {len(leaks)} residual identifier(s) ({summarise_leaks(leaks)}); "
                "nothing was written. Run `python -m src.evaluation audit --explain` on a previous export "
                "or report the field/kind/shape summary above."
            )
    if picked:
        notes = (f"Exported read-only from jobs.db: {len(jobs) - len(picked)} jobs stored as accepted plus a "
                 f"{len(picked)}-job {PIPELINE_REJECTED_NOTE} (uniform draw from {len(rejected_pool)} jobs whose "
                 "latest journal decision was a rejection at an opportunity stage). False negatives are measurable "
                 "on that subsample only; rejections made before the decision journal existed, or whose text "
                 "retention purged, are not represented. Anonymised: %s." % anonymize)
    else:
        notes = ("Exported read-only from jobs.db. SURVIVAL BIAS: accepted-at-the-time jobs only; "
                 "false negatives are not measurable. Anonymised: %s." % anonymize)
    manifest = write_dataset(
        out, dataset_id=dataset_id, version="1", kind="real", created_at=as_of,
        as_of=as_of, jobs=jobs, labels=[], profile_yaml=None, notes=notes,
    )
    sidecar: dict[str, Any] = {
        "WARNING": "PRIVATE: maps anonymised ids back to real jobs; never share or commit",
        "seed": seed, "salt": salt, "anonymized": anonymize, "exported_at": now, "first_seen_at": first_seen,
        "strata": {new: strata[old] for new, old in id_map.items()}, "id_map": id_map}
    if rejected:
        sidecar["rejected_requested"] = rejected
        sidecar["rejected_pool"] = len(rejected_pool)
        sidecar["rejected_stage"] = {new: rejected_stage[old] for new, old in id_map.items() if old in rejected_stage}
    (out / EXPORT_META).write_text(json.dumps(sidecar, indent=1, sort_keys=True), encoding="utf-8")
    write_annotation_template(out, jobs)
    return {"manifest": manifest.model_dump(), "n_jobs": len(jobs), "strata_counts": _count(strata.values()),
            "rejected_pool": len(rejected_pool)}


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


# -- annotation -----------------------------------------------------------------

_TEMPLATE_COLUMNS = ("job_id", "expected", "category", "reason", "title", "company", "location", "source", "description_excerpt")


def write_annotation_template(dataset_dir: Path, jobs: list[dict[str, Any]]) -> Path:
    """Blind template: no production score/decision is shown (avoids anchoring)."""
    path = dataset_dir / ANNOTATIONS
    with open(path, "w", newline="", encoding="utf-8-sig") as f:   # BOM: opens correctly in Excel
        w = csv.DictWriter(f, fieldnames=_TEMPLATE_COLUMNS)
        w.writeheader()
        for j in sorted(jobs, key=lambda j: j["id"]):
            w.writerow({
                "job_id": j["id"], "expected": "", "category": "", "reason": "",
                "title": j.get("title", ""), "company": j.get("company", ""),
                "location": j.get("location", ""), "source": j.get("source", ""),
                "description_excerpt": (j.get("description") or "")[:600].replace("\n", " "),
            })
    return path


def _read_annotation_rows(path: Path) -> list[dict[str, str]]:
    if path.suffix == ".jsonl":
        rows = []
        for i, line in enumerate(path.read_text(encoding="utf-8-sig").split("\n"), start=1):
            if line.strip():
                try:
                    rows.append({k: ("" if v is None else str(v)) for k, v in json.loads(line).items()})
                except (json.JSONDecodeError, AttributeError) as exc:
                    raise DatasetError(f"{path.name}:{i}: invalid JSON line ({exc})") from exc
        return rows
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or "job_id" not in reader.fieldnames or "expected" not in reader.fieldnames:
                raise DatasetError(f"{path.name}: header must contain job_id and expected")
            return [dict(r) for r in reader]
    except UnicodeDecodeError as exc:
        raise DatasetError(f"{path.name}: not valid UTF-8 (save as 'CSV UTF-8' from Excel): {exc}") from exc


def parse_annotations(
    dataset_dir: str | Path, annotations: str | Path, *, labeler: str, labeled_at: str | None = None,
    assisted: Collection[str] = (),
) -> tuple[list[LabelRecord], list[str]]:
    """Strictly validate an annotation file → ``(labels, unlabeled_job_ids)``.

    Rows with an empty ``expected`` are "not annotated yet" (reported, not errors).
    Everything else must be valid: unknown value, unknown/duplicate ``job_id`` → error.
    Provenance (``label_source``, ``labeler``, ``labeled_at``) is stamped here: ``human_assisted``
    for the ids in ``assisted`` (an assistant proposal was on record), ``human`` otherwise.
    """
    if not labeler.strip():
        raise DatasetError("labeler is required (provenance)")
    root = Path(dataset_dir)
    ds = load_dataset(root, require_complete_labels=False)
    known = {j["id"] for j in ds.job_dicts}
    stamp = labeled_at or datetime.now(UTC).isoformat(timespec="seconds")
    labels: dict[str, LabelRecord] = {}
    seen: set[str] = set()
    for i, row in enumerate(_read_annotation_rows(Path(annotations)), start=2):
        jid = (row.get("job_id") or "").strip()
        if jid not in known:
            raise DatasetError(f"{Path(annotations).name}:{i}: unknown job_id {jid!r}")
        if jid in seen:
            raise DatasetError(f"{Path(annotations).name}:{i}: duplicate job_id {jid!r}")
        seen.add(jid)
        expected = (row.get("expected") or "").strip().lower()
        if not expected:
            continue
        try:
            labels[jid] = LabelRecord(
                job_id=jid, expected=expected, category=(row.get("category") or "").strip() or None,  # type: ignore[arg-type]
                reason=(row.get("reason") or "").strip(),
                label_source="human_assisted" if jid in assisted else "human",
                labeler=labeler.strip(), labeled_at=stamp,
            )
        except ValidationError as exc:
            raise DatasetError(f"{Path(annotations).name}:{i}: invalid label for {jid!r}: {exc}") from exc
    return list(labels.values()), sorted(known - set(labels))


def import_annotations(
    dataset_dir: str | Path, annotations: str | Path, *, labeler: str,
    labeled_at: str | None = None, new_version: str | None = None, assisted: Collection[str] = (),
) -> dict[str, Any]:
    """Merge annotations into the dataset and write a NEW version when labels changed.

    * existing labels are kept; a row only replaces a label when its content (expected,
      category, reason) differs, so re-importing the same file is a no-op (same labels hash,
      no version bump, original provenance preserved);
    * provenance only ever moves towards less independence: an unchanged ``human`` label whose job is
      now known to be assisted is re-stamped ``human_assisted`` (a correction, which bumps the
      version); an unchanged ``human_assisted`` label is never upgraded back to ``human``;
    * an empty ``expected`` never deletes an existing label;
    * before overwriting, the previous version is archived as a complete, loadable dataset in
      ``versions/<version>/`` so results pinned to its labels hash stay reproducible.
    """
    root = Path(dataset_dir)
    parsed, _ = parse_annotations(root, annotations, labeler=labeler, labeled_at=labeled_at, assisted=assisted)
    ds = load_dataset(root, require_complete_labels=False)
    merged = dict(ds.labels)
    changed = 0
    for lab in parsed:
        old = merged.get(lab.job_id)
        if old is not None and (old.expected, old.category, old.reason) == (lab.expected, lab.category, lab.reason):
            if old.label_source == "human" and lab.label_source == "human_assisted":
                merged[lab.job_id] = old.model_copy(update={"label_source": "human_assisted"})
                changed += 1
            continue
        merged[lab.job_id] = lab
        changed += 1
    unlabelled = len(ds.job_dicts) - len(merged)
    provenance = {src: sum(1 for x in merged.values() if x.label_source == src)
                  for src in sorted({x.label_source for x in merged.values()})}
    if changed == 0:
        return {"version": ds.manifest.version, "labelled": len(merged), "unlabelled": unlabelled,
                "changed": 0, "provenance": provenance, "note": "no change; dataset left untouched"}
    version = new_version or (str(int(ds.manifest.version) + 1) if ds.manifest.version.isdigit()
                              else ds.manifest.version + ".1")
    archive = root / "versions" / ds.manifest.version
    if archive.exists():
        raise DatasetError(f"{archive} already exists; refusing to overwrite an archived version")
    archive.mkdir(parents=True)
    for name in (MANIFEST, JOBS, LABELS, "profile.yaml"):
        if (root / name).is_file():
            shutil.copy2(root / name, archive / name)
    profile = (root / "profile.yaml").read_bytes() if (root / "profile.yaml").is_file() else None
    write_dataset(
        root, dataset_id=ds.manifest.dataset_id, version=version, kind=ds.manifest.kind,
        created_at=ds.manifest.created_at, as_of=ds.manifest.as_of, jobs=ds.job_dicts,
        labels=list(merged.values()), profile_yaml=profile, notes=ds.manifest.notes,
    )
    return {"version": version, "labelled": len(merged), "unlabelled": unlabelled, "changed": changed,
            "provenance": provenance}


__all__ = [
    "ExportError", "export_real_dataset", "parse_annotations", "import_annotations",
    "scrub_text", "assert_not_committable", "open_readonly", "JOBS", "LABELS",
]
