"""Guided install of Ollama on Windows (plan §C5, §E4, F8.2): official installer only.

Flow: :func:`resolve_installer` sends a ``HEAD`` to Ollama's official download URL and
follows its redirects by hand, accepting only HTTPS hops to Ollama / GitHub release hosts;
the final ``Content-Length`` is the size shown to the user. The chain must pass through
``github.com/ollama/ollama/releases/download/<version>/OllamaSetup.exe`` (GitHub's asset
hosts serve every repository, so the release path is what proves the origin), and the
SHA-256 GitHub publishes for that release asset must exist: no published digest, no install.
:func:`install_worker` then runs only for a request whose ``confirm_size_bytes`` equals the
size it resolves again: it downloads the file under the data directory, checks size,
SHA-256 and the Authenticode signature (``Valid``, i.e. the chain verifies to a trusted
root, and a signer whose CN and O are exactly Ollama's publisher), and only then launches
the installer, whose own window asks the user to proceed. The downloaded file is deleted
when the installer exits, and leftovers are removed when the next install job starts.
Ollama already installed → refused.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from pydantic import BaseModel

from src.ai.runtime.detect import find_ollama
from src.ai.runtime.jobs import Job, JobFailed

logger = logging.getLogger(__name__)

OFFICIAL_URL = "https://ollama.com/download/OllamaSetup.exe"
RELEASE_API = "https://api.github.com/repos/ollama/ollama/releases/tags/{version}"
ALLOWED_HOSTS = frozenset({"ollama.com", "github.com", "api.github.com",
                           "objects.githubusercontent.com",
                           "release-assets.githubusercontent.com"})
RELEASE_PATH = re.compile(r"^/ollama/ollama/releases/download/(v[0-9][0-9A-Za-z.\-]*)/"
                          r"OllamaSetup\.exe$")
MAX_REDIRECTS = 5
TIMEOUT_S = (10.0, 60.0)
CHUNK = 1024 * 1024
# The publisher of Ollama's signed binaries, read (read-only) on 2026-10-25 from the installed
# C:/Users/<user>/AppData/Local/Programs/Ollama/ollama.exe with Get-AuthenticodeSignature:
# Status=Valid, Subject "CN=Ollama Inc., O=Ollama Inc., L=Toronto, S=Ontario, C=CA, ...",
# issued by "CN=DigiCert G5 CS ECC SHA384 2021 CA1, O="DigiCert, Inc.", C=US".
EXPECTED_PUBLISHER = {"CN": "Ollama Inc.", "O": "Ollama Inc."}
INSTALLER_GLOB = "OllamaSetup-*.exe"
_RDN = re.compile(r'(?:^|,)\s*([A-Za-z0-9.]+)=("(?:[^"]|"")*"|[^,]*)')

ERR_UNSUPPORTED = "unsupported_platform"
ERR_INSTALLED = "already_installed"
ERR_UNREACHABLE = "download_unreachable"
ERR_HTTP = "http_status"
ERR_REDIRECT = "redirect_not_allowed"
ERR_NOT_RELEASE = "not_an_ollama_release"
ERR_NO_HASH = "sha256_unpublished"
ERR_NO_SIZE = "size_unknown"
ERR_SIZE_MISMATCH = "size_mismatch"
ERR_HASH_MISMATCH = "sha256_mismatch"
ERR_SIGNATURE = "signature_invalid"
ERR_LAUNCH = "launch_failed"


class InstallerInfo(BaseModel):
    url: str  # the official URL the user consents to
    final_url_host: str
    size_bytes: int
    version: str  # from the github.com/ollama/ollama release-asset hop
    sha256: str  # published by GitHub for that release asset


def _session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False  # no proxy credentials or redirects from the environment
    return session


def _check_hop(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in ALLOWED_HOSTS:
        raise JobFailed(ERR_REDIRECT, host=parts.hostname)


def _follow(session: requests.Session, method: str, url: str,
            stream: bool = False) -> tuple[requests.Response, list[str]]:
    """Issue ``method`` and follow redirects by hand, every hop HTTPS on an allowed host."""
    hops = [url]
    for _ in range(MAX_REDIRECTS + 1):
        _check_hop(url)
        try:
            resp = session.request(method, url, allow_redirects=False, timeout=TIMEOUT_S,
                                   stream=stream)
        except requests.RequestException as exc:
            raise JobFailed(ERR_UNREACHABLE) from exc
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
            resp.close()
            url = urljoin(url, resp.headers["location"])
            hops.append(url)
            continue
        if resp.status_code != 200:
            resp.close()
            raise JobFailed(ERR_HTTP, http_status=resp.status_code)
        return resp, hops
    raise JobFailed(ERR_REDIRECT, host=urlsplit(url).hostname)


def _release_version(hops: list[str]) -> str | None:
    for hop in hops:
        parts = urlsplit(hop)
        if parts.hostname == "github.com" and (m := RELEASE_PATH.match(parts.path)):
            return m.group(1)
    return None


def _require_release(hops: list[str]) -> str:
    """The release version; refuse a chain that never passes through an ollama/ollama asset."""
    version = _release_version(hops)
    if version is None:
        raise JobFailed(ERR_NOT_RELEASE)
    return version


def _published_sha256(session: requests.Session, version: str, size: int) -> str | None:
    """The SHA-256 GitHub lists for ``OllamaSetup.exe`` in that release, if any."""
    try:
        resp, _ = _follow(session, "GET", RELEASE_API.format(version=version))
        with resp:
            data = json.loads(resp.content[:4 * 1024 * 1024])
    except (JobFailed, ValueError):
        return None
    for asset in data.get("assets", []) if isinstance(data, dict) else []:
        if not isinstance(asset, dict) or asset.get("name") != "OllamaSetup.exe":
            continue
        digest = asset.get("digest")
        if (asset.get("size") == size and isinstance(digest, str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
            return digest.split(":", 1)[1]
    return None


def resolve_installer(session: requests.Session | None = None) -> InstallerInfo:
    """HEAD the official URL: final size, release version and published SHA-256."""
    own = session is None
    session = session or _session()
    try:
        resp, hops = _follow(session, "HEAD", OFFICIAL_URL)
        resp.close()
        length = resp.headers.get("content-length", "")
        if not length.isdigit() or int(length) <= 0:
            raise JobFailed(ERR_NO_SIZE)
        size = int(length)
        version = _require_release(hops)
        sha256 = _published_sha256(session, version, size)
        if sha256 is None:
            raise JobFailed(ERR_NO_HASH, version=version)
        return InstallerInfo(url=OFFICIAL_URL, final_url_host=urlsplit(hops[-1]).hostname or "",
                             size_bytes=size, version=version, sha256=sha256)
    finally:
        if own:
            session.close()


def download(info: InstallerInfo, dest: Path, job: Job,
             session: requests.Session | None = None) -> str:
    """Stream the installer to ``dest``; check size and published hash. Returns the SHA-256."""
    own = session is None
    session = session or _session()
    digest = hashlib.sha256()
    done = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        resp, hops = _follow(session, "GET", info.url, stream=True)
        if _release_version(hops) != info.version:
            resp.close()
            raise JobFailed(ERR_NOT_RELEASE)
        with resp, part.open("wb") as out:
            for chunk in resp.iter_content(CHUNK):
                job.check_cancelled()
                if not chunk:
                    continue
                done += len(chunk)
                if done > info.size_bytes:
                    raise JobFailed(ERR_SIZE_MISMATCH, expected=info.size_bytes, actual=done)
                digest.update(chunk)
                out.write(chunk)
                job.update(completed_bytes=done)
        if done != info.size_bytes:
            raise JobFailed(ERR_SIZE_MISMATCH, expected=info.size_bytes, actual=done)
        sha = digest.hexdigest()
        if sha != info.sha256:
            raise JobFailed(ERR_HASH_MISMATCH)
        part.replace(dest)
        return sha
    except requests.RequestException as exc:
        raise JobFailed(ERR_UNREACHABLE) from exc
    finally:
        part.unlink(missing_ok=True)
        if own:
            session.close()


def authenticode(path: Path) -> tuple[str, str]:
    """``(status, signer subject)`` from PowerShell's ``Get-AuthenticodeSignature``."""
    script = ("$s = Get-AuthenticodeSignature -LiteralPath $env:FYJ_SIGNED_FILE; "
              "@{status = [string]$s.Status; subject = [string]$s.SignerCertificate.Subject}"
              " | ConvertTo-Json -Compress")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60, check=False, creationflags=flags,
            stdin=subprocess.DEVNULL, env={**os.environ, "FYJ_SIGNED_FILE": str(path)})
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return "Unknown", ""
    return str(data.get("status") or "Unknown"), str(data.get("subject") or "")


def subject_fields(subject: str) -> dict[str, str]:
    """``{"CN": ..., "O": ...}`` from an X.500 subject string (quoted values unquoted)."""
    fields: dict[str, str] = {}
    for key, value in _RDN.findall(subject):
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('""', '"')
        fields.setdefault(key.upper(), value)
    return fields


def is_ollama_publisher(status: str, subject: str) -> bool:
    fields = subject_fields(subject)
    return status == "Valid" and all(fields.get(k) == v for k, v in EXPECTED_PUBLISHER.items())


def cleanup_downloads(download_dir: Path) -> None:
    """Remove installers left by an earlier run (the installer was running, or the app quit)."""
    try:
        for leftover in download_dir.glob(INSTALLER_GLOB):
            leftover.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not clean %s", download_dir, exc_info=True)


def _delete_after_exit(proc: subprocess.Popen[bytes], path: Path) -> None:
    def wait_then_delete() -> None:
        try:
            proc.wait()
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:  # still locked: cleanup_downloads removes it next time
                logger.debug("Could not delete %s", path, exc_info=True)

    threading.Thread(target=wait_then_delete, daemon=True, name="ollama-installer-wait").start()


def launch(path: Path) -> None:
    """Start the installer with its own UI (the user confirms there); delete it once it exits."""
    try:
        proc = subprocess.Popen([str(path)], close_fds=True)  # noqa: S603 - verified installer
    except OSError as exc:
        raise JobFailed(ERR_LAUNCH) from exc
    _delete_after_exit(proc, path)


def install_worker(job: Job, confirm_size_bytes: int, download_dir: Path, *,
                   platform: str | None = None,
                   find: Callable[[], str | None] | None = None,
                   resolve: Callable[[], InstallerInfo] | None = None,
                   fetch: Callable[[InstallerInfo, Path, Job], str] | None = None,
                   verify: Callable[[Path], tuple[str, str]] | None = None,
                   start: Callable[[Path], None] | None = None) -> None:
    """The ``POST /api/ai/ollama/install`` job (dependencies injectable for tests)."""
    find, resolve = find or find_ollama, resolve or resolve_installer
    fetch, verify, start = fetch or download, verify or authenticode, start or launch
    if (platform or sys.platform) != "win32":
        raise JobFailed(ERR_UNSUPPORTED)
    if find() is not None:
        raise JobFailed(ERR_INSTALLED)
    cleanup_downloads(download_dir)
    job.update(phase="checking")
    info = resolve()
    job.update(total_bytes=info.size_bytes, version=info.version, sha256_published=True)
    if info.size_bytes != confirm_size_bytes:
        raise JobFailed(ERR_SIZE_MISMATCH, expected=confirm_size_bytes, actual=info.size_bytes)
    job.check_cancelled()
    job.update(phase="downloading", completed_bytes=0)
    dest = download_dir / f"OllamaSetup-{info.version}.exe"
    sha = fetch(info, dest, job)
    job.update(phase="verifying", sha256=sha)
    status, subject = verify(dest)
    job.update(signature_status=status, signer=subject)
    if not is_ollama_publisher(status, subject):
        dest.unlink(missing_ok=True)
        raise JobFailed(ERR_SIGNATURE, status=status, signer=subject)
    job.check_cancelled()
    job.update(phase="launching")
    start(dest)
    job.update(phase="launched", installer_path=str(dest))
