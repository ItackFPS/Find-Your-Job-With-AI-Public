"""LLM interface abstraction for Phase 2: Local AI Job Understanding.

The pipeline should never depend on Ollama/llama.cpp implementation details.
This module defines the contract that any local LLM provider must satisfy.
"""

from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import requests

from src.ai.models import (
    BatchJobUnderstandingResult,
    JobUnderstanding,
    JobUnderstandingResult,
)
from src.models.job import Job

logger = logging.getLogger(__name__)

# Callback signature for live Phase 2 progress: (completed, total, failed_so_far).
ProgressCallback = Callable[[int, int, int], None]


class TruncatedResponseError(Exception):
    """The API's own finish_reason says generation stopped at the token
    limit (finish_reason == "length"), not a natural stop.

    Treated as truncated regardless of whether the partial text happens to
    parse as valid JSON — the API told us it did not finish, so the content
    is not trusted even if it looks complete by coincidence.
    """

    def __init__(self, content: str):
        super().__init__("LLM response truncated at max_tokens (finish_reason=length)")
        self.content = content


class MalformedResponseError(Exception):
    """The LLM's text response is not valid JSON matching the JobUnderstanding
    schema.

    ``reason`` is one of:
      - "malformed_json": the text isn't valid JSON at all
      - "validation_error": valid JSON, but wrong shape/types for the schema
      - "empty_response": the model returned no visible content
    """

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


def _safe_progress(on_progress: ProgressCallback, completed: int, total: int, failed: int) -> None:
    try:
        on_progress(completed, total, failed)
    except Exception:
        logger.warning("Phase 2 progress callback raised — ignoring", exc_info=True)


def _classify_builtin_transport_error(exc: BaseException) -> str:
    """Map the builtin exception _call_llm actually raises to a
    JobUnderstandingResult.failure_reason value."""
    if isinstance(exc, ConnectionError):
        return "connection_error"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "http_error"  # RuntimeError: HTTPError or a generic RequestException


class LLMProvider(ABC):
    """Abstract interface for any local LLM provider.

    The pipeline depends on this interface, not on concrete implementations
    such as Ollama, llama.cpp, or any other local inference engine.
    """

    @abstractmethod
    def analyze_job(
        self,
        job: Job,
        prompt_version: str = "job_understanding_v1",
        profile: dict[str, Any] | None = None,
    ) -> JobUnderstandingResult:
        """Analyze a single job description using the local LLM.

        Args:
            job: The normalized, validated job to analyze.
            prompt_version: The version of the prompt template to use.
            profile: Optional user profile dict (JobSearchProfile.model_dump()).
                     When provided, uses the personalized matching prompt so the
                     model can evaluate fit against the user's preferences.

        Returns:
            A JobUnderstandingResult containing the structured analysis.
            The result may indicate success, failure, or skip status.
        """
        ...

    @abstractmethod
    def analyze_batch(
        self,
        jobs: list[Job],
        prompt_version: str = "job_understanding_v1",
        profile: dict[str, Any] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> BatchJobUnderstandingResult:
        """Analyze a batch of job descriptions using the local LLM.

        Args:
            jobs: List of normalized, validated jobs to analyze.
            prompt_version: The version of the prompt template to use.
            profile: Optional user profile dict forwarded to each analyze_job call.
            on_progress: Optional callback invoked as
                ``(completed_count, total, failed_so_far)`` after each job
                finishes — lets callers surface live progress. Never affects
                result ordering: results are always returned in the same
                order as ``jobs``.

        Returns:
            A BatchJobUnderstandingResult containing all individual results.
        """
        ...

    def complete(self, prompt: str, max_tokens: int | None = None) -> str:
        """Send one free-form prompt to the LLM and return its raw text response.

        This is the low-level completion primitive for callers that build
        their own prompt/schema outside the structured JobUnderstanding
        pipeline — e.g. ``RequirementExtractor``'s requirement-extraction
        prompt and ``EligibilityGate``'s eligibility prompt (see
        ``src/requirements/extractor.py`` and ``src/ai/eligibility.py``).
        Concrete providers are expected to override this; the base
        implementation only exists so ``LLMProvider`` stays usable by
        subclasses (e.g. test doubles) that never call it.

        Raises:
            NotImplementedError: when the concrete provider does not
                implement single-prompt completion.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement complete()"
        )


class LocalLLMProvider(LLMProvider):
    """Concrete implementation using a local LLM inference engine.

    This is the default provider for Phase 2. It can be swapped out for
    other implementations (e.g., a mock provider for testing) without
    changing any pipeline code.
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:1234",
        model_name: str = "local-model",
        temperature: float = 0.0,
        max_tokens: int = 2048,
        timeout_seconds: int = 30,
        thinking_enabled: bool = False,
        max_workers: int = 1,
    ):
        self.endpoint = endpoint
        self.model_name = model_name
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.thinking_enabled = thinking_enabled
        # Concurrent in-flight requests to the local LLM server. LM Studio (and
        # llama.cpp's server generally) can serve several requests in parallel
        # when the loaded model has more than one parallel slot configured —
        # sequential dispatch from this client would waste that capacity. See
        # Phase2Config.max_workers for the measured default (4, matching this
        # project's LM Studio server's own configured PARALLEL slot count).
        self.max_workers = max(1, max_workers)

    # Bounded, explicit retry for a *detected truncation* only (see
    # analyze_job) — never for plain malformed JSON or transport errors.
    # Sampling is deterministic (temperature=0 by default), so retrying with
    # identical parameters reproduces the identical output; only a larger
    # token budget can plausibly change the outcome for a truncation.
    _RETRY_MAX_TOKENS_MULTIPLIER = 2
    _RETRY_MAX_TOKENS_CAP = 4096

    def analyze_job(
        self,
        job: Job,
        prompt_version: str = "job_understanding_v1",
        profile: dict[str, Any] | None = None,
    ) -> JobUnderstandingResult:
        """Analyze a single job using the local LLM.

        Every non-success outcome is classified with a specific
        ``failure_reason`` (see ``JobUnderstandingResult``) instead of being
        collapsed into a generic failure: malformed JSON, a truncated
        response, an empty response, a timeout, a connection error, and an
        HTTP/model error are all distinguishable in the stored result and in
        Phase 2's aggregate metrics. A response the API itself reports as
        truncated (``finish_reason == "length"``) is retried exactly once
        with a larger (but capped) token budget — see ``_attempt`` and
        ``_RETRY_MAX_TOKENS_MULTIPLIER`` — since that is the one case where
        retrying can plausibly change the outcome under deterministic
        sampling. Every other failure is reported immediately, not retried,
        so one bad response costs at most 2x latency, never an unbounded
        retry loop, and never silently becomes an empty "success".
        """
        if profile:
            from src.ai.prompt import build_personalized_matching_prompt
            prompt = build_personalized_matching_prompt(
                job_description=job.description or "",
                profile=profile,
                job_title=job.title,
                job_company=job.company,
            )
        else:
            from src.ai.prompt import build_job_understanding_prompt
            prompt = build_job_understanding_prompt(
                job_description=job.description or "",
                job_title=job.title,
                job_company=job.company,
            )

        try:
            return self._attempt(job, prompt, prompt_version, self.max_tokens)
        except TruncatedResponseError:
            retry_tokens = min(
                self.max_tokens * self._RETRY_MAX_TOKENS_MULTIPLIER,
                self._RETRY_MAX_TOKENS_CAP,
            )
            logger.warning(
                "Phase 2: job %s response truncated at max_tokens=%d — "
                "retrying once with max_tokens=%d",
                job.id, self.max_tokens, retry_tokens,
            )
            try:
                result = self._attempt(job, prompt, prompt_version, retry_tokens)
                return result.model_copy(update={"retried": True})
            except TruncatedResponseError:
                return self._failed_result(
                    job, prompt_version, "truncated",
                    f"LLM response still truncated after retry with max_tokens={retry_tokens}",
                    retried=True,
                )
            except MalformedResponseError as exc2:
                return self._failed_result(job, prompt_version, exc2.reason, str(exc2), retried=True)
            except requests.exceptions.RequestException as exc2:
                logger.error("LLM analysis failed for job %s (request, retry): %s", job.id, exc2)
                return self._failed_result(job, prompt_version, "connection_error", str(exc2), retried=True)
            except (ConnectionError, TimeoutError, RuntimeError) as exc2:
                # _call_llm translates requests.exceptions.{ConnectionError,
                # Timeout,HTTPError,RequestException} into these builtins —
                # see the note on the matching except clause below.
                reason = _classify_builtin_transport_error(exc2)
                logger.error("LLM analysis failed for job %s (retry): %s", job.id, exc2)
                return self._failed_result(job, prompt_version, reason, str(exc2), retried=True)
        except MalformedResponseError as exc:
            return self._failed_result(job, prompt_version, exc.reason, str(exc), retried=False)
        except requests.exceptions.RequestException as exc:
            # A raw requests exception can only reach here if a caller invokes
            # _call_llm in a way that bypasses its own internal translation
            # (e.g. a test double, or a future direct requests.* call) — kept
            # as a safety net alongside the builtin-exception clause below,
            # which is what _call_llm actually raises in normal operation.
            logger.error("LLM analysis failed for job %s (request): %s", job.id, str(exc))
            return self._failed_result(job, prompt_version, "connection_error", str(exc), retried=False)
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            # _call_llm deliberately translates requests.exceptions.ConnectionError
            # -> builtin ConnectionError, requests.exceptions.Timeout -> builtin
            # TimeoutError, and HTTPError/RequestException -> RuntimeError. These
            # builtins (NOT requests.exceptions.*) are what actually propagates
            # from a real _call_llm call, so this clause — not a
            # requests.exceptions.ConnectionError/Timeout/HTTPError clause — is
            # what makes a single job's transport failure "failed" instead of
            # propagating and aborting the entire Phase 2 batch. (A previous
            # version of this method caught only requests.exceptions.ConnectionError
            # here, which is a distinct, unrelated exception class from the
            # builtin ConnectionError _call_llm actually raises — a real
            # connection failure escaped uncaught. See TestTransportErrorClassification.)
            logger.error("LLM analysis failed for job %s: %s", job.id, str(exc))
            reason = _classify_builtin_transport_error(exc)
            return self._failed_result(job, prompt_version, reason, str(exc), retried=False)

    def _attempt(
        self, job: Job, prompt: str, prompt_version: str, max_tokens: int
    ) -> JobUnderstandingResult:
        """One LLM call + parse attempt.

        Raises ``TruncatedResponseError`` or ``MalformedResponseError`` on
        any failure — never returns a "success" result for content that
        didn't actually parse into a valid ``JobUnderstanding``.
        """
        response_text = self._call_llm(prompt, max_tokens=max_tokens)

        # Reasoning models (e.g. bonsai-27b) can exhaust their token budget
        # on internal chain-of-thought and return empty visible content.
        if not response_text.strip():
            logger.warning(
                "LLM returned empty content for job %s — likely a reasoning model "
                "that exhausted its token budget on chain-of-thought.",
                job.id,
            )
            raise MalformedResponseError(
                "LLM returned empty response (reasoning model token budget exhausted, "
                "or the model produced no visible output)",
                reason="empty_response",
            )

        analysis = self._parse_response(response_text)  # raises MalformedResponseError on failure

        return JobUnderstandingResult(
            job_id=job.id,
            analysis=analysis,
            prompt_version=prompt_version,
            model_identifier=self.model_name,
            status="success",
        )

    def _failed_result(
        self,
        job: Job,
        prompt_version: str,
        failure_reason: str,
        error_message: str,
        retried: bool,
    ) -> JobUnderstandingResult:
        return JobUnderstandingResult(
            job_id=job.id,
            analysis=JobUnderstanding(),
            prompt_version=prompt_version,
            model_identifier=self.model_name,
            status="failed",
            error_message=error_message,
            failure_reason=failure_reason,
            retried=retried,
        )

    def analyze_batch(
        self,
        jobs: list[Job],
        prompt_version: str = "job_understanding_v1",
        profile: dict[str, Any] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> BatchJobUnderstandingResult:
        """Analyze a batch of jobs using the local LLM.

        Dispatches up to ``self.max_workers`` requests concurrently (default 1,
        i.e. sequential — unchanged behavior unless a caller opts in via a
        higher ``max_workers``). Uses ``ThreadPoolExecutor.map`` so results
        stay in the same order as ``jobs`` regardless of completion order,
        matching the plain-loop behavior this replaces. ``on_progress`` (if
        given) fires in true completion order from inside the worker thread
        that just finished, independent of the order ``executor.map`` hands
        results back in — the dispatch strategy itself is unchanged.
        """
        total = len(jobs)
        counters = {"completed": 0, "failed": 0}
        counters_lock = threading.Lock()

        def _run_one(job: Job) -> JobUnderstandingResult:
            result = self.analyze_job(job, prompt_version, profile=profile)
            if on_progress is not None:
                with counters_lock:
                    counters["completed"] += 1
                    if result.status == "failed":
                        counters["failed"] += 1
                    completed, failed = counters["completed"], counters["failed"]
                _safe_progress(on_progress, completed, total, failed)
            return result

        if self.max_workers <= 1 or total <= 1:
            results: list[JobUnderstandingResult] = [_run_one(job) for job in jobs]
        else:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                results = list(executor.map(_run_one, jobs))

        successful = sum(1 for r in results if r.status == "success")
        failed = sum(1 for r in results if r.status == "failed")
        skipped = sum(1 for r in results if r.status == "skipped")
        retried = sum(1 for r in results if r.retried)
        failure_reasons: dict[str, int] = {}
        for r in results:
            if r.status == "failed" and r.failure_reason:
                failure_reasons[r.failure_reason] = failure_reasons.get(r.failure_reason, 0) + 1

        return BatchJobUnderstandingResult(
            total_jobs=len(jobs),
            successful=successful,
            failed=failed,
            skipped=skipped,
            retried=retried,
            failure_reasons=failure_reasons,
            results=results,
        )

    def _call_llm(self, prompt: str, max_tokens: int | None = None) -> str:
        """Call the local LLM and return the response text.

        Uses the Ollama-compatible HTTP API at the configured endpoint.
        Sends a POST request with the prompt and returns the model's output.

        Args:
            max_tokens: Per-call override of ``self.max_tokens`` (used by the
                bounded truncation retry in ``analyze_job``). Defaults to
                ``self.max_tokens`` when omitted.

        Raises:
            TruncatedResponseError: when the API's own ``finish_reason`` says
                generation stopped at the token limit rather than naturally.
        """
        # requests is already imported at the top of this module
        url = f"{self.endpoint}/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        effective_max_tokens = self.max_tokens if max_tokens is None else max_tokens

        # When thinking is disabled, prepend a system message containing
        # /no_think.  This is the reliable way to suppress Qwen3's (and
        # compatible models') extended chain-of-thought phase, verified to
        # produce zero reasoning_tokens and immediate structured output.
        # Sending enable_thinking=false in the payload is NOT sufficient on
        # its own with current LM Studio builds.
        if not self.thinking_enabled:
            messages: list[dict] = [
                {"role": "system", "content": "/no_think"},
                {"role": "user", "content": prompt},
            ]
        else:
            messages = [{"role": "user", "content": prompt}]

        payload: dict = {
            "model": self.model_name,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": effective_max_tokens,
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices", [])
            if not choices or not isinstance(choices, list):
                return str(data.get("output", ""))
            choice = choices[0] if isinstance(choices, list) and len(choices) > 0 else {}
            message = choice.get("message", {}) if isinstance(choice, dict) else {}
            content = message.get("content", "") if isinstance(message, dict) else str(message)
            finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
            if finish_reason == "length":
                logger.warning(
                    "LLM response truncated at max_tokens=%d (finish_reason=length)",
                    effective_max_tokens,
                )
                raise TruncatedResponseError(str(content))
            return str(content)
        except requests.exceptions.ConnectionError as exc:
            logger.error("Connection error calling LLM at %s: %s", self.endpoint, exc)
            raise ConnectionError(f"Cannot connect to LLM at {self.endpoint}") from exc
        except requests.exceptions.Timeout as exc:
            logger.error(
                "Timeout calling LLM at %s after %ds: %s",
                self.endpoint,
                self.timeout_seconds,
                exc,
            )
            raise TimeoutError(f"LLM call to {self.endpoint} timed out") from exc
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            body = ""
            if exc.response is not None:
                try:
                    body = exc.response.json()
                except Exception:
                    body = exc.response.text[:500]
            logger.error(
                "HTTP %s error calling LLM at %s: %s | body: %s",
                status,
                self.endpoint,
                exc,
                body,
            )
            raise RuntimeError(
                f"HTTP {status} error from LLM at {self.endpoint}: {body}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            logger.error("Network error calling LLM at %s: %s", self.endpoint, exc)
            raise RuntimeError(f"Failed to call LLM: {exc}") from exc

    def complete(self, prompt: str, max_tokens: int | None = None) -> str:
        """Public single-call completion primitive — see ``LLMProvider.complete``.

        A thin public wrapper around ``_call_llm``: preserves the exact same
        truncation detection (raises ``TruncatedResponseError`` when the
        API's own ``finish_reason`` is ``"length"``) and transport-error
        translation (``ConnectionError``/``TimeoutError``/``RuntimeError``)
        that callers of the former private ``_call_llm`` already depended on.
        Callers that want retry-on-truncation (like ``analyze_job``) still
        implement that themselves on top of this primitive.
        """
        return self._call_llm(prompt, max_tokens=max_tokens)

    def _parse_response(self, response_text: str) -> JobUnderstanding:
        """Parse the LLM's text response into a structured JobUnderstanding.

        Raises ``MalformedResponseError`` (reason="malformed_json" or
        "validation_error") instead of silently returning an empty
        ``JobUnderstanding`` — a parse failure must never be indistinguishable
        from a genuine analysis in the caller's eyes (see
        ``JobUnderstandingResult.failure_reason``).
        """
        # Strip markdown fences if present
        cleaned = response_text.strip()
        if cleaned.startswith("```json") and cleaned.endswith("```"):
            cleaned = cleaned[7:-3]
        elif cleaned.startswith("```") and cleaned.endswith("```"):
            cleaned = cleaned[3:-3]

        # Try to parse as JSON
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "Malformed LLM response: %s", response_text[:200]
            )
            raise MalformedResponseError(
                f"LLM response is not valid JSON: {exc}", reason="malformed_json"
            ) from exc

        # Validate using Pydantic. A malformed shape raises pydantic.ValidationError,
        # which subclasses ValueError, so it's covered below alongside other
        # invalid-data-type/missing-field failures.
        try:
            return JobUnderstanding(**data)
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "Invalid data type or missing field in LLM response: %s", str(exc)
            )
            raise MalformedResponseError(
                f"LLM response JSON does not match schema: {exc}", reason="validation_error"
            ) from exc
