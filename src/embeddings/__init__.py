"""Embedding module for semantic matching.

Provides a lightweight, fully local semantic embedding system using sentence-transformers.
No cloud or paid AI APIs are required.
"""

from .embedding import EmbeddingService

__all__ = ["EmbeddingService"]
