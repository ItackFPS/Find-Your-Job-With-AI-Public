"""Assisted annotation: proposals never become labels without a reply, human rows are never touched."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from src.evaluation.annotation_assist import (
    AssistError, add_proposals, apply_decisions, latest_decisions, main, parse_reply, read_sheet, record, status,
)
from src.evaluation.dataset import write_dataset
from src.evaluation.export import ANNOTATIONS, parse_annotations, write_annotation_template

IDS = [f"j-{i:012x}" for i in range(1, 7)]


def _job(jid: str) -> dict:
    return {"id": jid, "title": f"Technicien {jid}", "company": "Company-00000000", "location": "Toulon",
            "source": "test", "description": "Support N1, débutant accepté."}


@pytest.fixture()
def ds(tmp_path: Path) -> Path:
    root = tmp_path / "ds"
    jobs = [_job(j) for j in IDS]
    write_dataset(root, dataset_id="d", version="1", kind="real", created_at="2026-10-01T00:00:00+00:00",
                  as_of="2026-10-01T00:00:00+00:00", jobs=jobs, labels=[])
    write_annotation_template(root, jobs)
    rows = read_sheet(root)
    rows[0].update(expected="borderline ", category="location, domain", reason="trop loin à mon goût")
    with open(root / ANNOTATIONS, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return root


def _proposal(jid: str, expected: str = "accept") -> dict:
    return {"job_id": jid, "expected": expected, "category": "domain", "reason": "support N1 junior",
            "confidence": "high", "evidence": ["débutant accepté"], "needs_review": False, "summary": "N1 · Toulon"}


def _proposed(ds: Path) -> Path:
    add_proposals(ds, [_proposal(j) for j in IDS[1:]], batch=1)
    return ds


def test_proposal_over_a_human_label_is_refused(ds):
    with pytest.raises(AssistError, match="already has a human label"):
        add_proposals(ds, [_proposal(IDS[0])], batch=1)


def test_proposals_are_sorted_and_never_reach_the_sheet(ds):
    before = (ds / ANNOTATIONS).read_bytes()
    add_proposals(ds, [_proposal(j) for j in reversed(IDS[1:])], batch=1)
    lines = (ds / "assist" / "proposals.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["job_id"] for x in lines] == IDS[1:]
    assert apply_decisions(ds)["written"] == 0
    assert (ds / ANNOTATIONS).read_bytes() == before


def test_invalid_proposal_values_are_refused(ds):
    with pytest.raises(AssistError, match="expected"):
        add_proposals(ds, [_proposal(IDS[1], "maybe")], batch=1)
    with pytest.raises(AssistError, match="URL"):
        add_proposals(ds, [{**_proposal(IDS[1]), "reason": "voir https://x.example/job"}], batch=1)


def test_reply_codes_inheritance_and_origin():
    batch = [_proposal(j) for j in IDS[1:]]
    got = dict(parse_reply("1 Y; 2 R c=location r=trop loin\n3 A\n4 R\n5 S", batch))
    assert got[IDS[1]] == {"action": "label", "expected": "accept", "category": "domain",
                           "reason": "support N1 junior", "origin": "suggestion_confirmed"}
    assert got[IDS[2]]["expected"] == "reject" and got[IDS[2]]["category"] == "location"
    assert got[IDS[2]]["reason"] == "trop loin" and got[IDS[2]]["origin"] == "human_modified"
    assert got[IDS[3]]["origin"] == "suggestion_confirmed"
    # a changed label does not inherit the suggestion's justification
    assert got[IDS[4]] == {"action": "label", "expected": "reject", "category": "", "reason": "",
                           "origin": "human_modified"}
    assert got[IDS[5]]["action"] == "defer"


def test_ranges_star_and_errors():
    batch = [_proposal(j) for j in IDS[1:]]
    got = dict(parse_reply("2-3 B; * Y", batch))
    assert [got[j]["expected"] for j in IDS[1:]] == ["accept", "borderline", "borderline", "accept", "accept"]
    assert set(dict(parse_reply("* Y", batch, undecided={IDS[1]}))) == {IDS[1]}
    for bad in ("9 Y", "1 Y; 1 R", "1 X", "1 Y foo"):
        with pytest.raises(AssistError):
            parse_reply(bad, batch)


def test_record_apply_fills_only_empty_rows_and_validates(ds):
    _proposed(ds)
    record(ds, 1, "1 Y; 2 R c=location r=trop loin; 3 S", now="2026-10-09T00:00:00+00:00")
    human_before = read_sheet(ds)[0]
    out = apply_decisions(ds)
    assert out == {"written": 2, "already_in_sheet": 0, "conflicts": []}
    rows = {r["job_id"]: r for r in read_sheet(ds)}
    assert rows[IDS[0]] == human_before                    # the human row is untouched, trailing space included
    assert (rows[IDS[1]]["expected"], rows[IDS[2]]["expected"], rows[IDS[3]]["expected"]) == ("accept", "reject", "")
    assert apply_decisions(ds)["written"] == 0              # idempotent
    assert len(list((ds / "assist" / "backups").glob("*.csv"))) == 1
    labels, unlabelled = parse_annotations(ds, ds / ANNOTATIONS, labeler="t")
    assert len(labels) == 3 and len(unlabelled) == 3
    assert status(ds)["deferred"] == [IDS[3]]


def test_latest_decision_wins_and_star_skips_decided(ds):
    _proposed(ds)
    record(ds, 1, "1 R", now="t1")
    record(ds, 1, "* Y", now="t2")
    decisions = latest_decisions(ds)
    assert decisions[IDS[1]]["expected"] == "reject"       # '*' did not overwrite an answered job
    record(ds, 1, "1 Y", now="t3")
    assert latest_decisions(ds)[IDS[1]]["expected"] == "accept"


def test_sheet_row_filled_by_hand_is_reported_not_overwritten(ds):
    _proposed(ds)
    record(ds, 1, "1 Y")
    rows = read_sheet(ds)
    rows[1]["expected"] = "reject"
    with open(ds / ANNOTATIONS, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    out = apply_decisions(ds)
    assert out["conflicts"] == [IDS[1]] and out["written"] == 0
    assert read_sheet(ds)[1]["expected"] == "reject"
    with pytest.raises(AssistError, match="already has a label"):
        record(ds, 1, "1 A")


def test_cli_roundtrip(ds, tmp_path, capsys):
    f = tmp_path / "p.json"
    f.write_text(json.dumps([_proposal(IDS[1])]), encoding="utf-8")
    assert main(["--dataset", str(ds), "add-proposals", str(f), "--batch", "1"]) == 0
    assert main(["--dataset", str(ds), "show", "1"]) == 0
    assert "ACCEPT (high)" in capsys.readouterr().out
    assert main(["--dataset", str(ds), "record", "1", "--reply", "1 Y"]) == 0
    assert main(["--dataset", str(ds), "apply"]) == 0
    assert main(["--dataset", str(ds), "record", "1", "--reply", "7 Y"]) == 2


# -- provenance: assisted labels are never passed off as independent --------------------------------


def _import(ds: Path, version: str | None = None) -> dict:
    from src.evaluation.annotation_assist import assisted_job_ids
    from src.evaluation.export import import_annotations

    return import_annotations(ds, ds / ANNOTATIONS, labeler="t", new_version=version, assisted=assisted_job_ids(ds))


def test_confirmed_or_edited_suggestions_import_as_human_assisted(ds):
    from src.evaluation.dataset import load_dataset

    _proposed(ds)
    record(ds, 1, "1 Y; 2 R c=location r=trop loin")
    apply_decisions(ds)
    out = _import(ds)
    labels = load_dataset(ds, require_complete_labels=False).labels
    assert labels[IDS[0]].label_source == "human"                  # labelled by hand, no proposal on record
    assert labels[IDS[1]].label_source == labels[IDS[2]].label_source == "human_assisted"
    assert out["provenance"] == {"human": 1, "human_assisted": 2}


def test_blind_labels_stay_independent_and_y_is_refused_in_blind_mode(ds):
    from src.evaluation.annotation_assist import assisted_job_ids
    from src.evaluation.dataset import load_dataset

    _proposed(ds)
    with pytest.raises(AssistError, match="blind review"):
        record(ds, 1, "1 Y", blind=True)
    rows = record(ds, 1, "1 R r=trop loin; 2 A", blind=True)
    assert [r["origin"] for r in rows] == ["blind", "blind"] and rows[0]["category"] == ""   # nothing inherited
    apply_decisions(ds)
    assert IDS[1] not in assisted_job_ids(ds) and IDS[3] in assisted_job_ids(ds)
    _import(ds)
    labels = load_dataset(ds, require_complete_labels=False).labels
    assert labels[IDS[1]].label_source == labels[IDS[2]].label_source == "human"


def test_a_shown_suggestion_forbids_a_later_blind_label(ds):
    from src.evaluation.annotation_assist import mark_exposed

    _proposed(ds)
    mark_exposed(ds, [IDS[1]])
    with pytest.raises(AssistError, match="already shown"):
        record(ds, 1, "1 A", blind=True)
    record(ds, 1, "2 R")                                          # a normal decision exposes too
    with pytest.raises(AssistError, match="already shown"):
        record(ds, 1, "2 A", blind=True)
    assert record(ds, 1, "3 A", blind=True)[0]["origin"] == "blind"


def test_cli_show_logs_exposure_but_show_blind_does_not(ds, capsys):
    from src.evaluation.annotation_assist import exposed_job_ids

    _proposed(ds)
    assert main(["--dataset", str(ds), "show", "1", "--blind"]) == 0
    out = capsys.readouterr().out
    assert "BLIND" in out and "ACCEPT" not in out and "support N1 junior" not in out
    assert exposed_job_ids(ds) == set()
    assert main(["--dataset", str(ds), "show", "1"]) == 0
    assert exposed_job_ids(ds) == set(IDS[1:])


def test_a_hand_edit_after_a_blind_label_makes_it_assisted(ds):
    from src.evaluation.annotation_assist import assisted_job_ids

    _proposed(ds)
    record(ds, 1, "1 R", blind=True)
    apply_decisions(ds)
    rows = read_sheet(ds)
    rows[1]["expected"] = "accept"
    with open(ds / ANNOTATIONS, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert IDS[1] in assisted_job_ids(ds)


def test_reimport_corrects_legacy_human_labels_but_never_upgrades_assisted(ds):
    from src.evaluation.dataset import load_dataset
    from src.evaluation.export import import_annotations

    _proposed(ds)
    record(ds, 1, "1 Y; 2 Y")
    apply_decisions(ds)
    import_annotations(ds, ds / ANNOTATIONS, labeler="t")                       # legacy: no provenance given
    assert load_dataset(ds, require_complete_labels=False).labels[IDS[1]].label_source == "human"
    out = _import(ds)                                                          # provenance-aware re-import
    assert out["changed"] == 2 and out["version"] == "3"
    labels = load_dataset(ds, require_complete_labels=False).labels
    assert labels[IDS[1]].label_source == "human_assisted"
    assert (ds / "versions" / "2" / "labels.jsonl").is_file()                  # the legacy version is archived
    again = import_annotations(ds, ds / ANNOTATIONS, labeler="t")              # assisted set not given
    assert again["changed"] == 0
    assert load_dataset(ds, require_complete_labels=False).labels[IDS[1]].label_source == "human_assisted"


def test_agreement_uses_blind_labels_only_and_counts_missed_accepts(ds):
    from src.evaluation.annotation_assist import agreement

    add_proposals(ds, [_proposal(IDS[1], "reject"), _proposal(IDS[2], "accept"), _proposal(IDS[3], "accept"),
                       _proposal(IDS[4], "accept")], batch=1)
    record(ds, 1, "1 A; 2 A; 3 R", blind=True)
    record(ds, 1, "4 Y")                                                       # confirmed: agrees by construction
    a = agreement(ds)
    assert a["n_blind"] == 3 and a["status"] == "insufficient_evidence"
    assert a["agreement"] == pytest.approx(1 / 3)
    assert a["human_accepts"] == 2 and a["assistant_missed_accepts"] == 1
    assert a["human_by_assistant"]["accept"] == {"accept": 1, "reject": 1, "borderline": 0}


def test_audit_and_run_flag_human_labels_sitting_over_proposals(ds):
    from src.evaluation.audit import audit_dataset
    from src.evaluation.export import import_annotations

    _proposed(ds)
    record(ds, 1, "* Y")
    apply_decisions(ds)
    import_annotations(ds, ds / ANNOTATIONS, labeler="t")                       # legacy import
    check = audit_dataset(ds)["checks"]["assisted_provenance"]
    assert not check["ok"] and check["advisory"] and check["detail"].startswith("5 label(s)")
    _import(ds)
    assert audit_dataset(ds)["checks"]["assisted_provenance"]["ok"]
