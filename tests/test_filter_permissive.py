"""Regression tests for the permissive filter redesign.

Verifies that the hard filter has HIGH RECALL:
- IT jobs (French, English, generic titles) survive.
- Internship / alternance jobs survive.
- Jobs with missing data survive.
- On-site, hybrid, and remote jobs survive.
- Only clearly incompatible jobs are rejected.

No Internet access.  No collectors called.  No real database touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


from src.models.job import Job
from src.pipeline.filter import filter_jobs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)
_RECENT = _NOW - timedelta(days=1)
_OLD_BUT_WITHIN_WINDOW = _NOW - timedelta(days=20)  # older than 7d, within 30d
_TOO_OLD = _NOW - timedelta(days=60)


def _job(**kwargs) -> Job:
    """Build a minimal Job for filter testing; kwargs override defaults."""
    defaults: dict = dict(
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


# ---------------------------------------------------------------------------
# Filter config equivalent to build_filter_config(current_profile, engine_cfg).
# Inline here so these unit tests remain independent of the config files.
# ---------------------------------------------------------------------------

_CFG = {
    "locations": {
        "include": ["France", "Europe"],
    },
    "remote_scope": {
        "european_countries": [
            "France", "Germany", "Netherlands", "Belgium", "Spain", "Italy",
            "Portugal", "Austria", "Switzerland", "Sweden", "Denmark", "Norway",
            "Finland", "Poland", "Czech Republic", "Ireland", "United Kingdom",
            "UK", "Romania", "Hungary", "Greece", "Croatia", "Slovakia",
            "Slovenia", "Bulgaria", "Estonia", "Latvia", "Lithuania",
            "Luxembourg", "Malta", "Cyprus",
        ],
        "european_keywords": ["Europe", "EU", "EMEA", "European"],
        "worldwide_keywords": ["worldwide", "global", "anywhere", "international"],
    },
    "remote": {"allowed": []},
    "seniority": {
        "exclude": [
            "senior", "director", "head of", "vice president", "vp ", "chief ",
            "cto", "ciso", "coo", "cpo",
        ],
    },
    "title_keywords": {
        "include": [
            # English
            "software engineer", "software developer", "backend engineer",
            "frontend engineer", "full stack", "full-stack", "developer",
            "programmer", "web developer", "devops", "sysadmin",
            "system administrator", "network engineer", "network administrator", "network technician",
            "security engineer", "cybersecurity", "data analyst", "data engineer",
            "data scientist", "data science", "machine learning",
            "qa engineer", "qa tester", "quality assurance", "automation engineer",
            "test engineer", "tester", "it support", "helpdesk", "help desk",
            "cloud engineer", "infrastructure engineer", "site reliability",
            "dba", "database administrator", "platform engineer", "mlops",
            # French
            "développeur", "developpeur", "développeuse", "developpeuse",
            "ingénieur logiciel", "ingenieur logiciel",
            "ingénieur informatique", "ingenieur informatique",
            "ingénieur développement", "ingenieur developpement",
            "technicien informatique", "technicien réseau", "technicien reseau",
            "technicien systèmes", "technicien systemes",
            "administrateur systèmes", "administrateur systemes",
            "administrateur réseau", "administrateur reseau",
            "ingénieur réseau", "ingenieur reseau",
            "ingénieur systèmes", "ingenieur systemes",
            "ingénieur cloud", "ingenieur cloud",
            "ingénieur sécurité", "ingenieur securite",
            "cybersécurité", "cybersecurite",
            "réseaux", "reseaux",
            "automatisation",
            "analyste données", "analyste donnees",
            "ingénieur données", "ingenieur donnees",
            "ingénieur data", "ingenieur data",
            "analyste data",
            "testeur logiciel", "testeur informatique",
            "ingénieur test", "ingenieur test",
            "support informatique", "support technique",
            "informatique",
            # Internship / alternance
            "stage", "stagiaire", "alternance", "alternant",
            "apprentissage", "apprenti",
            "internship", "intern ", "apprenticeship", "apprentice", "placement",
            # Technology names
            "python", "javascript", "typescript", "node.js", "nodejs",
            "react", "angular", "vue.js", "vuejs",
            "php", "java", "golang", "rust", "swift", "kotlin",
            "c#", ".net", "sql", "postgresql", "mysql", "mongodb",
            "redis", "elasticsearch",
            "aws", "azure", "gcp", "kubernetes", "docker",
            "terraform", "ansible", "linux",
            "microservices", "pytorch", "tensorflow",
            "hadoop", "spark", "airflow", "dbt",
            # German
            "entwickler", "softwareentwickler", "netzwerk",
            # QA
            "qa", "qe",
        ],
        # Stricter list for description/skills fallback — unambiguous IT signals only.
        # Excludes "intern" (→ interne), "stage" (→ staged), "placement" (non-IT French)
        # and other terms that generate false positives via substring matching.
        "description_include": [
            "python", "javascript", "typescript", "node.js", "nodejs",
            "angular", "vue.js", "vuejs", "php", "golang", "kotlin",
            "c#", ".net", "java", "postgresql", "mysql", "mongodb",
            "redis", "elasticsearch", "sql",
            "kubernetes", "docker", "terraform", "ansible",
            "devops", "linux", "aws", "azure", "gcp", "microservices",
            "pytorch", "tensorflow", "mlops", "machine learning",
            "cybersécurité", "cybersecurite", "cybersecurity",
            "développeur", "developpeur", "développeuse", "developpeuse",
            "software engineer", "software developer",
            "ingénieur logiciel", "ingenieur logiciel",
            "ingénieur informatique", "ingenieur informatique",
            "développement logiciel", "developpement logiciel",
            "programmation", "hadoop", "airflow",
            "entwickler", "softwareentwickler",
        ],
        "exclude": [
            "comptable", "juriste", "infirmier", "infirmière", "infirmiere",
            "plombier", "carrossier", "électricien", "electricien",
            "chaudronnier", "soudeur", "maçon", "macon",
            "pédiatre", "pediatre", "médecin", "medecin",
            "pharmacien", "kiné", "kine",
        ],
    },
    "posted_within_days": 30,
    "salary_requirements": {"min_salary": None, "max_salary": None},
}


# ---------------------------------------------------------------------------
# Helper: pass / fail assertions
# ---------------------------------------------------------------------------

def _passes(job: Job) -> bool:
    passed, _ = filter_jobs([job], _CFG)
    return any(j.id == job.id for j in passed)


def _rejection_reason(job: Job) -> str | None:
    _, rejected = filter_jobs([job], _CFG)
    return rejected.get(job.id)


# ---------------------------------------------------------------------------
# Should SURVIVE the filter
# ---------------------------------------------------------------------------

class TestShouldPass:

    # ── Geography ─────────────────────────────────────────────────────────

    def test_france_onsite_junior_developer(self):
        job = _job(
            id="fr-onsite-junior",
            title="Junior Software Developer",
            location="Paris, France",
            countries=["France"],
            remote=False,
        )
        assert _passes(job), "France onsite junior developer must pass"

    def test_france_hybrid_developer(self):
        job = _job(
            id="fr-hybrid",
            title="Software Developer",
            location="Lyon, France (hybrid)",
            countries=["France"],
            remote=False,
        )
        assert _passes(job), "France hybrid developer must pass"

    def test_france_remote_developer(self):
        job = _job(
            id="fr-remote",
            title="Software Developer",
            location="France",
            countries=["France"],
            remote=True,
        )
        assert _passes(job), "France remote developer must pass"

    def test_europe_remote_developer(self):
        job = _job(
            id="eu-remote",
            title="Software Developer",
            location="Remote, Europe",
            countries=[],
            remote=True,
        )
        assert _passes(job), "Europe remote developer must pass"

    # ── French titles ──────────────────────────────────────────────────────

    def test_french_developpeur_python(self):
        job = _job(
            id="fr-dev-py",
            title="Développeur Python",
            location="Paris, France",
            countries=["France"],
        )
        assert _passes(job), "Développeur Python must pass"

    def test_french_developpeur_no_accent(self):
        """Accent-insensitive: 'developpeur' (no accent) must also match."""
        job = _job(
            id="fr-dev-noacc",
            title="Developpeur Python",
            location="Paris, France",
            countries=["France"],
        )
        assert _passes(job), "developpeur (no accent) must pass"

    def test_french_ingenieur_logiciel(self):
        job = _job(
            id="fr-ing-log",
            title="Ingénieur Logiciel",
            location="Bordeaux, France",
            countries=["France"],
        )
        assert _passes(job), "Ingénieur Logiciel must pass"

    def test_french_technicien_informatique(self):
        job = _job(
            id="fr-tech-info",
            title="Technicien Informatique",
            location="Lille, France",
            countries=["France"],
        )
        assert _passes(job), "Technicien Informatique must pass"

    def test_french_analyste_cybersecurite(self):
        job = _job(
            id="fr-analyste-cyber",
            title="Analyste Cybersécurité",
            location="Lyon, France",
            countries=["France"],
        )
        assert _passes(job), "Analyste Cybersécurité must pass"

    def test_french_ingenieur_informatique(self):
        job = _job(
            id="fr-ing-info",
            title="Ingénieur Informatique",
            location="Toulouse, France",
            countries=["France"],
        )
        assert _passes(job), "Ingénieur Informatique must pass"

    def test_french_administrateur_reseaux(self):
        job = _job(
            id="fr-admin-reseau",
            title="Administrateur Réseaux",
            location="Nantes, France",
            countries=["France"],
        )
        assert _passes(job), "Administrateur Réseaux must pass"

    def test_french_title_without_informatique_but_it_description(self):
        """Generic title 'Analyste' but IT description → passes via description fallback."""
        job = _job(
            id="fr-analyste-generic",
            title="Analyste",
            location="Paris, France",
            countries=["France"],
            description="Développement Python, SQL, analyse de données et déploiement cloud.",
        )
        assert _passes(job), "Generic 'Analyste' with IT description must pass"

    def test_technicien_generic_title_it_description(self):
        """Generic title 'Technicien' but description clearly IT → passes."""
        job = _job(
            id="fr-tech-generic",
            title="Technicien",
            location="Paris, France",
            countries=["France"],
            description="Support technique niveau 2, administration réseau Linux, tickets helpdesk.",
        )
        assert _passes(job), "Generic 'Technicien' with IT description must pass"

    def test_ingenieur_generic_title_it_skills(self):
        """Generic title 'Ingénieur' but IT skills → passes via skills fallback."""
        job = _job(
            id="fr-ing-generic",
            title="Ingénieur",
            location="Paris, France",
            countries=["France"],
            skills=["Python", "Docker", "Kubernetes", "AWS"],
        )
        assert _passes(job), "Generic 'Ingénieur' with IT skills must pass"

    # ── Internship / alternance ────────────────────────────────────────────

    def test_stage_developpeur(self):
        job = _job(
            id="stage-dev",
            title="Stage Développeur Web",
            location="Paris, France",
            countries=["France"],
        )
        assert _passes(job), "Stage Développeur Web must pass"

    def test_alternance_developpeur(self):
        job = _job(
            id="alternance-dev",
            title="Alternance Développeur Python",
            location="Nantes, France",
            countries=["France"],
        )
        assert _passes(job), "Alternance Développeur Python must pass"

    def test_alternant_informatique(self):
        job = _job(
            id="alternant",
            title="Alternant Informatique",
            location="Lyon, France",
            countries=["France"],
        )
        assert _passes(job), "Alternant Informatique must pass"

    def test_stagiaire_devops(self):
        job = _job(
            id="stagiaire-devops",
            title="Stagiaire DevOps",
            location="Bordeaux, France",
            countries=["France"],
        )
        assert _passes(job), "Stagiaire DevOps must pass"

    def test_english_internship_developer(self):
        job = _job(
            id="intern-dev",
            title="Software Developer Internship",
            location="Paris, France",
            countries=["France"],
            remote=True,
        )
        assert _passes(job), "Software Developer Internship must pass"

    def test_apprentice(self):
        job = _job(
            id="apprentice",
            title="Apprentice Software Engineer",
            location="Remote, Europe",
            countries=[],
            remote=True,
        )
        assert _passes(job), "Apprentice Software Engineer must pass"

    # ── English IT roles ───────────────────────────────────────────────────

    def test_qa_engineer(self):
        job = _job(
            id="qa-eng",
            title="QA Engineer",
            location="Paris, France",
            countries=["France"],
        )
        assert _passes(job), "QA Engineer must pass"

    def test_data_analyst(self):
        job = _job(
            id="data-analyst",
            title="Data Analyst",
            location="Lyon, France",
            countries=["France"],
        )
        assert _passes(job), "Data Analyst must pass"

    def test_network_technician(self):
        job = _job(
            id="net-tech",
            title="Network Technician",
            location="Paris, France",
            countries=["France"],
        )
        assert _passes(job), "Network Technician must pass"

    def test_devops_engineer(self):
        job = _job(
            id="devops-eng",
            title="DevOps Engineer",
            location="Remote, Europe",
            countries=[],
            remote=True,
        )
        assert _passes(job), "DevOps Engineer must pass"

    def test_cybersecurity_analyst(self):
        job = _job(
            id="cyber-analyst",
            title="Cybersecurity Analyst",
            location="Paris, France",
            countries=["France"],
        )
        assert _passes(job), "Cybersecurity Analyst must pass"

    def test_it_support(self):
        job = _job(
            id="it-support",
            title="IT Support Specialist",
            location="Marseille, France",
            countries=["France"],
        )
        assert _passes(job), "IT Support Specialist must pass"

    def test_fullstack_developer(self):
        job = _job(
            id="fullstack",
            title="Full Stack Developer",
            location="Remote",
            countries=["France"],
            remote=True,
        )
        assert _passes(job), "Full Stack Developer must pass"

    def test_machine_learning_engineer(self):
        job = _job(
            id="ml-eng",
            title="Machine Learning Engineer",
            location="Paris, France",
            countries=["France"],
        )
        assert _passes(job), "Machine Learning Engineer must pass"

    # ── Missing data → allow ───────────────────────────────────────────────

    def test_missing_seniority_signal_passes(self):
        """A neutral title with no seniority keyword passes seniority check."""
        job = _job(
            id="no-seniority",
            title="Python Developer",
            location="Paris, France",
            countries=["France"],
        )
        assert _passes(job), "Job with no seniority signal must pass"

    def test_missing_remote_info_passes(self):
        """remote=False (default) with valid France location must pass."""
        job = _job(
            id="no-remote",
            title="Software Engineer",
            location="Paris, France",
            countries=["France"],
            remote=False,
        )
        assert _passes(job), "Job with missing remote info (remote=False) must pass"

    def test_missing_employment_type_passes(self):
        """employment_type not set must not trigger rejection."""
        job = _job(
            id="no-emptype",
            title="Developer",
            location="Paris, France",
            countries=["France"],
            employment_type=None,
        )
        assert _passes(job), "Job with missing employment_type must pass"

    def test_missing_salary_passes(self):
        job = _job(
            id="no-salary",
            title="Developer",
            location="Paris, France",
            countries=["France"],
            salary_min=None,
            salary_max=None,
        )
        assert _passes(job), "Job with missing salary must pass"

    def test_missing_published_date_passes(self):
        """When published_at is None and discovered_at is recent, job passes."""
        job = _job(
            id="no-pubdate",
            title="Software Engineer",
            location="Paris, France",
            countries=["France"],
            published_at=None,
        )
        assert _passes(job), "Job with missing published_at must pass (uses discovered_at)"

    def test_missing_skills_passes(self):
        job = _job(
            id="no-skills",
            title="Developer",
            location="Paris, France",
            countries=["France"],
            skills=[],
        )
        assert _passes(job), "Job with no skills must pass"

    # ── Experience requirements (conveyed via title / no signal) ──────────

    def test_job_one_to_two_years_no_seniority_keyword(self):
        """A title like 'Python Developer (1-2 years experience)' has no
        exclusion keywords → passes the seniority filter."""
        job = _job(
            id="one-two-years",
            title="Python Developer (1-2 years experience)",
            location="Paris, France",
            countries=["France"],
        )
        assert _passes(job), "Job asking for 1-2 years must pass"

    def test_job_up_to_three_years(self):
        job = _job(
            id="three-years",
            title="Software Engineer - up to 3 years experience",
            location="Lyon, France",
            countries=["France"],
        )
        assert _passes(job), "Job asking for up to 3 years must pass"

    # ── Publication age ────────────────────────────────────────────────────

    def test_job_older_than_7_days_within_30_days_passes(self):
        """A job posted 20 days ago must pass (new window is 30 days)."""
        job = _job(
            id="old-within-window",
            title="Developer",
            location="Paris, France",
            countries=["France"],
            published_at=_OLD_BUT_WITHIN_WINDOW,
        )
        assert _passes(job), "Job posted 20 days ago must pass (within 30-day window)"



# ---------------------------------------------------------------------------
# Should be REJECTED by the filter
# ---------------------------------------------------------------------------

class TestShouldReject:

    def test_explicitly_non_it_admin_job(self):
        """Comptable (accountant) is in the title_keywords.exclude list."""
        job = _job(
            id="comptable",
            title="Comptable",
            location="Paris, France",
            countries=["France"],
        )
        reason = _rejection_reason(job)
        assert reason == "title_keywords", f"Comptable must be rejected; got '{reason}'"

    def test_clearly_unrelated_profession(self):
        """Plombier (plumber) is in the title_keywords.exclude list."""
        job = _job(
            id="plombier",
            title="Plombier chauffagiste",
            location="Paris, France",
            countries=["France"],
        )
        reason = _rejection_reason(job)
        assert reason == "title_keywords", f"Plombier must be rejected; got '{reason}'"

    def test_senior_software_engineer_rejected(self):
        """'senior' is in the seniority.exclude list."""
        job = _job(
            id="senior-swe",
            title="Senior Software Engineer",
            location="Paris, France",
            countries=["France"],
        )
        reason = _rejection_reason(job)
        assert reason == "seniority", f"Senior Software Engineer must be rejected; got '{reason}'"

    def test_explicitly_cto_rejected(self):
        """CTO is in the seniority.exclude list."""
        job = _job(
            id="cto",
            title="CTO — Chief Technology Officer",
            location="Paris, France",
            countries=["France"],
        )
        reason = _rejection_reason(job)
        assert reason == "seniority", f"CTO must be rejected at seniority; got '{reason}'"

    def test_director_of_engineering_rejected(self):
        job = _job(
            id="director-eng",
            title="Director of Engineering",
            location="Paris, France",
            countries=["France"],
        )
        reason = _rejection_reason(job)
        assert reason == "seniority", f"Director of Engineering must be rejected; got '{reason}'"

    def test_germany_onsite_rejected(self):
        """Germany on-site: confirmed foreign, not remote → rejected at location."""
        job = _job(
            id="de-onsite",
            title="Software Engineer",
            location="Berlin",
            countries=["Germany"],
            remote=False,
        )
        reason = _rejection_reason(job)
        assert reason == "location", f"Germany on-site must be rejected at location; got '{reason}'"

    def test_germany_hybrid_rejected(self):
        """Germany hybrid (remote=False): confirmed foreign → rejected at location."""
        job = _job(
            id="de-hybrid",
            title="Software Developer",
            location="Berlin (hybrid)",
            countries=["Germany"],
            remote=False,
        )
        reason = _rejection_reason(job)
        assert reason == "location", f"Germany hybrid must be rejected at location; got '{reason}'"

    def test_germany_remote_eu_country_passes(self):
        """Germany remote: Germany in european_countries → passes location filter."""
        job = _job(
            id="de-remote-unk",
            title="Software Developer",
            location="Berlin",
            countries=["Germany"],
            remote=True,
        )
        reason = _rejection_reason(job)
        assert reason is None, f"Germany remote should pass location filter; got '{reason}'"

    def test_non_european_onsite_rejected(self):
        """Non-European on-site job (Japan, US, etc.) is rejected at location."""
        job = _job(
            id="jp-onsite",
            title="Software Engineer",
            location="Tokyo",
            countries=["Japan"],
            remote=False,
        )
        reason = _rejection_reason(job)
        assert reason == "location", f"Japan on-site must be rejected; got '{reason}'"

    def test_usa_remote_rejected(self):
        """Explicit USA country on a remote job → location rejection."""
        job = _job(
            id="usa-remote",
            title="Software Engineer",
            location="San Francisco, USA",
            countries=["USA"],
            remote=True,
        )
        reason = _rejection_reason(job)
        assert reason == "location", f"USA remote job must be rejected at location; got '{reason}'"

    def test_job_too_old(self):
        """Job posted 60 days ago exceeds the 30-day window."""
        job = _job(
            id="too-old",
            title="Software Engineer",
            location="Paris, France",
            countries=["France"],
            published_at=_TOO_OLD,
        )
        reason = _rejection_reason(job)
        assert reason == "published_age", f"60-day-old job must be rejected; got '{reason}'"

    def test_non_it_job_no_it_signal_in_description(self):
        """Title 'Plombier' (excluded) + no IT description → rejected."""
        job = _job(
            id="plombier-no-it",
            title="Plombier",
            location="Paris, France",
            countries=["France"],
            description="Travaux de plomberie, installation sanitaire, dépannage.",
        )
        reason = _rejection_reason(job)
        assert reason == "title_keywords", f"Plombier with non-IT description must be rejected; got '{reason}'"


# ---------------------------------------------------------------------------
# Accent normalization
# ---------------------------------------------------------------------------

class TestAccentNormalization:
    """Verify that accented and unaccented forms both match keywords."""

    def test_developpeur_with_accent(self):
        job = _job(id="acc-1", title="Développeur Web", countries=["France"])
        assert _passes(job)

    def test_developpeur_without_accent(self):
        job = _job(id="acc-2", title="Developpeur Web", countries=["France"])
        assert _passes(job)

    def test_developpeur_with_accent_variant(self):
        job = _job(id="acc-3", title="Développeuse Backend", countries=["France"])
        assert _passes(job)

    def test_ingenieur_informatique_with_accents(self):
        job = _job(id="acc-4", title="Ingénieur Informatique", countries=["France"])
        assert _passes(job)

    def test_ingenieur_informatique_without_accent(self):
        job = _job(id="acc-5", title="Ingenieur Informatique", countries=["France"])
        assert _passes(job)

    def test_cybersecurite_with_accent(self):
        job = _job(id="acc-6", title="Analyste Cybersécurité", countries=["France"])
        assert _passes(job)

    def test_cybersecurite_without_accent(self):
        job = _job(id="acc-7", title="Analyste Cybersecurite", countries=["France"])
        assert _passes(job)

    def test_reseaux_with_accent(self):
        job = _job(id="acc-8", title="Ingénieur Réseaux", countries=["France"])
        assert _passes(job)

    def test_reseaux_without_accent(self):
        job = _job(id="acc-9", title="Ingenieur Reseaux", countries=["France"])
        assert _passes(job)
