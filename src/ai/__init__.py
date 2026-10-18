"""Phase 2: Local AI Job Understanding module."""

from src.ai.config import Phase2Config, load_phase2_config
from src.ai.interfaces import LLMProvider, LocalLLMProvider
from src.ai.models import (
    BatchJobUnderstandingResult,
    JobUnderstanding,
    JobUnderstandingResult,
)
from src.ai.pipeline import Phase2Pipeline
from src.ai.prompt import PROMPT_VERSION, build_job_understanding_prompt
from src.ai.storage import AIAnalysisStorage

__all__ = [
    "PROMPT_VERSION",
    "AIAnalysisStorage",
    "BatchJobUnderstandingResult",
    "JobUnderstanding",
    "JobUnderstandingResult",
    "LLMProvider",
    "LocalLLMProvider",
    "Phase2Config",
    "Phase2Pipeline",
    "build_job_understanding_prompt",
    "load_phase2_config",
]
