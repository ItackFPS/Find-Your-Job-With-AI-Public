"""Regression tests for the conservative deduplication identity hierarchy.

Identity rules under test
-------------------------
Intra-source:
  (source, source_job_id)  → definite duplicate
  (source, canonical_url)  → duplicate when source_job_id absent
  neither                  → keep; do NOT deduplicate

Cross-source:
  same canonical_url       → duplicate
  (company, title, location, employment_type) → duplicate ONLY when company non-empty
  different location       → NOT duplicate
  different employment_type→ NOT duplicate
  null/empty company       → NOT deduplicated on content alone
"""

from __future__ import annotations


from src.models.job import Job
from src.pipeline.deduplicate import (
    cross_source_deduplicate,
    deduplicate_jobs,
    intra_source_deduplicate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job(**kwargs) -> Job:
    """Minimal Job with sensible defaults; override via kwargs."""
    base: dict = {
        "source": "source_a",
        "source_job_id": None,
        "canonical_url": None,
        "company": "Acme Corp",
        "title": "Software Engineer",
        "location": "Paris, France",
        "employment_type": "full-time",
    }
    base.update(kwargs)
    return Job(**base)


# ---------------------------------------------------------------------------
# Intra-source: same source_job_id → duplicate
# ---------------------------------------------------------------------------

class TestIntraSourceById:
    def test_same_id_is_duplicate(self):
        jobs = [
            _job(id="a", source_job_id="J001"),
            _job(id="b", source_job_id="J001"),
        ]
        result = intra_source_deduplicate(jobs)
        assert len(result) == 1
        assert result[0].id == "a"  # first occurrence kept

    def test_different_ids_not_duplicate(self):
        jobs = [
            _job(id="a", source_job_id="J001"),
            _job(id="b", source_job_id="J002"),
        ]
        result = intra_source_deduplicate(jobs)
        assert len(result) == 2

    def test_same_id_different_sources_not_intra_duplicate(self):
        jobs = [
            _job(id="a", source="src_a", source_job_id="J001"),
            _job(id="b", source="src_b", source_job_id="J001"),
        ]
        result = intra_source_deduplicate(jobs)
        # Different sources → intra-source dedup does not collapse them
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Intra-source: content_hash tier (LinkedIn location-variant dedup)
# ---------------------------------------------------------------------------

class TestIntraSourceByContentHash:
    """Intra-source content_hash tier catches same-source location variants.

    LinkedIn posts the same vacancy under different job IDs (and different
    canonical URLs) for each office location.  These have the same content_hash
    (md5(title + company) when no description is present).  The content_hash
    tier must collapse them while keeping genuinely different vacancies.
    """

    def _linkedin_job(self, job_id: str, content_hash: str, location: str = "Paris, France") -> Job:
        return _job(
            id=job_id,
            source="linkedin",
            source_job_id=job_id,
            canonical_url=f"https://www.linkedin.com/jobs/view/{job_id}",
            content_hash=content_hash,
            company="Acme Corp",
            title="DevOps Engineer",
            location=location,
        )

    def test_same_hash_different_ids_collapsed(self):
        """Same content_hash, different source_job_ids → deduplicated."""
        h = "aabbccddeeff00112233445566778899"
        jobs = [
            self._linkedin_job("LI-1001", h, "Paris, France"),
            self._linkedin_job("LI-1002", h, "Lyon, France"),
            self._linkedin_job("LI-1003", h, "Bordeaux, France"),
        ]
        result = intra_source_deduplicate(jobs)
        assert len(result) == 1, (
            "Three LinkedIn location variants with the same content_hash must collapse to one"
        )
        assert result[0].id == "LI-1001"  # first occurrence kept

    def test_different_hashes_different_ids_kept(self):
        """Different content_hash → distinct vacancies, both kept."""
        jobs = [
            self._linkedin_job("LI-2001", "aaaa" * 8, "Paris, France"),
            self._linkedin_job("LI-2002", "bbbb" * 8, "Paris, France"),
        ]
        result = intra_source_deduplicate(jobs)
        assert len(result) == 2, (
            "Jobs with different content hashes must not be merged even if same source"
        )

    def test_empty_hash_not_collapsed(self):
        """Jobs with empty content_hash must not be collapsed by the hash tier."""
        jobs = [
            _job(id="a", source="linkedin", source_job_id="LI-3001", content_hash=""),
            _job(id="b", source="linkedin", source_job_id="LI-3002", content_hash=""),
        ]
        result = intra_source_deduplicate(jobs)
        assert len(result) == 2, (
            "Jobs with empty content_hash must not be collapsed — hash is not reliable"
        )

    def test_different_sources_same_hash_not_collapsed(self):
        """Content-hash dedup is intra-source only — different sources never merged here."""
        h = "ccccddddeeee" + "0" * 20
        jobs = [
            _job(id="a", source="linkedin", source_job_id="LI-4001", content_hash=h),
            _job(id="b", source="apec", source_job_id="APEC-001", content_hash=h),
        ]
        result = intra_source_deduplicate(jobs)
        assert len(result) == 2, (
            "Cross-source same-content-hash must not be merged by intra_source_deduplicate "
            "(that is handled by cross_source_deduplicate)"
        )

    def test_content_hash_tier_does_not_affect_cross_source_policy(self):
        """Adding content_hash to intra-source must not break cross-source multi-location."""
        h = "deadbeef" * 4
        jobs = [
            _job(id="a", source="s1", source_job_id="X1", content_hash=h,
                 company="Thales", title="DevOps Engineer", location="Paris, France"),
            _job(id="b", source="s2", source_job_id="X2", content_hash=h,
                 company="Thales", title="DevOps Engineer", location="Lyon, France"),
        ]
        # Intra-source: different sources, so content_hash tier does not apply
        intra = intra_source_deduplicate(jobs)
        assert len(intra) == 2, "Different sources must not be merged by intra-source dedup"


# ---------------------------------------------------------------------------
# Intra-source: canonical URL fallback
# ---------------------------------------------------------------------------

class TestIntraSourceByUrl:
    def test_same_url_same_source_is_duplicate(self):
        url = "https://jobs.example.com/post/42"
        jobs = [
            _job(id="a", source_job_id=None, canonical_url=url),
            _job(id="b", source_job_id=None, canonical_url=url),
        ]
        result = intra_source_deduplicate(jobs)
        assert len(result) == 1

    def test_url_normalized_case_insensitive(self):
        jobs = [
            _job(id="a", source_job_id=None, canonical_url="https://Jobs.Example.com/post/42"),
            _job(id="b", source_job_id=None, canonical_url="https://jobs.example.com/post/42"),
        ]
        result = intra_source_deduplicate(jobs)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Intra-source: no strong identifier → keep
# ---------------------------------------------------------------------------

class TestIntraSourceNoIdentifier:
    def test_no_id_no_url_not_deduplicated(self):
        """Jobs with no source_job_id and no canonical_url must NOT be collapsed."""
        jobs = [
            _job(id="a", source_job_id=None, canonical_url=None,
                 company="Anonymous", title="Python Developer"),
            _job(id="b", source_job_id=None, canonical_url=None,
                 company="Anonymous", title="Python Developer"),
        ]
        result = intra_source_deduplicate(jobs)
        assert len(result) == 2, (
            "Jobs without any strong identifier must be kept, not collapsed on title/company"
        )


# ---------------------------------------------------------------------------
# Cross-source: canonical URL
# ---------------------------------------------------------------------------

class TestCrossSourceByUrl:
    def test_same_url_different_sources_is_duplicate(self):
        url = "https://careers.company.com/jobs/9999"
        jobs = [
            _job(id="a", source="arbeitnow", canonical_url=url),
            _job(id="b", source="france_travail", canonical_url=url),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 1

    def test_different_urls_not_duplicate(self):
        jobs = [
            _job(id="a", source="s1", company="Alpha", title="Backend Dev",
                 canonical_url="https://example.com/job/1"),
            _job(id="b", source="s2", company="Beta", title="Frontend Dev",
                 canonical_url="https://example.com/job/2"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Cross-source: content identity — positive cases
# ---------------------------------------------------------------------------

class TestCrossSourceContentIdentityPositive:
    def test_same_company_title_location_type_is_duplicate(self):
        jobs = [
            _job(id="a", source="s1", company="THALES", title="Ingénieur DevOps",
                 location="Paris, France", employment_type="full-time"),
            _job(id="b", source="s2", company="THALES", title="Ingénieur DevOps",
                 location="Paris, France", employment_type="full-time"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 1, "Identical content across sources must collapse"

    def test_full_pipeline_dedup_collapses_mirror_posting(self):
        jobs = [
            _job(id="a", source="s1", source_job_id="J-001",
                 company="MirrorCo", title="Data Engineer",
                 location="Lyon, France", employment_type="full-time"),
            _job(id="b", source="s2", source_job_id="J-999",
                 company="MirrorCo", title="Data Engineer",
                 location="Lyon, France", employment_type="full-time"),
        ]
        deduped, removed = deduplicate_jobs(jobs)
        assert removed == 1
        assert len(deduped) == 1


# ---------------------------------------------------------------------------
# Cross-source: different location → NOT duplicate
# ---------------------------------------------------------------------------

class TestLocationDistinguishes:
    def test_same_company_title_different_location_not_duplicate(self):
        """THALES + Ingénieur DevOps + Paris ≠ THALES + Ingénieur DevOps + Lyon."""
        jobs = [
            _job(id="a", source="s1", company="THALES", title="Ingénieur DevOps",
                 location="Paris, France", employment_type="full-time"),
            _job(id="b", source="s2", company="THALES", title="Ingénieur DevOps",
                 location="Lyon, France", employment_type="full-time"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 2, "Different locations must not collapse"

    def test_three_cities_all_kept(self):
        jobs = [
            _job(id="a", source="s1", company="BigCo", title="SRE", location="Paris, France"),
            _job(id="b", source="s2", company="BigCo", title="SRE", location="Lyon, France"),
            _job(id="c", source="s3", company="BigCo", title="SRE", location="Bordeaux, France"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Cross-source: different employment type → NOT duplicate
# ---------------------------------------------------------------------------

class TestEmploymentTypeDistinguishes:
    def test_same_company_title_location_different_type_not_duplicate(self):
        jobs = [
            _job(id="a", source="s1", company="THALES", title="Ingénieur DevOps",
                 location="Paris, France", employment_type="full-time"),
            _job(id="b", source="s2", company="THALES", title="Ingénieur DevOps",
                 location="Paris, France", employment_type="internship"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 2, "Different employment types must not collapse"


# ---------------------------------------------------------------------------
# Cross-source: null/empty company → NOT content-deduplicated
# ---------------------------------------------------------------------------

class TestNullCompanyNotDeduplicated:
    def test_null_company_same_title_location_not_duplicate(self):
        jobs = [
            _job(id="a", source="s1", company=None, title="Développeur Python",
                 location="Lyon, France"),
            _job(id="b", source="s2", company=None, title="Développeur Python",
                 location="Lyon, France"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 2, "Null company must not trigger content-based dedup"

    def test_empty_string_company_same_title_not_duplicate(self):
        jobs = [
            _job(id="a", source="s1", company="", title="Développeur Python",
                 location="Lyon, France"),
            _job(id="b", source="s2", company="", title="Développeur Python",
                 location="Lyon, France"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 2, "Empty-string company must not trigger content-based dedup"


# ---------------------------------------------------------------------------
# Cross-source: different companies with same title → NOT duplicate
# ---------------------------------------------------------------------------

class TestDifferentCompaniesNotDeduplicated:
    def test_different_companies_same_title_not_duplicate(self):
        jobs = [
            _job(id="a", source="s1", company="Acme", title="Python Developer",
                 location="Paris, France", employment_type="full-time"),
            _job(id="b", source="s2", company="Globex", title="Python Developer",
                 location="Paris, France", employment_type="full-time"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 2, "Different companies with same title must not collapse"


# ---------------------------------------------------------------------------
# Full deduplicate_jobs: order preservation
# ---------------------------------------------------------------------------

class TestOrderPreservation:
    def test_first_occurrence_kept(self):
        """deduplicate_jobs is deterministic and keeps the first occurrence."""
        jobs = [
            _job(id="first", source_job_id="J001"),
            _job(id="second", source_job_id="J001"),
        ]
        deduped, _ = deduplicate_jobs(jobs)
        assert deduped[0].id == "first"

    def test_output_order_matches_input_order(self):
        """Non-duplicate jobs must appear in the same order as input."""
        jobs = [
            _job(id="a", source="s1", source_job_id="J001",
                 company="Alpha", title="Engineer A", location="Paris, France"),
            _job(id="b", source="s2", source_job_id="J002",
                 company="Beta",  title="Engineer B", location="Lyon, France"),
            _job(id="c", source="s3", source_job_id="J003",
                 company="Gamma", title="Engineer C", location="Bordeaux, France"),
        ]
        deduped, removed = deduplicate_jobs(jobs)
        assert removed == 0
        assert [j.id for j in deduped] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Source attribution — provenance survives deduplication
# ---------------------------------------------------------------------------

class TestSourceAttribution:
    def test_cross_source_duplicate_merges_sources(self):
        """When a cross-source duplicate is collapsed, both sources are recorded."""
        url = "https://careers.company.com/jobs/42"
        jobs = [
            _job(id="a", source="linkedin", canonical_url=url),
            _job(id="b", source="france_travail", canonical_url=url),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 1
        kept = result[0]
        assert "linkedin" in kept.sources
        assert "france_travail" in kept.sources

    def test_source_preserved_when_no_duplicate(self):
        """Jobs with unique content have their own source in the sources list."""
        jobs = [
            _job(id="a", source="apec",
                 company="Alpha", title="Developer", location="Paris, France"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 1
        assert "apec" in result[0].sources

    def test_content_key_duplicate_merges_sources(self):
        """Content-identity dedup also merges sources."""
        jobs = [
            _job(id="a", source="linkedin",
                 company="THALES", title="Ingénieur DevOps",
                 location="Paris, France", employment_type="full-time"),
            _job(id="b", source="apec",
                 company="THALES", title="Ingénieur DevOps",
                 location="Paris, France", employment_type="full-time"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 1
        assert "linkedin" in result[0].sources
        assert "apec" in result[0].sources

    def test_three_way_merge_collects_all_sources(self):
        """Three identical cross-source postings: all three sources tracked."""
        url = "https://example.com/job/99"
        jobs = [
            _job(id="a", source="s1", canonical_url=url),
            _job(id="b", source="s2", canonical_url=url),
            _job(id="c", source="s3", canonical_url=url),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 1
        assert set(result[0].sources) == {"s1", "s2", "s3"}


# ---------------------------------------------------------------------------
# Title normalization — gender markers and formatting variants
# ---------------------------------------------------------------------------

class TestTitleNormalization:
    def test_gender_marker_hf_variant_is_duplicate(self):
        """'Développeur Java F/H' and 'Développeur Java (H/F)' collapse."""
        jobs = [
            _job(id="a", source="s1", company="ACME",
                 title="Développeur Java F/H", location="Paris, France"),
            _job(id="b", source="s2", company="ACME",
                 title="Développeur Java (H/F)", location="Paris, France"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 1, "Gender marker variants of the same title must collapse"

    def test_gender_marker_parenthesized_euse_is_duplicate(self):
        """'Développeur(euse) web' and 'Développeur web' collapse."""
        jobs = [
            _job(id="a", source="s1", company="BetaCo",
                 title="Développeur(euse) web junior", location="Lyon, France"),
            _job(id="b", source="s2", company="BetaCo",
                 title="Développeur web junior", location="Lyon, France"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 1

    def test_different_titles_not_duplicate(self):
        """'Java Developer' and 'Python Developer' must not collapse."""
        jobs = [
            _job(id="a", source="s1", company="ACME",
                 title="Java Developer", location="Paris, France"),
            _job(id="b", source="s2", company="ACME",
                 title="Python Developer", location="Paris, France"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Company normalization — legal suffix stripping
# ---------------------------------------------------------------------------

class TestCompanyNormalization:
    def test_sa_suffix_stripped(self):
        """'THALES S.A.' and 'THALES' collapse as the same company."""
        jobs = [
            _job(id="a", source="s1", company="THALES S.A.",
                 title="Ingénieur DevOps", location="Paris, France",
                 employment_type="full-time"),
            _job(id="b", source="s2", company="THALES",
                 title="Ingénieur DevOps", location="Paris, France",
                 employment_type="full-time"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 1, "Legal suffix (S.A.) must be stripped before comparison"

    def test_different_companies_same_suffix_not_duplicate(self):
        """'Alpha S.A.' and 'Beta S.A.' must NOT collapse."""
        jobs = [
            _job(id="a", source="s1", company="Alpha S.A.",
                 title="Developer", location="Paris, France",
                 employment_type="full-time"),
            _job(id="b", source="s2", company="Beta S.A.",
                 title="Developer", location="Paris, France",
                 employment_type="full-time"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# LinkedIn + France Travail cross-source dedup
# ---------------------------------------------------------------------------

class TestLinkedInFranceTravailDedup:
    def test_same_url_linkedin_france_travail(self):
        """Same posting found on LinkedIn and France Travail via URL."""
        url = "https://www.example-employer.com/careers/jobs/123"
        jobs = [
            _job(id="a", source="linkedin", canonical_url=url,
                 company="Example Corp", title="Software Engineer",
                 location="Paris, France"),
            _job(id="b", source="france_travail", canonical_url=url,
                 company="Example Corp", title="Software Engineer",
                 location="Paris, France"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 1
        assert set(result[0].sources) == {"linkedin", "france_travail"}

    def test_same_content_linkedin_france_travail(self):
        """Same job at same company/location discovered by content identity."""
        jobs = [
            _job(id="a", source="linkedin",
                 company="Capgemini", title="Développeur Java (H/F)",
                 location="Toulouse, France", employment_type="full-time"),
            _job(id="b", source="france_travail",
                 company="Capgemini", title="Développeur Java F/H",
                 location="Toulouse, France", employment_type="full-time"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 1
        assert "linkedin" in result[0].sources
        assert "france_travail" in result[0].sources


# ---------------------------------------------------------------------------
# Part 2 — Multi-location: same company+title, different cities → NOT merged
# ---------------------------------------------------------------------------

class TestMultiLocationNotMerged:
    def test_paris_and_lyon_not_merged(self):
        """Same company+title at Paris and Lyon are separate vacancies."""
        jobs = [
            _job(id="a", source="s1", company="Capgemini",
                 title="Développeur Java", location="Paris, France",
                 employment_type="full-time"),
            _job(id="b", source="s2", company="Capgemini",
                 title="Développeur Java", location="Lyon, France",
                 employment_type="full-time"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 2, "Different locations must never auto-collapse"

    def test_three_cities_all_kept(self):
        jobs = [
            _job(id="a", source="s1", company="Alten",
                 title="Ingénieur Systèmes", location="Paris, France"),
            _job(id="b", source="s2", company="Alten",
                 title="Ingénieur Systèmes", location="Bordeaux, France"),
            _job(id="c", source="s3", company="Alten",
                 title="Ingénieur Systèmes", location="Lille, France"),
        ]
        result = cross_source_deduplicate(jobs)
        assert len(result) == 3
