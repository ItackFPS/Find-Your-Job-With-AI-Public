"""Embedding service for semantic matching.

Provides a lightweight, local semantic embedding system: the model's official ONNX
export run by ONNX Runtime (``src/embeddings/onnx_encoder.py``), with no PyTorch.
No cloud or paid AI APIs are required. Network: the model is downloaded once from the
Hugging Face Hub, and each later load asks the Hub whether it changed unless
HF_HUB_OFFLINE=1 is set (safe once the model is cached). Hub telemetry is off.

Features:
- Configurable model (default: paraphrase-multilingual-MiniLM-L12-v2 — multilingual,
  needed since job postings/profile text mix French and English)
- Embedding caching to avoid unnecessary recomputation
- Normalized cosine similarity scoring
- Graceful handling of missing/empty data
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from src.embeddings.onnx_encoder import SUPPORTED_MODELS, OnnxSentenceEncoder, model_spec
from src.models.job import Job

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingResult:
    """Result of embedding a text string."""
    id: str
    text: str
    vector: list[float]


@dataclass
class BatchEmbeddingResult:
    """Result of embedding multiple texts."""
    ids: list[str]
    vectors: list[list[float]]


@dataclass(frozen=True)
class SemanticMatchResult:
    """Result of computing semantic similarity between two texts."""
    query_id: str
    candidate_id: str
    similarity_score: float  # Cosine similarity, normalized to [0, 1]


@dataclass
class SemanticCandidateResult:
    """Enriched job with semantic features for ranking."""
    job: Job
    job_embedding: list[float]
    profile_embedding: list[float]
    semantic_score: float  # Normalized cosine similarity


class EmbeddingCache:
    """In-memory cache for embeddings to avoid recomputation.

    Keys are content hashes; values are the computed vectors.
    """

    def __init__(self, max_entries: int = 10_000) -> None:
        self._cache: dict[str, list[float]] = {}
        self._max_entries = max_entries

    def get(self, key: str) -> list[float] | None:
        """Return cached vector if available."""
        return self._cache.get(key)

    def put(self, key: str, vector: list[float]) -> None:
        """Store vector in cache, evicting oldest if full."""
        if len(self._cache) >= self._max_entries:
            # Simple FIFO eviction
            first_key = next(iter(self._cache))
            del self._cache[first_key]
        self._cache[key] = vector

    def clear(self) -> None:
        """Clear all cached embeddings."""
        self._cache.clear()

    # Fix #6: expose size publicly instead of forcing callers to reach
    # into the private _cache dict.
    @property
    def size(self) -> int:
        """Current number of cached embeddings."""
        return len(self._cache)


class EmbeddingService:
    """Local semantic embedding service.

    Runs a supported model's pinned ONNX export on the CPU.
    Embeddings are cached to avoid recomputation.

    Configuration:
        - model_name: Name of a model listed in onnx_encoder.SUPPORTED_MODELS
          (default: paraphrase-multilingual-MiniLM-L12-v2)
        - cache_max_entries: Maximum number of embeddings to cache (default: 10,000)
        - device: kept for configuration compatibility; inference runs on the CPU
    """

    def __init__(
        self,
        model_name: str = "paraphrase-multilingual-MiniLM-L12-v2",
        cache_max_entries: int = 10_000,
        device: str = "auto",
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._cache = EmbeddingCache(max_entries=cache_max_entries)
        self._encoder_instance: OnnxSentenceEncoder | None = None

    @property
    def model_name(self) -> str:
        """The model name being used."""
        return self._model_name

    @property
    def cache_model_id(self) -> str:
        """Key for persisted vectors.

        The fp32 reference export reproduces the former PyTorch vectors (measured,
        see onnx_encoder), so it keeps the bare model name and reuses vectors
        already stored. Any other export gets its own key, so vectors of different
        numerics are never mixed.
        """
        spec = SUPPORTED_MODELS.get(self._model_name.removeprefix("sentence-transformers/"))
        if spec is None or spec.variant == "model":
            return self._model_name
        return f"{self._model_name}@{spec.variant}"

    @property
    def _encoder(self) -> OnnxSentenceEncoder:
        """The ONNX encoder, created on first use (no download until then).

        Raises ValueError for a model with no pinned ONNX export.
        """
        if self._encoder_instance is None:
            self._encoder_instance = OnnxSentenceEncoder(model_spec(self._model_name))
        return self._encoder_instance

    @property
    def cache_size(self) -> int:
        """Current number of cached embeddings."""
        # Fix #6: use the public EmbeddingCache.size property instead of
        # reaching into its private _cache attribute.
        return self._cache.size

    # -- Embedding methods ----------------------------------------------------

    def embed(self, text: str) -> list[float]:
        """Embed a single text string.

        Args:
            text: The text to embed. Empty strings return a zero vector.

        Returns:
            A list of floats representing the embedding vector.
        """
        if not text or not text.strip():
            logger.warning("Empty text provided for embedding, returning zero vector")
            return [0.0] * self._encoder.dimension

        cache_key = self._make_cache_key(text)
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit for embedding key: %s", cache_key[:16])
            return cached[:]  # Return a copy

        # A plain list (not numpy) so the return type matches list[float] and
        # serializes to JSON as is.
        vector: list[float] = self._encoder.encode([text])[0].tolist()
        self._cache.put(cache_key, vector)
        logger.debug("Computed embedding for text '%s' (len=%d)", text[:50], len(text))
        return vector

    def embed_batch(
        self,
        texts: list[str],
        ids: list[str] | None = None,
    ) -> BatchEmbeddingResult:
        """Embed multiple text strings.

        Args:
            texts: A list of text strings to embed. Empty list returns empty result.
            ids: Optional list of stable identifiers, same length as texts
                (e.g. job IDs). If omitted, positional string indices are used.
                Fix #3: previously the raw text itself was used as the id,
                which collapses duplicate texts and can't be traced back to
                a real entity (e.g. a Job).

        Returns:
            BatchEmbeddingResult containing all embeddings, in the same
            order as the input texts.
        """
        if not texts:
            return BatchEmbeddingResult(ids=[], vectors=[])

        if ids is not None and len(ids) != len(texts):
            raise ValueError("ids must be the same length as texts")

        resolved_ids = ids if ids is not None else [str(i) for i in range(len(texts))]

        dim = self._encoder.dimension

        # Fix #2: check the cache first and only encode texts that are
        # actually missing, then populate the cache with the new results —
        # matching the behavior embed() already has.
        results: list[list[float] | None] = [None] * len(texts)
        to_encode_texts: list[str] = []
        to_encode_positions: list[int] = []

        for i, text in enumerate(texts):
            if not text or not text.strip():
                results[i] = [0.0] * dim
                continue

            cache_key = self._make_cache_key(text)
            cached = self._cache.get(cache_key)
            if cached is not None:
                results[i] = cached[:]
            else:
                to_encode_texts.append(text)
                to_encode_positions.append(i)

        if to_encode_texts:
            batch_vectors = self._encoder.encode(to_encode_texts)
            for pos, text, vec in zip(to_encode_positions, to_encode_texts, batch_vectors):
                vec_list = vec.tolist()
                self._cache.put(self._make_cache_key(text), vec_list)
                results[pos] = vec_list

        return BatchEmbeddingResult(ids=resolved_ids, vectors=results)  # type: ignore[arg-type]

    # -- Similarity methods ---------------------------------------------------

    def cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        """Compute normalized cosine similarity between two embedding vectors.

        Args:
            vec1: First embedding vector.
            vec2: Second embedding vector.

        Returns:
            Cosine similarity score in [0, 1]. Returns 0.0 if either vector is empty.
        """
        if not vec1 or not vec2:
            return 0.0

        norm1 = self._norm(vec1)
        norm2 = self._norm(vec2)

        if norm1 == 0.0 or norm2 == 0.0:
            return 0.0

        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        similarity = dot_product / (norm1 * norm2)

        # Clamp to [0, 1] due to floating-point imprecision
        return max(0.0, min(1.0, similarity))

    def compute_similarity(
        self,
        query_embedding: list[float],
        candidate_embeddings: list[list[float]],
        query_id: str = "query",
        candidate_ids: list[str] | None = None,
    ) -> list[SemanticMatchResult]:
        """Compute similarity between a query embedding and multiple candidate embeddings.

        Args:
            query_embedding: The query vector to compare against.
            candidate_embeddings: List of candidate vectors to compare with.
            query_id: Identifier for the query (constant across all results,
                since there is only one query). Fix #4: previously this was
                built as f"query_{i}" inside the candidate loop, so it
                incorrectly changed per candidate instead of staying fixed.
            candidate_ids: Optional identifiers for each candidate, same
                length as candidate_embeddings. Defaults to positional indices.

        Returns:
            List of SemanticMatchResult objects, one per candidate.
        """
        if candidate_ids is not None and len(candidate_ids) != len(candidate_embeddings):
            raise ValueError("candidate_ids must be the same length as candidate_embeddings")

        results = []
        for i, cand_vec in enumerate(candidate_embeddings):
            sim = self.cosine_similarity(query_embedding, cand_vec)
            cand_id = candidate_ids[i] if candidate_ids is not None else f"cand_{i}"
            results.append(SemanticMatchResult(
                query_id=query_id,
                candidate_id=cand_id,
                similarity_score=sim,
            ))
        return results

    def compute_semantic_score(
        self,
        job_text: str,
        profile_embedding: list[float],
    ) -> float:
        """Compute semantic similarity between a job text and a candidate profile.

        The job text is embedded and compared to the provided profile embedding.

        Args:
            job_text: Combined text (title + description) of the job to embed.
            profile_embedding: Pre-computed embedding for the candidate profile.

        Returns:
            Normalized cosine similarity score in [0, 1].
        """
        if not profile_embedding:
            return 0.0

        job_embedding = self.embed(job_text)
        return self.cosine_similarity(job_embedding, profile_embedding)

    # -- Job-level semantic matching ------------------------------------------

    def compute_job_semantic_score(
        self,
        job: Job,
        profile_embedding: list[float],
    ) -> float:
        """Compute semantic similarity between a job and a candidate profile.

        Uses the combined title + description of the job for embedding.

        Args:
            job: The Job to score.
            profile_embedding: Pre-computed embedding for the candidate profile.

        Returns:
            Normalized cosine similarity score in [0, 1].
        """
        parts = []
        if job.title:
            parts.append(job.title.lower().strip())
        if job.description:
            parts.append(job.description.lower().strip())

        job_text = " ".join(parts)

        if not job_text.strip():
            return 0.0

        return self.compute_semantic_score(job_text, profile_embedding)

    def compute_job_semantic_scores(
        self,
        jobs: list[Job],
        profile_embedding: list[float],
    ) -> list[tuple[Job, float]]:
        """Compute semantic scores for a batch of jobs against a profile.

        Args:
            jobs: List of jobs to score.
            profile_embedding: Pre-computed embedding for the candidate profile.

        Returns:
            List of (job, semantic_score) tuples.
        """
        results = []
        for job in jobs:
            score = self.compute_job_semantic_score(job, profile_embedding)
            results.append((job, score))
        return results

    # -- Helpers --------------------------------------------------------------

    @staticmethod
    def _norm(vec: list[float]) -> float:
        """Compute the L2 norm of a vector."""
        return float(sum(x * x for x in vec) ** 0.5)

    def _make_cache_key(self, text: str) -> str:
        """Create a deterministic cache key from text content."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()