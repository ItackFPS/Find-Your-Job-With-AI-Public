"""Sentence embeddings with ONNX Runtime instead of PyTorch.

Runs the official ONNX export that the sentence-transformers project publishes in
the model's own Hugging Face repository, pinned to one revision, and reproduces
what ``SentenceTransformer.encode`` does for this model family: tokenize
(truncate to the model's ``max_seq_length``), run the transformer, mean-pool the
token embeddings over the attention mask. No normalization: the
paraphrase-multilingual models have no ``Normalize`` module.

Why: PyTorch and transformers weigh about 640 MB installed, and importing torch
dominates the backend's cold start. ONNX Runtime plus the ``tokenizers`` library
do the same inference for a small fraction of that (update plan §C6).

Measured before the switch, on 1,000 real job texts against sentence-transformers
6.1 + torch 2.14 (CPU): the fp32 export ``onnx/model.onnx`` gives the same vectors
(cosine 1.0 for every text, profile-similarity Spearman 1.0, max difference < 1e-5),
loads in about 1 s instead of a 16 s torch import, and encodes about 15 % slower.
The int8 export (``model_quint8_avx2.onnx``, 4x smaller) was rejected: Spearman
0.985 and similarity shifts up to 0.069, below the 0.99 bar the plan sets.

Network: the files are downloaded once from the Hugging Face Hub at the pinned
revision, then read from the local cache. ``HF_HUB_OFFLINE=1`` works once cached.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Read when huggingface_hub is first imported. A value the user set is kept.
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import numpy as np  # noqa: E402


@dataclass(frozen=True)
class OnnxModelSpec:
    """Where one supported model's ONNX export lives, pinned to a revision."""

    repo_id: str
    revision: str
    onnx_file: str
    dimension: int
    max_seq_length: int

    @property
    def variant(self) -> str:
        """Short id of the exported file (``model`` for the fp32 reference export)."""
        return Path(self.onnx_file).stem


# Models this app can run. A model name from config/semantic_config.yaml must be
# listed here: an unknown name fails loudly instead of downloading something
# unreviewed.
SUPPORTED_MODELS: dict[str, OnnxModelSpec] = {
    "paraphrase-multilingual-MiniLM-L12-v2": OnnxModelSpec(
        repo_id="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        revision="e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
        onnx_file="onnx/model.onnx",
        dimension=384,
        max_seq_length=128,
    ),
}


def model_spec(model_name: str) -> OnnxModelSpec:
    """The pinned ONNX export for a configured model name."""
    name = model_name.removeprefix("sentence-transformers/")
    try:
        return SUPPORTED_MODELS[name]
    except KeyError:
        supported = ", ".join(sorted(SUPPORTED_MODELS))
        raise ValueError(
            f"Embedding model {model_name!r} is not supported (supported: {supported})"
        ) from None


class OnnxSentenceEncoder:
    """Mean-pooled sentence embeddings from a pinned ONNX export (CPU)."""

    def __init__(self, spec: OnnxModelSpec, batch_size: int = 32) -> None:
        self.spec = spec
        self._batch_size = batch_size
        self._lock = threading.Lock()
        self._session: Any = None
        self._tokenizer: Any = None
        self._input_names: set[str] = set()

    @property
    def dimension(self) -> int:
        return self.spec.dimension

    def _download(self, filename: str) -> str:
        from huggingface_hub import hf_hub_download

        return hf_hub_download(self.spec.repo_id, filename, revision=self.spec.revision)

    def load(self) -> None:
        """Download (first time only) and open the model. Thread-safe, idempotent."""
        if self._session is not None:
            return
        with self._lock:
            if self._session is not None:
                return
            import onnxruntime as ort
            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_file(self._download("tokenizer.json"))
            tokenizer.enable_truncation(max_length=self.spec.max_seq_length)
            tokenizer.enable_padding()
            options = ort.SessionOptions()
            options.log_severity_level = 3
            session = ort.InferenceSession(
                self._download(self.spec.onnx_file),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            outputs = session.get_outputs()
            width = outputs[0].shape[-1] if outputs else None
            if isinstance(width, int) and width != self.spec.dimension:
                raise RuntimeError(
                    f"{self.spec.repo_id}: model outputs {width} dimensions, "
                    f"expected {self.spec.dimension}"
                )
            self._input_names = {i.name for i in session.get_inputs()}
            self._tokenizer = tokenizer
            self._session = session

    def encode(self, texts: list[str]) -> np.ndarray:
        """Embed texts; returns an array of shape (len(texts), dimension)."""
        self.load()
        if not texts:
            return np.zeros((0, self.spec.dimension), dtype=np.float32)
        chunks = []
        for start in range(0, len(texts), self._batch_size):
            encodings = self._tokenizer.encode_batch(texts[start:start + self._batch_size])
            ids = np.array([e.ids for e in encodings], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._input_names:
                feed["token_type_ids"] = np.zeros_like(ids)
            token_embeddings = self._session.run(None, feed)[0]
            weights = mask[..., None].astype(np.float32)
            summed = (token_embeddings * weights).sum(axis=1)
            chunks.append(summed / np.clip(weights.sum(axis=1), 1e-9, None))
        return np.vstack(chunks).astype(np.float32)
