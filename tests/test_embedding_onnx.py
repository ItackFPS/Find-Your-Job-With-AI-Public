"""EmbeddingService on ONNX Runtime: model pinning, pooling, cache keys.

The unit tests use a fake tokenizer and session (no download). The test marked
``network`` runs the real pinned export once and checks known properties.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from src.embeddings.embedding import EmbeddingService
from src.embeddings.onnx_encoder import SUPPORTED_MODELS, OnnxSentenceEncoder, model_spec

MODEL = "paraphrase-multilingual-MiniLM-L12-v2"


def test_supported_model_is_pinned_to_a_revision_and_file() -> None:
    spec = model_spec(MODEL)
    assert len(spec.revision) == 40 and spec.revision.isalnum()
    assert spec.onnx_file.startswith("onnx/") and spec.onnx_file.endswith(".onnx")
    assert spec.dimension == 384 and spec.max_seq_length == 128
    assert model_spec(f"sentence-transformers/{MODEL}") is spec


def test_unknown_model_fails_loudly_on_first_use_not_at_construction() -> None:
    service = EmbeddingService(model_name="some-unreviewed-model")
    assert service.cache_model_id == "some-unreviewed-model"
    with pytest.raises(ValueError, match="not supported"):
        service.embed("python developer")


def test_reference_export_keeps_the_stored_vectors_key() -> None:
    # The fp32 export reproduces the former PyTorch vectors, so vectors already
    # stored under the bare model name stay valid.
    assert SUPPORTED_MODELS[MODEL].onnx_file == "onnx/model.onnx"
    assert EmbeddingService(model_name=MODEL).cache_model_id == MODEL


def test_another_export_gets_its_own_cache_key(monkeypatch) -> None:
    from dataclasses import replace

    quantized = replace(SUPPORTED_MODELS[MODEL], onnx_file="onnx/model_quint8_avx2.onnx")
    monkeypatch.setitem(SUPPORTED_MODELS, MODEL, quantized)
    assert EmbeddingService(model_name=MODEL).cache_model_id == f"{MODEL}@model_quint8_avx2"


class _FakeTokenizer:
    """Token id = word length; pads to the longest text in the batch."""

    def encode_batch(self, texts: list[str]) -> list[SimpleNamespace]:
        rows = [[len(w) for w in t.split()] for t in texts]
        width = max(len(r) for r in rows)
        return [SimpleNamespace(ids=r + [0] * (width - len(r)),
                                attention_mask=[1] * len(r) + [0] * (width - len(r))) for r in rows]


class _FakeSession:
    """Token embedding = [id, 1, 0, ...]; padding positions get a large value."""

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.feeds: list[dict] = []

    def run(self, _outputs, feed):  # noqa: ANN001
        self.feeds.append(feed)
        ids = feed["input_ids"].astype(np.float32)
        out = np.zeros(ids.shape + (self.dim,), dtype=np.float32)
        out[..., 0] = ids
        out[..., 1] = 1.0
        out[feed["attention_mask"] == 0] = 1000.0  # must be ignored by pooling
        return [out]


def _fake_encoder(batch_size: int = 32) -> tuple[OnnxSentenceEncoder, _FakeSession]:
    enc = OnnxSentenceEncoder(model_spec(MODEL), batch_size=batch_size)
    session = _FakeSession(enc.dimension)
    enc._tokenizer = _FakeTokenizer()
    enc._session = session
    enc._input_names = {"input_ids", "attention_mask", "token_type_ids"}
    return enc, session


def test_mean_pooling_ignores_padding_and_batches() -> None:
    enc, session = _fake_encoder(batch_size=2)
    vectors = enc.encode(["ab abcd", "abc", "a b c d"])
    assert vectors.shape == (3, 384)
    np.testing.assert_allclose(vectors[:, 0], [3.0, 3.0, 1.0])  # mean token id, padding ignored
    np.testing.assert_allclose(vectors[:, 1], [1.0, 1.0, 1.0])
    assert len(session.feeds) == 2  # 3 texts in batches of 2
    assert (session.feeds[0]["token_type_ids"] == 0).all()


def test_service_uses_encoder_and_cache() -> None:
    service = EmbeddingService(model_name=MODEL)
    enc, session = _fake_encoder()
    service._encoder_instance = enc
    first = service.embed("ab abcd")
    again = service.embed("ab abcd")
    assert first == again and len(first) == 384 and isinstance(first[0], float)
    assert len(session.feeds) == 1  # second call served from the cache
    batch = service.embed_batch(["ab abcd", "", "abc"], ids=["a", "b", "c"])
    assert batch.ids == ["a", "b", "c"]
    assert batch.vectors[0] == first and batch.vectors[1] == [0.0] * 384
    assert len(session.feeds) == 2  # only "abc" needed encoding


def test_empty_input_returns_zero_vector_without_loading() -> None:
    service = EmbeddingService(model_name=MODEL)
    assert service.embed("   ") == [0.0] * 384
    assert service._encoder_instance is not None and service._encoder_instance._session is None


@pytest.mark.network
def test_real_export_is_multilingual_and_deterministic() -> None:
    service = EmbeddingService(model_name=MODEL)
    fr, en, other = (service.embed(t) for t in (
        "Développeur Python junior", "Junior Python developer", "Boulanger pâtissier en CDI"))
    assert len(fr) == 384
    assert service.cosine_similarity(fr, en) > service.cosine_similarity(fr, other)
    assert EmbeddingService(model_name=MODEL).embed("Junior Python developer") == en
