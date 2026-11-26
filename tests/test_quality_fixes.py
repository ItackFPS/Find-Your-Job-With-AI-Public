"""Quality fixes — regression tests.

Covers:
  Priority 9 — Cloud/DevOps vocabulary expansion
  The domain keyword configuration contract and the seniority filter.
"""

from __future__ import annotations


from tests.fixtures.profiles import TEST_PROFILE_PATH


# ---------------------------------------------------------------------------
# Priority 9 — Cloud/DevOps vocabulary expansion
# ---------------------------------------------------------------------------

class TestCloudDevOpsVocabulary:
    """Ensure new Cloud/DevOps terms pass the hard filter."""

    def _make_filter_config(self):
        from src.pipeline.filter import build_filter_config, load_filters, load_profile

        profile = load_profile(TEST_PROFILE_PATH)
        engine = load_filters("config/filters.yaml")
        return build_filter_config(profile, engine)

    def _passes_filter(self, title: str) -> bool:
        from src.models.job import Job
        from src.pipeline.filter import filter_jobs

        cfg = self._make_filter_config()
        job = Job(
            source="indeed",
            title=title,
            location="France",
            countries=["France"],
            remote=False,
        )
        passed, _ = filter_jobs([job], cfg)
        return len(passed) > 0

    def test_cloud_architect_passes(self):
        assert self._passes_filter("Cloud Architect AWS/Azure")

    def test_architecte_cloud_passes(self):
        assert self._passes_filter("Architecte Cloud GCP")

    def test_cloud_native_engineer_passes(self):
        assert self._passes_filter("Cloud Native Engineer Kubernetes")

    def test_cloud_native_hyphen_passes(self):
        assert self._passes_filter("Cloud-Native Platform Engineer")

    def test_cloudops_engineer_passes(self):
        assert self._passes_filter("CloudOps Engineer Azure")

    def test_cloud_infrastructure_passes(self):
        assert self._passes_filter("Cloud Infrastructure Engineer")

    def test_finops_passes(self):
        assert self._passes_filter("FinOps Cloud Engineer")

    def test_existing_devops_still_passes(self):
        assert self._passes_filter("DevOps Engineer France")

    def test_existing_sre_still_passes(self):
        assert self._passes_filter("Site Reliability Engineer")

    def test_existing_cloud_engineer_still_passes(self):
        assert self._passes_filter("Cloud Engineer GCP")

    def test_non_it_cloud_sales_rejected(self):
        """'Cloud Sales' is not a DevOps/Cloud engineering role."""
        # A Sales role title — check it doesn't accidentally pass
        # (depends on excluded_title_keywords and domain mismatch)
        assert not self._passes_filter("Cloud Sales Representative")

    def test_systems_administrator_passes(self):
        assert self._passes_filter("Systems Administrator Linux")

    # -- Automation-domain vocabulary (2026-09-12 forensic audit fix) -------
    # config/profile.yaml's target_domains used "automation" (English) but
    # this richer cloud/devops/sre/platform vocabulary previously lived
    # under a separate, never-referenced "automatisation" (French) key in
    # config/filters.yaml — merged into "automation" as the single
    # canonical bucket. These specific titles were the confirmed failures.

    def test_ingenieur_devops_passes(self):
        assert self._passes_filter("Ingénieur DevOps")

    def test_automation_engineer_passes(self):
        assert self._passes_filter("Automation Engineer")

    def test_ingenieur_automatisation_passes(self):
        assert self._passes_filter("Ingénieur Automatisation")

    def test_platform_engineer_passes(self):
        assert self._passes_filter("Platform Engineer")

    def test_devsecops_passes(self):
        assert self._passes_filter("DevSecOps Engineer")

    # -- Systems-domain vocabulary (2026-09-12 Pass 4 audit fix) -------------
    # config/filters.yaml's "systems" (English, referenced by
    # profile.yaml's target_domains) bucket was missing the plural
    # "systems administrator" keyword that its "systèmes" (French alias)
    # counterpart already had — a smaller instance of the same
    # French/English drift pattern as the automation/automatisation split.
    # "Systems Administrator" (no extra qualifying words) was the confirmed
    # live failure; the others are regression guards proving the fix
    # didn't disturb adjacent, already-working title variants.

    def test_systems_administrator_bare_title_passes(self):
        """The confirmed failure: no qualifying word (e.g. "Linux") to
        rescue it via a different keyword — this title depended entirely
        on the missing plural keyword."""
        assert self._passes_filter("Systems Administrator")

    def test_system_administrator_singular_still_passes(self):
        assert self._passes_filter("System Administrator")

    def test_administrateur_systemes_still_passes(self):
        assert self._passes_filter("Administrateur Systèmes")

    def test_technicien_systemes_still_passes(self):
        assert self._passes_filter("Technicien Systèmes")

    def test_sysadmin_still_passes(self):
        assert self._passes_filter("Sysadmin")

    def test_unrelated_titles_not_falsely_accepted_by_systems_fix(self):
        """The single-keyword addition must not broaden matching for
        titles that merely contain 'system'/'systems' in an unrelated
        sense."""
        for title in ["Solar Systems Sales Representative", "Ecosystems Marketing Manager"]:
            assert not self._passes_filter(title), f"{title!r} should still be rejected"


class TestDomainKeywordConfigurationContract:
    """Configuration contract: every profile.target_domains entry must
    resolve to a present, non-empty domain_keywords bucket in
    filters.yaml — never silently fall through to a missing/empty bucket
    (the literal-domain-string fallback in build_filter_config). This is
    the generic invariant that the automation/automatisation split
    violated: profile.yaml said "automation", filters.yaml's richer
    vocabulary was keyed "automatisation", and nothing caught the mismatch
    until a job like "Cloud Architect" was silently rejected.

    Deliberately does NOT assert a minimum keyword count (arbitrary,
    brittle) — only presence and non-emptiness, which is the actual,
    well-defined contract. Vocabulary *correctness/richness* for specific
    domains is covered by dedicated tests (e.g. TestCloudDevOpsVocabulary
    for "automation"), not by this generic structural check.
    """

    def test_every_target_domain_resolves_to_a_populated_bucket(self):
        from src.pipeline.filter import load_filters, load_profile

        profile = load_profile(TEST_PROFILE_PATH)
        engine_cfg = load_filters("config/filters.yaml")
        domain_keywords = engine_cfg.get("domain_keywords", {})

        unresolved = []
        empty = []
        for domain in profile.target_domains:
            bucket = domain_keywords.get(domain)
            if bucket is None:
                unresolved.append(domain)
                continue
            if not bucket.get("title"):
                empty.append(domain)

        assert not unresolved, (
            f"target_domains {unresolved} have NO matching domain_keywords "
            "entry in filters.yaml — build_filter_config() silently falls "
            "back to using the literal domain name as the only keyword."
        )
        assert not empty, (
            f"target_domains {empty} resolve to a domain_keywords entry "
            "with zero title keywords — effectively unmatchable."
        )

    def test_automation_domain_is_not_split_across_two_keys(self):
        """Regression guard for the specific bug: filters.yaml must not
        have a French "automatisation" key sitting alongside the canonical
        "automation" key — that split is exactly what silently orphaned
        the cloud/devops/sre vocabulary from profile.yaml's selected
        domain last time. A future edit reintroducing a parallel
        "automatisation" bucket should fail this test immediately."""
        from src.pipeline.filter import load_filters

        engine_cfg = load_filters("config/filters.yaml")
        domain_keywords = engine_cfg.get("domain_keywords", {})
        assert "automatisation" not in domain_keywords, (
            "A separate 'automatisation' domain_keywords key has reappeared — "
            "merge its content into 'automation' (the canonical key profile.yaml "
            "uses) instead of maintaining two divergent copies."
        )

    # -- French-language alias pairs must not silently drift -----------------
    # config/filters.yaml documents four "French alias for X" domain_keywords
    # entries (informatique has no single English 1:1 counterpart, so it's
    # excluded): systèmes/systems, cybersécurité/cybersecurity, réseaux/
    # networking, développement logiciel/software_development. Each pair is
    # maintained as two INDEPENDENT hand-written keyword lists rather than a
    # true single source of truth — exactly the design that let
    # "systems"/"systèmes" drift by one keyword ("systems administrator"),
    # silently rejecting a standard, common job title (2026-09-12 Pass 4
    # audit). Only the two pairs proven to be in sync (and that a real,
    # common job title depends on) are asserted strictly equal here.
    #
    # "réseaux"/"networking" and "développement logiciel"/"software_development"
    # are KNOWN to currently differ (see git history / Pass 3-4 audit
    # reports) — réseaux's extra keywords were proven harmless (all
    # functionally redundant via networking's own bare "réseaux"/"reseaux"
    # substring matches, except one unrealistic hybrid title with low
    # real-world likelihood), and développement logiciel's gap only matters
    # for a profile that selects the French domain name (not the case
    # today). Deliberately excluded here rather than silently passed, so
    # this comment — not test-list omission — is the record of that
    # decision; fixing them is out of this pass's scope.
    _SYNCED_ALIAS_PAIRS = [
        ("systems", "systèmes"),
        ("cybersecurity", "cybersécurité"),
    ]

    def test_synced_alias_pairs_have_identical_title_vocabulary(self):
        from src.pipeline.filter import load_filters

        engine_cfg = load_filters("config/filters.yaml")
        domain_keywords = engine_cfg.get("domain_keywords", {})

        mismatches = {}
        for english_key, french_key in self._SYNCED_ALIAS_PAIRS:
            eng = set(domain_keywords.get(english_key, {}).get("title", []))
            fr = set(domain_keywords.get(french_key, {}).get("title", []))
            diff = eng ^ fr
            if diff:
                mismatches[f"{english_key}/{french_key}"] = sorted(diff)

        assert not mismatches, (
            f"Alias pairs expected to stay in sync have drifted: {mismatches}. "
            "Add the missing keyword(s) to whichever side is behind."
        )


# ---------------------------------------------------------------------------
# Seniority filtering — "Sr." abbreviation
# ---------------------------------------------------------------------------

class TestSeniorityFilter:
    """Seniority exclusion must catch 'Sr.' abbreviation for Senior."""

    def _passes_filter(self, title: str) -> bool:
        from src.models.job import Job
        from src.pipeline.filter import build_filter_config, filter_jobs, load_filters, load_profile

        profile = load_profile(TEST_PROFILE_PATH)
        engine = load_filters("config/filters.yaml")
        cfg = build_filter_config(profile, engine)
        job = Job(source="indeed", title=title, location="France", countries=["France"], remote=False)
        passed, _ = filter_jobs([job], cfg)
        return len(passed) > 0

    def test_sr_devops_rejected(self):
        """'Sr. DevOps Engineer' must be rejected — abbreviation for Senior."""
        assert not self._passes_filter("Sr. DevOps Engineer")

    def test_sr_space_rejected(self):
        assert not self._passes_filter("Sr Software Engineer")

    def test_senior_devops_rejected(self):
        assert not self._passes_filter("Senior DevOps Engineer")

    def test_junior_devops_passes(self):
        assert self._passes_filter("Junior DevOps Engineer")

    def test_devops_no_seniority_passes(self):
        """Title with no seniority marker passes normally."""
        assert self._passes_filter("DevOps Engineer France")

    def test_sre_no_seniority_passes(self):
        assert self._passes_filter("Site Reliability Engineer")

    # -- principal/staff/c_suite (2026-09-12 forensic audit fix) -------------
    # filters.yaml's seniority_exclusions already defined "principal",
    # "staff", and "c_suite" groups, but profile.yaml's excluded_seniority
    # never referenced them — so a junior-only profile silently accepted
    # "Staff DevOps Engineer"-style titles. Fixed by adding all three to
    # profile.yaml's excluded_seniority (the existing configuration
    # mechanism), plus extending the "staff" group's compound-phrase list
    # (it had no DevOps/Cloud/Platform entries at all — see filters.yaml).

    def test_lead_devops_rejected(self):
        """Already correctly configured before this fix — regression guard
        proving the fix didn't disturb existing seniority semantics."""
        assert not self._passes_filter("Lead DevOps Engineer")

    def test_staff_devops_rejected(self):
        assert not self._passes_filter("Staff DevOps Engineer")

    def test_principal_devops_rejected(self):
        assert not self._passes_filter("Principal DevOps Engineer")

    def test_c_suite_example_rejected(self):
        assert not self._passes_filter("Chief Technology Officer")

    def test_devops_engineer_still_passes(self):
        """No seniority marker at all — must be unaffected by the fix."""
        assert self._passes_filter("DevOps Engineer")

    def test_junior_devops_engineer_still_passes(self):
        assert self._passes_filter("Junior DevOps Engineer")
