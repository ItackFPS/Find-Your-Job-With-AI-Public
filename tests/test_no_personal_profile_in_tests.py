"""Guard for issue #9: tests must not read the personal config/profile.yaml.

Use ``tests.fixtures.profiles.TEST_PROFILE_PATH`` instead. The only allowed
reader is the schema test that validates the shipped default.
"""

from __future__ import annotations

import re
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_ALLOWED = {
    "test_profile_schema_validation.py",
    "test_no_personal_profile_in_tests.py",
    "fixtures/profiles.py",  # defines the frozen path itself
}
_FORBIDDEN = [
    # Reading the file, not merely naming it (e.g. a .gitignore check).
    re.compile(
        r"""\b(?:open|load_profile|load_filter_config|Path)\(\s*["']config/profile(?:\.local)?\.yaml["']"""
    ),
    re.compile(r"""["']config["']\s*/\s*["']profile(?:\.local)?\.yaml["']"""),
    re.compile(r"""\bload_filter_config\(\s*\)"""),
]


def test_tests_do_not_read_the_personal_profile() -> None:
    offenders = []
    for path in sorted(_TESTS_DIR.rglob("*.py")):
        if path.relative_to(_TESTS_DIR).as_posix() in _ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if any(p.search(line) for p in _FORBIDDEN):
                offenders.append(f"{path.relative_to(_TESTS_DIR)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Tests must use tests.fixtures.profiles.TEST_PROFILE_PATH, not the personal "
        "config/profile.yaml:\n" + "\n".join(offenders)
    )
