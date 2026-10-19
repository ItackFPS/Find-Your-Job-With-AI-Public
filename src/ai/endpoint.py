"""Local-first guard: job text is sent only to an LLM server on this machine.

Every component that posts job or profile text to an OpenAI-compatible endpoint
(``LocalLLMProvider``, the discovery query expander) calls
:func:`require_local_endpoint` first. A non-loopback address — another machine,
a cloud API — is refused with :class:`RemoteLLMRefused` instead of being used
silently; the AI stages already treat a provider failure as "run without AI"
and record the error in the run's stats.

A deliberately remote server (a GPU box on your own network) needs an explicit
opt-in: ``FYJ_ALLOW_REMOTE_LLM=1`` in the environment.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

ALLOW_REMOTE_ENV = "FYJ_ALLOW_REMOTE_LLM"


class RemoteLLMRefused(ValueError):
    """The configured LLM endpoint is not on this machine and remote use was not opted into."""


def is_loopback_endpoint(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "::1") or host == "127.0.0.1" or host.startswith("127.")


def require_local_endpoint(url: str) -> None:
    """Raise :class:`RemoteLLMRefused` unless ``url`` is loopback or remote use is opted into."""
    if is_loopback_endpoint(url) or os.environ.get(ALLOW_REMOTE_ENV, "").strip() == "1":
        return
    host = urlsplit(url).hostname if "://" in url else url
    raise RemoteLLMRefused(
        f"LLM endpoint {host!r} is not on this machine: job text is only sent to a local server "
        f"(localhost / 127.0.0.1 / ::1). Point the endpoint at LM Studio or Ollama here, or set "
        f"{ALLOW_REMOTE_ENV}=1 to allow a server you control elsewhere."
    )
