"""I17 — EXPERIMENTAL shadow domain classifier tests.

Covers: shadow classification behavior, safe failure on malformed/missing
input, the production<->shadow label comparison, the offline evaluation
harness's precision/recall/disagreement math, and — critically — a
structural proof that production code never imports this package, so shadow
mode cannot possibly affect eligibility, score, tier, ranking,
personalization, or notifications.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.experimental.domain_classifier import (
    ExperimentalDomainClassifier,
    build_domain_classifier_from_config,
    production_label_from_score,
)
from src.experimental.domain_shadow_eval import evaluate_shadow_classifier

REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeEmbeddingService:
    """Deterministic bag-of-words embedding: cosine similarity reflects
    word overlap with a fixed, test-defined vocabulary (no reliance on
    Python's randomized str hash, so results are stable across processes).
    """

    def __init__(self, vocab: list[str]) -> None:
        self._vocab = {w: i for i, w in enumerate(dict.fromkeys(vocab))}

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * len(self._vocab)
        for word in text.lower().replace(",", " ").split():
            idx = self._vocab.get(word)
            if idx is not None:
                vec[idx] += 1.0
        return vec

    def cosine_similarity(self, a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = sum(x * x for x in a) ** 0.5
        mag_b = sum(x * x for x in b) ** 0.5
        if mag_a == 0.0 or mag_b == 0.0:
            return 0.0
        return dot / (mag_a * mag_b)


class _RaisingEmbeddingService:
    def embed(self, text: str) -> list[float]:
        raise RuntimeError("model unavailable")

    def cosine_similarity(self, a: list[float], b: list[float]) -> float:
        return 0.0


_VOCAB = [
    "python",
    "developer",
    "software",
    "engineer",
    "backend",
    "sales",
    "hospitality",
    "customer",
    "representative",
    "manager",
    "team",
]


class TestClassify:
    def test_clear_on_domain_job(self) -> None:
        clf = ExperimentalDomainClassifier(
            _FakeEmbeddingService(_VOCAB),
            domain_keywords=["python developer", "software engineer"],
            off_domain_keywords=["sales representative", "hospitality customer"],
        )
        result = clf.classify("Python Developer", "Backend software engineer role")
        assert result.label == "on_domain"
        assert result.error is None
        assert result.confidence > 0.0

    def test_clear_off_domain_job(self) -> None:
        clf = ExperimentalDomainClassifier(
            _FakeEmbeddingService(_VOCAB),
            domain_keywords=["python developer", "software engineer"],
            off_domain_keywords=["sales representative", "hospitality customer"],
        )
        result = clf.classify("Sales Representative", "Customer hospitality role")
        assert result.label == "off_domain"
        assert result.error is None

    def test_no_signal_either_way_is_uncertain(self) -> None:
        clf = ExperimentalDomainClassifier(
            _FakeEmbeddingService(_VOCAB),
            domain_keywords=["python developer"],
            off_domain_keywords=["sales representative"],
        )
        result = clf.classify("Manager", "Team manager role")
        assert result.label == "uncertain"

    def test_empty_text_fails_safely(self) -> None:
        clf = ExperimentalDomainClassifier(
            _FakeEmbeddingService(_VOCAB), domain_keywords=["python"], off_domain_keywords=["sales"]
        )
        result = clf.classify(None, None)
        assert result.label == "uncertain"
        assert result.error == "empty_text"
        assert result.confidence == 0.0

    def test_no_anchor_vocabulary_fails_safely(self) -> None:
        clf = ExperimentalDomainClassifier(
            _FakeEmbeddingService(_VOCAB), domain_keywords=[], off_domain_keywords=[]
        )
        result = clf.classify("Python Developer", "some description")
        assert result.label == "uncertain"
        assert result.error == "no_anchor_vocabulary"

    def test_broken_embedding_model_fails_safely_never_raises(self) -> None:
        clf = ExperimentalDomainClassifier(
            _RaisingEmbeddingService(), domain_keywords=["python"], off_domain_keywords=["sales"]
        )
        result = clf.classify("Python Developer", "some description")
        assert result.label == "uncertain"
        assert result.error == "model unavailable"

    def test_duplicate_keywords_are_deduplicated_without_error(self) -> None:
        clf = ExperimentalDomainClassifier(
            _FakeEmbeddingService(_VOCAB),
            domain_keywords=["python developer", "python developer", "python developer"],
            off_domain_keywords=["sales representative"],
        )
        result = clf.classify("Python Developer", "Backend software engineer")
        assert result.label == "on_domain"

    def test_anchor_vectors_computed_once_not_per_call(self) -> None:
        embedding_service = _FakeEmbeddingService(_VOCAB)
        calls = {"n": 0}
        original_embed = embedding_service.embed

        def counting_embed(text: str) -> list[float]:
            calls["n"] += 1
            return original_embed(text)

        embedding_service.embed = counting_embed  # type: ignore[method-assign]
        clf = ExperimentalDomainClassifier(
            embedding_service,
            domain_keywords=["python developer", "software engineer"],
            off_domain_keywords=["sales representative"],
        )
        clf.classify("Python Developer", "Backend software engineer")
        calls_after_first = calls["n"]
        clf.classify("Python Developer", "Backend software engineer")
        # Second call embeds only the job text again (1 more call), not the
        # 3 anchor keywords a second time.
        assert calls["n"] == calls_after_first + 1


class TestProductionLabelFromScore:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (0.05, "off_domain"),
            (0.0, "off_domain"),
            (1.0, "on_domain"),
            (0.7, "on_domain"),
            (0.35, "uncertain"),
            (0.45, "uncertain"),
        ],
    )
    def test_mapping(self, score: float, expected: str) -> None:
        assert production_label_from_score(score) == expected


class TestBuildFromConfig:
    def test_extracts_keywords_from_injected_scoring_config(self) -> None:
        scoring_config = {
            "scoring": {
                "domain_relevance": {
                    "domain_title_keywords": ["python developer"],
                    "domain_description_keywords": ["software engineer"],
                    "off_domain_keywords": ["sales representative"],
                }
            }
        }
        clf = build_domain_classifier_from_config(_FakeEmbeddingService(_VOCAB), scoring_config)
        result = clf.classify("Python Developer", "Backend software engineer")
        assert result.label == "on_domain"

    def test_missing_config_sections_fail_safely(self) -> None:
        clf = build_domain_classifier_from_config(_FakeEmbeddingService(_VOCAB), {})
        result = clf.classify("Python Developer", "Backend")
        assert result.label == "uncertain"
        assert result.error == "no_anchor_vocabulary"


class TestEvaluateShadowClassifier:
    def _classifier(self) -> ExperimentalDomainClassifier:
        return ExperimentalDomainClassifier(
            _FakeEmbeddingService(_VOCAB),
            domain_keywords=["python developer", "software engineer"],
            off_domain_keywords=["sales representative", "hospitality customer"],
        )

    def test_precision_recall_and_disagreement_counted_correctly(self) -> None:
        samples = [
            {  # true positive: shadow says on_domain, ground truth on_domain
                "title": "Python Developer",
                "description": "Backend software engineer",
                "production_domain_relevance": 1.0,  # production also on_domain -> agreement
                "ground_truth": "on_domain",
            },
            {  # false positive: shadow says on_domain (weak signal), ground truth off_domain
                "title": "Software Engineer",
                "description": "some unrelated words",
                "production_domain_relevance": 0.05,  # production says off_domain -> disagreement
                "ground_truth": "off_domain",
            },
            {  # true negative
                "title": "Sales Representative",
                "description": "Customer hospitality role",
                "production_domain_relevance": 0.05,
                "ground_truth": "off_domain",
            },
            {  # unlabeled sample: contributes to disagreement count only
                "title": "Manager",
                "description": "Team manager role",
                "production_domain_relevance": 0.45,
            },
        ]
        metrics = evaluate_shadow_classifier(samples, self._classifier())
        assert metrics.n_samples == 4
        assert metrics.n_labeled == 3
        assert metrics.true_positives == 1
        assert metrics.false_positives == 1
        assert metrics.true_negatives == 1
        assert metrics.false_negatives == 0
        assert metrics.precision == pytest.approx(0.5)
        assert metrics.recall == pytest.approx(1.0)

    def test_deterministic_same_input_same_output(self) -> None:
        samples = [
            {
                "title": "Python Developer",
                "description": "Backend software engineer",
                "production_domain_relevance": 1.0,
                "ground_truth": "on_domain",
            }
        ]
        m1 = evaluate_shadow_classifier(samples, self._classifier())
        m2 = evaluate_shadow_classifier(samples, self._classifier())
        assert m1 == m2

    def test_empty_samples_returns_zeroed_metrics_without_error(self) -> None:
        metrics = evaluate_shadow_classifier([], self._classifier())
        assert metrics.n_samples == 0
        assert metrics.disagreement_rate == 0.0
        assert metrics.precision is None
        assert metrics.recall is None


class TestShadowModeCannotAffectProduction:
    """Structural proof, not just behavioral: production code paths never
    import src.experimental, so a shadow classification literally cannot
    reach eligibility, scoring, ranking, personalization, or notifications.
    """

    @pytest.mark.parametrize(
        "production_file",
        [
            "src/pipeline/orchestrator.py",
            "src/pipeline/scoring.py",
            "src/requirements/gate.py",
            "src/personalization/engine.py",
            "src/notifications/engine.py",
            "src/notifications/discord.py",
        ],
    )
    def test_production_module_never_imports_experimental_package(
        self, production_file: str
    ) -> None:
        source = (REPO_ROOT / production_file).read_text(encoding="utf-8")
        assert "src.experimental" not in source
        assert "src import experimental" not in source
