"""Bring an installed ``config/`` folder up to the installed version (ADR 0028, M1).

The installer writes ``config/*.yaml`` only when missing, so that an upgrade never overwrites a
setting. Without this module an upgraded application would run new code on old configuration: no
daily search, no France Travail web source. The installer therefore also ships the current
defaults in ``config_defaults/`` (next to ``config/``), and the first start of each version does
this, file by file:

* a file still byte-identical to a default of an earlier release is replaced by the new default;
* a file the user edited is kept. Only the entries a new version cannot work without are added to
  it (``ENTRIES_TO_ADD``), as text, so the user's comments and values survive;
* a file removed from the defaults is archived if unedited, and left alone otherwise.

Every file changed is copied to ``data/backups/config-<UTC time>/`` first (that folder is covered
by "Delete all"). The work is done once per version, so an entry the user later deletes on purpose
does not come back. A folder with no ``config_defaults/`` (a development checkout) is never touched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULTS_DIRNAME = "config_defaults"
STATE_FILENAME = "upgrade_state.json"

# SHA-256 of each configuration file as an earlier release installed it, with line endings
# normalised to LF. When a release ships, add its files' hashes here.
KNOWN_RELEASED_DEFAULTS: dict[str, frozenset[str]] = {
    name: frozenset({digest}) for name, digest in {
        # v1.0.0
        "decisions.yaml": "3dc64814fa2b529c6d0456685e2ca003aa1b7fb95cdd5630fd72c6185af6066c",
        "disagreement.yaml": "8f293755b12cbda92b1a577b28405947c7c369f4b3e0cd0669eff83b88079d5f",
        "feedback.yaml": "245eeb05bcecc6b9c626b888c232260e2f532dc0f2cc53138e105a04aeb47330",
        "filters.yaml": "17faefbb79a31d8fd0a6d67519b982435126dc04602f4f89610e3ae9c3db8439",
        "linkedin.yaml": "5c49af37673a7868b79d2f7d5c60e89ee6e6aa1683b2432a148dbe613d9fb8b1",
        "n8n_weekly_digest.json": "297ec79d6cecc7f1d340006691f6c787023cca696bbad98545d8177c4718c801",
        "notifications.yaml": "8a7314838ae1dfbb9dedc26daba18cbddf4c93df5baca92eccb72a351a59e1a9",
        "personalization.yaml": "ba7be6bc647fa8b236799afe76b3340d026da2ac19b887a18d1f33e5fa05935b",
        "profile.yaml": "9ea0dc82901ed55f3538a12d15b32feb957580865f694a87e9934d3d45439235",
        "retention.yaml": "79ce7e36b970ece9e973b37640379d1d4c09f58408661d4132bfde6c8d3de3ee",
        "schedules.yaml": "3f39066dd09c42128e8f14361a7e053f83d0e3abd16437555d23f29cb2451dce",
        "scoring.yaml": "af8f8fad8834d530bdedf941cb6aa5e830f78dffcaa0b11c5298bfb7700e36b6",
        "semantic_config.yaml": "a4c201a0ff9376665ffd5c718997fa7d76409a4aab8ce5ba3aa267527536e096",
        "sources.yaml": "159dc0c7c888ef9fc9e4e2e8ece804c53286af31e4758657b6b9fc74dca80225",
    }.items()
}


@dataclass(frozen=True)
class EntryToAdd:
    """One list entry an edited file must have: ``<list_key>: [{<id_key>: <id_value>, ...}]``."""

    list_key: str
    id_key: str
    id_value: str


ENTRIES_TO_ADD: dict[str, tuple[EntryToAdd, ...]] = {
    "schedules.yaml": (EntryToAdd("schedules", "id", "pipeline_daily"),),
    "sources.yaml": (EntryToAdd("sources", "name", "france-travail-web"),),
}


@dataclass
class ConfigMigrationReport:
    """What one migration did. ``failed`` files are retried at the next start."""

    version: str
    replaced: list[str] = field(default_factory=list)
    entries_added: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    backup_dir: str | None = None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".upgrade-tmp")
    try:
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _entry_block(default_text: str, entry: EntryToAdd) -> tuple[list[str], int] | None:
    """The lines of ``entry`` in the default file and their indent; comments after it excluded."""
    lines = default_text.splitlines()
    start_re = re.compile(rf"^(\s*)- {re.escape(entry.id_key)}:\s*[\"']?{re.escape(entry.id_value)}[\"']?\s*(#.*)?$")
    for i, line in enumerate(lines):
        match = start_re.match(line)
        if not match:
            continue
        indent = len(match.group(1))
        end = i + 1
        while end < len(lines):
            stripped = lines[end].strip()
            line_indent = len(lines[end]) - len(lines[end].lstrip())
            if stripped and line_indent <= indent:
                break
            end += 1
        block = lines[i:end]
        while block and not block[-1].strip():
            block.pop()
        return block, indent
    return None


def _add_entry(text: str, default_text: str, entry: EntryToAdd) -> str | None:
    """``text`` with the default's entry added as the first list item, or None if that cannot
    be done safely. Every existing line stays as it was."""
    found = _entry_block(default_text, entry)
    if found is None:
        return None
    block, block_indent = found
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    key_re = re.compile(rf"^{re.escape(entry.list_key)}:\s*(#.*)?$")
    key_at = next((i for i, line in enumerate(lines) if key_re.match(line)), None)
    if key_at is None:
        return None
    first_item = next((ln for ln in lines[key_at + 1:] if ln.lstrip().startswith("- ")), None)
    indent = (len(first_item) - len(first_item.lstrip())) if first_item else block_indent
    shifted = [(" " * indent + ln[block_indent:]) if ln.strip() else "" for ln in block]
    result = lines[:key_at + 1] + shifted + [""] + lines[key_at + 1:]
    return newline.join(result) + (newline if text.endswith(("\n", "\r")) else "")


def _with_required_entries(name: str, text: str, default_text: str) -> tuple[str, list[str]] | None:
    """``text`` plus each missing required entry, and what was added; None if it cannot be done."""
    try:
        before = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(before, dict):
        return None
    added: list[str] = []
    current = text
    expected: dict[str, Any] = dict(before)
    for entry in ENTRIES_TO_ADD.get(name, ()):
        items = expected.get(entry.list_key) or []
        if not isinstance(items, list):
            return None
        if any(isinstance(it, dict) and it.get(entry.id_key) == entry.id_value for it in items):
            continue
        changed = _add_entry(current, default_text, entry)
        if changed is None:
            return None
        current = changed
        default_items = (yaml.safe_load(default_text) or {}).get(entry.list_key) or []
        new_item = next(it for it in default_items if it.get(entry.id_key) == entry.id_value)
        expected[entry.list_key] = [new_item] + items
        added.append(f"{name}:{entry.id_value}")
    # The edit is textual: accept it only if it parses to exactly the user's data plus the entries.
    try:
        if added and yaml.safe_load(current) != expected:
            return None
    except yaml.YAMLError:
        return None
    return current, added


class _Backup:
    """Copies files into one timestamped folder, created on first use."""

    def __init__(self, root: Path, now: datetime) -> None:
        self.folder = root / f"config-{now.strftime('%Y%m%dT%H%M%SZ')}"

    def keep(self, path: Path) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, self.folder / path.name)


def state_path(data_dir: Path) -> Path:
    return data_dir / STATE_FILENAME


def _already_done(data_dir: Path, version: str) -> bool:
    try:
        state = json.loads(state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(state, dict) and state.get("config_migrated_for") == version


def _record(data_dir: Path, report: ConfigMigrationReport) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    state = {"config_migrated_for": report.version, "last_config_migration": asdict(report)}
    _atomic_write(state_path(data_dir), json.dumps(state, indent=2).encode("utf-8"))


def _migrate_file(name: str, path: Path, default_path: Path | None, backup: _Backup,
                  report: ConfigMigrationReport) -> None:
    previous = KNOWN_RELEASED_DEFAULTS.get(name, frozenset())
    if default_path is None:  # no longer shipped
        if _digest(path.read_bytes()) in previous:
            backup.keep(path)
            path.unlink()
            report.archived.append(name)
        return
    if not path.exists():
        _atomic_write(path, default_path.read_bytes())
        report.replaced.append(name)
        return
    current = path.read_bytes()
    default = default_path.read_bytes()
    digest = _digest(current)
    if digest == _digest(default):
        return
    if digest in previous:
        backup.keep(path)
        _atomic_write(path, default)
        report.replaced.append(name)
        return
    if name not in ENTRIES_TO_ADD:
        report.kept.append(name)
        return
    changed = _with_required_entries(name, current.decode("utf-8"), default.decode("utf-8"))
    if changed is None:
        logger.warning("Upgrade: could not add the new entries to the edited %s; it is unchanged.", name)
        report.kept.append(name)
        return
    text, added = changed
    if added:
        backup.keep(path)
        _atomic_write(path, text.encode("utf-8"))
        report.entries_added.extend(added)
    report.kept.append(name)


def migrate_config(config_dir: Path, data_dir: Path, version: str,
                   now: datetime | None = None) -> ConfigMigrationReport | None:
    """Migrate ``config_dir`` once for ``version``. Returns the report, or None if nothing ran.

    Never raises: a start must not fail because of this. A file that could not be handled is
    named in ``failed``, and the whole migration runs again at the next start."""
    defaults_dir = config_dir.parent / DEFAULTS_DIRNAME
    if not defaults_dir.is_dir() or _already_done(data_dir, version):
        return None
    report = ConfigMigrationReport(version=version)
    backup = _Backup(data_dir / "backups", now or datetime.now(UTC))
    try:
        shipped = {p.name: p for p in defaults_dir.iterdir() if p.is_file()}
    except OSError as exc:
        logger.warning("Upgrade: cannot read %s (%s); retrying at the next start.", defaults_dir, exc)
        return None
    retired = {n for n in KNOWN_RELEASED_DEFAULTS if n not in shipped and (config_dir / n).is_file()}
    for name in sorted(set(shipped) | retired):
        try:
            _migrate_file(name, config_dir / name, shipped.get(name), backup, report)
        except Exception as exc:  # any cause: a start must not fail because of this file
            logger.warning("Upgrade: %s not migrated (%s); retrying at the next start.", name, exc)
            report.failed.append(name)
    if backup.folder.is_dir():
        report.backup_dir = str(backup.folder)
    logger.info(
        "Upgrade to %s: configuration replaced=%s entries_added=%s archived=%s kept=%s failed=%s",
        version, report.replaced, report.entries_added, report.archived, report.kept, report.failed,
    )
    if not report.failed:
        try:
            _record(data_dir, report)
        except OSError as exc:
            logger.warning("Upgrade: could not record the migration (%s); it runs again next start.", exc)
    return report
