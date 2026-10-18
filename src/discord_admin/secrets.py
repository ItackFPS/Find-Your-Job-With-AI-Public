"""The one place in the whole system that ever holds a raw webhook URL.

Writes reuse the project's existing secret convention exactly: a single
root-level .env file loaded via python-dotenv at process startup (see
src/main.py's ``load_dotenv(...)`` call) — no new secret-storage mechanism
is introduced. ``store_webhook_secret`` never returns the value it wrote;
callers (src.discord_admin.reconciler, and transitively any MCP tool) only
ever see the env var *name* and a redacted preview, never the secret
itself — enforced here at the API boundary, not by caller discipline.
"""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"


def redact(value: str, keep_last: int = 4) -> str:
    """Last-N-characters preview for logging/confirmation — never the
    full secret."""
    if len(value) <= keep_last:
        return "*" * len(value)
    return f"...{value[-keep_last:]}"


def store_webhook_secret(env_var_name: str, url: str, env_path: Path | None = None) -> None:
    """Atomically set ``env_var_name=url`` in .env, and update the current
    process's environment immediately so a running NotificationEngine (which
    re-reads config fresh on every call) can use it without a restart.

    Preserves every other line in .env untouched (including comments);
    replaces the line for ``env_var_name`` if present, else appends it.
    """
    import os

    path = env_path or _ENV_PATH
    lines: list[str] = []
    if path.exists():
        with path.open(encoding="utf-8") as f:
            lines = f.read().splitlines()

    prefix = f"{env_var_name}="
    new_line = f"{env_var_name}={url}"
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        tmp_path.replace(path)
    except BaseException:
        # The temp file holds every secret in .env: never leave it behind
        # (issue #69; it is also gitignored).
        tmp_path.unlink(missing_ok=True)
        raise

    os.environ[env_var_name] = url
