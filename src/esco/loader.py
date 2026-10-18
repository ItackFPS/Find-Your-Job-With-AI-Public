"""ESCO data loader — populates the local SQLite FTS database.

Two sources are supported:

1. ESCO public REST API (default, free, no auth required):
   ``https://ec.europa.eu/esco/api/search``
   Used by the setup script to seed common IT skills in English and French.

2. In-process seed data (bundled):
   A minimal curated list of IT concepts.  Always available, used as fallback
   when the setup has not been run.

Usage (from ``scripts/setup_esco.py``)::

    from src.esco.db import EscoDb
    from src.esco.loader import load_from_api, load_seed_data

    db = EscoDb("data/esco.db")
    db.init_db()
    load_seed_data(db)        # always works, no network
    load_from_api(db)         # requires internet, may fail gracefully
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.esco.db import EscoConcept, EscoDb

logger = logging.getLogger(__name__)

# Public ESCO REST API — free, no authentication required.
_ESCO_API_BASE = "https://ec.europa.eu/esco/api"
_SEARCH_URL = f"{_ESCO_API_BASE}/search"

# Skill search terms for seeding the local database.
# We query both English and French forms to get bilingual labels.
# Historically IT-only ("_IT_SEED_TERMS") — broadened to cover the non-IT
# domains added to config/filters.yaml (sales, cleaning, healthcare, …) so
# skill autocomplete isn't limited to tech roles.
_SKILL_SEED_TERMS_EN = [
    "software development", "web development", "python", "javascript",
    "typescript", "java", "c#", "go", "rust", "php", "ruby", "kotlin",
    "swift", "react", "angular", "vue.js", "node.js", "django", "fastapi",
    "spring", "docker", "kubernetes", "git", "linux", "aws", "azure",
    "google cloud", "devops", "ci/cd", "terraform", "ansible", "postgresql",
    "mysql", "mongodb", "redis", "elasticsearch", "machine learning",
    "data science", "artificial intelligence", "sql", "data analysis",
    "cybersecurity", "networking", "system administration", "cloud computing",
    "microservices", "rest api", "agile", "scrum", "testing", "qa",
    "fullstack", "backend", "frontend", "mobile development", "ios", "android",
    "data engineering", "mlops", "big data", "hadoop", "spark", "airflow",
    # Sales / retail
    "sales negotiation", "customer relationship management", "retail sales",
    "cash handling", "upselling",
    # Cleaning / facilities
    "cleaning techniques", "waste disposal", "sanitation",
    # Healthcare support
    "patient care", "first aid", "vital signs monitoring",
    # Accounting / finance
    "bookkeeping", "financial reporting", "payroll",
    # Legal / admin
    "legal research", "contract review", "office administration",
    # Trades
    "electrical wiring", "plumbing systems", "welding techniques",
    # Logistics / manual labor
    "inventory management", "forklift operation", "warehouse safety",
    # Hospitality
    "food safety", "customer service", "hospitality management",
    # Recruitment / HR
    "recruitment", "interviewing techniques", "talent management",
    # Procurement
    "supplier negotiation", "purchasing", "supply chain management",
    # Marketing
    "digital marketing", "social media marketing", "market research",
]

_SKILL_SEED_TERMS_FR = [
    "développement logiciel", "développement web", "python",
    "développeur", "ingénieur logiciel", "programmation",
    "machine learning", "apprentissage automatique", "intelligence artificielle",
    "sécurité informatique", "cybersécurité", "administration système",
    "informatique en nuage", "analyse de données", "science des données",
    "développement mobile", "tests logiciels", "assurance qualité",
    "architecture logicielle", "bases de données", "réseaux informatiques",
    "devops", "automatisation", "intégration continue",
    # Vente / commerce
    "techniques de vente", "relation client", "encaissement",
    # Nettoyage / propreté
    "techniques de nettoyage", "hygiène et propreté",
    # Santé
    "soins aux patients", "premiers secours",
    # Comptabilité / finance
    "comptabilité", "gestion de la paie",
    # Juridique / administratif
    "recherche juridique", "administration",
    # Métiers manuels
    "électricité", "plomberie", "soudure",
    # Logistique
    "gestion des stocks", "conduite de chariot élévateur",
    # Hôtellerie / restauration
    "sécurité alimentaire", "service client", "hôtellerie",
    # RH / recrutement
    "recrutement", "gestion des talents",
    # Achats
    "négociation fournisseurs", "achats",
    # Marketing
    "marketing digital", "études de marché",
]

# Occupation search terms — used with concept_type="occupation" to power the
# Profile page's domain search (see src/api/routes/esco.py:/occupations).
# One representative occupation per new non-IT domain in config/filters.yaml,
# in English and French so a user can search in either language.
_OCCUPATION_SEED_TERMS_EN = [
    "software developer", "web developer", "data scientist", "devops engineer",
    "cloud engineer", "cybersecurity analyst", "network engineer", "it support technician",
    "artificial intelligence engineer", "backend developer", "frontend developer",
    "systems administrator", "database administrator", "ux designer", "product manager",
    "quality assurance engineer", "mobile developer", "solutions architect",
    "salesperson", "sales representative", "retail sales assistant",
    "cleaner", "housekeeper", "janitor",
    "nurse", "caregiver", "healthcare assistant",
    "accountant", "bookkeeper",
    "paralegal", "legal assistant",
    "electrician", "plumber", "welder",
    "warehouse worker", "truck driver", "forklift operator",
    "hotel receptionist", "waiter", "chef",
    "recruiter", "hr assistant",
    "buyer", "procurement officer",
    "marketing manager", "digital marketer",
]

_OCCUPATION_SEED_TERMS_FR = [
    "développeur informatique", "développeur web", "ingénieur cybersécurité",
    "ingénieur intelligence artificielle", "développeur backend", "développeur frontend",
    "administrateur systèmes", "administrateur de bases de données", "concepteur ux",
    "chef de produit", "ingénieur qualité logicielle", "développeur mobile", "architecte solutions",
    "vendeur", "représentant commercial", "conseiller de vente",
    "agent d'entretien", "femme de ménage", "agent de propreté",
    "infirmier", "aide-soignant",
    "comptable", "aide-comptable",
    "assistant juridique",
    "électricien", "plombier", "soudeur",
    "agent d'entrepôt", "chauffeur routier", "cariste",
    "réceptionniste d'hôtel", "serveur", "cuisinier",
    "recruteur", "assistant rh",
    "acheteur",
    "responsable marketing",
]


def load_seed_data(db: EscoDb) -> int:
    """Load a minimal curated set of IT concepts (no network required).

    These are hand-curated entries matching known IT roles and skills.
    Each includes English and French labels.  The concept URIs are
    real ESCO URIs where known, or use a ``local:`` prefix for entries
    that are not yet confirmed against the live ESCO taxonomy.

    Returns the number of concepts inserted.
    """
    concepts = _build_seed_concepts()
    if not concepts:
        return 0
    db.upsert_many(concepts)
    logger.info("ESCO seed: inserted %d bundled concepts", len(concepts))
    return len(concepts)


def load_from_api(
    db: EscoDb,
    terms: list[str] | None = None,
    timeout: int = 10,
    delay_seconds: float = 0.3,
    concept_type: str = "skill",
) -> int:
    """Download ESCO concepts from the public REST API and store locally.

    Args:
        db:             Target ESCO database (must already be initialised).
        terms:          Search terms to query.  Defaults to the built-in skill
                         list, or the occupation list when ``concept_type``
                         is ``"occupation"``.
        timeout:        HTTP timeout per request in seconds.
        delay_seconds:  Polite delay between requests to avoid hammering the API.
        concept_type:   ``"skill"`` or ``"occupation"`` — passed straight
                         through to the ESCO API's ``type`` search parameter.

    Returns:
        Total number of unique concepts upserted.
    """
    try:
        import requests
    except ImportError:
        logger.error("requests library not available — cannot download ESCO data")
        return 0

    if terms is None:
        terms = (
            (_OCCUPATION_SEED_TERMS_EN + _OCCUPATION_SEED_TERMS_FR)
            if concept_type == "occupation"
            else (_SKILL_SEED_TERMS_EN + _SKILL_SEED_TERMS_FR)
        )
    seen: set[str] = set()
    total = 0

    for term in terms:
        for lang in ("en", "fr"):
            try:
                concepts = _fetch_concepts(term, lang, concept_type=concept_type, timeout=timeout)
                new_concepts = [c for c in concepts if c.concept_uri not in seen]
                if new_concepts:
                    db.upsert_many(new_concepts)
                    for c in new_concepts:
                        seen.add(c.concept_uri)
                    total += len(new_concepts)
                    logger.debug(
                        "ESCO API: '%s' (%s) → %d new concepts", term, lang, len(new_concepts)
                    )
                time.sleep(delay_seconds)
            except Exception as exc:
                logger.warning("ESCO API: failed for '%s' (%s): %s", term, lang, exc)
                continue

    logger.info("ESCO API: upserted %d unique concepts total", total)
    return total


def _fetch_concepts(
    query: str,
    language: str = "en",
    concept_type: str = "skill",
    limit: int = 20,
    timeout: int = 10,
) -> list[EscoConcept]:
    """Query the ESCO REST API for concepts matching a search term."""
    import requests

    params: dict[str, str | int] = {
        "text": query,
        "type": concept_type,
        "language": language,
        "limit": limit,
        "full": "true",
    }
    response = requests.get(_SEARCH_URL, params=params, timeout=timeout)
    response.raise_for_status()

    data = response.json()
    embedded = data.get("_embedded", {})
    results = embedded.get("results", [])

    concepts: list[EscoConcept] = []
    for item in results:
        concept = _parse_api_result(item)
        if concept is not None:
            concepts.append(concept)
    return concepts


def _parse_api_result(item: dict[str, Any]) -> EscoConcept | None:
    """Parse a single ESCO API search result into an EscoConcept."""
    uri: str = item.get("uri") or item.get("conceptUri") or ""
    if not uri:
        return None

    # preferredLabel may be a dict {lang: label} or a plain string
    preferred = item.get("preferredLabel") or {}
    if isinstance(preferred, str):
        preferred_en = preferred
        preferred_fr = ""
    elif isinstance(preferred, dict):
        preferred_en = preferred.get("en") or preferred.get("label") or ""
        preferred_fr = preferred.get("fr") or ""
    else:
        preferred_en = str(preferred)
        preferred_fr = ""

    if not preferred_en:
        preferred_en = item.get("title") or ""

    # Alternative labels: dict {lang: [labels]}. The ESCO API's actual field
    # is "alternativeLabel" — "altLabels" (checked here previously) doesn't
    # exist in the response, so this always came back empty.
    alt = item.get("alternativeLabel") or {}
    if isinstance(alt, dict):
        alt_en = [str(s) for s in (alt.get("en") or []) if s]
        alt_fr = [str(s) for s in (alt.get("fr") or []) if s]
    else:
        alt_en = []
        alt_fr = []

    # Concept type: the API reports this as "className" ("Skill" /
    # "Occupation" / "KnowledgeSkill" / …), not "conceptType" or "type" —
    # those two keys don't exist in the response ("type" is only the
    # request's own search-filter parameter), so this always fell through
    # to the "skill" default regardless of what was actually returned.
    raw_type = item.get("className") or "Skill"
    if "Occupation" in raw_type:
        ctype = "occupation"
    else:
        ctype = "skill"

    return EscoConcept(
        concept_uri=uri,
        preferred_label_en=preferred_en,
        preferred_label_fr=preferred_fr,
        alt_labels_en=alt_en,
        alt_labels_fr=alt_fr,
        concept_type=ctype,
    )


def _build_seed_concepts() -> list[EscoConcept]:
    """Return a minimal curated list of IT concepts for offline use.

    URIs that start with ``http://data.europa.eu/esco/skill/`` are real
    ESCO URIs.  URIs that start with ``local:`` are placeholders pending
    lookup against the live ESCO taxonomy.
    """
    raw: list[dict[str, Any]] = [
        # Programming languages
        {
            "uri": "local:skill/python",
            "en": "Python (programming language)",
            "fr": "Python (langage de programmation)",
            "alt_en": ["Python", "python programming", "python scripting"],
            "alt_fr": ["Python", "programmation Python"],
        },
        {
            "uri": "local:skill/javascript",
            "en": "JavaScript",
            "fr": "JavaScript",
            "alt_en": ["JS", "ECMAScript"],
            "alt_fr": ["JavaScript", "JS"],
        },
        {
            "uri": "local:skill/typescript",
            "en": "TypeScript",
            "fr": "TypeScript",
            "alt_en": ["TS"],
            "alt_fr": ["TypeScript"],
        },
        {
            "uri": "local:skill/java",
            "en": "Java (programming language)",
            "fr": "Java (langage de programmation)",
            "alt_en": ["Java"],
            "alt_fr": ["Java"],
        },
        {
            "uri": "local:skill/csharp",
            "en": "C# (programming language)",
            "fr": "C# (langage de programmation)",
            "alt_en": ["C#", "C sharp", "dotnet", ".NET"],
            "alt_fr": ["C#", "C sharp", ".NET"],
        },
        {
            "uri": "local:skill/go",
            "en": "Go (programming language)",
            "fr": "Go (langage de programmation)",
            "alt_en": ["Go", "Golang"],
            "alt_fr": ["Go", "Golang"],
        },
        {
            "uri": "local:skill/rust",
            "en": "Rust (programming language)",
            "fr": "Rust (langage de programmation)",
            "alt_en": ["Rust"],
            "alt_fr": ["Rust"],
        },
        {
            "uri": "local:skill/php",
            "en": "PHP (scripting language)",
            "fr": "PHP (langage de script)",
            "alt_en": ["PHP"],
            "alt_fr": ["PHP"],
        },
        {
            "uri": "local:skill/kotlin",
            "en": "Kotlin (programming language)",
            "fr": "Kotlin (langage de programmation)",
            "alt_en": ["Kotlin"],
            "alt_fr": ["Kotlin"],
        },
        {
            "uri": "local:skill/swift",
            "en": "Swift (programming language)",
            "fr": "Swift (langage de programmation)",
            "alt_en": ["Swift"],
            "alt_fr": ["Swift"],
        },
        {
            "uri": "local:skill/ruby",
            "en": "Ruby (programming language)",
            "fr": "Ruby (langage de programmation)",
            "alt_en": ["Ruby", "Ruby on Rails"],
            "alt_fr": ["Ruby", "Ruby on Rails"],
        },
        {
            "uri": "local:skill/cpp",
            "en": "C++ (programming language)",
            "fr": "C++ (langage de programmation)",
            "alt_en": ["C++", "cpp"],
            "alt_fr": ["C++"],
        },
        # Web frameworks
        {
            "uri": "local:skill/react",
            "en": "React (JavaScript library)",
            "fr": "React (bibliothèque JavaScript)",
            "alt_en": ["React", "ReactJS", "React.js"],
            "alt_fr": ["React", "ReactJS"],
        },
        {
            "uri": "local:skill/angular",
            "en": "Angular (web framework)",
            "fr": "Angular (framework web)",
            "alt_en": ["Angular", "AngularJS"],
            "alt_fr": ["Angular"],
        },
        {
            "uri": "local:skill/vue",
            "en": "Vue.js (JavaScript framework)",
            "fr": "Vue.js (framework JavaScript)",
            "alt_en": ["Vue", "Vue.js", "VueJS"],
            "alt_fr": ["Vue", "Vue.js"],
        },
        {
            "uri": "local:skill/nodejs",
            "en": "Node.js (JavaScript runtime)",
            "fr": "Node.js (environnement d'exécution JavaScript)",
            "alt_en": ["Node.js", "NodeJS", "Node"],
            "alt_fr": ["Node.js", "NodeJS"],
        },
        {
            "uri": "local:skill/django",
            "en": "Django (web framework)",
            "fr": "Django (framework web)",
            "alt_en": ["Django"],
            "alt_fr": ["Django"],
        },
        {
            "uri": "local:skill/fastapi",
            "en": "FastAPI (web framework)",
            "fr": "FastAPI (framework web)",
            "alt_en": ["FastAPI"],
            "alt_fr": ["FastAPI"],
        },
        {
            "uri": "local:skill/spring",
            "en": "Spring Framework",
            "fr": "Spring Framework",
            "alt_en": ["Spring", "Spring Boot"],
            "alt_fr": ["Spring", "Spring Boot"],
        },
        # Databases
        {
            "uri": "local:skill/sql",
            "en": "SQL (database query language)",
            "fr": "SQL (langage de requête de base de données)",
            "alt_en": ["SQL", "Structured Query Language"],
            "alt_fr": ["SQL"],
        },
        {
            "uri": "local:skill/postgresql",
            "en": "PostgreSQL (database management system)",
            "fr": "PostgreSQL (système de gestion de base de données)",
            "alt_en": ["PostgreSQL", "Postgres"],
            "alt_fr": ["PostgreSQL"],
        },
        {
            "uri": "local:skill/mysql",
            "en": "MySQL (database management system)",
            "fr": "MySQL (système de gestion de base de données)",
            "alt_en": ["MySQL"],
            "alt_fr": ["MySQL"],
        },
        {
            "uri": "local:skill/mongodb",
            "en": "MongoDB (document database)",
            "fr": "MongoDB (base de données de documents)",
            "alt_en": ["MongoDB", "Mongo"],
            "alt_fr": ["MongoDB"],
        },
        {
            "uri": "local:skill/redis",
            "en": "Redis (in-memory data store)",
            "fr": "Redis (magasin de données en mémoire)",
            "alt_en": ["Redis"],
            "alt_fr": ["Redis"],
        },
        {
            "uri": "local:skill/elasticsearch",
            "en": "Elasticsearch (search engine)",
            "fr": "Elasticsearch (moteur de recherche)",
            "alt_en": ["Elasticsearch", "ES"],
            "alt_fr": ["Elasticsearch"],
        },
        # DevOps / Infrastructure
        {
            "uri": "local:skill/docker",
            "en": "Docker (containerisation platform)",
            "fr": "Docker (plateforme de conteneurisation)",
            "alt_en": ["Docker", "containerisation"],
            "alt_fr": ["Docker", "conteneurisation"],
        },
        {
            "uri": "local:skill/kubernetes",
            "en": "Kubernetes (container orchestration)",
            "fr": "Kubernetes (orchestration de conteneurs)",
            "alt_en": ["Kubernetes", "K8s"],
            "alt_fr": ["Kubernetes", "K8s"],
        },
        {
            "uri": "local:skill/terraform",
            "en": "Terraform (infrastructure as code)",
            "fr": "Terraform (infrastructure en tant que code)",
            "alt_en": ["Terraform", "IaC"],
            "alt_fr": ["Terraform"],
        },
        {
            "uri": "local:skill/ansible",
            "en": "Ansible (automation platform)",
            "fr": "Ansible (plateforme d'automatisation)",
            "alt_en": ["Ansible"],
            "alt_fr": ["Ansible"],
        },
        {
            "uri": "local:skill/git",
            "en": "Git (version control system)",
            "fr": "Git (système de contrôle de version)",
            "alt_en": ["Git", "version control", "VCS"],
            "alt_fr": ["Git", "contrôle de version"],
        },
        {
            "uri": "local:skill/linux",
            "en": "Linux (operating system)",
            "fr": "Linux (système d'exploitation)",
            "alt_en": ["Linux", "Unix", "bash"],
            "alt_fr": ["Linux", "Unix"],
        },
        # Cloud
        {
            "uri": "local:skill/aws",
            "en": "Amazon Web Services (cloud platform)",
            "fr": "Amazon Web Services (plateforme cloud)",
            "alt_en": ["AWS", "Amazon Web Services"],
            "alt_fr": ["AWS", "Amazon Web Services"],
        },
        {
            "uri": "local:skill/azure",
            "en": "Microsoft Azure (cloud platform)",
            "fr": "Microsoft Azure (plateforme cloud)",
            "alt_en": ["Azure", "Microsoft Azure"],
            "alt_fr": ["Azure", "Microsoft Azure"],
        },
        {
            "uri": "local:skill/gcp",
            "en": "Google Cloud Platform",
            "fr": "Google Cloud Platform",
            "alt_en": ["GCP", "Google Cloud"],
            "alt_fr": ["GCP", "Google Cloud"],
        },
        # AI / ML
        {
            "uri": "local:skill/machine-learning",
            "en": "machine learning",
            "fr": "apprentissage automatique",
            "alt_en": ["ML", "machine learning", "statistical learning"],
            "alt_fr": ["ML", "apprentissage automatique", "apprentissage machine"],
        },
        {
            "uri": "local:skill/deep-learning",
            "en": "deep learning",
            "fr": "apprentissage profond",
            "alt_en": ["deep learning", "neural networks", "DL"],
            "alt_fr": ["apprentissage profond", "réseaux de neurones"],
        },
        {
            "uri": "local:skill/pytorch",
            "en": "PyTorch (machine learning framework)",
            "fr": "PyTorch (framework d'apprentissage automatique)",
            "alt_en": ["PyTorch"],
            "alt_fr": ["PyTorch"],
        },
        {
            "uri": "local:skill/tensorflow",
            "en": "TensorFlow (machine learning framework)",
            "fr": "TensorFlow (framework d'apprentissage automatique)",
            "alt_en": ["TensorFlow", "TF"],
            "alt_fr": ["TensorFlow"],
        },
        # Software development practices
        {
            "uri": "local:skill/agile",
            "en": "agile software development",
            "fr": "développement logiciel agile",
            "alt_en": ["Agile", "Agile methodology", "Scrum", "Kanban"],
            "alt_fr": ["Agile", "méthode agile", "Scrum"],
        },
        {
            "uri": "local:skill/cicd",
            "en": "continuous integration and continuous delivery",
            "fr": "intégration continue et livraison continue",
            "alt_en": ["CI/CD", "CI CD", "continuous integration", "continuous delivery"],
            "alt_fr": ["CI/CD", "intégration continue", "déploiement continu"],
        },
        {
            "uri": "local:skill/testing",
            "en": "software testing",
            "fr": "tests logiciels",
            "alt_en": ["software testing", "unit testing", "QA", "test automation"],
            "alt_fr": ["tests logiciels", "tests unitaires", "automatisation des tests"],
        },
        {
            "uri": "local:skill/microservices",
            "en": "microservices architecture",
            "fr": "architecture microservices",
            "alt_en": ["microservices", "microservice architecture"],
            "alt_fr": ["microservices", "architecture microservices"],
        },
        {
            "uri": "local:skill/rest-api",
            "en": "REST API design",
            "fr": "conception d'API REST",
            "alt_en": ["REST", "REST API", "RESTful", "web API"],
            "alt_fr": ["REST", "API REST", "RESTful"],
        },
        # Data engineering
        {
            "uri": "local:skill/apache-spark",
            "en": "Apache Spark (data processing framework)",
            "fr": "Apache Spark (framework de traitement de données)",
            "alt_en": ["Spark", "Apache Spark", "PySpark"],
            "alt_fr": ["Spark", "Apache Spark"],
        },
        {
            "uri": "local:skill/airflow",
            "en": "Apache Airflow (workflow orchestration)",
            "fr": "Apache Airflow (orchestration de flux de travail)",
            "alt_en": ["Airflow", "Apache Airflow"],
            "alt_fr": ["Airflow"],
        },
    ]

    concepts: list[EscoConcept] = []
    for r in raw:
        concepts.append(EscoConcept(
            concept_uri=r["uri"],
            preferred_label_en=r["en"],
            preferred_label_fr=r["fr"],
            alt_labels_en=r.get("alt_en", []),
            alt_labels_fr=r.get("alt_fr", []),
            concept_type="skill",
        ))
    return concepts
