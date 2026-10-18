"""Tests for the normalize pipeline stage.

Covers:
- HTML stripping from job descriptions
- URL field preservation (canonical_url, apply_url)
- source_job_id preservation
- skills preservation
- published_at parsing
- Plain-text descriptions are returned unchanged
"""

from __future__ import annotations



from src.pipeline.normalize import _strip_html, normalize_job, normalize_jobs


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

class TestStripHtml:
    def test_plain_text_unchanged(self):
        text = "Software Engineer at Acme. We build cool things."
        assert _strip_html(text) == text

    def test_strips_simple_tags(self):
        html = "<p>Hello <b>world</b></p>"
        result = _strip_html(html)
        assert "<" not in result
        assert "Hello" in result
        assert "world" in result

    def test_strips_inline_css(self):
        html = '<p><span style="font-size: 16px; color: #000000;">Job description</span></p>'
        result = _strip_html(html)
        assert "style=" not in result
        assert "Job description" in result

    def test_preserves_text_content(self):
        html = (
            "<h2>About the role</h2>"
            "<p>We are looking for a <strong>junior software engineer</strong>.</p>"
            "<ul><li>Python</li><li>Django</li></ul>"
        )
        result = _strip_html(html)
        assert "About the role" in result
        assert "junior software engineer" in result
        assert "Python" in result
        assert "Django" in result
        assert "<" not in result

    def test_collapses_blank_lines(self):
        html = "<p>Line one</p><p></p><p></p><p>Line two</p>"
        result = _strip_html(html)
        assert "\n\n\n" not in result

    def test_skips_style_blocks(self):
        html = "<style>.foo { color: red; }</style><p>Actual content</p>"
        result = _strip_html(html)
        assert ".foo" not in result
        assert "Actual content" in result

    def test_empty_string(self):
        assert _strip_html("") == ""

    def test_no_html_marker_no_parsing(self):
        text = "No HTML here, just plain text with 2 < 3 math."
        # Less-than sign without closing ">tag" — should not crash
        result = _strip_html(text)
        assert isinstance(result, str)

    def test_html_entities_decoded(self):
        html = "<p>AT&amp;T &mdash; great company</p>"
        result = _strip_html(html)
        assert "AT&T" in result or "AT" in result  # entities resolved
        assert "&amp;" not in result


# ---------------------------------------------------------------------------
# URL preservation
# ---------------------------------------------------------------------------

class TestUrlPreservation:
    def test_url_becomes_canonical_url(self):
        raw = {
            "title": "Junior Dev",
            "company": "Acme",
            "url": "https://example.com/jobs/123",
        }
        job = normalize_job("test", raw)
        assert job.canonical_url == "https://example.com/jobs/123"

    def test_canonical_url_field_wins_over_url(self):
        raw = {
            "title": "Junior Dev",
            "canonical_url": "https://example.com/canonical/123",
            "url": "https://example.com/generic",
        }
        job = normalize_job("test", raw)
        assert job.canonical_url == "https://example.com/canonical/123"

    def test_apply_url_preserved(self):
        raw = {
            "title": "Junior Dev",
            "apply_url": "https://example.com/apply/123",
        }
        job = normalize_job("test", raw)
        assert job.apply_url == "https://example.com/apply/123"

    def test_empty_url_becomes_none(self):
        raw = {"title": "Junior Dev", "url": ""}
        job = normalize_job("test", raw)
        assert job.canonical_url is None

    def test_missing_url_is_none(self):
        raw = {"title": "Junior Dev"}
        job = normalize_job("test", raw)
        assert job.canonical_url is None
        assert job.apply_url is None

    def test_source_job_id_preserved(self):
        raw = {"title": "Junior Dev", "source_job_id": "slug-abc-123"}
        job = normalize_job("test", raw)
        assert job.source_job_id == "slug-abc-123"

    def test_slug_becomes_source_job_id(self):
        raw = {"title": "Junior Dev", "slug": "my-job-slug"}
        job = normalize_job("test", raw)
        assert job.source_job_id == "my-job-slug"

    def test_generic_url_does_not_overwrite_real_url(self):
        """A collector-level URL should survive normalization."""
        raw = {
            "title": "Junior Dev",
            "url": "https://boards.greenhouse.io/acme/jobs/123",
        }
        job = normalize_job("test", raw)
        assert job.canonical_url == "https://boards.greenhouse.io/acme/jobs/123"


# ---------------------------------------------------------------------------
# Skills and published_at
# ---------------------------------------------------------------------------

class TestSkillsAndDates:
    def test_skills_list_preserved(self):
        raw = {"title": "Dev", "skills": ["Python", "Django", "SQL"]}
        job = normalize_job("test", raw)
        assert "Python" in job.skills
        assert "Django" in job.skills

    def test_published_at_isoformat_parsed(self):
        raw = {"title": "Dev", "published_at": "2025-06-01T12:00:00"}
        job = normalize_job("test", raw)
        assert job.published_at is not None
        assert job.published_at.year == 2025

    def test_published_at_missing_is_none(self):
        raw = {"title": "Dev"}
        job = normalize_job("test", raw)
        assert job.published_at is None


# ---------------------------------------------------------------------------
# HTML stripping integration in normalize_job
# ---------------------------------------------------------------------------

class TestNormalizeJobHtmlStrip:
    def test_description_html_stripped(self):
        raw = {
            "title": "Junior Dev",
            "description": '<p><span style="color: red;">We are hiring!</span></p>',
        }
        job = normalize_job("test", raw)
        assert "<" not in (job.description or "")
        assert "We are hiring!" in (job.description or "")

    def test_plain_description_unchanged(self):
        raw = {"title": "Dev", "description": "We need a developer."}
        job = normalize_job("test", raw)
        assert job.description == "We need a developer."


# ---------------------------------------------------------------------------
# Arbeitnow-style raw dict (regression)
# ---------------------------------------------------------------------------

class TestArbeitnowNormalization:
    def test_arbeitnow_url_preserved(self):
        raw = {
            "company": "Acme GmbH",
            "title": "Junior Software Engineer",
            "description": "<p>We build cool software.</p>",
            "location": "Berlin",
            "countries": ["Germany"],
            "remote": True,
            "url": "https://www.arbeitnow.com/jobs/acme/junior-software-engineer-12345",
            "source_job_id": "junior-software-engineer-12345",
        }
        job = normalize_job("arbeitnow", raw)
        assert job.canonical_url == "https://www.arbeitnow.com/jobs/acme/junior-software-engineer-12345"
        assert "<" not in (job.description or "")
        assert "We build cool software" in (job.description or "")
        assert job.remote is True


# ---------------------------------------------------------------------------
# normalize_jobs batch helper
# ---------------------------------------------------------------------------

class TestDepartmentAndWorkMode:
    def test_department_code_populated_from_department_code_key(self):
        raw = {"title": "Dev", "department_code": "75"}
        job = normalize_job("test", raw)
        assert job.department == "75"

    def test_department_code_missing_is_none(self):
        raw = {"title": "Dev"}
        job = normalize_job("test", raw)
        assert job.department is None

    def test_unrelated_department_key_does_not_leak_into_department_field(self):
        """Regression: Ashby's raw 'department' key is an org/team department,
        not geography, and must not populate Job.department."""
        raw = {"title": "Dev", "department": "Tech & Data"}
        job = normalize_job("test", raw)
        assert job.department is None

    def test_work_mode_valid_value_preserved(self):
        for value in ("remote", "hybrid", "on_site"):
            raw = {"title": "Dev", "work_mode": value}
            job = normalize_job("test", raw)
            assert job.work_mode == value

    def test_work_mode_invalid_value_becomes_none(self):
        raw = {"title": "Dev", "work_mode": "some_unexpected_value"}
        job = normalize_job("test", raw)
        assert job.work_mode is None

    def test_work_mode_missing_is_none(self):
        raw = {"title": "Dev"}
        job = normalize_job("test", raw)
        assert job.work_mode is None


class TestNormalizeJobsBatch:
    def test_batch_normalizes_all(self):
        raws = [
            {"title": "Job A", "url": "https://example.com/a"},
            {"title": "Job B", "url": "https://example.com/b"},
        ]
        jobs = normalize_jobs("test_source", raws)
        assert len(jobs) == 2
        assert jobs[0].canonical_url == "https://example.com/a"

    def test_batch_skips_on_error(self):
        raws = [
            {"title": "Good job"},
            None,  # invalid entry
        ]
        jobs = normalize_jobs("test_source", raws)
        # None is not a dict so it causes a warning but should not crash
        # The batch returns what it can
        assert isinstance(jobs, list)
