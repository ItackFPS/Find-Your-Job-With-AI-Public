"""Synthetic job postings for the LLM semantic eligibility gate benchmark.

Distinct from ``tests/fixtures/ai_filter_benchmark`` (which benchmarks the
*requirements-gate* LLM extraction path). These jobs specifically target the
eligibility gate's core job: reasoning about *implicit* education/seniority
signals — a title that conventionally implies a level the posting never
states explicitly — while correctly using posting context (internship/
alternance/junior framing, explicit lower-level acceptance, or a description
that targets junior candidates despite a senior-sounding title) to avoid
false rejections.

STRICT NO-LEAKAGE RULE (same discipline as ai_filter_benchmark/jobs.py):
this module contains ONLY posting data. Ground truth lives exclusively in
``ground_truth.py`` and is joined only at evaluation time.

Evaluated against the profile in ``config/profile.yaml``:
max_education: bac+2, experience.max_years: 3, preferred_levels include
internship/alternance/junior/entry_level/graduate.
"""

from __future__ import annotations

JOBS: list[dict] = [
    {
        "id": "eg01",
        "title": "Ingénieur logiciel",
        "company": "Meridian Systems",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "Meridian Systems recherche un(e) ingénieur logiciel pour concevoir et développer "
            "des applications distribuées au sein de notre équipe R&D. Vous interviendrez sur "
            "l'ensemble du cycle de vie logiciel : architecture, développement, tests, déploiement. "
            "Vous collaborerez étroitement avec les équipes produit et infrastructure.\n\n"
            "Compétences attendues : solide expérience en conception logicielle, maîtrise d'un "
            "langage orienté objet, bonnes pratiques de développement (tests, CI/CD, code review)."
        ),
    },
    {
        "id": "eg02",
        "title": "Ingénieur logiciel en alternance",
        "company": "Meridian Systems",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "alternance",
        "description": (
            "Dans le cadre d'une alternance (BTS, DUT ou Bachelor), rejoignez notre équipe R&D "
            "pour contribuer au développement de nos applications distribuées. Vous serez "
            "accompagné(e) par un tuteur tout au long de votre alternance.\n\n"
            "Profil recherché : étudiant(e) en alternance, curieux(se), à l'aise avec au moins "
            "un langage de programmation. Aucune expérience professionnelle requise."
        ),
    },
    {
        "id": "eg03",
        "title": "Ingénieur logiciel — BTS/Bac+2 accepté",
        "company": "Meridian Systems",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "Poste ouvert aux profils Bac+2 (BTS SIO, DUT Informatique) avec une première "
            "expérience en développement. Vous participerez au développement d'applications "
            "internes sous la supervision d'un développeur senior.\n\n"
            "Formation : Bac+2 accepté, débutant accepté si stage/alternance significatif."
        ),
    },
    {
        "id": "eg04",
        "title": "Software Engineer Intern",
        "company": "Northline Cloud",
        "location": "Remote",
        "countries": [],
        "remote": True,
        "work_mode": "remote",
        "employment_type": "internship",
        "description": (
            "6-month internship on our platform team, building internal tooling in Python and "
            "TypeScript. You'll work alongside senior engineers who will mentor you throughout "
            "the internship.\n\n"
            "Requirements: currently enrolled in a degree program, some programming experience "
            "(coursework or personal projects), eagerness to learn."
        ),
    },
    {
        "id": "eg05",
        "title": "Senior Software Engineer",
        "company": "Northline Cloud",
        "location": "Remote",
        "countries": [],
        "remote": True,
        "work_mode": "remote",
        "employment_type": "full-time",
        "description": (
            "We're looking for a Senior Software Engineer to lead the design of our next-generation "
            "platform services. You'll set technical direction, mentor other engineers, and drive "
            "architecture decisions across the team.\n\n"
            "You bring deep expertise in distributed systems and a track record of shipping "
            "production services at scale."
        ),
    },
    {
        "id": "eg06",
        "title": "Développeur junior",
        "company": "Atelier Web Lyon",
        "location": "Lyon, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "Première expérience acceptée. Vous rejoindrez une petite équipe pour développer des "
            "sites et applications web pour nos clients PME.\n\n"
            "Profil : Bac+2 en informatique, sortie d'école ou 1ère expérience. Vous serez "
            "accompagné(e) par un développeur confirmé."
        ),
    },
    {
        "id": "eg07",
        "title": "Senior Backend Engineer",
        "company": "Fintrack Labs",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": True,
        "work_mode": "remote",
        "employment_type": "internship",
        "description": (
            "Note: this listing uses our standard internal title template — this specific opening "
            "is our summer internship program for the backend team. You'll pair with engineers on "
            "real production tickets, no prior professional experience required.\n\n"
            "Ouvert aux étudiants (Bac+2/Bac+3), stage de 4 à 6 mois, gratification légale."
        ),
    },
    {
        "id": "eg08",
        "title": "Architecte logiciel",
        "company": "Vantage Systems",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "Vous définirez l'architecture technique de nos plateformes et accompagnerez les "
            "équipes de développement dans son application. Vous serez le garant de la cohérence "
            "technique et des choix structurants du système d'information.\n\n"
            "Vous justifiez d'une expertise reconnue en architecture logicielle."
        ),
    },
    {
        "id": "eg09",
        "title": "Technicien informatique",
        "company": "ServiceDesk Plus",
        "location": "Marseille, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "Support utilisateurs de niveau 1/2, gestion du parc informatique, installation et "
            "maintenance des postes de travail.\n\n"
            "Profil : Bac+2 informatique ou équivalent, débutant accepté."
        ),
    },
    {
        "id": "eg10",
        "title": "Lead Developer",
        "company": "Fintrack Labs",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": True,
        "work_mode": "remote",
        "employment_type": "full-time",
        "description": (
            "As Lead Developer, you will own the technical roadmap for our payments team, "
            "coordinate a team of 4 engineers, and represent engineering in cross-functional "
            "planning.\n\n"
            "You have significant prior experience leading engineering teams."
        ),
    },
    {
        "id": "eg11",
        "title": "Alternance BTS CIEL",
        "company": "IndustrioTech",
        "location": "Toulouse, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "alternance",
        "description": (
            "Dans le cadre de votre BTS CIEL (Cybersécurité, Informatique et réseaux, Électronique), "
            "rejoignez notre service informatique pour une alternance de 2 ans.\n\n"
            "Aucune expérience professionnelle requise, formation assurée en interne."
        ),
    },
    {
        "id": "eg12",
        "title": "Staff Engineer",
        "company": "Vantage Systems",
        "location": "Remote",
        "countries": [],
        "remote": True,
        "work_mode": "remote",
        "employment_type": "full-time",
        "description": (
            "As a Staff Engineer, you'll operate across multiple teams to solve our hardest "
            "technical problems and set engineering standards company-wide.\n\n"
            "This is a senior individual-contributor role for engineers with an extensive track "
            "record of technical leadership."
        ),
    },
]
