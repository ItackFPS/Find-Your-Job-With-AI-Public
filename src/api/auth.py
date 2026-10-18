"""Minimal, opt-in API key auth for n8n-facing endpoints.

Disabled by default — matches this project's local-first design (see
src/api/app.py's module docstring). Unless the N8N_CALLER_API_KEY
environment variable is set, require_n8n_api_key() is a complete no-op and
every existing local/dev call keeps working exactly as before. Once set, a
caller must send the SAME value in the X-N8N-API-Key header.

Naming note: this is deliberately NOT called N8N_API_KEY. This project's
.env already has a pre-existing, unrelated N8N_HOST/N8N_PORT/N8N_PROTOCOL/
N8N_API_KEY block — that N8N_API_KEY is n8n's OWN server/public-API key
(a credential for calling n8n's API), the opposite direction from what this
module checks (a credential n8n must present TO this backend). Reusing that
exact name would have silently "enabled" this feature using an unrelated
placeholder value already sitting in .env — confirmed to actually happen
during implementation. N8N_CALLER_API_KEY names the concept unambiguously
and cannot collide with it.

Scope is intentionally narrow: this dependency is applied per-endpoint
(never at the router level) only to the specific n8n-facing routes named in
the 2026-09-12 notification-system audit — GET /api/jobs/digest/weekly,
GET /api/jobs/digest/weekly-combined, GET /api/pipeline/runs/{run_id}/jobs/n8n.
The rest of the API stays unauthenticated. Making auth app-wide is a
separate, deliberate future decision, not a side effect of this change.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException


def require_n8n_api_key(x_n8n_api_key: str = Header(default="")) -> None:
    """FastAPI dependency.

    No-op when N8N_CALLER_API_KEY is unset or empty (auth disabled — the
    current, unchanged local-first default). Otherwise requires the
    X-N8N-API-Key request header to match it exactly, compared in constant
    time (this is a shared secret, not a public identifier, so a timing
    side-channel on the comparison itself should not leak it).
    """
    expected = os.environ.get("N8N_CALLER_API_KEY", "")
    if not expected:
        return
    if not hmac.compare_digest(x_n8n_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-N8N-API-Key header")
