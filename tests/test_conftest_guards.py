"""The real-config guard in tests/conftest.py (issue: leaked registry writes).

Uses pytest's `pytester` so the guarded "leaky" tests run in an isolated
sub-session whose config dir is a tmp copy — the real config/ is never touched.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_CONFTEST = Path(__file__).with_name("conftest.py")


def _sub_session(pytester: pytest.Pytester, tmp_path: Path) -> Path:
    """A sub-session using this repo's conftest, pointed at a fake repo root."""
    fake_root = tmp_path / "fake_repo"
    (fake_root / "config").mkdir(parents=True)
    (fake_root / "data").mkdir()
    (fake_root / "config" / "notifications.yaml").write_text("tracked: true\n", encoding="utf-8")
    source = _CONFTEST.read_text(encoding="utf-8").replace(
        "_REPO_ROOT = Path(__file__).resolve().parents[1]",
        f"_REPO_ROOT = Path({str(fake_root)!r})",
    )
    pytester.makeconftest(source)
    return fake_root


def test_new_config_file_fails_the_test_and_is_removed(pytester, tmp_path):
    root = _sub_session(pytester, tmp_path)
    pytester.makepyfile(textwrap.dedent(f"""
        from pathlib import Path

        def test_leaks():
            Path({str(root / "config" / "notifications.local.yaml")!r}).write_text("x: 1\\n")
    """))
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*real config/ directory*notifications.local.yaml*"])
    result.stdout.fnmatch_lines(["*Live threads*"])
    assert not (root / "config" / "notifications.local.yaml").exists()


def test_modified_existing_config_file_fails_but_is_not_removed(pytester, tmp_path):
    root = _sub_session(pytester, tmp_path)
    target = root / "config" / "notifications.yaml"
    pytester.makepyfile(textwrap.dedent(f"""
        from pathlib import Path

        def test_modifies():
            Path({str(target)!r}).write_text("tracked: false\\n")
    """))
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    assert target.read_text(encoding="utf-8") == "tracked: false\n"


def test_leak_from_a_background_thread_names_the_thread(pytester, tmp_path):
    root = _sub_session(pytester, tmp_path)
    pytester.makepyfile(textwrap.dedent(f"""
        import threading, time
        from pathlib import Path

        def test_leaks_in_thread():
            done = threading.Event()
            def worker():
                Path({str(root.joinpath("config", "profile.local.yaml"))!r}).write_text("x: 1\\n")
                done.set()
                time.sleep(2)
            threading.Thread(target=worker, name="leaky-worker", daemon=True).start()
            done.wait(5)
    """))
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*leaky-worker*"])


def test_environment_changes_do_not_leak_into_later_tests(pytester, tmp_path):
    # Code that writes os.environ directly once leaked a value that changed
    # later tests (issue #46).
    _sub_session(pytester, tmp_path)
    pytester.makepyfile(textwrap.dedent("""
        import os

        def test_a_sets_a_webhook():
            os.environ["DISCORD_LEAK_TEST_WEBHOOK_URL"] = "https://discord.com/api/webhooks/1/t"

        def test_b_does_not_see_it():
            assert "DISCORD_LEAK_TEST_WEBHOOK_URL" not in os.environ
    """))
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)


def test_environment_values_changed_or_removed_are_restored(pytester, tmp_path):
    _sub_session(pytester, tmp_path)
    pytester.makepyfile(textwrap.dedent("""
        import os

        os.environ["FYJ_GUARD_KEEP"] = "original"
        os.environ["FYJ_GUARD_DROP"] = "present"

        def test_a_mutates():
            os.environ["FYJ_GUARD_KEEP"] = "changed"
            del os.environ["FYJ_GUARD_DROP"]

        def test_b_sees_the_originals():
            assert os.environ["FYJ_GUARD_KEEP"] == "original"
            assert os.environ["FYJ_GUARD_DROP"] == "present"
    """))
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
