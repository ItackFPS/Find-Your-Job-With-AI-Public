"""Build the G-MO-T fixture datasets (ADR 0028): six occupations, one shared synthetic corpus.

Each dataset is the same corpus seen from one profile: that occupation's offers are labelled
``accept``, every other occupation's ``reject``. The corpus is deliberately small and built to grow
towards ADR 0023's 600 offers. Output is deterministic; run it only to change the corpus, then
re-record the baselines:

    python scripts/build_g_mo_t_datasets.py
    python scripts/build_g_mo_t_datasets.py --record
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.evaluation.dataset import load_dataset, write_dataset
from src.evaluation.models import LabelRecord, StrategyRef
from src.evaluation.runner import run_evaluation
from src.evaluation.store import golden_to_json, to_golden

ROOT = Path(__file__).resolve().parent.parent
DATASETS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets"
GOLDEN = ROOT / "tests" / "fixtures" / "evaluation" / "golden"
AS_OF = "2026-11-08T00:00:00+00:00"


@dataclass(frozen=True)
class Occupation:
    slug: str
    domains: tuple[str, ...]
    skills: tuple[str, ...]
    titles: tuple[str, ...]
    missions: str
    diploma: str


OCCUPATIONS = (
    Occupation(
        "nurse", ("healthcare",), ("soins infirmiers", "pharmacologie", "dossier patient"),
        ("Infirmier / Infirmière H/F", "Infirmier diplômé d'État en EHPAD", "IDE de nuit - service de cardiologie",
         "Infirmière en soins généraux", "Infirmier de bloc opératoire (IBODE)", "Registered Nurse",
         "Infirmier scolaire", "Infirmier en santé au travail", "Infirmière coordinatrice à domicile",
         "Infirmier aux urgences", "IDE en clinique psychiatrique", "Staff Nurse - Oncology"),
        "Vous assurez les soins infirmiers, la préparation et l'administration des traitements, la surveillance "
        "clinique des patients et la tenue du dossier de soins, en lien avec les médecins et les aides-soignants.",
        "Diplôme d'État d'infirmier exigé, inscription à l'Ordre.",
    ),
    Occupation(
        "accountant", ("accounting",), ("comptabilité", "fiscalité", "Excel", "Sage"),
        ("Comptable H/F", "Comptable général", "Assistant comptable", "Comptable fournisseurs",
         "Accountant", "Collaborateur comptable en cabinet", "Comptable clients", "Comptable unique PME",
         "Chargé de comptabilité", "Gestionnaire comptable", "Comptable analytique", "Staff Accountant"),
        "Vous tenez la comptabilité générale, saisissez les écritures, réalisez les rapprochements bancaires, "
        "préparez les déclarations de TVA et participez aux clôtures mensuelles et au bilan.",
        "BTS CG ou DCG, maîtrise d'Excel et d'un logiciel comptable.",
    ),
    Occupation(
        "warehouse", ("manual_labor",), ("manutention", "préparation de commandes", "CACES 1"),
        ("Magasinier H/F", "Préparateur de commandes", "Agent logistique en entrepôt", "Cariste CACES 1 et 3",
         "Warehouse Operative", "Manutentionnaire", "Magasinier cariste", "Agent de quai",
         "Opérateur logistique", "Préparatrice de commandes", "Réceptionnaire entrepôt", "Picker Packer"),
        "Vous réceptionnez les marchandises, préparez les commandes, conditionnez les colis, rangez les stocks "
        "et chargez les camions, dans le respect des consignes de sécurité de l'entrepôt.",
        "CACES apprécié, port de charges, travail en équipe.",
    ),
    Occupation(
        "sales", ("sales",), ("prospection", "négociation", "CRM"),
        ("Commercial terrain H/F", "Technico-commercial", "Sales Representative", "Attaché commercial B2B",
         "Commerciale sédentaire", "Business Developer", "Chargé d'affaires", "Conseiller commercial",
         "Account Executive", "Responsable de secteur", "Vendeur en magasin", "Commercial grands comptes"),
        "Vous prospectez de nouveaux clients, présentez les offres, négociez les contrats, suivez votre "
        "portefeuille dans le CRM et atteignez vos objectifs de vente sur votre secteur.",
        "Bac+2 commercial, permis B, sens de la négociation.",
    ),
    Occupation(
        "sysadmin", ("systems",), ("Linux", "Windows Server", "Active Directory", "VMware"),
        ("Administrateur systèmes H/F", "Ingénieur systèmes Linux", "System Administrator", "Admin sys Windows Server",
         "Administrateur systèmes et réseaux", "Technicien systèmes", "Administratrice infrastructure",
         "Ingénieur d'exploitation", "Administrateur VMware", "Sysadmin Linux", "Technicien d'exploitation informatique",
         "Administrateur Active Directory"),
        "Vous administrez les serveurs Linux et Windows Server, gérez Active Directory et la virtualisation VMware, "
        "assurez les sauvegardes, la supervision et le maintien en condition opérationnelle de l'infrastructure.",
        "Bac+2 à Bac+3 en informatique, administration de systèmes.",
    ),
    Occupation(
        "chef", ("hospitality",), ("cuisine", "HACCP", "gestion des stocks"),
        ("Chef de partie H/F", "Cuisinier", "Chef de cuisine", "Second de cuisine", "Commis de cuisine",
         "Sous-chef", "Cuisinière en restauration collective", "Chef Cook", "Line Cook",
         "Pâtissier en restaurant", "Cuisinier traiteur", "Head Chef"),
        "Vous élaborez les plats, assurez la mise en place et le service, gérez les commandes et les stocks, "
        "et veillez au respect des règles d'hygiène HACCP en cuisine.",
        "CAP ou BEP cuisine, expérience en restauration appréciée.",
    ),
)


def _jobs() -> list[dict[str, object]]:
    jobs: list[dict[str, object]] = []
    for occ in OCCUPATIONS:
        for i, title in enumerate(occ.titles, start=1):
            jobs.append({
                "id": f"{occ.slug}-{i:02d}",
                "title": title,
                "company": f"{occ.slug.capitalize()} Employeur {i}",
                "description": (
                    f"Nous recrutons pour notre site de Lyon : {title}. {occ.missions}\n\n"
                    f"Profil recherché : {occ.diploma} Poste en CDI à temps plein, sur site."
                ),
                "countries": ["France"],
                "location": "Lyon, France",
                "remote": False,
                "work_mode": "on-site",
                "employment_type": "CDI",
                "skills": list(occ.skills),
                "published_at": "2026-11-01T00:00:00+00:00",
            })
    return jobs


def _labels(me: Occupation) -> list[LabelRecord]:
    return [
        LabelRecord(
            job_id=f"{occ.slug}-{i:02d}",
            expected="accept" if occ.slug == me.slug else "reject",
            category="clear_target" if occ.slug == me.slug else "other_occupation",
            reason=f"{occ.slug} offer seen from a {me.slug} profile",
            label_source="human",
            labeler="g-mo-t-fixture",
            labeled_at=AS_OF,
        )
        for occ in OCCUPATIONS for i, _ in enumerate(occ.titles, start=1)
    ]


def _profile(occ: Occupation) -> bytes:
    profile = {
        "target_domains": list(occ.domains),
        "focus_domain": True,
        "target_skills": list(occ.skills),
        "geography": {
            "preferred_countries": ["France"],
            "allowed_regions": ["Europe"],
            "home": {"name": "Lyon", "insee": "69123", "lat": 45.764, "lon": 4.8357,
                     "department": "69", "precision": "commune"},
            "max_distance_km": 50,
            "strict_distance": False,
            "remote_policy": {"remote_france": True, "remote_eu": False, "remote_worldwide": False, "hybrid": True},
        },
        "work_modes": ["remote", "on-site", "hybrid"],
        "contract_types": ["cdi", "cdd", "full-time", "part-time"],
        "experience": {
            "preferred_levels": ["entry_level", "junior", "mid", "senior"],
            "max_years": None,
            "excluded_seniority": ["director", "vp", "executive", "c_suite"],
        },
        "languages": ["French", "English"],
        "freshness_days": 30,
        "max_education": "bac+5",
        "matching": {
            "education_hard_constraint": True,
            "education_preferred_penalty": 0.2,
            "principal_skill_gate": True,
            "mandatory_central_skill_gate": True,
            "llm_extraction_enabled": False,
            "llm_eligibility_enabled": False,
            "llm_eligibility_reject_confidence": 0.75,
        },
    }
    header = f"# G-MO-T fixture profile: {occ.slug} (scripts/build_g_mo_t_datasets.py). Not a real person.\n"
    return (header + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)).encode("utf-8")


def dataset_dir(occ: Occupation) -> Path:
    return DATASETS / f"g-mo-t-{occ.slug}"


def golden_file(occ: Occupation) -> Path:
    return GOLDEN / f"g-mo-t-{occ.slug}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true", help="re-record the baselines from the current code")
    args = parser.parse_args()
    if args.record:
        for occ in OCCUPATIONS:
            result = run_evaluation(load_dataset(dataset_dir(occ)), StrategyRef())
            golden_file(occ).write_text(golden_to_json(to_golden(result)), encoding="utf-8", newline="\n")
        return 0
    jobs = _jobs()
    for occ in OCCUPATIONS:
        write_dataset(
            dataset_dir(occ), dataset_id=f"g-mo-t-{occ.slug}", version="1", kind="synthetic",
            created_at=AS_OF, as_of=AS_OF, jobs=jobs, labels=_labels(occ), profile_yaml=_profile(occ),
            notes="G-MO-T temporary gate (ADR 0028): synthetic, not the ADR 0023 criterion.",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
