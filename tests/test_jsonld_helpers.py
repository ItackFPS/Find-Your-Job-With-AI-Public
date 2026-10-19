"""Tests for src/collectors/_jsonld.py -- shared JobPosting JSON-LD parsing."""

from __future__ import annotations

import json

import pytest

from src.collectors._jsonld import (
    detect_work_mode,
    extract_itemlist_urls,
    extract_jobposting_ld,
    map_jobposting_ld_to_raw,
)

JOBPOSTING_LD: dict = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Développeur Python",
    "description": "<p>Full remote job</p>",
    "datePosted": "2026-09-01T00:00:00Z",
    "employmentType": "FULL_TIME",
    "jobLocationType": "TELECOMMUTE",
    "hiringOrganization": {"@type": "Organization", "name": "Acme"},
    "jobLocation": {
        "@type": "Place",
        "address": {
            "@type": "PostalAddress",
            "addressLocality": "Paris",
            "addressRegion": "Île-de-France",
            "addressCountry": "FR",
            "postalCode": "75000",
        },
    },
}


def _html_with_ld(*objects: dict) -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(o)}</script>' for o in objects
    )
    return f"<html><head>{scripts}</head><body></body></html>"


class TestExtractJobPostingLd:
    def test_extracts_dict_jobposting(self) -> None:
        html = _html_with_ld(JOBPOSTING_LD)
        result = extract_jobposting_ld(html)
        assert result is not None
        assert result["title"] == "Développeur Python"

    def test_extracts_from_list(self) -> None:
        html = _html_with_ld({"@type": "Organization", "name": "x"})
        html += _html_with_ld([{"@type": "BreadcrumbList"}, JOBPOSTING_LD])
        result = extract_jobposting_ld(html)
        assert result is not None
        assert result["@type"] == "JobPosting"

    def test_no_jsonld_returns_none(self) -> None:
        assert extract_jobposting_ld("<html><body>no scripts</body></html>") is None

    def test_malformed_json_skipped(self) -> None:
        html = '<script type="application/ld+json">{not valid json</script>'
        assert extract_jobposting_ld(html) is None

    def test_no_matching_type_returns_none(self) -> None:
        html = _html_with_ld({"@type": "Organization", "name": "Acme"})
        assert extract_jobposting_ld(html) is None


class TestExtractItemListUrls:
    def test_extracts_urls_in_order(self) -> None:
        itemlist = {
            "@type": "ItemList",
            "itemListElement": [
                {"@type": "ListItem", "url": "https://example.com/jobs/1", "position": 1},
                {"@type": "ListItem", "url": "https://example.com/jobs/2", "position": 2},
            ],
        }
        html = _html_with_ld(itemlist)
        assert extract_itemlist_urls(html) == [
            "https://example.com/jobs/1",
            "https://example.com/jobs/2",
        ]

    def test_no_itemlist_returns_empty(self) -> None:
        html = _html_with_ld(JOBPOSTING_LD)
        assert extract_itemlist_urls(html) == []

    def test_ignores_entries_without_url(self) -> None:
        itemlist = {"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "position": 1}]}
        assert extract_itemlist_urls(_html_with_ld(itemlist)) == []


class TestDetectWorkMode:
    def test_full_remote_signal(self) -> None:
        assert detect_work_mode("This is a 100% remote position") == "remote"

    def test_hybrid_signal(self) -> None:
        assert detect_work_mode("Télétravail partiel possible") == "hybrid"

    def test_no_signal_returns_none(self) -> None:
        assert detect_work_mode("Office job in Paris") is None


class TestMapJobPostingLdToRaw:
    def test_full_mapping(self) -> None:
        out = map_jobposting_ld_to_raw(JOBPOSTING_LD, "https://example.com/job/1", "job-1")
        assert out["title"] == "Développeur Python"
        assert out["company"] == "Acme"
        assert out["location"] == "Paris, Île-de-France, FR"
        assert out["countries"] == ["FR"]
        assert out["department_code"] == "75"
        assert out["canonical_url"] == "https://example.com/job/1"
        assert out["source_job_id"] == "job-1"
        assert out["type"] == "full-time"
        assert out["remote"] is True
        assert out["work_mode"] == "remote"

    def test_missing_hiring_organization(self) -> None:
        ld = dict(JOBPOSTING_LD)
        del ld["hiringOrganization"]
        out = map_jobposting_ld_to_raw(ld, "https://example.com/job/2", None)
        assert out["company"] is None

    def test_jobLocation_as_list(self) -> None:
        ld = dict(JOBPOSTING_LD)
        ld["jobLocation"] = [JOBPOSTING_LD["jobLocation"]]
        out = map_jobposting_ld_to_raw(ld, "https://example.com/job/3", "job-3")
        assert out["countries"] == ["FR"]

    def test_missing_location(self) -> None:
        ld = dict(JOBPOSTING_LD)
        del ld["jobLocation"]
        out = map_jobposting_ld_to_raw(ld, "https://example.com/job/4", "job-4")
        assert out["location"] == ""
        assert out["countries"] == []
        assert out["department_code"] is None

    def test_non_french_country_no_department(self) -> None:
        ld = json.loads(json.dumps(JOBPOSTING_LD))
        ld["jobLocation"]["address"]["addressCountry"] = "DE"
        ld["jobLocation"]["address"]["postalCode"] = "10115"
        out = map_jobposting_ld_to_raw(ld, "https://example.com/job/5", "job-5")
        assert out["department_code"] is None
        assert out["countries"] == ["DE"]

    def test_unmapped_employment_type_falls_back_to_lowercase(self) -> None:
        ld = dict(JOBPOSTING_LD)
        ld["employmentType"] = "SOMETHING_NEW"
        out = map_jobposting_ld_to_raw(ld, "https://example.com/job/6", "job-6")
        assert out["type"] == "something_new"

    def test_missing_employment_type(self) -> None:
        ld = dict(JOBPOSTING_LD)
        del ld["employmentType"]
        out = map_jobposting_ld_to_raw(ld, "https://example.com/job/7", "job-7")
        assert out["type"] is None

    def test_no_telecommute_falls_back_to_description_keywords(self) -> None:
        ld = dict(JOBPOSTING_LD)
        del ld["jobLocationType"]
        ld["description"] = "Poste en présentiel, télétravail partiel possible"
        out = map_jobposting_ld_to_raw(ld, "https://example.com/job/8", "job-8")
        assert out["work_mode"] == "hybrid"
        assert out["remote"] is False

    def test_telecommute_at_a_workplace_without_full_remote_wording_is_hybrid(self) -> None:
        """HelloWork marks "Télétravail partiel" jobs TELECOMMUTE (3 of 3 checked live, 2026-10-19):
        with a workplace address and no full-remote wording, that is partial telework, not remote."""
        ld = json.loads(json.dumps(JOBPOSTING_LD))
        ld["jobLocation"]["address"]["addressLocality"] = "Vannes"
        ld["description"] = "<p>Poste basé à Vannes (56). Stack PHP / MySQL.</p>"
        out = map_jobposting_ld_to_raw(ld, "https://example.com/job/10", "job-10")
        assert out["work_mode"] == "hybrid"
        assert out["remote"] is False

    def test_telecommute_without_a_workplace_is_remote(self) -> None:
        ld = dict(JOBPOSTING_LD)
        del ld["jobLocation"]
        ld["description"] = "<p>Stack PHP / MySQL.</p>"
        out = map_jobposting_ld_to_raw(ld, "https://example.com/job/11", "job-11")
        assert out["work_mode"] == "remote"
        assert out["remote"] is True

    def test_missing_title_and_description(self) -> None:
        out = map_jobposting_ld_to_raw({"@type": "JobPosting"}, "https://example.com/job/9", "job-9")
        assert out["title"] == ""
        assert out["description"] == ""
        assert out["company"] is None


class TestEntityEncodedScriptType:
    """HelloWork (2026-10) serves ``type="application/ld&#x2B;json"``: the ``+`` HTML-entity encoded.

    Browsers decode attribute entities, so it is a valid JSON-LD block; reading it as "no block" made
    the collector find 0 jobs on every page without any error.
    """

    @pytest.mark.parametrize("plus", ["+", "&#x2B;", "&#x2b;", "&#43;", "&plus;"])
    def test_every_encoding_of_the_plus_is_a_jsonld_block(self, plus: str) -> None:
        itemlist = {"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "url": "https://e.com/1"}]}
        html = (f'<script type="application/ld{plus}json">{json.dumps(itemlist)}</script>'
                f'<script type="application/ld{plus}json">{json.dumps(JOBPOSTING_LD)}</script>')
        assert extract_itemlist_urls(html) == ["https://e.com/1"]
        assert extract_jobposting_ld(html)["title"] == "Développeur Python"

    def test_other_script_types_are_still_ignored(self) -> None:
        html = f'<script type="application/json">{json.dumps(JOBPOSTING_LD)}</script>'
        assert extract_jobposting_ld(html) is None
