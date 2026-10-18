"""Tests proving the filter is driven by the job search profile.

Each test uses a named profile (A–E) to show that changing profile.yaml
changes filtering behaviour without modifying any Python code.

No Internet access.  No collectors called.  No real database touched.
Profiles and engine configs are constructed inline — no real config files
are read, keeping the tests deterministic and config-file-independent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


from src.models.job import Job
from src.models.profile import ExperiencePreferences, GeographyPreferences, JobSearchProfile
from src.pipeline.filter import build_filter_config, filter_jobs

# ---------------------------------------------------------------------------
# Shared engine vocabulary (minimal subset of filters.yaml)
# ---------------------------------------------------------------------------

_ENGINE: dict = {
    "domain_keywords": {
        "software_development": {
            "title": [
                "software engineer", "software developer", "developer",
                "programmer", "full stack", "full-stack",
                "développeur", "developpeur",
            ],
            "description": [
                "software engineer", "software developer",
                "développeur", "developpeur", "programmation",
            ],
        },
        "cybersecurity": {
            "title": ["cybersecurity", "security engineer", "cybersécurité", "cybersecurite"],
            "description": ["cybersecurity", "cybersécurité", "cybersecurite"],
        },
        "data": {
            "title": ["data analyst", "data engineer", "data scientist", "machine learning"],
            "description": ["machine learning", "sql", "postgresql"],
        },
        "devops": {
            "title": ["devops", "site reliability"],
            "description": ["docker", "kubernetes"],
        },
    },
    "contract_keywords": {
        "internship": {
            "title": ["internship", "intern ", "stagiaire", "stage"],
            "description": [],
        },
        "alternance": {
            "title": ["alternance", "alternant"],
            "description": [],
        },
        "cdi": {"title": [], "description": []},
    },
    "seniority_exclusions": {
        "senior": ["senior"],
        "director": ["director", "head of"],
        "vp": ["vice president", "vp "],
        "c_suite": ["chief ", "cto", "ciso", "coo", "cpo"],
    },
    "excluded_title_keywords": [
        "comptable", "juriste", "plombier", "infirmier",
    ],
    "remote_scope": {
        "european_countries": [
            "France", "Germany", "Netherlands", "Belgium",
            "Spain", "United Kingdom", "UK", "Switzerland",
        ],
        "european_keywords": ["Europe", "EU", "EMEA", "European"],
        "worldwide_keywords": ["worldwide", "global", "anywhere", "international"],
    },
}

_NOW = datetime.now(UTC)
_RECENT = _NOW - timedelta(days=1)


def _job(**kwargs) -> Job:
    defaults = dict(
        source="test",
        title="Software Engineer",
        company="Acme",
        published_at=_RECENT,
        remote=False,
        location="Paris, France",
        countries=["France"],
    )
    defaults.update(kwargs)
    return Job(**defaults)


def _cfg(profile: JobSearchProfile) -> dict:
    return build_filter_config(profile, _ENGINE)


def _passes(job: Job, profile: JobSearchProfile) -> bool:
    passed, _ = filter_jobs([job], _cfg(profile))
    return any(j.id == job.id for j in passed)


def _reason(job: Job, profile: JobSearchProfile) -> str | None:
    _, rejected = filter_jobs([job], _cfg(profile))
    return rejected.get(job.id)


# ---------------------------------------------------------------------------
# Profile A — France only
# Expectation: German on-site jobs are rejected; France jobs pass.
# ---------------------------------------------------------------------------

class TestProfileA_FranceOnly:
    """Profile A: geography restricted to France (no European region)."""

    _profile = JobSearchProfile(
        target_domains=["software_development"],
        geography=GeographyPreferences(
            preferred_countries=["France"],
            allowed_regions=[],  # No Europe — stricter than the default profile
        ),
        experience=ExperiencePreferences(excluded_seniority=["senior"]),
        freshness_days=30,
    )

    def test_france_onsite_passes(self):
        job = _job(id="fr", location="Paris, France", countries=["France"], remote=False)
        assert _passes(job, self._profile), "France onsite must pass Profile A"

    def test_france_remote_passes(self):
        job = _job(id="fr-r", location="Paris, France", countries=["France"], remote=True)
        assert _passes(job, self._profile), "France remote must pass Profile A"

    def test_germany_onsite_rejected(self):
        """Without allowed_regions=Europe, Germany on-site is rejected at location."""
        job = _job(id="de", location="Berlin", countries=["Germany"], remote=False)
        assert _reason(job, self._profile) == "location", (
            "Germany on-site must be rejected by location filter in Profile A"
        )

    def test_germany_remote_rejected(self):
        """German remote job with explicit non-France country → rejected."""
        job = _job(id="de-r", location="Berlin", countries=["Germany"], remote=True)
        assert _reason(job, self._profile) == "location", (
            "Germany remote with non-France country must be rejected in Profile A"
        )

    def test_usa_remote_rejected(self):
        job = _job(id="us", location="New York", countries=["USA"], remote=True)
        assert _reason(job, self._profile) == "location"


# ---------------------------------------------------------------------------
# Profile B — France + Europe
# France-first policy: German on-site jobs are now REJECTED (require physical
# presence abroad).  European remote eligibility requires an explicit EU/worldwide
# scope indicator in the location string.
# ---------------------------------------------------------------------------

class TestProfileB_FranceAndEurope:
    """Profile B: geography includes France (preferred) and Europe (region).

    allowed_regions=[Europe] means European remote jobs with explicit EU/worldwide
    scope are accepted.  It does NOT mean European on-site jobs are accepted.
    """

    _profile = JobSearchProfile(
        target_domains=["software_development"],
        geography=GeographyPreferences(
            preferred_countries=["France"],
            allowed_regions=["Europe"],
        ),
        experience=ExperiencePreferences(excluded_seniority=["senior"]),
        freshness_days=30,
    )

    def test_france_onsite_passes(self):
        job = _job(id="fr", location="Paris, France", countries=["France"], remote=False)
        assert _passes(job, self._profile)

    def test_germany_onsite_rejected(self):
        """Germany on-site: confirmed foreign, not remote → REJECT."""
        job = _job(id="de", location="Berlin", countries=["Germany"], remote=False)
        assert _reason(job, self._profile) == "location", (
            "Germany on-site must be rejected — physical presence abroad required"
        )

    def test_germany_remote_eu_country_passes(self):
        """Germany remote: Germany in european_countries → ACCEPT."""
        job = _job(id="de-r", location="Berlin", countries=["Germany"], remote=True)
        assert _passes(job, self._profile), (
            "Germany remote should pass — Germany is in european_countries"
        )

    def test_germany_remote_eu_scope_passes(self):
        """Germany remote with explicit EU scope in location → ACCEPT."""
        job = _job(id="de-eu", location="Remote, Europe", countries=["Germany"], remote=True)
        assert _passes(job, self._profile), (
            "Germany remote with EU scope must pass Profile B"
        )

    def test_germany_remote_worldwide_passes(self):
        job = _job(id="de-ww", location="Remote (Worldwide)", countries=["Germany"], remote=True)
        assert _passes(job, self._profile)

    def test_europe_keyword_remote_passes(self):
        job = _job(id="eu", location="Remote, Europe", countries=[], remote=True)
        assert _passes(job, self._profile)

    def test_usa_remote_rejected(self):
        job = _job(id="us", location="New York", countries=["USA"], remote=True)
        assert _reason(job, self._profile) == "location"

    def test_japan_onsite_rejected(self):
        job = _job(id="jp", location="Tokyo", countries=["Japan"], remote=False)
        assert _reason(job, self._profile) == "location"


# ---------------------------------------------------------------------------
# Profile C — Remote preferred (work_modes: remote/hybrid/on-site all allowed)
# Expectation: all work modes pass the HARD filter; remote preference is for scoring.
# ---------------------------------------------------------------------------

class TestProfileC_AllWorkModes:
    """Profile C: work_modes includes remote, hybrid, and on_site.

    The hard filter never rejects on work mode alone — that is a scoring
    preference.  Both remote and on-site France jobs must survive.
    """

    _profile = JobSearchProfile(
        target_domains=["software_development"],
        geography=GeographyPreferences(
            preferred_countries=["France"],
            allowed_regions=["Europe"],
        ),
        work_modes=["remote", "hybrid", "on_site"],
        experience=ExperiencePreferences(excluded_seniority=["senior"]),
        freshness_days=30,
    )

    def test_onsite_france_passes(self):
        job = _job(id="on", remote=False)
        assert _passes(job, self._profile), "On-site France job must pass Profile C"

    def test_remote_france_passes(self):
        job = _job(id="rm", remote=True)
        assert _passes(job, self._profile), "Remote France job must pass Profile C"

    def test_remote_germany_eu_scope_passes(self):
        """Germany remote with explicit EU scope → ACCEPT even under Profile C."""
        job = _job(id="de-r", location="Remote, Europe", countries=["Germany"], remote=True)
        assert _passes(job, self._profile)

    def test_remote_germany_eu_country_passes(self):
        """Germany remote: Germany in european_countries → ACCEPT under Profile C."""
        job = _job(id="de-unk", location="Berlin", countries=["Germany"], remote=True)
        assert _passes(job, self._profile)

    def test_onsite_usa_still_rejected_by_location(self):
        """Work mode doesn't override location — US on-site is still rejected."""
        job = _job(id="us-on", location="New York", countries=["USA"], remote=False)
        assert _reason(job, self._profile) == "location"


# ---------------------------------------------------------------------------
# Profile D — Cybersecurity + Programming
# Expectation: security and software jobs pass; unrelated domains fail.
# ---------------------------------------------------------------------------

class TestProfileD_CybersecurityAndProgramming:
    """Profile D: domains = cybersecurity + software_development.

    Only IT jobs related to these two domains should survive the keyword filter.
    """

    _profile = JobSearchProfile(
        target_domains=["cybersecurity", "software_development"],
        geography=GeographyPreferences(
            preferred_countries=["France"],
            allowed_regions=["Europe"],
        ),
        experience=ExperiencePreferences(excluded_seniority=["senior", "director", "vp", "c_suite"]),
        freshness_days=30,
    )

    def test_cybersecurity_analyst_passes(self):
        job = _job(id="sec", title="Cybersecurity Analyst", location="Paris, France")
        assert _passes(job, self._profile), "Cybersecurity Analyst must pass Profile D"

    def test_software_developer_passes(self):
        job = _job(id="dev", title="Software Developer")
        assert _passes(job, self._profile)

    def test_security_engineer_passes(self):
        job = _job(id="se", title="Security Engineer")
        assert _passes(job, self._profile)

    def test_developer_in_france_passes(self):
        job = _job(id="df", title="Developer", location="Lyon, France", countries=["France"])
        assert _passes(job, self._profile)

    def test_data_scientist_rejected(self):
        """data domain is not selected → data scientist title rejected by title_keywords."""
        job = _job(id="ds", title="Data Scientist")
        assert _reason(job, self._profile) == "title_keywords", (
            "Data Scientist must be rejected — data domain not in Profile D"
        )

    def test_marketing_manager_rejected(self):
        job = _job(id="mkt", title="Marketing Manager")
        assert _reason(job, self._profile) == "title_keywords"

    def test_comptable_rejected(self):
        """Excluded title keywords always apply regardless of domain selection."""
        job = _job(id="cpt", title="Comptable Junior")  # no seniority signal → hits title_keywords
        assert _reason(job, self._profile) == "title_keywords"

    def test_cybersecurity_description_fallback(self):
        """Generic title 'Analyste' with cybersecurity description → passes."""
        job = _job(
            id="gen",
            title="Analyste",
            description="Vous travaillerez sur des projets de cybersecurity et protection des systèmes.",
        )
        assert _passes(job, self._profile), (
            "Generic title with cybersecurity in description must pass Profile D"
        )


# ---------------------------------------------------------------------------
# Profile E — Internship / alternance with IT domains
# Expectation: internship-specific keywords are added; senior roles excluded.
# ---------------------------------------------------------------------------

class TestProfileE_InternshipOnly:
    """Profile E: contract_types=[internship, alternance], seniority exclusions active.

    Shows that:
    - Selecting internship adds internship-specific title keywords.
    - Senior/director roles are hard-rejected.
    - A pure internship title passes without domain keywords in the title.
    """

    _profile = JobSearchProfile(
        target_domains=["software_development"],
        contract_types=["internship", "alternance"],
        geography=GeographyPreferences(
            preferred_countries=["France"],
            allowed_regions=["Europe"],
        ),
        experience=ExperiencePreferences(
            excluded_seniority=["senior", "director", "vp", "c_suite"]
        ),
        freshness_days=30,
    )

    def test_internship_title_with_domain_keyword_passes(self):
        """'Intern (m/f/d) Software Development' passes via the genuine domain
        keyword 'software developer' — NOT via 'intern ' alone (see
        TestContractKeywordNeverAloneEstablishesDomain below)."""
        job = _job(id="int", title="Intern (m/f/d) Software Developer")
        assert _passes(job, self._profile), "Internship title must pass Profile E"

    def test_stage_developpeur_passes(self):
        job = _job(id="stg", title="Stage Développeur Web")
        assert _passes(job, self._profile)

    def test_alternance_developer_passes(self):
        job = _job(id="alt", title="Alternance Développeur Python")
        assert _passes(job, self._profile)

    def test_developer_without_level_signal_passes(self):
        """Generic developer title passes — contract type cannot always be inferred."""
        job = _job(id="dev", title="Software Developer")
        assert _passes(job, self._profile)


class TestContractKeywordNeverAloneEstablishesDomain:
    """A contract-type keyword (stage/alternance/apprenti/intern) must NEVER
    by itself make a job pass the domain filter — it only describes HOW a job
    is offered, never WHAT domain it is in. Regression coverage for the
    2026-09-05 real-job benchmark's apprenticeship-bypass bug: a retail
    ("Apprenti Employé Libre Service") and a hotel-reception ("Réceptionniste
    ... alternance") posting both incorrectly passed the IT domain filter
    purely because build_filter_config used to merge contract-type keywords
    into the same inclusion list as genuine domain/skill keywords.
    """

    _profile = JobSearchProfile(
        target_domains=["software_development"],
        contract_types=["internship", "alternance"],
        geography=GeographyPreferences(
            preferred_countries=["France"],
            allowed_regions=["Europe"],
        ),
        experience=ExperiencePreferences(
            excluded_seniority=["senior", "director", "vp", "c_suite"]
        ),
        freshness_days=30,
    )

    def test_pure_internship_title_with_no_domain_signal_is_rejected(self):
        """'Intern (m/f/d)' alone, with no domain/skill word anywhere, must
        NOT pass just because 'intern ' is a contract keyword."""
        job = _job(
            id="int-no-domain",
            title="Intern (m/f/d)",
            description="Vous rejoignez notre équipe pour une durée de 6 mois.",
        )
        assert not _passes(job, self._profile)

    def test_retail_apprenticeship_is_rejected(self):
        job = _job(
            id="apprenti-retail",
            title="Apprenti - Employé Libre Service (H/F)",
            description="Mise en rayon, tenue de caisse, accueil clientèle en grande distribution.",
        )
        assert not _passes(job, self._profile)

    def test_hotel_reception_alternance_is_rejected(self):
        job = _job(
            id="alternance-hotel",
            title="Réceptionniste / Agent d'accueil hôtellerie - Alternance (H/F)",
            description="Accueil des clients, gestion des réservations, standard téléphonique.",
        )
        assert not _passes(job, self._profile)

    def test_stage_with_genuine_domain_word_still_passes(self):
        """Same contract type, but the title also carries a real domain
        signal ('développeur') — this must still pass."""
        job = _job(id="stage-dev", title="Stage Développeur Web")
        assert _passes(job, self._profile)

    def test_senior_software_engineer_rejected(self):
        job = _job(id="sn", title="Senior Software Engineer")
        assert _reason(job, self._profile) == "seniority"

    def test_director_engineering_rejected(self):
        job = _job(id="dir", title="Director of Engineering")
        assert _reason(job, self._profile) == "seniority"

    def test_cto_rejected(self):
        job = _job(id="cto", title="CTO")
        assert _reason(job, self._profile) == "seniority"

    def test_international_title_not_matched_as_intern(self):
        """'Internationales Marketing' must NOT match the 'intern ' keyword."""
        job = _job(id="mkt", title="Werkstudent Internationales Marketing")
        assert _reason(job, self._profile) == "title_keywords", (
            "'Internationales' must not trigger the 'intern ' keyword match"
        )


# ---------------------------------------------------------------------------
# build_filter_config contract tests
# ---------------------------------------------------------------------------

class TestBuildFilterConfig:
    """Verify build_filter_config produces the expected filter config structure."""

    def test_profile_domains_populate_title_include(self):
        profile = JobSearchProfile(
            target_domains=["software_development"],
            geography=GeographyPreferences(preferred_countries=["France"]),
            freshness_days=30,
        )
        cfg = build_filter_config(profile, _ENGINE)
        assert "software engineer" in cfg["title_keywords"]["include"]
        assert "developer" in cfg["title_keywords"]["include"]

    def test_target_skills_added_to_title_and_description(self):
        profile = JobSearchProfile(
            target_skills=["python", "javascript"],
            geography=GeographyPreferences(preferred_countries=["France"]),
            freshness_days=30,
        )
        cfg = build_filter_config(profile, _ENGINE)
        assert "python" in cfg["title_keywords"]["include"]
        assert "python" in cfg["title_keywords"]["description_include"]
        assert "javascript" in cfg["title_keywords"]["include"]

    def test_contract_keywords_never_populate_title_include(self):
        """Contract-type keywords (internship/alternance/...) must never be
        added to title_keywords.include: matches_title_keywords() accepts on
        ANY include-list hit, so a contract keyword there would let it alone
        establish IT-domain relevance — the 2026-09-05 apprenticeship-bypass
        bug (see TestContractKeywordNeverAloneEstablishesDomain)."""
        profile = JobSearchProfile(
            contract_types=["internship"],
            geography=GeographyPreferences(preferred_countries=["France"]),
            freshness_days=30,
        )
        cfg = build_filter_config(profile, _ENGINE)
        assert "internship" not in cfg["title_keywords"]["include"]
        assert "intern " not in cfg["title_keywords"]["include"]
        # Must NOT appear in description_include either (ambiguous substring matching risk)
        assert "intern " not in cfg["title_keywords"]["description_include"]
        assert "internship" not in cfg["title_keywords"]["description_include"]

    def test_seniority_exclusions_expanded(self):
        profile = JobSearchProfile(
            geography=GeographyPreferences(preferred_countries=["France"]),
            experience=ExperiencePreferences(excluded_seniority=["senior", "director"]),
            freshness_days=30,
        )
        cfg = build_filter_config(profile, _ENGINE)
        assert "senior" in cfg["seniority"]["exclude"]
        assert "director" in cfg["seniority"]["exclude"]
        assert "head of" in cfg["seniority"]["exclude"]

    def test_location_include_from_profile(self):
        profile = JobSearchProfile(
            geography=GeographyPreferences(
                preferred_countries=["France"],
                allowed_regions=["Europe"],
            ),
            freshness_days=30,
        )
        cfg = build_filter_config(profile, _ENGINE)
        assert "France" in cfg["locations"]["include"]
        assert "Europe" in cfg["locations"]["include"]

    def test_freshness_days_from_profile(self):
        profile = JobSearchProfile(
            geography=GeographyPreferences(preferred_countries=["France"]),
            freshness_days=14,
        )
        cfg = build_filter_config(profile, _ENGINE)
        assert cfg["posted_within_days"] == 14

    def test_excluded_title_keywords_from_engine(self):
        profile = JobSearchProfile(
            geography=GeographyPreferences(preferred_countries=["France"]),
            freshness_days=30,
        )
        cfg = build_filter_config(profile, _ENGINE)
        assert "comptable" in cfg["title_keywords"]["exclude"]

    def test_no_domains_no_title_restriction(self):
        """Empty target_domains + no skills → include list is empty → all titles pass."""
        profile = JobSearchProfile(
            geography=GeographyPreferences(preferred_countries=["France"]),
            freshness_days=30,
        )
        cfg = build_filter_config(profile, _ENGINE)
        job = _job(id="any", title="Anything At All")
        passed, _ = filter_jobs([job], cfg)
        assert any(j.id == "any" for j in passed)

    def test_changing_domain_changes_keywords(self):
        """Switching from software_development to cybersecurity changes the keyword set."""
        profile_sw = JobSearchProfile(
            target_domains=["software_development"],
            geography=GeographyPreferences(preferred_countries=["France"]),
            freshness_days=30,
        )
        profile_cy = JobSearchProfile(
            target_domains=["cybersecurity"],
            geography=GeographyPreferences(preferred_countries=["France"]),
            freshness_days=30,
        )
        cfg_sw = build_filter_config(profile_sw, _ENGINE)
        cfg_cy = build_filter_config(profile_cy, _ENGINE)

        assert "developer" in cfg_sw["title_keywords"]["include"]
        assert "developer" not in cfg_cy["title_keywords"]["include"]
        assert "cybersecurity" in cfg_cy["title_keywords"]["include"]
        assert "cybersecurity" not in cfg_sw["title_keywords"]["include"]


# ---------------------------------------------------------------------------
# to_semantic_info tests
# ---------------------------------------------------------------------------

class TestToSemanticInfo:
    """Verify JobSearchProfile.to_semantic_info() populates all expected keys."""

    def test_all_keys_present(self):
        profile = JobSearchProfile(
            target_domains=["software_development"],
            target_skills=["python", "javascript"],
            geography=GeographyPreferences(preferred_countries=["France"]),
            contract_types=["internship", "cdi"],
            experience=ExperiencePreferences(preferred_levels=["junior", "internship"]),
            languages=["French", "English"],
            freshness_days=30,
        )
        info = profile.to_semantic_info()
        for key in ("seniority", "employment_type", "location", "languages", "skills", "responsibilities"):
            assert key in info, f"Missing key: {key}"

    def test_seniority_from_preferred_levels(self):
        profile = JobSearchProfile(
            experience=ExperiencePreferences(preferred_levels=["junior", "internship"]),
        )
        info = profile.to_semantic_info()
        assert "junior" in info["seniority"]
        assert "internship" in info["seniority"]

    def test_location_from_preferred_countries(self):
        profile = JobSearchProfile(
            geography=GeographyPreferences(preferred_countries=["France"]),
        )
        info = profile.to_semantic_info()
        assert "France" in info["location"]

    def test_skills_include_target_skills_and_domains(self):
        profile = JobSearchProfile(
            target_domains=["cybersecurity"],
            target_skills=["python"],
        )
        info = profile.to_semantic_info()
        assert "python" in info["skills"]
        assert "cybersecurity" in info["skills"]

    def test_empty_profile_uses_defaults(self):
        info = JobSearchProfile().to_semantic_info()
        assert info["seniority"] == "entry-level"
        assert info["location"] == "Europe"

    def test_looking_for_folded_into_responsibilities(self):
        profile = JobSearchProfile(
            target_domains=["cybersecurity"],
            looking_for="I like on-site jobs in cybersecurity",
        )
        info = profile.to_semantic_info()
        assert "I like on-site jobs in cybersecurity" in info["responsibilities"]
        assert "cybersecurity" in info["responsibilities"]

    def test_looking_for_absent_when_not_set(self):
        info = JobSearchProfile(target_domains=["cybersecurity"]).to_semantic_info()
        assert info["responsibilities"] == "cybersecurity"

    def test_skills_is_a_joined_string(self):
        """Regression: `skills` used to be a raw list (target_skills +
        target_domains), which str.format() renders as Python-list syntax
        ("['python', 'sql']") inside the embedding text — noise the
        sentence-transformer model was never trained on. Must be a joined
        string like every other to_semantic_info() field (mirrors the
        existing `languages` fix, see TestProfileLanguagesString in
        tests/test_ranking_correctness.py)."""
        profile = JobSearchProfile(target_skills=["python", "sql"], target_domains=["backend"])
        info = profile.to_semantic_info()
        assert isinstance(info["skills"], str), (
            f"skills must be a string for embedding, got {type(info['skills'])}: {info['skills']!r}"
        )
        assert "[" not in info["skills"], f"skills must not be a Python list repr, got: {info['skills']!r}"
        assert "python" in info["skills"]
        assert "backend" in info["skills"]

    def test_skills_empty_profile_defaults_to_string(self):
        info = JobSearchProfile().to_semantic_info()
        assert isinstance(info["skills"], str)

    def test_different_looking_for_changes_responsibilities(self):
        """Two profiles differing only in `looking_for` must produce
        different `responsibilities` text — this is the exact field Phase
        3's semantic embedding template consumes (see
        build_profile_embedding's default template in
        src/pipeline/semantic_matching.py). Proves the field is wired into
        the text that ultimately becomes the profile embedding, generically,
        for arbitrary free text (not a hardcoded keyword)."""
        base = {"target_domains": ["software development"]}
        profile_a = JobSearchProfile(**base, looking_for="cybersecurity")
        profile_b = JobSearchProfile(**base, looking_for="web development")
        profile_none = JobSearchProfile(**base)

        info_a = profile_a.to_semantic_info()
        info_b = profile_b.to_semantic_info()
        info_none = profile_none.to_semantic_info()

        assert info_a["responsibilities"] != info_b["responsibilities"]
        assert info_a["responsibilities"] != info_none["responsibilities"]
        assert info_b["responsibilities"] != info_none["responsibilities"]
