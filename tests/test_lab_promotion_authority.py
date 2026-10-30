"""Who may promote, and on what evidence (ADR 0011, "Promotion authority").

* the approval path recomputes the evidence: a verdict, fingerprint or dataset forged in ``lab.db`` (which
  the lab and its agent may write) is refused, and so is a dataset kept in the lab's own bundle store;
* evidence that is not independently validated needs a recorded acknowledgement;
* the supported command refuses to run where no human is typing (agent shells, the guarded lab process)
  and has no flag that confirms on its own;
* ``isolation-check`` tells whether an OS account can write production state, read its secrets or reach
  the API (which writes production state for whoever it serves).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from src.evaluation.dataset import load_dataset, write_dataset
from src.evaluation.models import StrategyRef
from src.lab.bundle import create_bundle
from src.lab.experiment import run_experiment
from src.lab import isolation
from src.lab.isolation import PROBE_SUFFIX, check_isolation
from src.lab.promotion import PromotionError, approve, current_pointer, strategy_change
from src.lab.store import LabStore

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """Production runs a harmful keyword; the fix is a bundle; a real experiment says it is an improvement."""
    tmp = tmp_path_factory.mktemp("authority")
    cfg = tmp / "config"
    cfg.mkdir()
    for name in ("filters.yaml", "scoring.yaml"):
        shutil.copy(ROOT / "config" / name, cfg / name)
    data = yaml.safe_load((cfg / "filters.yaml").read_text(encoding="utf-8"))
    wanted = list(data["seniority_exclusions"]["senior"])
    data["seniority_exclusions"]["senior"] = [*wanted, "junior"]
    (cfg / "filters.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8")
    store = tmp / "store"
    fix_hash, fix = create_bundle(cfg, {"filters.yaml": {"seniority_exclusions": {"senior": wanted}}}, store)
    noop_hash, noop = create_bundle(cfg, {}, store)
    ds = load_dataset(DS)
    base = StrategyRef(config_dir=str(cfg))
    return {"tmp": tmp, "cfg": cfg, "store": store, "ds": ds, "fix": (fix_hash, fix), "noop": (noop_hash, noop),
            "fix_result": run_experiment(ds, base, fix), "noop_result": run_experiment(ds, base, noop)}


@pytest.fixture()
def env(world, tmp_path):
    lab = LabStore(tmp_path / "lab.db")
    jobs = tmp_path / "jobs.db"
    sqlite3.connect(jobs).close()
    yield {**world, "lab": lab, "jobs": jobs, "here": tmp_path}
    lab.close()


def _record(env, bundle, result, dataset=None):
    """What the lab (or anything that can write lab.db) records: an experiment for a candidate."""
    lab = env["lab"]
    digest, path = bundle
    cid = lab.add_candidate(lab.add_hypothesis("h"), digest, str(path), {})
    manifest = (dataset or env["ds"]).manifest.model_dump()
    return lab.add_experiment(cid, manifest, result)


def _approve(env, eid, dataset=None, **kw):
    return approve(env["lab"], env["jobs"], eid, dataset=dataset or env["ds"], approved_by="reviewer",
                   reason="fixes junior rejections", config_dir=env["cfg"], **kw)


def test_approval_recomputes_the_evidence_and_records_who_and_why(env):
    eid = _record(env, env["fix"], env["fix_result"])
    pid = _approve(env, eid)
    ptr = current_pointer(env["jobs"])
    assert ptr["pointer_id"] == pid and ptr["bundle_hash"] == env["fix"][0]
    ev = json.loads(ptr["approval_evidence"])
    assert ev["verdict"] == "improvement" and ev["promotion_evidence"] == "independently_supported"
    assert ev["acknowledged_exploratory"] is False and ev["experiment_id"] == eid
    assert ev["dataset"]["labels_sha256"] == env["ds"].manifest.labels_sha256 and ev["dataset"]["path"] == str(DS)
    assert ev["baseline_fingerprint"] == env["fix_result"]["baseline_fingerprint"]
    assert set(ev["approver"]) == {"name", "os_account", "host"} and ev["approver"]["name"] == "reviewer"
    assert ev["reason"] == "fixes junior rejections" and ev["bundle_hash"] == env["fix"][0]


def test_the_approver_sees_and_records_the_change_itself_not_the_lab_s_description_of_it(env, monkeypatch):
    lab = env["lab"]
    digest, path = env["fix"]
    # the lab (or a proposer) may describe the change however it likes; the approver is shown the bundle diff
    cid = lab.add_candidate(lab.add_hypothesis("only fixes a typo"), digest, str(path), {"filters.yaml": {}})
    eid = lab.add_experiment(cid, env["ds"].manifest.model_dump(), env["fix_result"])
    shown = {}
    approve(env["lab"], env["jobs"], eid, dataset=env["ds"], approved_by="angel", reason="r", config_dir=env["cfg"],
            confirm=lambda ev: shown.update(ev) or True)
    removed = [line for line in shown["change"] if line.startswith("-") and not line.startswith("---")]
    assert removed == ["-  - junior"] and not [ln for ln in shown["change"] if ln.startswith("+ ")]
    assert "--- production/filters.yaml" in shown["change"] and "+++ bundle/filters.yaml" in shown["change"]
    assert json.loads(current_pointer(env["jobs"])["approval_evidence"])["change"] == shown["change"]

    from src.lab.__main__ import _approval_summary
    assert "    -  - junior" in _approval_summary(shown).splitlines()


def test_the_change_shown_to_the_approver_cannot_hide_lines_from_a_terminal(env, tmp_path):
    words = ["a\u202eb", "c\u200bd"]     # bidi override, zero-width space: printable to YAML, not to a reader
    digest, path = create_bundle(env["cfg"], {"filters.yaml": {"seniority_exclusions": {"senior": words}}},
                                 tmp_path / "s")
    change = "\n".join(strategy_change(env["cfg"], path))
    assert "\u202e" not in change and "\u200b" not in change
    assert "a\\u202eb" in change and "c\\u200bd" in change


def test_a_forged_eligible_verdict_is_not_evidence(env):
    forged = {**env["noop_result"], "verdict": "improvement", "eligible_for_approval": True,
              "promotion_evidence": "independently_supported", "independently_validated": True}
    eid = _record(env, env["noop"], forged)
    with pytest.raises(PromotionError, match="does not match a fresh replay"):
        _approve(env, eid)
    assert current_pointer(env["jobs"]) is None
    assert env["lab"]._conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_an_honestly_ineligible_experiment_cannot_be_approved(env):
    eid = _record(env, env["noop"], env["noop_result"])          # insufficient_evidence, as recorded
    with pytest.raises(PromotionError, match="not eligible"):
        _approve(env, eid, accept_exploratory=True)
    assert current_pointer(env["jobs"]) is None


def test_forged_fingerprints_or_a_different_dataset_are_refused(env, tmp_path):
    eid = _record(env, env["fix"], {**env["fix_result"], "baseline_fingerprint": "f" * 64})
    with pytest.raises(PromotionError, match="different baseline or candidate"):
        _approve(env, eid)
    # an experiment recorded on other labels cannot borrow this dataset's evidence
    other = env["ds"].manifest.model_copy(update={"labels_sha256": "0" * 64})
    cid = env["lab"].add_candidate(env["lab"].add_hypothesis("h2"), env["fix"][0], str(env["fix"][1]), {})
    eid2 = env["lab"].add_experiment(cid, other.model_dump(), env["fix_result"])
    with pytest.raises(PromotionError, match="is not the one"):
        _approve(env, eid2)
    assert current_pointer(env["jobs"]) is None


def test_production_changed_since_the_experiment_is_refused(env, tmp_path):
    eid = _record(env, env["fix"], env["fix_result"])
    other_profile = tmp_path / "profile.yaml"
    other_profile.write_bytes((DS / "profile.yaml").read_bytes() + b"\n# edited since\n")
    with pytest.raises(PromotionError, match="re-run it"):
        _approve(env, eid, profile_path=str(other_profile))
    assert current_pointer(env["jobs"]) is None


def test_a_dataset_in_the_lab_bundle_store_is_not_human_evidence(env):
    inside = env["store"] / "planted-dataset"
    if not inside.exists():
        shutil.copytree(DS, inside)
    ds = load_dataset(inside)
    eid = _record(env, env["fix"], env["fix_result"], dataset=ds)
    with pytest.raises(PromotionError, match="writable bundle store"):
        _approve(env, eid, dataset=ds)


def _assisted_copy(dest: Path):
    """The same jobs and labels as the fixture, every label recorded as assistant-assisted."""
    src = load_dataset(DS)
    m = src.manifest
    write_dataset(dest, dataset_id=m.dataset_id + "-assisted", version=m.version, kind=m.kind, created_at=m.created_at,
                  as_of=m.as_of, jobs=src.job_dicts,
                  labels=[lab.model_copy(update={"label_source": "human_assisted"}) for lab in src.labels.values()],
                  profile_yaml=(DS / "profile.yaml").read_bytes(), notes=m.notes)
    return load_dataset(dest)


def test_exploratory_evidence_needs_a_recorded_acknowledgement(env):
    ds = _assisted_copy(env["here"] / "assisted")
    res = run_experiment(ds, StrategyRef(config_dir=str(env["cfg"])), env["fix"][1])
    assert res["eligible_for_approval"] and res["promotion_evidence"] == "exploratory_only"
    eid = _record(env, env["fix"], res, dataset=ds)
    with pytest.raises(PromotionError, match="not independently validated"):
        _approve(env, eid, dataset=ds)
    assert current_pointer(env["jobs"]) is None
    _approve(env, eid, dataset=ds, accept_exploratory=True)
    ev = json.loads(current_pointer(env["jobs"])["approval_evidence"])
    assert ev["acknowledged_exploratory"] is True and ev["promotion_evidence"] == "exploratory_only"
    assert ev["evidence_basis"] == "not_independent"


def test_approve_then_rollback_is_append_only_and_traceable(env):
    from src.lab.promotion import rollback

    eid = _record(env, env["fix"], env["fix_result"])
    p1 = _approve(env, eid)
    cur = current_pointer(env["jobs"])
    assert cur["action"] == "approve" and cur["approved_by"] == "reviewer"
    p2 = rollback(env["jobs"], p1, approved_by="reviewer", reason="re-check", config_dir=env["cfg"])
    now = current_pointer(env["jobs"])
    assert now["pointer_id"] == p2 and now["bundle_hash"] == cur["bundle_hash"]
    assert json.loads(now["approval_evidence"])["to_pointer_id"] == p1
    with sqlite3.connect(env["jobs"]) as c, pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM active_strategy")
    with pytest.raises(PromotionError, match="unknown pointer"):
        rollback(env["jobs"], "nope", approved_by="a", reason="r")
    with sqlite3.connect(env["lab"].path) as c, pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE approvals SET approved_by = 'x'")


def test_approval_is_atomic_not_repeatable_and_never_creates_a_database(env):
    eid = _record(env, env["fix"], env["fix_result"])
    typo = env["here"] / "typo.db"
    with pytest.raises(PromotionError, match="not found"):
        approve(env["lab"], typo, eid, dataset=env["ds"], approved_by="me", reason="r", config_dir=env["cfg"])
    assert not typo.exists()
    assert env["lab"]._conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0     # nothing half-recorded
    _approve(env, eid)
    with pytest.raises(PromotionError, match="already approved"):
        _approve(env, eid)


def test_the_cli_shows_the_limitations_of_the_recomputed_evidence(env, monkeypatch, capsys):
    import src.lab.__main__ as cli

    ds = _assisted_copy(env["here"] / "assisted")
    res = run_experiment(ds, StrategyRef(config_dir=str(env["cfg"])), env["fix"][1])
    eid = _record(env, env["fix"], {**res, "comparison": {**res["comparison"], "limitations": []}}, dataset=ds)
    monkeypatch.setattr(cli, "_not_a_present_human", lambda: None)
    monkeypatch.setattr(cli.secrets, "token_hex", lambda _n: "c0ffee")
    monkeypatch.setattr("builtins.input", lambda _prompt: "c0ffee")
    argv = ["--lab-db", str(env["here"] / "lab.db"), "--jobs-db", str(env["jobs"]), "--config-dir", str(env["cfg"]),
            "approve", eid, "--dataset", str(ds.path), "--by", "reviewer", "--reason", "r"]
    assert cli.main(argv) == 1 and "--accept-exploratory-evidence" in capsys.readouterr().err
    assert cli.main([*argv, "--accept-exploratory-evidence"]) == 0
    captured = capsys.readouterr()
    # shown from the fresh replay, although the stored record was stripped of them
    assert "limitation: labels by provenance {'human_assisted'" in captured.err
    assert "EXPLORATORY ONLY" in captured.out and "NOT independently validated" in captured.out


def test_nothing_is_recorded_when_the_human_does_not_confirm(env):
    eid = _record(env, env["fix"], env["fix_result"])
    shown = []
    with pytest.raises(PromotionError, match="not confirmed"):
        _approve(env, eid, confirm=lambda ev: shown.append(ev) or False)
    assert shown and shown[0]["verdict"] == "improvement" and current_pointer(env["jobs"]) is None


# -- the supported command, as an agent's shell would run it -----------------------------------

def _cli(env, *args, extra_env=None):
    environ = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "FYJ_LAB_AGENT", "CODEX_SANDBOX",
                                                                 "CODEX_SANDBOX_NETWORK_DISABLED")}
    environ.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "src.lab", "--lab-db", str(env["here"] / "lab.db"),
                           "--jobs-db", str(env["jobs"]), "--config-dir", str(env["cfg"]), *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=120, env=environ, stdin=subprocess.DEVNULL)


@pytest.mark.parametrize("extra_env, why", [({}, "not an interactive terminal"),
                                            ({"FYJ_LAB_AGENT": "1"}, "automated-agent environment"),
                                            ({"CLAUDECODE": "1"}, "automated-agent environment")])
def test_an_agent_shell_cannot_promote_through_the_cli(env, extra_env, why):
    eid = _record(env, env["fix"], env["fix_result"])
    for args in (["approve", eid, "--dataset", str(DS), "--by", "agent", "--reason", "x"],
                 ["deactivate", "--by", "agent", "--reason", "x"],
                 ["rollback", "ptr-x", "--by", "agent", "--reason", "x"]):
        out = _cli(env, *args, extra_env=extra_env)
        assert out.returncode == 2 and why in out.stderr, out.stderr
    assert current_pointer(env["jobs"]) is None


def test_no_flag_confirms_on_its_own(env):
    eid = _record(env, env["fix"], env["fix_result"])
    out = _cli(env, "approve", eid, "--dataset", str(DS), "--by", "a", "--reason", "x", "--yes")
    assert out.returncode == 2 and "--yes was removed" in out.stderr
    assert current_pointer(env["jobs"]) is None


def test_a_human_at_a_terminal_confirms_by_typing_the_code_shown(env, monkeypatch, capsys):
    import src.lab.__main__ as cli

    eid = _record(env, env["fix"], env["fix_result"])
    argv = ["--lab-db", str(env["here"] / "lab.db"), "--jobs-db", str(env["jobs"]), "--config-dir", str(env["cfg"]),
            "approve", eid, "--dataset", str(DS), "--by", "reviewer", "--reason", "r"]
    monkeypatch.setattr(cli, "_not_a_present_human", lambda: None)       # stands for a real terminal
    monkeypatch.setattr(cli.secrets, "token_hex", lambda _n: "c0ffee")
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert cli.main(argv) == 1 and "not confirmed" in capsys.readouterr().err
    assert current_pointer(env["jobs"]) is None
    monkeypatch.setattr("builtins.input", lambda _prompt: "c0ffee")
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "INDEPENDENTLY SUPPORTED" in out and "re-verified now: improvement" in out
    assert current_pointer(env["jobs"])["experiment_id"] == eid


# -- isolation-check ---------------------------------------------------------------------------

@pytest.fixture()
def ports(monkeypatch):
    """Network probe answers per port, without touching the real loopback (which may run the API)."""
    answers: dict[int, str] = {}
    monkeypatch.setattr(isolation, "probe_port", lambda port, **_: answers.get(port, "not_listening"))
    return answers


def _production_tree(root: Path) -> dict[str, Path]:
    (root / "config").mkdir(parents=True)
    (root / "config" / "filters.yaml").write_text("a: 1\n")
    (root / "src").mkdir()
    (root / "data").mkdir()
    sqlite3.connect(root / "data" / "jobs.db").close()
    (root / ".env").write_text("SECRET=x\n")
    return {"jobs": root / "data" / "jobs.db", "config": root / "config"}


def _content(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_isolation_check_reports_what_this_account_can_write_and_changes_nothing(tmp_path, ports):
    t = _production_tree(tmp_path)
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("rules\n")
    before = _content(tmp_path)
    report = check_isolation(tmp_path, t["jobs"], t["config"])
    assert report["os_enforced"] is False
    assert {Path(p).name for p in report["writable"]} >= {"jobs.db", "config", "filters.yaml", "src", ".env"}
    # the checkout's root (a planted module shadows the real one), .git (hooks), every top-level file
    # (agent instructions), and data/ itself (a planted jobs.db-wal is replayed into jobs.db)
    assert {Path(p).name for p in report["writable"]} >= {tmp_path.name, ".git", "AGENTS.md", "data"}
    assert [Path(p).name for p in report["readable_secrets"]] == [".env"]
    assert _content(tmp_path) == before and not list(tmp_path.rglob(f"*{PROBE_SUFFIX}"))


def test_a_files_only_check_never_claims_isolation(tmp_path):
    t = _production_tree(tmp_path)
    for p in (t["jobs"], t["config"] / "filters.yaml", tmp_path / ".env"):
        p.chmod(0o444)
    try:
        report = check_isolation(tmp_path, t["jobs"], t["config"], probe_dirs=False)
        assert report["writable"] == [] and report["os_enforced"] is False
    finally:
        for p in (t["jobs"], t["config"] / "filters.yaml", tmp_path / ".env"):
            p.chmod(0o644)


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="POSIX permission bits (Windows needs ACLs; root ignores them)")
def test_an_account_that_cannot_write_production_is_reported_isolated(tmp_path, ports):
    t = _production_tree(tmp_path)
    files = [t["jobs"], t["config"] / "filters.yaml"]
    dirs = [t["config"], tmp_path / "src", t["jobs"].parent, tmp_path]
    ports[8000] = "refused"
    for p in files:
        p.chmod(0o444)
    (tmp_path / ".env").chmod(0o000)
    for d in dirs:
        d.chmod(0o555)
    try:
        report = check_isolation(tmp_path, t["jobs"], t["config"])
        assert report["os_enforced"] is True and report["writable"] == []
    finally:
        for d in dirs:
            d.chmod(0o755)
        for p in [*files, tmp_path / ".env"]:
            p.chmod(0o644)


@pytest.mark.skipif(os.name != "nt", reason="NTFS ACLs")
def test_a_directory_the_account_may_not_write_is_reported_promptly(tmp_path):
    """tempfile.mkstemp retries a PermissionError for as long as os.access() — which ignores ACLs — says the
    directory is writable, up to TMP_MAX (2**31) times on Windows: the probe used to hang there."""
    denied = tmp_path / "denied"
    denied.mkdir()
    sid = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["icacls", str(denied), "/deny", f"*{sid}:(WD,AD)"], capture_output=True, check=True)
    try:
        code = "import sys; from src.lab.isolation import _dir_writable; print(_dir_writable(sys.argv[1]))"
        run = subprocess.run([sys.executable, "-c", code, str(denied)], cwd=ROOT, capture_output=True, text=True,
                             timeout=60)
        assert run.stdout.strip() == "False", run.stderr
    finally:
        subprocess.run(["icacls", str(denied), "/reset"], capture_output=True)


def test_isolation_check_and_require_isolation_fail_closed(tmp_path, monkeypatch, capsys, ports):
    import src.lab.__main__ as cli

    t = _production_tree(tmp_path)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    lab_db = t["jobs"].parent / "lab" / "lab.db"
    common = ["--lab-db", str(lab_db), "--jobs-db", str(t["jobs"]), "--config-dir", str(t["config"])]
    assert cli.main([*common, "isolation-check"]) == 3
    assert json.loads(capsys.readouterr().out)["os_enforced"] is False
    assert cli.main([*common, "agent", str(DS), "--require-isolation"]) == 2
    assert "refusing to run" in capsys.readouterr().err
    assert not lab_db.exists()                                           # refused before touching anything


@pytest.mark.parametrize("answers, ok", [
    ({8000: "refused"}, True),                                           # the API turns this account away
    ({}, False),                                                         # nothing on 8000: cannot verify
    ({8000: "reachable"}, False),                                        # the API would write for it
    ({8000: "unverified"}, False),
    ({8000: "refused", 5173: "reachable"}, False),                       # Vite would relay as the human
    ({8000: "refused", 5678: "reachable"}, False),                       # so might n8n
])
def test_the_api_must_refuse_this_account_and_no_relay_may_answer(ports, answers, ok):
    ports.update(answers)
    results, problems = isolation.check_network()
    assert (not problems) is ok and set(results) == {"8000", "5173", "5678"}


def test_readable_secrets_break_isolation(tmp_path, ports):
    t = _production_tree(tmp_path)
    (tmp_path / ".env.example").write_text("SECRET=\n")
    (t["config"] / "notifications.local.yaml").write_text("token: x\n")
    found = {Path(p).name for p in check_isolation(tmp_path, t["jobs"], t["config"])["readable_secrets"]}
    assert found == {".env", "notifications.local.yaml"}                  # the example is no secret


def test_the_instance_lock_is_a_secret_and_the_live_port_is_probed(tmp_path, monkeypatch, capsys, ports):
    # the launcher picks a free API port at each start (update plan §E4): the check must probe that one too
    import src.lab.__main__ as cli
    from src.launcher.core import Settings

    t = _production_tree(tmp_path)
    settings = Settings(root=tmp_path)
    assert settings.instance_file == tmp_path / isolation.INSTANCE_LOCK         # the lab keeps its own copy
    assert settings.port_file == tmp_path / isolation.LIVE_PORT_FILE
    assert isolation.live_api_ports(tmp_path) == ()
    settings.state_dir.mkdir(parents=True)
    settings.instance_file.write_text('{"instance_token": "x"}')
    settings.port_file.write_text('{"pid": 1, "port": 51234}')
    assert isolation.live_api_ports(tmp_path) == (51234,)
    found = {Path(p).name for p in check_isolation(tmp_path, t["jobs"], t["config"])["readable_secrets"]}
    assert "instance.json" in found
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    ports.update({8000: "refused", 51234: "reachable"})                         # the live API serves this account
    common = ["--lab-db", str(t["jobs"].parent / "lab" / "lab.db"), "--jobs-db", str(t["jobs"]),
              "--config-dir", str(t["config"])]
    assert cli.main([*common, "isolation-check", "--api-port", "8000"]) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["network"]["51234"] == "reachable"


def test_the_isolated_agent_keeps_its_lab_database_in_data_lab(tmp_path, monkeypatch, capsys, ports):
    import src.lab.__main__ as cli

    t = _production_tree(tmp_path)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    beside_jobs = t["jobs"].parent / "lab.db"                             # where the isolated account cannot write
    assert cli.main(["--lab-db", str(beside_jobs), "--jobs-db", str(t["jobs"]), "--config-dir", str(t["config"]),
                     "agent", str(DS), "--require-isolation"]) == 2
    assert "must be under" in capsys.readouterr().err and not beside_jobs.exists()


def test_a_place_the_human_executes_from_must_not_be_writable(tmp_path, ports, monkeypatch):
    monkeypatch.setattr(isolation, "in_administrators", lambda: False)
    t = _production_tree(tmp_path / "repo")
    tools = tmp_path / "tools"                       # e.g. a PATH directory on a drive any account may write
    tools.mkdir()
    (tools / "git.exe").write_bytes(b"")
    ports[8000] = "refused"
    report = check_isolation(tmp_path / "repo", t["jobs"], t["config"],
                             exec_paths=(tools, tools / "git.exe", tools / "missing" / "task.exe"))
    flagged = [pr for pr in report["problems"] if "executed by the human" in pr]
    assert len(flagged) == 3 and report["os_enforced"] is False


def test_an_administrator_account_is_never_isolated(tmp_path, ports, monkeypatch):
    monkeypatch.setattr(isolation, "in_administrators", lambda: True)
    t = _production_tree(tmp_path)
    assert any("Administrators" in pr for pr in check_isolation(tmp_path, t["jobs"], t["config"])["problems"])


def _link(link: Path, target: Path) -> None:
    if os.name == "nt":                                         # a junction: no privilege, may dangle
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_a_link_leading_outside_the_repository_to_a_writable_or_creatable_place_is_found(tmp_path, ports):
    repo = tmp_path / "repo"
    t = _production_tree(repo)
    (repo / ".claude" / "skills").mkdir(parents=True)
    (repo / ".agents" / "skills" / "inside").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _link(repo / ".claude" / "skills" / "present", outside)
    _link(repo / ".claude" / "skills" / "dangling", tmp_path / "old-checkout" / "skills" / "x")   # target missing
    _link(repo / ".claude" / "skills" / "inside", repo / ".agents" / "skills" / "inside")         # stays inside
    found = {link.name: target for link, target in isolation.outside_links(repo)}
    assert set(found) == {"present", "dangling"}
    problems = check_isolation(repo, t["jobs"], t["config"])["problems"]
    assert any("present" in pr and "writable" in pr for pr in problems)
    assert any("dangling" in pr and "missing and creatable" in pr for pr in problems)


def test_a_port_nobody_listens_on_is_not_reported_as_refusing():
    with socket.socket() as s:                                           # bound, never listening
        s.bind(("127.0.0.1", 0))
        assert isolation.probe_port(s.getsockname()[1], timeout=5) in ("not_listening", "unverified")


def test_a_relay_listening_on_ipv6_loopback_only_is_found():
    """The Vite dev server (Node binds "localhost" to ::1) is invisible on 127.0.0.1."""
    import http.server
    import threading

    class V6Server(http.server.HTTPServer):
        address_family = socket.AF_INET6

    try:
        server = V6Server(("::1", 0), http.server.BaseHTTPRequestHandler)
    except OSError:
        pytest.skip("no IPv6 loopback")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert isolation.probe_port(server.server_address[1], timeout=5) == "reachable"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(os.name != "nt", reason="NTFS ACLs")
def test_the_documented_windows_acls_isolate_an_account_and_the_check_terminates(tmp_path):
    """Apply scripts/lab_isolation.ps1 for THIS account to a scratch tree, then run the real check as it.

    This process stands in for the agent. Its account owns the scratch tree, so the audit must (and does)
    flag the owner's implicit WRITE_DAC: the real agent account must own nothing outside data/lab. The
    check runs in a subprocess with a timeout: tempfile.mkstemp used to spin forever on an ACL-denied
    directory."""
    repo = tmp_path / "repo"
    t = _production_tree(repo)
    (t["config"] / "notifications.local.yaml").write_text("token: x\n")
    sid = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
                         capture_output=True, text=True, check=True).stdout.strip()
    script = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
              str(ROOT / "scripts" / "lab_isolation.ps1"), "-Repo", str(repo), "-Account", sid,
              "-UnsafeCurrentAccountForTests"]
    console = {"capture_output": True, "text": True, "errors": "replace"}      # localized principal names
    try:
        applied = subprocess.run([*script, "-Mode", "Apply"], timeout=120, **console)
        assert applied.returncode == 0, applied.stdout + applied.stderr
        code = ("import json, sys; from src.lab import isolation; from src.lab.isolation import check_isolation as c; "
                "isolation.in_administrators = lambda: False; r = sys.argv[1]; "   # the stand-in is an admin
                "print(json.dumps(c(r, r + '/data/jobs.db', r + '/config', api_ports=(), relay_ports=())))")
        run = subprocess.run([sys.executable, "-c", code, str(repo)], cwd=ROOT, capture_output=True, text=True,
                             timeout=120)
        report = json.loads(run.stdout)
        assert report["os_enforced"] is True, report
        assert report["writable"] == [] and report["readable_secrets"] == []
        # what the account still needs: read the code and configuration, write its own lab directory
        assert (t["config"] / "filters.yaml").read_text() == "a: 1\n"
        (repo / "data" / "lab" / "lab.db").write_bytes(b"")
        for attempt in (lambda: t["jobs"].write_bytes(b"x"), lambda: t["jobs"].unlink(),
                        lambda: t["jobs"].rename(t["jobs"].with_name("moved.db")),
                        lambda: (repo / "data" / "new.db").write_bytes(b""),
                        lambda: (repo / "src" / "m.py").write_text("x = 1\n"),
                        lambda: (repo / ".env").read_text()):
            with pytest.raises(PermissionError):
                attempt()
        audit = subprocess.run([*script, "-Mode", "Audit"], timeout=300, **console)
        assert audit.returncode == 1 and "implicit WRITE_DAC" in audit.stdout, audit.stdout
        assert "isolation-check --exec-path" in audit.stdout             # the command to run as the account
        assert "could not write" in audit.stdout                          # the stand-in may not write data\
    finally:
        subprocess.run(["icacls", str(tmp_path), "/reset", "/T", "/C", "/Q"], capture_output=True)


# -- what production shows once an approval is live ---------------------------------------------

def test_a_live_strategy_says_what_evidence_it_was_approved_on(env):
    from src.decisions.active_strategy import resolve_active_strategy
    from src.lab.promotion import deactivate, rollback

    ds = _assisted_copy(env["here"] / "assisted")
    res = run_experiment(ds, StrategyRef(config_dir=str(env["cfg"])), env["fix"][1])
    p1 = _approve(env, _record(env, env["fix"], res, dataset=ds), dataset=ds, accept_exploratory=True)
    live = resolve_active_strategy(env["jobs"], env["cfg"]).to_dict()
    assert live["status"] == "active" and live["approval_basis"] == "exploratory_only"
    deactivate(env["jobs"], approved_by="reviewer", reason="back")
    assert resolve_active_strategy(env["jobs"], env["cfg"]).to_dict()["approval_basis"] is None
    rollback(env["jobs"], p1, approved_by="reviewer", reason="again", config_dir=env["cfg"])
    # a rollback keeps the basis of the approval it restores: it never upgrades it
    assert resolve_active_strategy(env["jobs"], env["cfg"]).to_dict()["approval_basis"] == "exploratory_only"


def test_an_independently_supported_approval_and_a_legacy_pointer_are_told_apart(env):
    from src.decisions.active_strategy import resolve_active_strategy
    from src.lab.promotion import _append

    _approve(env, _record(env, env["fix"], env["fix_result"]))
    assert resolve_active_strategy(env["jobs"], env["cfg"]).to_dict()["approval_basis"] == "independently_supported"
    # a pointer written without approval evidence (before it existed): never presented as validated
    _append(env["jobs"], ("ptr-old", "2999-01-01T00:00:00+00:00", env["fix"][0], str(env["fix"][1]), "a", "c", "e",
                          "r", "approve"))
    assert resolve_active_strategy(env["jobs"], env["cfg"]).to_dict()["approval_basis"] == "unrecorded"


@pytest.mark.skipif(os.name != "nt", reason="NTFS ACLs")
def test_the_acl_setup_is_idempotent_and_removable(tmp_path):
    """Apply twice, then Remove (the rollback) for another principal (Guests stands in for fyj-lab, so the
    account running the script is not the denied one, as in the real setup): only its own ACEs come and go."""
    repo = tmp_path / "repo"
    t = _production_tree(repo)
    other = tmp_path / "other-root"                                       # stands in for -DenyRoot D:\
    other.mkdir()
    guests = "S-1-5-32-546"
    script = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
              str(ROOT / "scripts" / "lab_isolation.ps1"), "-Repo", str(repo), "-Account", guests,
              "-DenyRoot", str(other)]
    console = {"capture_output": True, "text": True, "errors": "replace", "timeout": 120}

    def explicit_aces(path: Path) -> int:
        ps = (f"@((Get-Acl -LiteralPath '{path}').GetAccessRules($true, $false, "
              f"[System.Security.Principal.SecurityIdentifier]) | Where-Object {{ $_.IdentityReference.Value -eq '{guests}' }}).Count")
        return int(subprocess.run(["powershell", "-NoProfile", "-Command", ps], **console).stdout.strip())

    def acl(path: Path) -> str:
        return subprocess.run(["icacls", str(path)], **console).stdout.replace(str(path), "<path>")

    paths = (repo, repo / "data" / "lab", repo / ".env", other)
    (repo / "data" / "lab").mkdir()
    before = [acl(p) for p in paths]
    try:
        first = subprocess.run([*script, "-Mode", "Apply"], **console)
        again = subprocess.run([*script, "-Mode", "Apply"], **console)
        assert first.returncode == 0 and again.returncode == 0 and "done:" not in again.stdout, again.stdout
        assert [explicit_aces(p) for p in paths] == [1, 1, 1, 1]
        removed = subprocess.run([*script, "-Mode", "Remove"], **console)
        assert removed.returncode == 0, removed.stdout + removed.stderr
        assert [explicit_aces(p) for p in paths] == [0, 0, 0, 0]
        assert [acl(p) for p in paths] == before                      # nobody else's ACEs changed
        assert t["jobs"].read_bytes() == b""                               # nothing else touched
    finally:
        subprocess.run(["icacls", str(tmp_path), "/reset", "/T", "/C", "/Q"], capture_output=True)


@pytest.mark.skipif(os.name != "nt", reason="NTFS ACLs")
@pytest.mark.parametrize("principal", ["S-1-5-32-545", "S-1-5-11", "S-1-1-0", "Users", "self"])
def test_the_acl_setup_never_denies_a_principal_the_human_is_or_belongs_to(tmp_path, principal):
    """A deny for Users, Authenticated Users or Everyone (or the human) would lock the human out of the
    repository and, with -DenyRoot D:\\, of a whole drive: refused before any change, Plan included."""
    repo = tmp_path / "repo"
    _production_tree(repo)
    if principal == "self":
        principal = subprocess.run(["powershell", "-NoProfile", "-Command",
                                    "[System.Security.Principal.WindowsIdentity]::GetCurrent().Name"],
                                   capture_output=True, text=True, check=True).stdout.strip()
    elif principal == "Users":                                 # by name, whatever the Windows language
        principal = subprocess.run(["powershell", "-NoProfile", "-Command",
                                    "(New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-545'))"
                                    ".Translate([System.Security.Principal.NTAccount]).Value"],
                                   capture_output=True, text=True, check=True, encoding="utf-8",
                                   errors="replace").stdout.strip()
    before = subprocess.run(["icacls", str(repo)], capture_output=True, text=True, errors="replace").stdout
    for mode in ("Plan", "Apply", "Remove"):
        run = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                              str(ROOT / "scripts" / "lab_isolation.ps1"), "-Repo", str(repo), "-Account", principal,
                              "-DenyRoot", str(tmp_path), "-Mode", mode],
                             capture_output=True, text=True, errors="replace", timeout=120)
        assert run.returncode != 0 and "refusing" in (run.stdout + run.stderr), (mode, run.stdout, run.stderr)
    assert not (repo / "data" / "lab").exists()
    assert subprocess.run(["icacls", str(repo)], capture_output=True, text=True, errors="replace").stdout == before


@pytest.mark.skipif(os.name != "nt", reason="Windows services and tasks")
def test_the_audit_lists_what_runs_as_system_too(tmp_path):
    """A writable service binary, or a file a SYSTEM task runs, hands the agent SYSTEM, which beats every
    ACL: the Audit's isolation-check command must probe those as well as what runs as the human."""
    repo = tmp_path / "repo"
    _production_tree(repo)
    run = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(ROOT / "scripts" / "lab_isolation.ps1"), "-Repo", str(repo),
                          "-Account", "S-1-5-32-546", "-Mode", "Audit"],
                         capture_output=True, text=True, errors="replace", timeout=300)
    command = next(line for line in run.stdout.splitlines() if "isolation-check" in line)
    assert "--exec-paths-file data/isolation-exec-paths.txt" in command   # hundreds of paths: no command line
    listed = (repo / "data" / "isolation-exec-paths.txt").read_text(encoding="utf-8-sig").lower().splitlines()
    assert any(p.endswith("svchost.exe") for p in listed)            # every machine has svchost services
    assert all(len(p) > 3 and p[1:3] == ":\\" for p in listed)        # absolute paths only


def _ps(command: str) -> str:
    return subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True,
                          errors="replace", check=True).stdout.strip()


@pytest.mark.skipif(os.name != "nt", reason="git configuration read by the Windows script")
def test_the_audit_lists_a_git_hooks_folder_outside_the_checkout_even_with_spaces(tmp_path):
    """core.hooksPath naming a folder outside the checkout runs whatever appears there at your next commit
    or push: on this machine it named a deleted old checkout that any account could recreate."""
    repo = tmp_path / "repo"
    _production_tree(repo)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    hooks = tmp_path / "old checkout" / ".githooks"                                  # missing, with a space
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)], check=True)
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(ROOT / "scripts" / "lab_isolation.ps1"), "-Repo", str(repo),
                    "-Account", "S-1-5-32-546", "-Mode", "Audit"],
                   capture_output=True, text=True, errors="replace", timeout=300)
    listed = (repo / "data" / "isolation-exec-paths.txt").read_text(encoding="utf-8-sig").lower().splitlines()
    assert str(hooks).lower() in listed


@pytest.mark.skipif(os.name != "nt", reason="NTFS ACLs")
def test_several_deny_roots_reach_the_script_and_the_system_drive_is_refused(tmp_path):
    """`powershell -File` hands `-DenyRoot a,b` over as ONE string and rejects a repeated -DenyRoot: roots
    are separated by ';' (and ',' before a drive letter). Denying the system drive would stop the agent
    account from logging on, so it is refused."""
    repo = tmp_path / "repo"
    _production_tree(repo)
    roots = [tmp_path / "root a", tmp_path / "root-b"]
    for r in roots:
        r.mkdir()
    base = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(ROOT / "scripts" / "lab_isolation.ps1"), "-Repo", str(repo), "-Account", "S-1-5-32-546"]
    console = {"capture_output": True, "text": True, "errors": "replace", "timeout": 120}
    for spelled in (f"{roots[0]};{roots[1]}", f"{roots[0]},{roots[1]}"):
        plan = subprocess.run([*base, "-DenyRoot", spelled], **console)
        assert plan.returncode == 0, plan.stdout + plan.stderr
        assert all(f"on {r}" in plan.stdout for r in roots), plan.stdout
    refused = subprocess.run([*base, "-DenyRoot", f"{roots[0]};{os.environ['SystemDrive']}\\"], **console)
    assert refused.returncode != 0 and "refusing" in refused.stdout + refused.stderr, refused.stdout


@pytest.mark.skipif(os.name != "nt", reason="NTFS ACLs")
def test_inheritance_disabled_is_a_problem_only_when_it_lets_the_account_write(tmp_path):
    """pytest makes .pytest_cache owner-only with inheritance disabled: the deny does not reach it, but nor
    can the account write it. A protected folder that grants the account write is the real hole."""
    repo = tmp_path / "repo"
    _production_tree(repo)
    owner_only, open_ = repo / ".pytest_cache", repo / "open"
    owner_only.mkdir()
    open_.mkdir()
    _ps(f"icacls '{owner_only}' /inheritance:r /grant:r '*{_ps('[Security.Principal.WindowsIdentity]::GetCurrent().User.Value')}:(OI)(CI)F' | Out-Null")
    _ps(f"icacls '{open_}' /inheritance:d /grant '*S-1-5-32-546:(OI)(CI)M' | Out-Null")
    try:
        audit = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                str(ROOT / "scripts" / "lab_isolation.ps1"), "-Repo", str(repo),
                                "-Account", "S-1-5-32-546", "-Mode", "Audit"],
                               capture_output=True, text=True, errors="replace", timeout=300)
        flagged = [line for line in audit.stdout.splitlines() if "inheritance disabled" in line]
        assert any(line.endswith(str(open_)) for line in flagged), audit.stdout
        assert not any(str(owner_only) in line for line in flagged), audit.stdout
    finally:
        subprocess.run(["icacls", str(tmp_path), "/reset", "/T", "/C", "/Q"], capture_output=True)


@pytest.mark.skipif(os.name != "nt", reason="NTFS ACLs")
def test_a_missing_program_is_creatable_where_only_folders_may_be_created(tmp_path):
    """A drive root commonly lets every user create FOLDERS but not files; the creator owns the folder it
    makes, so a missing program two levels down is still plantable (e.g. a scheduled task's deleted target)."""
    folders_only = tmp_path / "root"
    folders_only.mkdir()
    me = _ps("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value")
    _ps("$i = Get-Item -LiteralPath '" + str(folders_only) + "'; $a = $i.GetAccessControl('Access'); "
        "$a.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule("
        f"(New-Object Security.Principal.SecurityIdentifier('{me}')), 'WriteData', 'None', 'None', 'Deny'))); "
        "$i.SetAccessControl($a)")
    try:
        code = ("import sys; from pathlib import Path; from src.lab.isolation import _creatable as c; r = Path(sys.argv[1]); "
                "print(c(r / 'tool.exe'), c(r / 'Vendor' / 'tool.exe'))")
        run = subprocess.run([sys.executable, "-c", code, str(folders_only)], cwd=ROOT, capture_output=True,
                             text=True, timeout=60)
        assert run.stdout.split() == ["False", "True"], run.stdout + run.stderr
        assert list(folders_only.iterdir()) == []                         # the probe folder is gone
    finally:
        subprocess.run(["icacls", str(tmp_path), "/reset", "/T", "/C", "/Q"], capture_output=True)


def test_exec_paths_can_come_from_a_file_and_an_unreadable_list_fails_closed(tmp_path, monkeypatch, capsys, ports):
    import src.lab.__main__ as cli

    monkeypatch.setattr(isolation, "in_administrators", lambda: False)
    t = _production_tree(tmp_path / "repo")
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path / "repo")
    tools = tmp_path / "tools"
    tools.mkdir()
    listing = tmp_path / "exec.txt"
    listing.write_text(f"﻿{tools}\n\n", encoding="utf-8")
    common = ["--lab-db", str(t["jobs"].parent / "lab" / "lab.db"), "--jobs-db", str(t["jobs"]),
              "--config-dir", str(t["config"]), "isolation-check"]
    assert cli.main([*common, "--exec-paths-file", str(listing)]) == 3
    assert any(str(tools) in pr for pr in json.loads(capsys.readouterr().out)["problems"])
    assert cli.main([*common, "--exec-paths-file", str(tmp_path / "missing.txt")]) == 2
    assert "--exec-paths-file" in capsys.readouterr().err
