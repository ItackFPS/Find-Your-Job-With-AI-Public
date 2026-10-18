"""Tests for EmbeddingService device resolution (audit fix #1).

`device: "auto"` in config/semantic_config.yaml was previously passed
straight through to SentenceTransformer, which is not a valid torch device
string and crashes. These tests cover the resolution logic in isolation
(no model loading, so they don't need network/HF Hub access).
"""

from __future__ import annotations

from unittest.mock import patch

from src.embeddings.embedding import EmbeddingService


class TestResolveDevice:
    def test_auto_resolves_to_cpu_when_no_cuda(self) -> None:
        with patch("torch.cuda.is_available", return_value=False):
            assert EmbeddingService._resolve_device("auto") == "cpu"

    def test_auto_resolves_to_cuda_when_available(self) -> None:
        with patch("torch.cuda.is_available", return_value=True):
            assert EmbeddingService._resolve_device("auto") == "cuda"

    def test_explicit_cpu_is_respected_regardless_of_cuda(self) -> None:
        with patch("torch.cuda.is_available", return_value=True):
            assert EmbeddingService._resolve_device("cpu") == "cpu"

    def test_explicit_cuda_is_respected_even_without_cuda_available(self) -> None:
        # An explicit choice is never second-guessed, even if it might fail
        # later at model-load time -- only "auto" gets auto-detection.
        with patch("torch.cuda.is_available", return_value=False):
            assert EmbeddingService._resolve_device("cuda") == "cuda"

    def test_explicit_device_string_passthrough(self) -> None:
        assert EmbeddingService._resolve_device("cuda:0") == "cuda:0"

    def test_never_returns_the_literal_string_auto(self) -> None:
        for cuda_available in (True, False):
            with patch("torch.cuda.is_available", return_value=cuda_available):
                resolved = EmbeddingService._resolve_device("auto")
                assert resolved != "auto"
                assert resolved in ("cpu", "cuda")


class TestEnsureModelLoadedUsesResolvedDevice:
    def test_sentence_transformer_never_receives_auto(self) -> None:
        """The literal string "auto" must never reach SentenceTransformer()."""
        service = EmbeddingService(model_name="all-MiniLM-L6-v2", device="auto")

        with (
            patch("src.embeddings.embedding.torch.cuda.is_available", return_value=False),
            patch("src.embeddings.embedding.SentenceTransformer") as mock_st,
        ):
            service._ensure_model_loaded()

        mock_st.assert_called_once()
        _args, kwargs = mock_st.call_args
        assert kwargs["device"] != "auto"
        assert kwargs["device"] == "cpu"

    def test_sentence_transformer_receives_cuda_when_available(self) -> None:
        service = EmbeddingService(model_name="all-MiniLM-L6-v2", device="auto")

        with (
            patch("src.embeddings.embedding.torch.cuda.is_available", return_value=True),
            patch("src.embeddings.embedding.SentenceTransformer") as mock_st,
        ):
            service._ensure_model_loaded()

        _args, kwargs = mock_st.call_args
        assert kwargs["device"] == "cuda"

    def test_explicit_device_config_passed_through_to_model_load(self) -> None:
        service = EmbeddingService(model_name="all-MiniLM-L6-v2", device="cpu")

        with patch("src.embeddings.embedding.SentenceTransformer") as mock_st:
            service._ensure_model_loaded()

        _args, kwargs = mock_st.call_args
        assert kwargs["device"] == "cpu"
