"""CLI: ``python -m src.lab <hypothesis|candidate|run|agent|memory|approve|rollback|deactivate|status>``.

``--base-config active`` (the default) means the strategy production runs right now: the approved bundle
when ``active_strategy`` points at one the pipeline would use, else ``config/``
(``src/decisions/active_strategy.py``). Candidates built on an active bundle record their overrides
relative to ``config/``, so the pipeline can check them against the current configuration.

Every command except the human promotion commands (``approve`` / ``rollback`` / ``deactivate``) and
``isolation-check`` runs under the lab write guard (``src/lab/guard.py``): it can write only ``--lab-db``
and the bundle ``--store``, never ``jobs.db``, ``config/`` or the code, cannot start processes and cannot
reach the network.

The human commands are confirmed interactively (a one-time code typed back at a terminal) and refuse to
run inside the guard or with an automated-agent marker in the environment. That keeps an agent from
promoting through the supported command by habit or instruction-following; it is NOT a security
boundary (an agent can emulate a terminal or write ``jobs.db`` directly). The boundary is which OS
accounts can write ``jobs.db`` — ``isolation-check`` verifies it from the agent's account (ADR 0011)."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.decisions.active_strategy import resolve_active_strategy
from src.evaluation.dataset import DatasetError, load_dataset
from src.evaluation.models import StrategyRef
from src.lab.bundle import BundleError, create_bundle
from src.lab.experiment import run_experiment
from src.lab.guard import GuardViolation, guard_installed, install_write_guard, lab_writable_roots
from src.lab import sandbox
from src.lab.isolation import API_PORTS, RELAY_PORTS, check_isolation
from src.lab.promotion import PromotionError, approve, current_pointer, deactivate, rollback
from src.lab.store import LabStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORE = "data/lab/strategies"
ACTIVE = "active"
_HUMAN = ("approve", "rollback", "deactivate")
_UNGUARDED = (*_HUMAN, "isolation-check", "sandbox")
# set by automated agents' shells (Claude Code, Codex sandboxes) or by whoever launches one (FYJ_LAB_AGENT)
AGENT_ENV_MARKERS = ("FYJ_LAB_AGENT", "CLAUDECODE", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")


def _not_a_present_human() -> str | None:
    """Why this process cannot stand for a human at a terminal, or ``None``. Procedural, not a boundary."""
    if guard_installed():
        return "this process runs under the lab write guard (the lab agent's context)"
    if sandbox.in_app_container():
        return "this process runs in the lab's AppContainer (the lab agent's sandbox)"
    markers = [m for m in AGENT_ENV_MARKERS if os.environ.get(m)]
    if markers:
        return f"automated-agent environment ({', '.join(markers)}): run it yourself, in your own terminal"
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return "not an interactive terminal: confirmation must be typed by a human"
    return None


def _inside(path: str | Path, directory: Path) -> bool:
    return Path(os.path.realpath(directory)) in Path(os.path.realpath(path)).parents


def _confirm(summary: str) -> bool:
    """Show what is about to change and require a one-time code typed back (never accepted from arguments)."""
    code = secrets.token_hex(3)
    print(summary, flush=True)
    try:
        typed = input(f"Type {code} to confirm (anything else aborts): ")
    except EOFError:
        return False
    return typed.strip() == code


def _approval_summary(evidence: dict[str, Any]) -> str:
    ind = evidence.get("independent_evidence") or {}
    lines = [f"APPROVE {evidence['experiment_id']} -> bundle {evidence['bundle_hash'][:12]} becomes production's strategy",
             f"  re-verified now: {evidence['verdict']} ({evidence['explanation']})",
             f"  dataset {evidence['dataset']['dataset_id']} at {evidence['dataset']['path']} "
             f"(labels {evidence['dataset']['labels_sha256'][:12]}), label basis {evidence['evidence_basis']}",
             f"  evidence: {str(evidence['promotion_evidence']).replace('_', ' ').upper()}"
             f" - independent labels: {ind.get('why', 'n/a')}"]
    if evidence["acknowledged_exploratory"]:
        lines.append("  you are acknowledging that this is NOT independently validated")
    a = evidence["approver"]
    lines.append(f"  recorded as approved by {a['name']} (account {a['os_account']} on {a['host']}): {evidence['reason']}")
    return "\n".join(lines)


def _base_config(ns: argparse.Namespace) -> str:
    """``--base-config``, with ``active`` resolved to what production runs (read-only on ``jobs.db``)."""
    if ns.base_config != ACTIVE:
        return str(ns.base_config)
    active = resolve_active_strategy(ns.jobs_db, ns.config_dir)
    if active.uses_bundle and active.bundle_dir is not None:
        print(f"baseline: active strategy {active.to_dict()['bundle_hash'][:12]} ({active.to_dict()['pointer_id']})",
              file=sys.stderr)
        return str(active.bundle_dir)
    return str(ns.config_dir)


def _sandbox(ns: argparse.Namespace) -> int:
    """``sandbox grant|revoke|run`` (``src/lab/sandbox.py``): the AppContainer boundary, no administrator."""
    if not sandbox.supported():
        print("error: the AppContainer sandbox exists on Windows only (see ADR 0011 for the account setup)",
              file=sys.stderr)
        return 2
    lab_dir = (PROJECT_ROOT / ns.jobs_db).parent / "lab"
    extra = [Path(p).resolve() for p in ns.grant_read]
    try:
        sid = sandbox.container_sid()
        if ns.action in ("grant", "revoke"):
            if ns.action == "grant":
                lab_dir.mkdir(parents=True, exist_ok=True)
            plan = (sandbox.grant_plan if ns.action == "grant" else sandbox.revoke_plan)(PROJECT_ROOT, lab_dir, sid, extra)
            for cmd in plan:
                print(subprocess.list2cmdline(cmd))
            if not ns.apply:
                print(f"plan only ({len(plan)} commands for AppContainer {sandbox.DEFAULT_NAME}, SID {sid}): "
                      "add --apply to change the ACLs", file=sys.stderr)
                return 0
            sandbox.apply(plan)
            print(f"{ns.action}: done", file=sys.stderr)
            return 0
        command = list(ns.command[1:] if ns.command[:1] == ["--"] else ns.command)
        if not command:
            print("error: sandbox run needs a command after '--'", file=sys.stderr)
            return 2
        if command[0] == "python":
            command[0] = sys.executable
        return sandbox.run(command, PROJECT_ROOT, internet=ns.internet)
    except (sandbox.SandboxError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="python -m src.lab")
    ap.add_argument("--lab-db", default="data/lab.db")
    ap.add_argument("--jobs-db", default="data/jobs.db")
    ap.add_argument("--config-dir", default="config", help="production configuration directory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hypothesis")
    h.add_argument("statement")
    h.add_argument("--rationale", default="")
    c = sub.add_parser("candidate", help="create an immutable bundle from YAML overrides")
    c.add_argument("hypothesis_id")
    c.add_argument("--overrides", required=True, help="JSON: {file: {key: value}}")
    c.add_argument("--base-config", default=ACTIVE)
    c.add_argument("--store", default=DEFAULT_STORE)
    r = sub.add_parser("run", help="replay baseline vs candidate on a frozen dataset")
    r.add_argument("candidate_id")
    r.add_argument("dataset")
    r.add_argument("--base-config", default=ACTIVE)
    a = sub.add_parser("approve", help="HUMAN approval of an eligible experiment")
    a.add_argument("experiment_id")
    a.add_argument("--dataset", required=True, help="the experiment's dataset: its evidence is recomputed from it")
    a.add_argument("--profile", default=None, help="profile the experiment was replayed with, if the dataset pins none")
    a.add_argument("--accept-exploratory-evidence", action="store_true",
                   help="approve although the evidence is not independently validated (recorded)")
    a.add_argument("--by", required=True)
    a.add_argument("--reason", required=True)
    b = sub.add_parser("rollback", help="HUMAN: point production back at an earlier pointer's bundle")
    b.add_argument("pointer_id")
    b.add_argument("--by", required=True)
    b.add_argument("--reason", required=True)
    d = sub.add_parser("deactivate", help="HUMAN: return production to the configuration directory")
    d.add_argument("--by", required=True)
    d.add_argument("--reason", required=True)
    for human in (a, b, d):        # removed: a flag is exactly what an agent can pass; confirmation is typed
        human.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    i = sub.add_parser("isolation-check", help="can THIS OS account write production state? (exit 3 if it can)")
    sub.add_parser("status", help="the latest pointer and what the pipeline will actually run")
    g = sub.add_parser("agent", help="investigate a labelled dataset, test data-only hypotheses, recommend (never approves)")
    g.add_argument("dataset")
    g.add_argument("--base-config", default=ACTIVE)
    g.add_argument("--profile", default=None, help="profile to replay with when the dataset pins none")
    g.add_argument("--store", default=DEFAULT_STORE)
    g.add_argument("--max-hypotheses", type=int, default=5)
    g.add_argument("--no-experiments", action="store_true")
    g.add_argument("--json", action="store_true")
    g.add_argument("--require-isolation", action="store_true",
                   help="refuse to run unless this OS account cannot write production state (isolation-check)")
    for p in (i, g):
        p.add_argument("--api-port", type=int, action="append", default=None,
                       help=f"API port that must refuse this account (default {', '.join(map(str, API_PORTS))})")
        p.add_argument("--relay-port", type=int, action="append", default=None,
                       help=f"port that must not answer (default {', '.join(map(str, RELAY_PORTS))})")
        p.add_argument("--exec-path", action="append", default=[],
                       help="file or directory the human executes from (lab_isolation.ps1 -Mode Audit lists them)")
        p.add_argument("--exec-paths-file", action="append", default=[],
                       help="file listing --exec-path places, one per line (Audit writes "
                            "data/isolation-exec-paths.txt: too many for one command line)")
    s = sub.add_parser("sandbox", help="Windows AppContainer for the lab agent: grant / revoke its folders, run in it")
    s.add_argument("action", choices=("grant", "revoke", "run"))
    s.add_argument("--apply", action="store_true", help="grant/revoke: change the ACLs (default: print the plan)")
    s.add_argument("--grant-read", action="append", default=[],
                   help="grant/revoke: another folder the sandboxed command must read (e.g. an agent's install)")
    s.add_argument("--internet", action="store_true",
                   help="run: allow outbound internet (loopback stays blocked by the OS)")
    s.add_argument("command", nargs=argparse.REMAINDER,
                   help="run: the command after '--'; a leading 'python' means this interpreter")
    m = sub.add_parser("memory", help="list the lab memory with current status and staleness")
    m.add_argument("--kind", default=None, choices=("evidence", "hypothesis", "observation", "conclusion"))
    m.add_argument("--dataset", default=None, help="mark items stale against this dataset (and --base-config)")
    m.add_argument("--base-config", default=ACTIVE)
    m.add_argument("--profile", default=None)
    ns = ap.parse_args(argv)
    if ns.cmd in ("isolation-check", "agent"):
        api_ports, relay_ports = tuple(ns.api_port or API_PORTS), tuple(ns.relay_port or RELAY_PORTS)
        for listing in ns.exec_paths_file:
            try:
                ns.exec_path += [line.strip() for line in Path(listing).read_text(encoding="utf-8-sig").splitlines()
                                 if line.strip()]
            except OSError as exc:            # fail closed: a list that cannot be read checks nothing
                print(f"error: --exec-paths-file {listing}: {exc}", file=sys.stderr)
                return 2
    if ns.cmd == "sandbox":
        return _sandbox(ns)
    if ns.cmd == "isolation-check":
        report = check_isolation(PROJECT_ROOT, ns.jobs_db, ns.config_dir, lab_dir=Path(ns.lab_db).parent,
                                 api_ports=api_ports, relay_ports=relay_ports, exec_paths=tuple(ns.exec_path))
        print(json.dumps(report, indent=1))
        return 0 if report["os_enforced"] else 3
    if ns.cmd == "agent":
        if ns.require_isolation and not _inside(ns.lab_db, Path(ns.jobs_db).parent / "lab"):
            # the isolated account can write only data/lab/, and the Lab page reads the agent's work there
            print(f"error: --require-isolation: the lab database must be under {Path(ns.jobs_db).parent / 'lab'} "
                  "(--lab-db data/lab/lab.db)", file=sys.stderr)
            return 2
        # by default files only (writes nothing); --require-isolation runs the full check and fails closed
        iso = check_isolation(PROJECT_ROOT, ns.jobs_db, ns.config_dir, probe_dirs=ns.require_isolation,
                              lab_dir=Path(ns.lab_db).parent, api_ports=api_ports, relay_ports=relay_ports,
                              exec_paths=tuple(ns.exec_path))
        if iso["writable"] or iso["readable_secrets"] or ns.require_isolation and not iso["os_enforced"]:
            print(f"isolation: NOT enforced - this account can write {len(iso['writable'])} production target(s), "
                  f"read {len(iso['readable_secrets'])} secret file(s)"
                  + "".join(f"; {p}" for p in iso["problems"])
                  + "; only the in-process guard and procedure keep it out (isolation-check)", file=sys.stderr)
            if ns.require_isolation:
                print("error: --require-isolation: refusing to run", file=sys.stderr)
                return 2
    if ns.cmd in _HUMAN:
        if ns.yes:
            print("error: --yes was removed: the command asks for a code typed at your terminal", file=sys.stderr)
            return 2
        why = _not_a_present_human()
        if why:
            print(f"refusing {ns.cmd}: {why}", file=sys.stderr)
            return 2
    if ns.cmd not in _UNGUARDED:
        try:
            # no network at all: the lab runs offline, and loopback would reach the app's own API
            install_write_guard(PROJECT_ROOT, lab_writable_roots(ns.lab_db, getattr(ns, "store", DEFAULT_STORE)),
                                jobs_db=ns.jobs_db, loopback_ports=())
        except GuardViolation as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        if ns.cmd == "status":
            print(json.dumps({"pointer": current_pointer(ns.jobs_db),
                              "pipeline": resolve_active_strategy(ns.jobs_db, ns.config_dir).to_dict()}, indent=1))
            return 0
        if ns.cmd in ("rollback", "deactivate"):
            what = (f"ROLLBACK production to the bundle of pointer {ns.pointer_id}" if ns.cmd == "rollback"
                    else "DEACTIVATE: production returns to the configuration directory")
            if not _confirm(f"{what}\n  recorded as {ns.by}: {ns.reason}"):
                print("not confirmed: nothing was recorded", file=sys.stderr)
                return 2
            print(rollback(ns.jobs_db, ns.pointer_id, approved_by=ns.by, reason=ns.reason, config_dir=ns.config_dir)
                  if ns.cmd == "rollback" else deactivate(ns.jobs_db, approved_by=ns.by, reason=ns.reason))
            return 0
        lab = LabStore(ns.lab_db)
        try:
            if ns.cmd == "hypothesis":
                print(lab.add_hypothesis(ns.statement, ns.rationale))
            elif ns.cmd == "candidate":
                ov = json.loads(ns.overrides)
                digest, path = create_bundle(_base_config(ns), ov, ns.store)
                print(lab.add_candidate(ns.hypothesis_id, digest, str(path), ov))
            elif ns.cmd == "run":
                cand = lab.get_candidate(ns.candidate_id)
                ds = load_dataset(ns.dataset, require_complete_labels=False)
                res = run_experiment(ds, StrategyRef(config_dir=_base_config(ns)), cand["bundle_path"], name=ns.candidate_id)
                eid = lab.add_experiment(ns.candidate_id, ds.manifest.model_dump(), res)
                print(json.dumps({"experiment_id": eid, **{k: res[k] for k in
                      ("verdict", "explanation", "eligible_for_approval", "fixed", "broken", "guardrails",
                       "comparison")}}, indent=1))
            elif ns.cmd == "agent":
                from src.lab.agent import render_report, run_agent

                ds = load_dataset(ns.dataset)
                report = run_agent(lab, ds, StrategyRef(config_dir=_base_config(ns), profile_path=ns.profile),
                                   bundle_store=ns.store, max_hypotheses=ns.max_hypotheses,
                                   experiment=not ns.no_experiments)
                print(json.dumps(report, indent=1, default=str) if ns.json else render_report(report))
            elif ns.cmd == "memory":
                from src.lab.memory import LabMemory

                current = None
                if ns.dataset:
                    from src.evaluation.runner import run_evaluation

                    ds = load_dataset(ns.dataset, require_complete_labels=False)
                    run = run_evaluation(ds, StrategyRef(config_dir=_base_config(ns), profile_path=ns.profile))
                    current = {"dataset_id": ds.manifest.dataset_id, "dataset_jobs_sha256": ds.manifest.jobs_sha256,
                               "dataset_labels_sha256": ds.manifest.labels_sha256,
                               "strategy_fingerprint": run.strategy["fingerprint"]}
                for item in LabMemory(lab).recall(kind=ns.kind, current=current):
                    stale = "" if item["stale"] is None else (" STALE" if item["stale"] else "")
                    print(f"{item['item_id']}  {item['kind']:<11} {item['status']:<10}{stale}  {item['statement'][:160]}")
            elif ns.cmd == "approve":
                ds = load_dataset(ns.dataset, require_complete_labels=False)
                print(f"re-running experiment {ns.experiment_id} on {ds.path} (the lab's stored verdict is not trusted)...",
                      file=sys.stderr)

                def confirm(evidence: dict[str, Any]) -> bool:
                    # what limits the evidence (e.g. assistant-assisted labels only), in front of the approver
                    for limit in evidence.get("limitations") or ():
                        print(f"limitation: {limit}", file=sys.stderr)
                    return _confirm(_approval_summary(evidence))

                print(approve(lab, ns.jobs_db, ns.experiment_id, dataset=ds, approved_by=ns.by, reason=ns.reason,
                              config_dir=ns.config_dir, profile_path=ns.profile,
                              accept_exploratory=ns.accept_exploratory_evidence, confirm=confirm))
        finally:
            lab.close()
        return 0
    except (BundleError, PromotionError, DatasetError, KeyError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
