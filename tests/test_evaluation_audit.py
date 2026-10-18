"""`audit`: read-only checks on an exported dataset; prints no job content."""

from __future__ import annotations

import csv
import json

import pytest

from src.evaluation.__main__ import main
from src.evaluation.audit import audit_dataset
from src.evaluation.export import export_real_dataset
from tests.test_evaluation_export import _build_db


@pytest.fixture()
def exported(tmp_path, monkeypatch):
    monkeypatch.setattr("src.evaluation.export.assert_not_committable", lambda p: None)
    monkeypatch.setattr("src.evaluation.audit.assert_not_committable", lambda p: None)
    db = _build_db(tmp_path / "jobs.db")
    out = tmp_path / "real-v1"
    export_real_dataset(db, out, n=20, seed=7, salt="s", as_of="2026-10-01T00:00:00+00:00")
    return db, out


def test_clean_export_passes_and_reports_insufficient_labels(exported):
    db, out = exported
    rep = audit_dataset(out, db=db)
    c = rep["checks"]
    assert rep["ok"] and c["integrity"]["ok"] and c["anonymisation"]["ok"] and c["reproducible"]["ok"]
    assert c["annotation_sheet"]["ok"] and "0 filled" in c["annotation_sheet"]["detail"]
    assert not c["evidence_threshold"]["ok"] and "insufficient_evidence" in c["evidence_threshold"]["detail"]


def test_residual_identifier_or_tampering_fails(exported):
    _, out = exported
    jobs = out / "jobs.jsonl"
    jobs.write_text(jobs.read_text().replace("[email]", "leak@example.com", 1), encoding="utf-8")
    rep = audit_dataset(out)
    assert not rep["ok"] and not rep["checks"]["integrity"]["ok"]


def test_bad_annotation_columns_and_unreproducible_db_are_flagged(exported, tmp_path):
    db, out = exported
    ann = out / "annotations.csv"
    rows = list(csv.reader(open(ann, encoding="utf-8-sig")))
    rows[0][0] = "id"
    csv.writer(open(ann, "w", newline="", encoding="utf-8-sig")).writerows(rows)
    assert not audit_dataset(out)["checks"]["annotation_sheet"]["ok"]
    meta_path = out / "PRIVATE_export_meta.json"
    meta = json.loads(meta_path.read_text())
    meta["salt"] = "another-salt"                      # a re-export can no longer match
    meta_path.write_text(json.dumps(meta))
    assert not audit_dataset(out, db=db)["checks"]["reproducible"]["ok"]


def test_cli_prints_no_job_content(exported, capsys):
    db, out = exported
    assert main(["audit", str(out), "--db", str(db)]) == 0
    printed = capsys.readouterr().out
    assert "PASS  integrity" in printed and "Dev " not in printed and "acme" not in printed.lower()


# -- review fixes ----------------------------------------------------------------------------

def _rewrite_jobs(out, mutate):
    """Edit jobs.jsonl AND refresh the manifest hash so only the content check can fail."""
    import hashlib

    jobs = out / "jobs.jsonl"
    rows = [json.loads(line) for line in jobs.read_text(encoding="utf-8").split("\n") if line.strip()]
    mutate(rows)
    data = ("\n".join(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows) + "\n").encode()
    jobs.write_bytes(data)
    man = json.loads((out / "manifest.json").read_text())
    man["jobs_sha256"] = hashlib.sha256(data).hexdigest()
    (out / "manifest.json").write_text(json.dumps(man))


def test_identifiers_in_any_field_and_real_company_names_fail(exported):
    db, out = exported

    def leak(rows):
        rows[0]["countries"] = ["bob@x.com"]
        rows[1]["source"] = "https://x.io"
        rows[2]["company"] = "Stripe"

    _rewrite_jobs(out, leak)
    c = audit_dataset(out)["checks"]["anonymisation"]
    assert not c["ok"] and "email@countries: 1" in c["detail"] and "company_not_alias@company: 1" in c["detail"]
    assert "url@source: 1" in c["detail"]


def test_real_company_found_in_text_is_detected_with_db(exported):
    db, out = exported
    clean = audit_dataset(out, db=db)["checks"]
    assert clean["anonymisation"]["ok"] and clean["company_names"]["ok"]
    assert "checked against the database" in clean["company_names"]["detail"]
    _rewrite_jobs(out, lambda rows: [r.__setitem__("description", r["description"] + " Acme Corp Globex") for r in rows])
    rep = audit_dataset(out, db=db)
    assert not rep["checks"]["company_names"]["ok"] and not rep["ok"]
    assert len({x["job_id"] for x in rep["leaks"]}) == 20
    assert all(x["kind"] == "company" and x["field"] == "description" for x in rep["leaks"])
    assert not any("Acme" in json.dumps(x) for x in rep["leaks"])          # shapes never carry content


def test_missing_or_malformed_sidecar_never_passes(exported):
    db, out = exported
    side = out / "PRIVATE_export_meta.json"
    side.write_text("[]")
    rep = audit_dataset(out, db=db)
    assert not rep["ok"] and not rep["checks"]["sidecar"]["ok"] and not rep["checks"]["reproducible"]["ok"]
    side.write_text(json.dumps({"anonymized": "false", "salt": "s", "seed": 7}))
    assert not audit_dataset(out)["checks"]["anonymisation"]["ok"]       # string "false" is not True
    side.unlink()
    rep = audit_dataset(out, db=db)
    assert not rep["ok"] and "sidecar missing" in rep["checks"]["reproducible"]["detail"]


def test_annotation_sheet_ids_values_and_identifiers_are_validated(exported):
    _, out = exported
    ann = out / "annotations.csv"
    rows = list(csv.reader(open(ann, encoding="utf-8-sig")))
    rows[1][0] = rows[2][0]                  # duplicate id (and one unknown)
    rows[1][1] = "maybe"                     # invalid expected
    rows[1][8] = "write to bob@acme.com"     # identifier in the sheet
    csv.writer(open(ann, "w", newline="", encoding="utf-8-sig")).writerows(rows)
    d = audit_dataset(out)["checks"]["annotation_sheet"]["detail"]
    assert "job_id set differs" in d and "invalid `expected`" in d and "identifiers in sheet" in d
    ann.write_bytes(b"\xff\xfe\x00bad")      # non-UTF8: reported, never a traceback
    assert not audit_dataset(out)["checks"]["annotation_sheet"]["ok"]


def test_evidence_threshold_is_advisory_warn_and_exit_code_follows_safety_checks(exported, capsys):
    db, out = exported
    assert audit_dataset(out)["ok"] is True
    assert main(["audit", str(out)]) == 0
    assert "WARN  evidence_threshold" in capsys.readouterr().out


def test_git_guard_failure_path_including_versions(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    (repo / "ds").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    # build a dataset inside the (un-ignored) repo
    from src.evaluation.dataset import write_dataset
    from src.evaluation.models import LabelRecord

    write_dataset(repo / "ds", dataset_id="d", version="1", kind="real", created_at="2026-10-01T00:00:00+00:00",
                  as_of="2026-10-01T00:00:00+00:00",
                  jobs=[{"id": "j-" + "0" * 12, "source": "s", "title": "t", "description": "x" * 50}],
                  labels=[LabelRecord(job_id="j-" + "0" * 12, expected="accept", label_source="human",
                                      labeler="me", labeled_at="2026-10-02T00:00:00+00:00")])
    rep = audit_dataset(repo / "ds")
    assert not rep["checks"]["git_ignored"]["ok"] and not rep["ok"]
    (repo / ".gitignore").write_text("ds/\n")
    assert audit_dataset(repo / "ds")["checks"]["git_ignored"]["ok"]


def test_unreadable_dataset_is_a_failed_check_not_a_traceback(tmp_path):
    rep = audit_dataset(tmp_path / "does-not-exist")
    assert not rep["ok"] and not rep["checks"]["integrity"]["ok"]


# -- root causes of the first real-data audit (phone / real company name leaks) ---------------

def test_audit_never_assembles_a_phone_number_across_fields():
    from src.evaluation.export import find_leaks

    job = {"id": "j-" + "0" * 12, "company": "Company-0123abcd", "title": "Dev 06", "location": "12 34 56 78",
           "description": "x", "skills": ["06", "12 34", "56 78"]}
    assert find_leaks([job], {job["id"]: "Real Co"}) == []        # the old "\n".join scan matched this


def test_company_names_inside_larger_words_are_not_leaks_but_whole_words_are():
    from src.evaluation.export import find_leaks

    job = {"id": "j-" + "1" * 12, "company": "Company-0123abcd", "description": "A balanced balancing act, Alan"}
    leaks = find_leaks([job], {job["id"]: "Alan"})
    assert [x["kind"] for x in leaks] == ["company"]              # only the whole word 'Alan', not 'bALANced'


@pytest.mark.parametrize("phone", ["+1 (555) 123-4567", "(555) 123-4567", "555-123-4567", "+44 (0)20 7946 0958",
                                   "0044 20 7946 0958", "+33 (0)1 23 45 67 89"])
def test_more_phone_formats_are_scrubbed(phone):
    from src.evaluation.export import scrub_text

    assert scrub_text(f"Call {phone} now") == "Call [phone] now"


def test_every_string_field_is_scrubbed_not_only_four():
    from src.evaluation.export import scrub_record

    rec = {"id": "j-x", "company": "Company-0123abcd", "title": "t", "source": "mail bob@x.com",
           "countries": ["tel 06 12 34 56 78"], "work_mode": "https://x.io", "employment_type": "Acme Corp job",
           "salary_currency": "EUR", "published_at": "2026-09-17T08:30:00", "skills": ["python", "Acme Corp"]}
    out = scrub_record(rec, "Acme Corp", "Company-9999aaaa")
    assert out["source"] == "mail [email]" and out["countries"] == ["tel [phone]"] and out["work_mode"] == "[url]"
    assert out["employment_type"] == "Company-9999aaaa job" and out["skills"] == ["python", "Company-9999aaaa"]
    assert out["published_at"] == "2026-09-17T08:30:00" and out["id"] == "j-x"


def test_alias_digits_cannot_create_a_phone_after_substitution():
    from src.evaluation.export import find_leaks, scrub_text

    text = scrub_text("Acme Corp 12 recrute", "Acme Corp", "Company-06123456")
    assert "Company-06123456" in text and "[phone]" not in text          # alias protected
    job = {"id": "j-" + "2" * 12, "company": "Company-06123456", "description": text}
    assert find_leaks([job], {job["id"]: "Acme Corp"}) == []


def test_exporter_refuses_to_write_a_dataset_that_leaks(tmp_path, monkeypatch):
    from src.evaluation import export as ex
    from tests.test_evaluation_export import _build_db

    monkeypatch.setattr(ex, "assert_not_committable", lambda p: None)
    monkeypatch.setattr(ex, "scrub_record", lambda rec, company, alias: {**rec, "company": alias,
                                                                         "description": "call bob@x.com"})
    db = _build_db(tmp_path / "jobs.db")
    out = tmp_path / "o"
    with pytest.raises(ex.ExportError, match="nothing was written"):
        ex.export_real_dataset(db, out, n=5, seed=1, salt="s")
    assert not (out / "manifest.json").exists() and not (out / "jobs.jsonl").exists()


def test_audit_explain_prints_field_kind_shape_only(exported, capsys):
    db, out = exported
    _rewrite_jobs(out, lambda rows: rows[0].__setitem__("countries", ["tel 06 12 34 56 78 bob@x.com"]))
    assert main(["audit", str(out), "--explain"]) == 1
    printed = capsys.readouterr().out
    assert "LEAK" in printed and "field=countries" in printed and "kind=phone" in printed
    assert "shape=##" in printed and "06 12" not in printed and "bob" not in printed


# -- review of the anonymisation fix -----------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Salaire 30 000 35 000 40 000", "2023-11-05 2023-12-06", "+500 000 000 €", "+100 000 000",
    "1 000 000 000 000", "From 200 300 4000", "Salaire 45 000 - 55 000 EUR, 2020-2024, Python 3.11.4, Bac+5",
    "publié 2026-09-17T08:30:00", "3 à 5 ans", "Budget 12 500 000 € sur 2024-2026",
])
def test_legitimate_numbers_are_not_mangled(text):
    from src.evaluation.export import scrub_text

    assert scrub_text(text) == text


@pytest.mark.parametrize("phone", ["07911 123456", "0800 123 456", "0 800 123 456", "０６１２３４５６７８",
                                   "06 12 34 56 7 8", "+ 33 6 12 34 56 78", "020 7946 0958"])
def test_more_phone_formats_incl_uk_freephone_fullwidth_and_irregular_grouping(phone):
    from src.evaluation.export import scrub_text

    assert "[phone]" in scrub_text(f"tel {phone} ok"), phone


@pytest.mark.parametrize("mail", ["bob [at] real [dot] com", "bob(at)real.com", "ｂｏｂ＠real.com",
                                  "bo­b@real.com"])
def test_obfuscated_and_fullwidth_emails_are_scrubbed(mail):
    from src.evaluation.export import scrub_text

    assert "[email]" in scrub_text(f"write {mail} now"), mail


def test_email_and_phone_regexes_are_linear_on_adversarial_input():
    import time

    from src.evaluation.export import scrub_text

    for blob in ["a" * 200_000, "1 " * 100_000, ("a.b+" * 50_000), "+" + "1" * 100_000]:
        t0 = time.perf_counter()
        scrub_text(blob, "Acme", "Company-0123abcd")
        assert time.perf_counter() - t0 < 5


@pytest.mark.parametrize("company,text", [
    ("Acme, Inc.", "Acme recrute. Rejoignez Acme Inc et Acme, Inc."),
    ("Acme Group SAS", "Acme Group, Acme"),
    ("HP", "chez HP aujourd'hui"),
    ("3M", "3M recrute"),
    ("Société Générale", "Societe Generale et SOCIÉTÉ GÉNÉRALE"),
    ("L'Oréal", "chez L’Oréal"),
    ("AT&T", "AT&amp;T recrute, at&t aussi"),
    ("Acme Beta", "Acme/Beta et Acme_Beta"),
])
def test_company_variants_accents_suffixes_short_names_are_scrubbed_and_detected(company, text):
    from src.evaluation.export import find_leaks, scrub_text

    out = scrub_text(text, company, "Company-0123abcd")
    assert out.count("Company-0123abcd") >= 1
    job = {"id": "j-" + "3" * 12, "company": "Company-0123abcd", "description": out}
    assert find_leaks([job], {job["id"]: company}) == []
    raw = {"id": job["id"], "company": "Company-0123abcd", "description": text}
    assert any(x["kind"] == "company" for x in find_leaks([raw], {job["id"]: company}))   # detector sees it too


def test_short_company_names_are_case_sensitive_whole_words():
    from src.evaluation.export import scrub_text

    assert scrub_text("hp laptop, HP", "HP", "A") == "hp laptop, A"
    assert scrub_text("a 3Mg dose", "3M", "A") == "a 3Mg dose"


def test_nested_containers_are_scrubbed_and_unsupported_types_fail_closed():
    from src.evaluation.export import find_leaks, scrub_record

    rec = {"id": "j-" + "4" * 12, "company": "Company-0123abcd", "skills": [{"k": "Acme x@y.fr"}, ["z@y.fr"]]}
    out = scrub_record(rec, "Acme", "Company-0123abcd")
    assert out["skills"] == [{"k": "Company-0123abcd [email]"}, ["[email]"]]
    assert find_leaks([out], {out["id"]: "Acme"}) == []
    assert find_leaks([{**out, "weird": object()}], {})[0]["kind"] == "unsupported_type"
    assert {x["kind"] for x in find_leaks([rec], {rec["id"]: "Acme"})} == {"email", "company"}


def test_audit_db_from_the_wrong_database_is_not_reported_as_checked(exported, tmp_path):
    from src.storage.sqlite import Storage

    other = tmp_path / "other.db"
    Storage(str(other)).init_db()
    db, out = exported
    c = audit_dataset(out, db=other)["checks"]["company_names"]
    assert not c["ok"] and c["advisory"] and "not found in that database" in c["detail"]


def test_without_db_company_names_is_a_warning_not_a_pass(exported, capsys):
    _, out = exported
    assert main(["audit", str(out)]) == 0
    assert "WARN  company_names: not checked" in capsys.readouterr().out


def test_annotation_sheet_cells_never_assemble_a_phone_across_cells(exported):
    _, out = exported
    ann = out / "annotations.csv"
    rows = list(csv.reader(open(ann, encoding="utf-8-sig")))
    rows[1][3], rows[1][4] = "Dev 06 12 34", "56 78 ok"
    csv.writer(open(ann, "w", newline="", encoding="utf-8-sig")).writerows(rows)
    assert audit_dataset(out)["checks"]["annotation_sheet"]["ok"]
