"""Assisted annotation of an exported dataset: proposals stay proposals until a human confirms them.

Offline tooling for ``annotations.csv`` (never imported by production code). Three kinds of data are
kept apart, all under ``<dataset>/assist/`` (git-ignored with the dataset):

* ``proposals.jsonl``  — machine suggestions (expected/category/reason/confidence/evidence/
  needs_review), one per job, sorted by ``job_id``. Never read by the harness.
* ``decisions.jsonl``  — append-only log of the reviewer's answers (confirm / change / defer);
  the latest line per job wins, so an answer can be corrected later.
* ``annotations.csv``  — the human sheet of the dataset. ``apply`` copies confirmed decisions into
  rows whose ``expected`` is still empty and never touches a row that is already filled.

The harness only ever sees ``annotations.csv`` through ``annotate-validate`` / ``annotate-import``,
which stamp every label of a job with an assistant proposal on record ``label_source=human_assisted``
(``assisted_job_ids``): a confirmed or edited suggestion is never passed off as independent ground
truth. The exception is a **blind** decision (``show --blind`` / ``record --blind``): the proposal
was hidden, the reviewer gave the label themselves, and the sheet still holds exactly that label.
Blind labels stay ``human`` and are what ``agreement`` compares the assistant against.

Reply grammar for ``record`` (one entry per line or separated by ``;``)::

    <sel> <code> [c=<category>] [r=<reason until end of entry>]

``sel`` is a 1-based position in the batch, a range ``3-7``, a job id, or ``*`` (every job of the
batch not named elsewhere in the reply that is still undecided). ``code``: ``Y`` confirm the
suggestion as is; ``A`` / ``R`` / ``B`` set accept / reject / borderline; ``S`` defer. Category and
reason are inherited from the suggestion only when the label is the suggested one; otherwise they
are empty unless given. In blind mode ``Y`` is refused and nothing is inherited.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.evaluation.audit import _identifier_hits
from src.evaluation.dataset import DatasetError, load_dataset
from src.evaluation.export import _TEMPLATE_COLUMNS, ANNOTATIONS

ASSIST_DIR = "assist"
PROPOSALS = "proposals.jsonl"
DECISIONS = "decisions.jsonl"
EXPOSURES = "exposures.jsonl"   # append-only: when a batch's suggestions were displayed (non-blind `show`)
LABELS = ("accept", "reject", "borderline")
CONFIDENCE = ("high", "medium", "low")
DECISIVE_MIN = 150
AGREEMENT_MIN = 30   # blind labels below this: the agreement figures describe the sample, nothing more
BLIND = "blind"
_CODES = {"Y": None, "OK": None, ".": None, "A": "accept", "R": "reject", "B": "borderline", "S": "defer"}
_PROPOSAL_KEYS = ("job_id", "expected", "category", "reason", "confidence", "evidence", "needs_review", "summary")


class AssistError(ValueError):
    pass


# -- files ------------------------------------------------------------------------


def _assist(root: Path) -> Path:
    return root / ASSIST_DIR


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def read_sheet(root: Path) -> list[dict[str, str]]:
    with open(root / ANNOTATIONS, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        if tuple(rd.fieldnames or ()) != _TEMPLATE_COLUMNS:
            raise AssistError(f"{ANNOTATIONS}: columns differ from the template {_TEMPLATE_COLUMNS}")
        return [dict(r) for r in rd]


def _sheet_bytes(rows: list[dict[str, str]]) -> bytes:
    import io

    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=_TEMPLATE_COLUMNS)   # default \r\n line ends, as the exporter
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def load_proposals(root: Path) -> dict[str, dict[str, Any]]:
    return {p["job_id"]: p for p in _read_jsonl(_assist(root) / PROPOSALS)}


def latest_decisions(root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for d in _read_jsonl(_assist(root) / DECISIONS):
        out[d["job_id"]] = d
    return out


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def mark_exposed(root: Path, job_ids: list[str], *, now: str | None = None) -> None:
    """Record that these jobs' suggestions were displayed: from now on no blind label is possible for them."""
    stamp = now or datetime.now(UTC).isoformat(timespec="seconds")
    _append_jsonl(_assist(root) / EXPOSURES, [{"job_id": j, "shown_at": stamp} for j in job_ids])


def exposed_job_ids(root: Path) -> set[str]:
    """Jobs whose suggestion the reviewer may have seen: logged exposures plus every job with a non-blind
    decision line (decisions recorded before exposures were logged count as exposed)."""
    out = {e["job_id"] for e in _read_jsonl(_assist(root) / EXPOSURES)}
    out |= {d["job_id"] for d in _read_jsonl(_assist(root) / DECISIONS) if d.get("mode") != BLIND}
    return out


def assisted_job_ids(root: Path) -> set[str]:
    """Jobs whose label cannot count as independent: an assistant proposal is on record and the label is
    not the reviewer's own blind decision (latest decision blind AND the sheet holds that label or none).

    Conservative: a job proposed and then labelled by hand in the sheet counts as assisted, since the
    reviewer may have seen the proposal. Read-only; empty when the dataset has no ``assist/`` files.
    """
    proposals = load_proposals(root)
    if not proposals:
        return set()
    decisions = latest_decisions(root)
    sheet = ({r["job_id"]: (r.get("expected") or "").strip().lower() for r in read_sheet(root)}
             if (root / ANNOTATIONS).is_file() else {})
    out = set()
    for jid in proposals:
        d = decisions.get(jid)
        blind_and_kept = (d is not None and d.get("origin") == BLIND and d["action"] == "label"
                          and sheet.get(jid, "") in ("", d["expected"]))
        if not blind_and_kept:
            out.add(jid)
    return out


def _check_text(text: str, where: str) -> None:
    hits = _identifier_hits([text])
    if any(hits.values()):
        raise AssistError(f"{where}: contains an e-mail/URL/phone {hits}; the audit would fail")


# -- proposals --------------------------------------------------------------------


def add_proposals(root: Path, items: list[dict[str, Any]], *, batch: int) -> int:
    """Validate and merge machine proposals. Refuses jobs that already carry a human label."""
    ds = load_dataset(root, require_complete_labels=False)
    known = {j["id"] for j in ds.job_dicts}
    human = {r["job_id"] for r in read_sheet(root) if (r.get("expected") or "").strip()} | set(ds.labels)
    current = load_proposals(root)
    seen: set[str] = set()
    for i, raw in enumerate(items, start=1):
        p: dict[str, Any] = {k: raw.get(k) for k in _PROPOSAL_KEYS}
        jid = str(p["job_id"] or "")
        if jid not in known:
            raise AssistError(f"proposal {i}: unknown job_id {jid!r}")
        if jid in human:
            raise AssistError(f"proposal {i}: {jid} already has a human label; never proposed over")
        if jid in seen:
            raise AssistError(f"proposal {i}: duplicate job_id {jid!r}")
        seen.add(jid)
        if p["expected"] not in LABELS:
            raise AssistError(f"proposal {i}: expected must be one of {LABELS}")
        if p["confidence"] not in CONFIDENCE:
            raise AssistError(f"proposal {i}: confidence must be one of {CONFIDENCE}")
        if not isinstance(p["evidence"], list) or not all(isinstance(e, str) for e in p["evidence"]):
            raise AssistError(f"proposal {i}: evidence must be a list of strings")
        p["needs_review"] = bool(p["needs_review"])
        for k in ("category", "reason", "summary"):
            p[k] = str(p[k] or "").strip()
            _check_text(p[k], f"proposal {i} {k}")
        p["batch"] = batch
        p["proposed_by"] = "assistant"
        p["jobs_sha256"] = ds.manifest.jobs_sha256
        current[jid] = p
    body = "".join(json.dumps(current[k], ensure_ascii=False, sort_keys=True) + "\n" for k in sorted(current))
    _write_atomic(_assist(root) / PROPOSALS, body.encode("utf-8"))
    return len(seen)


def batch_jobs(root: Path, batch: int) -> list[dict[str, Any]]:
    return [p for _, p in sorted(load_proposals(root).items()) if p["batch"] == batch]


# -- decisions --------------------------------------------------------------------


def _split_entries(text: str) -> list[str]:
    return [e.strip() for part in text.splitlines() for e in part.split(";") if e.strip()]


_ENTRY = re.compile(r"^(?P<sel>\S+)\s+(?P<code>\S+)(?P<rest>.*)$")


def parse_reply(
    text: str, batch: list[dict[str, Any]], undecided: set[str] | None = None, *, blind: bool = False,
) -> list[tuple[str, dict[str, Any]]]:
    """Reply → ``[(job_id, decision)]``. Pure: no file access, raises on anything ambiguous.

    Positions refer to the whole batch; ``*`` covers the jobs not named in the reply that are in
    ``undecided`` (every job when it is None). ``blind``: the reviewer did not see the proposals, so
    confirming one (``Y``) is meaningless and refused, and nothing is inherited from them.
    """
    ids = [p["job_id"] for p in batch]
    entries: list[tuple[list[str], str, str | None, str | None]] = []
    star: tuple[str, str | None, str | None] | None = None
    for e in _split_entries(text):
        m = _ENTRY.match(e)
        if not m:
            raise AssistError(f"cannot read {e!r}: expected '<n> <code> [c=...] [r=...]'")
        sel, code, rest = m["sel"], m["code"].upper(), m["rest"].strip()
        if code not in _CODES:
            raise AssistError(f"{e!r}: unknown code {code!r} (Y, A, R, B, S)")
        if blind and _CODES[code] is None:
            raise AssistError(f"{e!r}: blind review: give the label yourself (A, R, B) or defer (S)")
        cat = reason = None
        if rest:
            rm = re.match(r"^(?:c=(?P<c>.*?)\s*)?(?:(?<!\S)r=(?P<r>.*))?$", rest)
            if not rm or (rm["c"] is None and rm["r"] is None):
                raise AssistError(f"{e!r}: after the code only 'c=<category>' then 'r=<reason>' are allowed")
            cat = rm["c"].strip() if rm["c"] is not None else None
            reason = rm["r"].strip() if rm["r"] is not None else None
        if sel == "*":
            if star is not None:
                raise AssistError("'*' given twice")
            star = (code, cat, reason)
            continue
        if re.fullmatch(r"\d+-\d+", sel):
            a, b = (int(x) for x in sel.split("-"))
            if not 1 <= a <= b <= len(ids):
                raise AssistError(f"{e!r}: range outside 1-{len(ids)}")
            targets = ids[a - 1:b]
        elif sel.isdigit():
            if not 1 <= int(sel) <= len(ids):
                raise AssistError(f"{e!r}: position outside 1-{len(ids)}")
            targets = [ids[int(sel) - 1]]
        elif sel in ids:
            targets = [sel]
        else:
            raise AssistError(f"{e!r}: {sel!r} is not a position or a job id of this batch")
        entries.append((targets, code, cat, reason))
    named = [t for targets, *_ in entries for t in targets]
    dupes = sorted({t for t in named if named.count(t) > 1})
    if dupes:
        raise AssistError(f"job(s) answered twice: {dupes}")
    by_id = {p["job_id"]: p for p in batch}
    out: list[tuple[str, dict[str, Any]]] = []
    plan = [(t, code, cat, reason) for targets, code, cat, reason in entries for t in targets]
    if star:
        plan += [(t, *star) for t in ids if t not in named and (undecided is None or t in undecided)]
    for jid, code, cat, reason in plan:
        out.append((jid, _decision(by_id[jid], code, cat, reason, blind=blind)))
    return out


def _decision(p: dict[str, Any], code: str, cat: str | None, reason: str | None, *,
              blind: bool = False) -> dict[str, Any]:
    label = _CODES[code]
    if label == "defer":
        return {"action": "defer", "expected": None, "category": None, "reason": None, "origin": "deferred"}
    if blind:
        return {"action": "label", "expected": label, "category": cat or "", "reason": reason or "", "origin": BLIND}
    expected = label or p["expected"]
    same = expected == p["expected"]
    category = cat if cat is not None else (p["category"] if same else "")
    final_reason = reason if reason is not None else (p["reason"] if same else "")
    unchanged = same and category == p["category"] and final_reason == p["reason"]
    return {"action": "label", "expected": expected, "category": category, "reason": final_reason,
            "origin": "suggestion_confirmed" if unchanged else "human_modified"}


def record(root: Path, batch: int, reply: str, *, now: str | None = None,
           blind: bool = False) -> list[dict[str, Any]]:
    """Parse the reviewer's reply for ``batch`` and append it to the decisions log.

    ``blind=True`` only for a reply given from ``show --blind`` (the proposals were not seen).
    """
    jobs = batch_jobs(root, batch)
    if not jobs:
        raise AssistError(f"no proposals in batch {batch}")
    human = {r["job_id"] for r in read_sheet(root) if (r.get("expected") or "").strip()}
    stamp = now or datetime.now(UTC).isoformat(timespec="seconds")
    done = latest_decisions(root)
    undecided = {p["job_id"] for p in jobs if p["job_id"] not in done or done[p["job_id"]]["action"] == "defer"}
    parsed = parse_reply(reply, jobs, undecided, blind=blind)
    exposed = exposed_job_ids(root) if blind else set()
    rows = []
    for jid, d in parsed:
        if jid in human:
            raise AssistError(f"{jid} already has a label in {ANNOTATIONS}; change it there by hand")
        if jid in exposed:
            raise AssistError(f"{jid}: its suggestion was already shown, so a blind label is no longer possible; "
                              "record it without --blind")
        for k in ("category", "reason"):
            if d[k]:
                _check_text(d[k], f"{jid} {k}")
        rows.append({"job_id": jid, "batch": batch, **d, "mode": BLIND if blind else "assisted",
                     "decided_at": stamp})
    _append_jsonl(_assist(root) / DECISIONS, rows)
    return rows


# -- sheet ------------------------------------------------------------------------


def apply_decisions(root: Path) -> dict[str, Any]:
    """Copy confirmed decisions into empty rows of ``annotations.csv``. Filled rows are never changed."""
    rows = read_sheet(root)
    decisions = latest_decisions(root)
    filled = written = 0
    conflicts: list[str] = []
    for r in rows:
        d = decisions.get(r["job_id"])
        if not d or d["action"] != "label":
            continue
        if (r.get("expected") or "").strip():
            same = ((r["expected"].strip().lower(), r["category"].strip(), r["reason"].strip())
                    == (d["expected"], d["category"], d["reason"]))
            filled += same
            if not same:
                conflicts.append(r["job_id"])
            continue
        r["expected"], r["category"], r["reason"] = d["expected"], d["category"], d["reason"]
        written += 1
    if written:
        backups = _assist(root) / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        n = len(list(backups.glob("annotations.*.csv"))) + 1
        (backups / f"annotations.{n:03d}.csv").write_bytes((root / ANNOTATIONS).read_bytes())
        _write_atomic(root / ANNOTATIONS, _sheet_bytes(rows))
    return {"written": written, "already_in_sheet": filled, "conflicts": conflicts}


def status(root: Path) -> dict[str, Any]:
    rows = read_sheet(root)
    sheet = {r["job_id"]: (r.get("expected") or "").strip().lower() for r in rows}
    counts = {k: sum(1 for v in sheet.values() if v == k) for k in LABELS}
    proposals = load_proposals(root)
    decisions = latest_decisions(root)
    labelled = {d for d, v in decisions.items() if v["action"] == "label"}
    deferred = sorted(d for d, v in decisions.items() if v["action"] == "defer")
    return {
        "sheet_labels": counts,
        "decisive": counts["accept"] + counts["reject"],
        "decisive_needed": DECISIVE_MIN,
        "unlabelled_in_sheet": sum(1 for v in sheet.values() if not v),
        "proposals": len(proposals),
        "proposed_and_undecided": sorted(set(proposals) - set(decisions)),
        "deferred": deferred,
        "decided_not_yet_in_sheet": sorted(j for j in labelled if not sheet.get(j)),
        "without_proposal": sum(1 for j, v in sheet.items() if not v and j not in proposals),
        "batches": sorted({p["batch"] for p in proposals.values()}),
        "blind_decisions": sum(1 for v in decisions.values() if v.get("origin") == BLIND and v["action"] == "label"),
        "sheet_labels_assisted": sum(1 for j in assisted_job_ids(root) if sheet.get(j)),
    }


def agreement(root: Path) -> dict[str, Any]:
    """How often the assistant's proposal matches the reviewer's **blind** label (proposal hidden).

    Only blind decisions count: a confirmed suggestion agrees by construction. ``assistant_missed_accepts``
    is the number that matters most here — a job the reviewer wants that the assistant proposed to reject.
    """
    proposals = load_proposals(root)
    pairs = [(proposals[j]["expected"], d["expected"]) for j, d in sorted(latest_decisions(root).items())
             if d.get("origin") == BLIND and d["action"] == "label" and j in proposals]
    n = len(pairs)
    matrix = {h: dict.fromkeys(LABELS, 0) for h in LABELS}           # human label -> assistant proposal -> n
    for assistant, human in pairs:
        matrix[human][assistant] += 1
    observed = sum(matrix[lab][lab] for lab in LABELS) / n if n else None
    kappa = None
    if n:
        expected = sum(sum(matrix[lab].values()) * sum(matrix[h][lab] for h in LABELS) for lab in LABELS) / n ** 2
        kappa = (observed - expected) / (1 - expected) if observed is not None and expected < 1 else None
    human_accepts = sum(matrix["accept"].values())
    return {
        "status": "insufficient_evidence" if n < AGREEMENT_MIN else "measured",
        "n_blind": n,
        "agreement": observed,
        "cohen_kappa": kappa,
        "human_by_assistant": matrix,
        "human_accepts": human_accepts,
        "assistant_missed_accepts": human_accepts - matrix["accept"]["accept"],
        "note": (f"descriptive only: fewer than {AGREEMENT_MIN} blind labels" if n < AGREEMENT_MIN else
                 "blind labels only; jobs were not drawn at random, so this describes these batches"),
    }


def render_batch(root: Path, batch: int, *, only_open: bool = False, blind: bool = False) -> str:
    """Print a batch. ``blind`` hides every machine output (proposal, summary, evidence, flags) and the
    state of earlier non-blind decisions, and shows the job's own title, location and text instead."""
    jobs = {j["id"]: j for j in load_dataset(root, require_complete_labels=False).job_dicts}
    decisions = latest_decisions(root)
    lines = [f"Batch {batch}" + (" (BLIND: no suggestion shown; answer A/R/B or S)" if blind else "")]
    for n, p in enumerate(batch_jobs(root, batch), start=1):
        d = decisions.get(p["job_id"])
        if only_open and d and d["action"] == "label":
            continue
        j = jobs[p["job_id"]]
        if blind:
            if d and d["action"] == "label":
                continue                                    # already decided: re-showing it would not be blind
            text = " ".join(str(j.get("description") or "").split())
            lines.append(f"{n:>2}. {p['job_id']} {str(j.get('title') or '')[:80]} | {j.get('location') or '?'}")
            lines.append(f"    {text[:400]}{'…' if len(text) > 400 else ''}")
            continue
        state = "pending" if not d else ("DEFERRED" if d["action"] == "defer" else f"DONE {d['expected']}")
        flag = " ⚠ review" if p["needs_review"] else ""
        lines.append(f"{n:>2}. {p['job_id']} [{state}] {j.get('title', '')[:80]}")
        lines.append(f"    {p['summary']}")
        lines.append(f"    → {p['expected'].upper()} ({p['confidence']}){flag} c={p['category']} | {p['reason']}")
        if p["evidence"]:
            lines.append("    evidence: " + " · ".join(p["evidence"]))
    return "\n".join(lines)


# -- cli --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="python -m src.evaluation.annotation_assist", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default="data/benchmarks/real-v1")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add-proposals", help="merge machine proposals (JSON list or JSONL file)")
    p.add_argument("file")
    p.add_argument("--batch", type=int, required=True)
    s = sub.add_parser("show", help="print a batch with its suggestions")
    s.add_argument("batch", type=int)
    s.add_argument("--open", action="store_true", help="hide jobs already decided")
    s.add_argument("--blind", action="store_true", help="hide the suggestions (independent labelling)")
    r = sub.add_parser("record", help="record a reply for a batch (reply text on stdin or --reply)")
    r.add_argument("batch", type=int)
    r.add_argument("--reply", default=None)
    r.add_argument("--blind", action="store_true", help="the reply was given from `show --blind`")
    sub.add_parser("apply", help="copy confirmed decisions into empty rows of annotations.csv")
    sub.add_parser("status", help="counts and progress")
    sub.add_parser("agreement", help="assistant proposals vs blind human labels")
    ns = ap.parse_args(argv)
    root = Path(ns.dataset)
    try:
        if ns.cmd == "add-proposals":
            text = Path(ns.file).read_text(encoding="utf-8-sig").strip()
            items = json.loads(text) if text.startswith("[") else [json.loads(x) for x in text.splitlines() if x.strip()]
            print(f"added {add_proposals(root, items, batch=ns.batch)} proposal(s) to batch {ns.batch}")
        elif ns.cmd == "show":
            print(render_batch(root, ns.batch, only_open=ns.open, blind=ns.blind))
            if not ns.blind:   # the suggestions were displayed: no blind label for these jobs any more
                mark_exposed(root, [p["job_id"] for p in batch_jobs(root, ns.batch)])
        elif ns.cmd == "record":
            rows = record(root, ns.batch, ns.reply if ns.reply is not None else sys.stdin.read(), blind=ns.blind)
            for x in rows:
                print(f"{x['job_id']}  {x['origin']:<20} {x['expected'] or '-':<10} c={x['category'] or ''}")
            print(f"recorded {len(rows)} decision(s)")
        elif ns.cmd == "apply":
            print(json.dumps(apply_decisions(root), indent=2))
        elif ns.cmd == "status":
            print(json.dumps(status(root), indent=2, ensure_ascii=False))
        elif ns.cmd == "agreement":
            print(json.dumps(agreement(root), indent=2))
        return 0
    except (AssistError, DatasetError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
