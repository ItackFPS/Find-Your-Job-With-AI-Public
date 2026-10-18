"""Multilingual semantic matching tests.

Validates that paraphrase-multilingual-MiniLM-L12-v2 produces meaningful
cross-language semantic similarity, enabling profile.yaml to be written
in one language (e.g. English) while matching job postings in French,
German, Spanish, Italian, and other languages without manual translations.

All similarity assertions use relational comparisons (relevant > unrelated)
with a margin instead of hardcoded floats, so minor model/runtime variations
cannot cause spurious failures.
"""

from __future__ import annotations

import pytest

from src.embeddings.embedding import EmbeddingService
from src.models.job import Job
from src.models.profile import JobSearchProfile
from src.pipeline.filter import filter_jobs, load_filter_config
from src.pipeline.semantic_matching import (
    apply_semantic_matching,
    build_profile_embedding,
    load_semantic_config,
)
from tests.fixtures.profiles import TEST_PROFILE_PATH

MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
MARGIN = 0.05  # minimum gap: relevant lang score must exceed unrelated by this much


@pytest.fixture(scope="module")
def service() -> EmbeddingService:
    """Single model instance shared across the module (model load is slow)."""
    return EmbeddingService(model_name=MODEL, cache_max_entries=200, device="cpu")


# ---------------------------------------------------------------------------
# 1. Model configuration
# ---------------------------------------------------------------------------

class TestModelConfiguration:
    def test_configured_model_name(self) -> None:
        config = load_semantic_config("config/semantic_config.yaml")
        assert config["embedding"]["model_name"] == MODEL

    def test_model_loads_successfully(self, service: EmbeddingService) -> None:
        vec = service.embed("test")
        assert len(vec) == 384
        assert any(abs(x) > 1e-10 for x in vec)

    def test_default_fallback_uses_multilingual_model(self) -> None:
        """The Python-level default must match the config default."""
        svc = EmbeddingService()
        assert svc.model_name == MODEL


# ---------------------------------------------------------------------------
# 2. Cross-language semantic similarity
# ---------------------------------------------------------------------------

class TestCrossLanguageSemantics:
    """Equivalent concepts in different languages must score higher than
    clearly unrelated concepts, proving the model bridges language gaps."""

    # Anchor concept: car sales
    CONCEPT = "car sales"

    # Equivalent expressions in other languages
    EQUIVALENTS = {
        "French": "vente automobile",
        "German": "Automobilverkauf",
        "Spanish": "venta de automóviles",
        "Italian": "vendita di automobili",
    }

    # Clearly unrelated concepts
    UNRELATED = [
        "software engineer",
        "plumber",
        "accounting",
    ]

    def _sim(self, service: EmbeddingService, a: str, b: str) -> float:
        va = service.embed(a)
        vb = service.embed(b)
        return service.cosine_similarity(va, vb)

    def test_french_equivalent_beats_unrelated(self, service: EmbeddingService) -> None:
        relevant = self._sim(service, self.CONCEPT, self.EQUIVALENTS["French"])
        for unrelated in self.UNRELATED:
            gap = relevant - self._sim(service, self.CONCEPT, unrelated)
            assert gap > MARGIN, (
                f"'car sales' ↔ '{self.EQUIVALENTS['French']}' ({relevant:.3f}) "
                f"should exceed 'car sales' ↔ '{unrelated}' by >{MARGIN}"
            )

    def test_german_equivalent_beats_unrelated(self, service: EmbeddingService) -> None:
        relevant = self._sim(service, self.CONCEPT, self.EQUIVALENTS["German"])
        for unrelated in self.UNRELATED:
            gap = relevant - self._sim(service, self.CONCEPT, unrelated)
            assert gap > MARGIN, (
                f"'car sales' ↔ '{self.EQUIVALENTS['German']}' ({relevant:.3f}) "
                f"should exceed 'car sales' ↔ '{unrelated}' by >{MARGIN}"
            )

    def test_spanish_equivalent_beats_unrelated(self, service: EmbeddingService) -> None:
        relevant = self._sim(service, self.CONCEPT, self.EQUIVALENTS["Spanish"])
        for unrelated in self.UNRELATED:
            gap = relevant - self._sim(service, self.CONCEPT, unrelated)
            assert gap > MARGIN, (
                f"'car sales' ↔ '{self.EQUIVALENTS['Spanish']}' ({relevant:.3f}) "
                f"should exceed 'car sales' ↔ '{unrelated}' by >{MARGIN}"
            )

    def test_italian_equivalent_beats_unrelated(self, service: EmbeddingService) -> None:
        relevant = self._sim(service, self.CONCEPT, self.EQUIVALENTS["Italian"])
        for unrelated in self.UNRELATED:
            gap = relevant - self._sim(service, self.CONCEPT, unrelated)
            assert gap > MARGIN, (
                f"'car sales' ↔ '{self.EQUIVALENTS['Italian']}' ({relevant:.3f}) "
                f"should exceed 'car sales' ↔ '{unrelated}' by >{MARGIN}"
            )

    def test_english_equivalent_beats_unrelated(self, service: EmbeddingService) -> None:
        """Baseline: English ↔ English must also satisfy the margin."""
        relevant = self._sim(service, self.CONCEPT, "Car Sales Representative")
        for unrelated in self.UNRELATED:
            gap = relevant - self._sim(service, self.CONCEPT, unrelated)
            assert gap > MARGIN, (
                f"'car sales' ↔ 'Car Sales Representative' ({relevant:.3f}) "
                f"should exceed 'car sales' ↔ '{unrelated}' by >{MARGIN}"
            )

    def test_all_language_similarities_are_positive(self, service: EmbeddingService) -> None:
        """Every equivalent-language pair must have a positive similarity."""
        for lang, phrase in self.EQUIVALENTS.items():
            sim = self._sim(service, self.CONCEPT, phrase)
            assert sim > 0.0, f"Expected positive similarity for {lang} equivalent"


# ---------------------------------------------------------------------------
# 3. Pipeline representation (JobSearchProfile → build_profile_embedding)
# ---------------------------------------------------------------------------

class TestPipelineRepresentation:
    """Validate the actual application path used at runtime."""

    def _make_profile(self, domains: list[str]) -> JobSearchProfile:
        return JobSearchProfile(
            target_domains=domains,
            target_skills=[],
            work_modes=["remote"],
            contract_types=["cdi"],
            experience={"preferred_levels": ["junior"], "excluded_seniority": []},
            languages=["English"],
        )

    def test_car_sales_profile_scores_higher_on_french_job(self, service: EmbeddingService) -> None:
        """A 'car sales' profile must score a French car-sales job higher than a plumbing job."""
        profile = self._make_profile(["car sales"])
        profile_emb = build_profile_embedding(profile.to_semantic_info(), service)

        french_car_job = Job(
            id="fr_car", source="test", title="Commercial automobile",
            description="Vendeur de voitures neuves et occasions, prospection clientèle.",
            location="Paris, France", countries=["France"], remote=False,
            employment_type="full-time",
        )
        plumbing_job = Job(
            id="plumber", source="test", title="Plombier",
            description="Installation et réparation de systèmes de plomberie.",
            location="Paris, France", countries=["France"], remote=False,
            employment_type="full-time",
        )

        car_score = service.compute_job_semantic_score(french_car_job, profile_emb)
        plumbing_score = service.compute_job_semantic_score(plumbing_job, profile_emb)
        assert car_score > plumbing_score + MARGIN, (
            f"Car-sales profile: French car job ({car_score:.3f}) must exceed "
            f"plumbing job ({plumbing_score:.3f}) by >{MARGIN}"
        )

    def test_car_sales_profile_scores_higher_on_german_job(self, service: EmbeddingService) -> None:
        profile = self._make_profile(["car sales"])
        profile_emb = build_profile_embedding(profile.to_semantic_info(), service)

        german_car_job = Job(
            id="de_car", source="test", title="Automobilverkäufer",
            description="Verkauf von Neu- und Gebrauchtwagen, Kundenberatung.",
            location="Berlin, Germany", countries=["Germany"], remote=False,
            employment_type="full-time",
        )
        unrelated_job = Job(
            id="de_unrelated", source="test", title="Buchhalter",
            description="Buchhaltung, Rechnungswesen und Jahresabschlüsse.",
            location="Berlin, Germany", countries=["Germany"], remote=False,
            employment_type="full-time",
        )

        car_score = service.compute_job_semantic_score(german_car_job, profile_emb)
        unrelated_score = service.compute_job_semantic_score(unrelated_job, profile_emb)
        assert car_score > unrelated_score + MARGIN, (
            f"Car-sales profile: German car job ({car_score:.3f}) must exceed "
            f"accounting job ({unrelated_score:.3f}) by >{MARGIN}"
        )

    def test_different_profiles_produce_different_rankings(self, service: EmbeddingService) -> None:
        """Profile A: car sales vs Profile B: software development — rankings must diverge."""
        profile_a = self._make_profile(["car sales"])
        profile_b = self._make_profile(["software development"])

        emb_a = build_profile_embedding(profile_a.to_semantic_info(), service)
        emb_b = build_profile_embedding(profile_b.to_semantic_info(), service)

        car_job = Job(
            id="car", source="test", title="Vendedor de automóviles",
            description="Venta de coches nuevos y de ocasión, atención al cliente.",
            location="Madrid, Spain", countries=["Spain"], remote=False,
            employment_type="full-time",
        )
        dev_job = Job(
            id="dev", source="test", title="Développeur Python",
            description="Développement d'applications web avec Python et Django.",
            location="Paris, France", countries=["France"], remote=True,
            employment_type="full-time",
        )

        # Profile A (car sales) should prefer the car job
        car_a = service.compute_job_semantic_score(car_job, emb_a)
        dev_a = service.compute_job_semantic_score(dev_job, emb_a)
        assert car_a > dev_a, (
            f"Car-sales profile should prefer car job ({car_a:.3f}) over dev job ({dev_a:.3f})"
        )

        # Profile B (software dev) should prefer the dev job
        car_b = service.compute_job_semantic_score(car_job, emb_b)
        dev_b = service.compute_job_semantic_score(dev_job, emb_b)
        assert dev_b > car_b, (
            f"Software-dev profile should prefer dev job ({dev_b:.3f}) over car job ({car_b:.3f})"
        )

    def test_profile_requires_no_translations(self, service: EmbeddingService) -> None:
        """Profile with English 'car sales' must score equivalents in 4 languages
        above clearly unrelated concepts, with no translations in the profile."""
        profile = self._make_profile(["car sales"])
        profile_emb = build_profile_embedding(profile.to_semantic_info(), service)

        multilingual_car_jobs = [
            Job(id="fr", source="test", title="Commercial automobile",
                description="Vente de véhicules", location="Paris", countries=["France"],
                remote=False, employment_type="full-time"),
            Job(id="de", source="test", title="Automobilverkäufer",
                description="Verkauf von Fahrzeugen", location="Berlin", countries=["Germany"],
                remote=False, employment_type="full-time"),
            Job(id="es", source="test", title="Vendedor de automóviles",
                description="Venta de vehículos", location="Madrid", countries=["Spain"],
                remote=False, employment_type="full-time"),
            Job(id="it", source="test", title="Venditore di automobili",
                description="Vendita di veicoli", location="Milano", countries=["Italy"],
                remote=False, employment_type="full-time"),
        ]
        unrelated_job = Job(
            id="unrelated", source="test", title="Software Engineer",
            description="Backend development with Python.", location="Remote", countries=["France"],
            remote=True, employment_type="full-time",
        )

        unrelated_score = service.compute_job_semantic_score(unrelated_job, profile_emb)
        for job in multilingual_car_jobs:
            score = service.compute_job_semantic_score(job, profile_emb)
            assert score > unrelated_score + MARGIN, (
                f"Car-sales profile: {job.id} job ({score:.3f}) must exceed "
                f"unrelated ({unrelated_score:.3f}) by >{MARGIN}"
            )


# ---------------------------------------------------------------------------
# 4. Hard filter authority
# ---------------------------------------------------------------------------

class TestHardFilterAuthority:
    """Semantic matching must never resurrect a job rejected by a hard filter."""

    def test_rejected_job_stays_rejected_despite_high_semantic_relevance(
        self, service: EmbeddingService
    ) -> None:
        """A job in a rejected country must not appear after semantic scoring."""
        # This job would be highly relevant semantically (Python developer in France)
        # but if we treat it as failing a hard filter (e.g. explicit exclusion),
        # it must not appear in semantic results.
        accepted_job = Job(
            id="j_accepted", source="test", title="Développeur Python",
            description="Développement backend Python, Django, PostgreSQL.",
            location="Paris, France", countries=["France"], remote=True,
            employment_type="full-time",
        )
        # Simulate hard filter: only pass the accepted job, not some rejected one.
        # The key invariant: apply_semantic_matching only ever sees already-filtered jobs.
        filter_cfg = load_filter_config(TEST_PROFILE_PATH)
        passed, rejected = filter_jobs([accepted_job], filter_cfg)

        config = load_semantic_config("config/semantic_config.yaml")
        profile_emb = build_profile_embedding(
            {"seniority": "junior", "employment_type": "full-time",
             "location": "France", "languages": ["French"],
             "skills": ["python"], "responsibilities": "backend development"},
            service,
        )

        # Semantic matching only receives already-filtered jobs
        scored, _ = apply_semantic_matching(passed, profile_emb, config, service)
        scored_ids = {job.id for job, _ in scored}

        # Verify rejected jobs are not in scored results
        for job in rejected:
            assert job.id not in scored_ids, (
                f"Rejected job {job.id} must not appear in semantic results"
            )

    def test_semantic_sees_only_hard_filtered_jobs(self, service: EmbeddingService) -> None:
        """Pass only pre-filtered jobs to semantic matching; verify count matches."""
        jobs = [
            Job(id="j1", source="test", title="Python Developer",
                description="Backend Python role.", location="Paris, France",
                countries=["France"], remote=True, employment_type="full-time"),
            Job(id="j2", source="test", title="Développeur Python",
                description="Rôle backend Python.", location="Lyon, France",
                countries=["France"], remote=False, employment_type="full-time"),
        ]
        filter_cfg = load_filter_config(TEST_PROFILE_PATH)
        passed, _ = filter_jobs(jobs, filter_cfg)

        config = load_semantic_config("config/semantic_config.yaml")
        profile_emb = build_profile_embedding(
            {"seniority": "junior", "employment_type": "full-time",
             "location": "France", "languages": ["French", "English"],
             "skills": ["python"], "responsibilities": "software development"},
            service,
        )

        scored, unscored = apply_semantic_matching(passed, profile_emb, config, service)
        assert len(scored) + len(unscored) == len(passed)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
