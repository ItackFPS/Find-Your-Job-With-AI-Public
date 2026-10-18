"""Configuration for Phase 2: Local AI Job Understanding.

Loads configuration from environment variables and YAML files.
The project must remain runnable with AI disabled (AI_ENABLED=false).

Supported LLM providers
-----------------------
LM Studio (default):
    PHASE2_LLM_PROVIDER=lmstudio
    PHASE2_LLM_ENDPOINT=http://localhost:1234
    PHASE2_LLM_MODEL_NAME=<model identifier shown in LM Studio>

Ollama:
    PHASE2_LLM_PROVIDER=ollama
    PHASE2_LLM_ENDPOINT=http://localhost:11434
    PHASE2_LLM_MODEL_NAME=llama3.2:8b

Both providers implement the OpenAI-compatible chat completions API
(POST /v1/chat/completions), so the same LocalLLMProvider implementation
works for both. Only the endpoint and model name differ.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_PROVIDERS = ("lmstudio", "ollama")


class Phase2Config(BaseSettings):
    """Configuration for the Phase 2 local AI understanding stage."""

    # Master toggle — when false, Phase 2 is completely skipped
    ai_enabled: bool = Field(
        default=False,
        description="Set to true to enable Phase 2 local AI analysis.",
    )

    # Provider selection — determines default endpoint/model and health-check path
    llm_provider: str = Field(
        default="lmstudio",
        description=(
            "Local LLM provider. Supported values: 'lmstudio', 'ollama'. "
            "Controls the health-check path and documents which server is expected."
        ),
    )

    # LLM endpoint — default is LM Studio's port
    llm_endpoint: str = Field(
        default="http://localhost:1234",
        description=(
            "Base URL of the local LLM server. "
            "LM Studio default: http://localhost:1234. "
            "Ollama default: http://localhost:11434."
        ),
    )

    # Model name — LM Studio typically ignores this field and uses the loaded model;
    # Ollama requires it to select the correct pulled model.
    llm_model_name: str = Field(
        default="local-model",
        description=(
            "Model identifier sent in the request. "
            "LM Studio: set to the model name shown in the LM Studio UI, e.g. "
            "'lmstudio-community/Bonsai-27B-v1-GGUF' (the field is advisory — "
            "LM Studio uses whichever model is currently loaded). "
            "Ollama: must match the pulled model tag, e.g. 'llama3.2:8b'."
        ),
    )

    llm_temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Temperature for LLM sampling (0.0 = deterministic).",
    )

    llm_max_tokens: int = Field(
        default=1024,
        ge=1,
        description=(
            "Maximum tokens to generate per response. Evidence-based default "
            "(2026-09-05 truncation audit): the full-pipeline benchmark measured "
            "an average successful response of ~1,150 chars (~290 tokens at "
            "~4 chars/token) against the minimized 11-field schema in "
            "src/ai/prompt.py, but 5/100 responses (5%) were truncated mid-JSON "
            "at the previous 512-token cap on jobs with long skill/responsibility "
            "lists. 1024 gives roughly 2x headroom over the measured average "
            "while staying schema-sized rather than an arbitrary large number. "
            "LocalLLMProvider also retries a detected truncation once with a "
            "bounded multiplier (see _RETRY_MAX_TOKENS_MULTIPLIER) as a safety "
            "net for the remaining tail, rather than raising this further."
        ),
    )

    llm_timeout_seconds: int = Field(
        default=60,
        ge=1,
        description=(
            "Timeout in seconds for LLM requests. Raised from 30s alongside the "
            "2026-09-05 max_tokens increase (512->1024): the original full-pipeline "
            "benchmark already showed a 30.3s max latency at 512 tokens (essentially "
            "at the old ceiling), and a focused 100-job re-benchmark at 1024 tokens "
            "measured a 32.1s p99 with 8 requests hitting the 30s timeout that would "
            "have succeeded given more time — autoregressive generation time scales "
            "with token count, so doubling the token budget without raising the "
            "timeout just trades silent truncation for explicit timeouts. 60s is "
            "proportional to the token-budget increase, not an arbitrary bump."
        ),
    )

    max_workers: int = Field(
        default=4,
        ge=1,
        description=(
            "Concurrent in-flight requests to the local LLM server. Default 4 "
            "matches this project's measured optimal concurrency against LM "
            "Studio's own configured PARALLEL slot count (see the eligibility "
            "gate benchmark) — raise/lower via PHASE2_MAX_WORKERS if your "
            "server is configured with a different number of parallel slots."
        ),
    )

    llm_thinking_enabled: bool = Field(
        default=False,
        description=(
            "Enable the model's extended thinking / reasoning mode. "
            "When False (default), 'enable_thinking: false' is sent in the "
            "API payload so that Qwen3 and similar reasoning models skip their "
            "chain-of-thought phase and return structured output directly. "
            "Set PHASE2_LLM_THINKING_ENABLED=true only if you want reasoning."
        ),
    )

    # Candidate selection limit — Phase 2 only analyses the top-N jobs
    # (ranked by semantic + deterministic score after hard filtering).
    # All other jobs remain in the pipeline; Phase 2 is enrichment only.
    max_candidates: int = Field(
        default=100,
        ge=1,
        description=(
            "Maximum candidates to analyse in Phase 2. "
            "Candidates are the top-ranked jobs after hard filtering and "
            "semantic matching. Set via PHASE2_MAX_CANDIDATES."
        ),
    )

    # Prompt version identifier
    prompt_version: str = Field(
        default="job_understanding_v1",
        description="Version identifier for the job understanding prompt.",
    )

    model_config = SettingsConfigDict(env_prefix="PHASE2_")

    @field_validator("llm_provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        normalised = v.lower().strip()
        if normalised not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"PHASE2_LLM_PROVIDER must be one of {SUPPORTED_PROVIDERS}, got {v!r}"
            )
        return normalised


def load_phase2_config() -> Phase2Config:
    """Load Phase 2 configuration from environment variables.

    Environment variables (all prefixed PHASE2_):
        PHASE2_LLM_PROVIDER=lmstudio        # or 'ollama'
        PHASE2_LLM_ENDPOINT=http://localhost:1234
        PHASE2_LLM_MODEL_NAME=local-model
        PHASE2_LLM_TEMPERATURE=0.0
        PHASE2_LLM_MAX_TOKENS=2048
        PHASE2_LLM_TIMEOUT_SECONDS=30
        PHASE2_PROMPT_VERSION=job_understanding_v1
    """
    return Phase2Config()
