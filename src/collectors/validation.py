"""Per-collector validation criteria for leaving the collection freeze (Phase B B8-G, D7, ADR 0032).

Owner decision D7 keeps every external collector off until it "has passed its own validation
criteria" and the owner approves its reactivation. This module is the list of those criteria,
collector by collector, and what currently backs each one:

- ``evidence``: offline tests (``tests/<file>.py::<test>``) that exercise the criterion without
  contacting the source. ``tests/test_collector_validation.py`` checks that each one exists.
- ``supervised``: what only a supervised run can show (a live run the owner authorises for this
  collector alone). Nothing here is verified while collection is frozen.
- ``open``: a known gap or finding to resolve, or accept explicitly, before validation.

The eight criteria are the ones D7 names. A collector can be added to
``src.collectors.freeze.VALIDATED_COLLECTORS`` only when every criterion has evidence, no open item
remains, and its supervised checks are recorded; the user then still has to turn it back on. Today
no collector meets that (no supervised run is authorised), so ``VALIDATED_COLLECTORS`` stays empty.

``python -m src.collectors.validation`` prints the table.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CRITERIA: tuple[str, ...] = (
    "coverage", "reliability", "latency", "deduplication", "pagination", "error_handling",
    "source_enablement", "access_rules",
)


@dataclass(frozen=True)
class Criterion:
    evidence: tuple[str, ...] = ()
    supervised: tuple[str, ...] = ()
    open: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.open:
            return "open"
        if not self.evidence:
            return "no evidence"
        return "offline ok, supervised pending" if self.supervised else "offline ok"


@dataclass(frozen=True)
class CollectorValidation:
    source_type: str
    configured: bool
    criteria: dict[str, Criterion] = field(default_factory=dict)
    note: str = ""

    def ready_for_supervised_check(self) -> bool:
        """Every criterion backed by offline evidence and no open item left."""
        return self.configured and all(
            c.evidence and not c.open for c in (self.criteria.get(k) for k in CRITERIA) if c is not None
        ) and set(self.criteria) == set(CRITERIA)


# Shared by every collector: the freeze and the one enablement rule, and the source contract.
_ENABLEMENT = Criterion(evidence=(
    "tests/test_collection_freeze.py::test_a_pipeline_run_never_builds_a_frozen_collector",
    "tests/test_collection_freeze.py::test_a_validated_collector_comes_back_only_after_the_users_reactivation",
    "tests/test_collection_freeze.py::test_the_scheduler_dispatches_nothing_frozen_and_records_no_execution",
    "tests/test_source_enablement.py::test_a_switchable_source_is_on_only_when_configured_and_chosen",
    "tests/test_source_enablement.py::test_a_switched_off_source_is_not_reinjected_into_a_run",
))
_ISOLATION = (
    "tests/test_orchestrator_collector_isolation.py::test_one_failing_collector_does_not_fail_the_run",
)
_LATENCY_LIVE = "collection seconds per run (by_source.seconds, B7-B) within the range agreed for this source"
_YIELD_LIVE = "newly discovered useful offers per request and per second (src/pipeline/counts.py) on a live run"

FT = "tests/test_france_travail.py::test_"
FTW = "tests/test_france_travail_web_collector.py::test_"
EU = "tests/test_eures_collector.py::test_"
IN = "tests/test_indeed_collector.py::test_"
HW = "tests/test_hellowork_collector.py::test_"
LI = "tests/test_linkedin_scanner.py::test_"
SC = "tests/test_source_contract.py::test_"
X2 = "tests/test_second_expansion_collectors.py::test_"
JS = "tests/test_collector_json_safety.py::test_"

COLLECTORS: dict[str, CollectorValidation] = {
    "france_travail": CollectorValidation("france_travail", True, {
        "coverage": Criterion(
            evidence=(FT + "multiple_queries_each_run_to_exhaustion", FT + "discovery_queries_override_search_keywords",
                      FT + "no_default_search_term_assumes_a_profession"),
            supervised=(_YIELD_LIVE,),
            open=("A saturated query (e.g. 'informatique') hits the API's result ceiling and is incomplete "
                  "(Phase B B4-D); a narrower query plan is needed.",)),
        "reliability": Criterion(evidence=(FT + "malformed_api_response_missing_resultats_captured",
                                           FT + "all_jobs_pass_validation", FT + "job_fields_are_populated")),
        "latency": Criterion(evidence=(FT + "429_with_retry_after_header",), supervised=(_LATENCY_LIVE,)),
        "deduplication": Criterion(evidence=(FT + "duplicate_jobs_across_queries_are_removed",
                                             FT + "no_duplicates_across_pages")),
        "pagination": Criterion(evidence=(FT + "three_pages_returns_47_jobs_not_20",
                                          FT + "api_total_from_content_range_stops_pagination",
                                          FT + "empty_page_terminates_pagination")),
        "error_handling": Criterion(evidence=(FT + "token_http_error_is_captured_not_raised",
                                              FT + "search_http_error_is_captured_not_raised",
                                              FT + "network_error_on_token_request_is_captured",
                                              FT + "failed_query_does_not_abort_remaining_queries",
                                              FT + "transient_500_is_retried", *_ISOLATION)),
        "source_enablement": _ENABLEMENT,
        "access_rules": Criterion(
            evidence=(FT + "missing_credentials_makes_no_http_call", FT + "bearer_token_sent_in_authorization_header",
                      "tests/test_collection_freeze.py::test_preflight_skips_frozen_sources_and_never_authenticates"),
            supervised=("API access granted to this installation (restricted API; no access is set up here)",),
            open=("Requests go through its own HTTP client and are not counted (counts_requests = False, B8-A): "
                  "no request yield until they are.",)),
    }, note="Documented API with credentials."),
    "france_travail_web": CollectorValidation("france_travail_web", True, {
        "coverage": Criterion(
            evidence=(FTW + "a_junior_it_profile_with_typed_labels_plans_pages",
                      FTW + "a_typed_label_reaches_the_codes_of_the_family_it_resolves_to",
                      FTW + "target_terms_from_occupations_are_read_by_name",
                      FTW + "a_profile_matching_no_occupation_page_is_skipped_not_failed"),
            supervised=(_YIELD_LIVE,),
            open=("Preset occupation codes are broad: on the 363 replayed offers, 62 pass the filter and 1 is "
                  "useful (B8-A replay); the B5-E occupation boost was not shipped for that reason.",)),
        "reliability": Criterion(evidence=(FTW + "live_listing_pages_still_parse",
                                           SC + "recorded_fixture_still_parses_to_at_least_one_job",
                                           SC + "canary_reports_a_served_page_that_yielded_nothing")),
        "latency": Criterion(evidence=(FTW + "requests_are_spaced_by_the_rate_limit",
                                       FTW + "rate_limit_is_never_below_one_second"), supervised=(_LATENCY_LIVE,)),
        # An offer listed on two département pages comes back twice with one offer number; the
        # pipeline's same-source deduplication keeps one.
        "deduplication": Criterion(evidence=("tests/test_deduplication.py::test_same_id_is_duplicate",
                                             "tests/test_deduplication.py::test_same_hash_different_ids_collapsed")),
        "pagination": Criterion(evidence=(FTW + "page_cap_is_shared_across_departements",
                                          FTW + "collect_reads_only_pages_published_in_the_sitemap")),
        "error_handling": Criterion(evidence=(FTW + "a_server_error_is_tried_once_more",
                                              FTW + "a_lasting_server_error_is_reported_per_page",
                                              FTW + "unreachable_site_is_one_clear_error",
                                              FTW + "nothing_to_search_is_reported_not_raised", *_ISOLATION)),
        "source_enablement": _ENABLEMENT,
        "access_rules": Criterion(evidence=(FTW + "robots_txt_not_served_means_nothing_is_crawled",
                                            FTW + "sitemap_urls_outside_the_site_are_not_followed",
                                            SC + "france_travail_web_keeps_its_strict_fail_closed_reading",
                                            SC + "no_public_page_collector_requests_a_url_robots_txt_disallows")),
    }, note="Public pages, robots.txt read strictly (fail closed)."),
    "eures": CollectorValidation("eures", True, {
        "coverage": Criterion(
            evidence=(EU + "discovery_queries_run_against_the_default_location_codes",
                      EU + "explicit_queries_and_discovery_queries_are_both_run",
                      EU + "no_default_queries_assume_a_profession"),
            supervised=(_YIELD_LIVE,),
            open=("Low yield in the audited run: 1,995 collected, 99 accepted (run 5f38fef7, P-004); the query "
                  "plan must be revisited before reactivation (no budget change now, D7).",)),
        "reliability": Criterion(evidence=(EU + "result_contract", EU + "malformed_response_missing_jvs_key")),
        "latency": Criterion(evidence=(SC + "requests_to_a_site_wait_its_crawl_delay", SC + "a_huge_crawl_delay_is_capped"),
                             supervised=(_LATENCY_LIVE + "; the audited collection stage took 457-464 s with its "
                                         "10 s crawl delay",)),
        "deduplication": Criterion(evidence=(EU + "duplicate_ids_skipped", EU + "cross_query_deduplication")),
        "pagination": Criterion(evidence=(EU + "pagination_stops_when_fewer_than_page_size",
                                          EU + "max_pages_per_query_caps_requests",
                                          EU + "page_cap_with_every_counted_offer_fetched_is_complete",
                                          EU + "per_query_status_reports_why_each_query_stopped")),
        "error_handling": Criterion(evidence=(EU + "network_error_recorded_continues_next_query",
                                              EU + "http_500_retried_then_error",
                                              EU + "http_307_sorry_page_retried_then_error", *_ISOLATION)),
        "source_enablement": _ENABLEMENT,
        "access_rules": Criterion(evidence=(SC + "crawl_delay_is_read_from_the_group_that_applies",
                                            SC + "request_cap_refuses_further_requests_and_is_reported",
                                            SC + "robots_refusal_is_not_counted_as_a_request")),
    }, note="Public search API of the European job portal; robots.txt Crawl-delay 10 s honoured."),
    "indeed": CollectorValidation("indeed", True, {
        "coverage": Criterion(
            evidence=(IN + "collect_passes_the_query_to_jobspy_and_dedups_across_queries",),
            supervised=(_YIELD_LIVE,),
            open=("Listing completeness cannot be known: the answer gives no total (Phase B B4-D).",)),
        "reliability": Criterion(evidence=(IN + "parser_canary_reads_the_fixture", IN + "fixture_rows_map_with_stable_ids_and_urls",
                                           IN + "proxy_failure_is_no_answer_and_unreadable_rows_raise_the_canary")),
        "latency": Criterion(evidence=(IN + "queries_are_spaced_by_the_rate_limit",), supervised=(_LATENCY_LIVE,)),
        "deduplication": Criterion(evidence=(IN + "collect_passes_the_query_to_jobspy_and_dedups_across_queries",)),
        "pagination": Criterion(evidence=(IN + "pages_returned_count_against_the_run_budget",)),
        "error_handling": Criterion(evidence=(IN + "jobspy_error_is_reported_and_the_next_query_still_runs",
                                              IN + "an_exception_in_jobspy_never_stops_the_run",
                                              IN + "jobspy_13_errors_are_read_from_its_log",
                                              IN + "empty_answer_is_a_genuine_empty_result", *_ISOLATION)),
        "source_enablement": _ENABLEMENT,
        "access_rules": Criterion(
            evidence=(IN + "pages_returned_count_against_the_run_budget",
                      "tests/test_indeed_redundancy_coverage.py::test_indeed_collector_documents_tos_risk"),
            open=("Reads the site through a scraping library; its terms-of-use risk is documented, not "
                  "resolved: the owner must accept or reject it explicitly.",)),
    }, note="Read through the jobspy library."),
    "hellowork": CollectorValidation("hellowork", True, {
        "coverage": Criterion(
            evidence=(HW + "domain_expands_to_metier_slugs", HW + "full_collection_using_domains"),
            supervised=(_YIELD_LIVE,),
            open=("Each listing page exposes 20 offers and robots.txt forbids query-string paging: breadth "
                  "comes only from the number of occupation pages read (collector docstring, verified "
                  "live 2026-09-05).",)),
        "reliability": Criterion(evidence=(HW + "entity_encoded_jsonld_pages_are_collected",
                                           HW + "listing_pages_it_cannot_read_are_reported_not_silent",
                                           SC + "recorded_fixture_still_parses_to_at_least_one_job")),
        "latency": Criterion(evidence=(SC + "configured_delay_never_goes_below_the_policy_floor",),
                             supervised=(_LATENCY_LIVE,)),
        "deduplication": Criterion(evidence=(HW + "duplicate_job_ids_across_slugs_deduplicated",
                                             HW + "multiple_domains_deduplicated_and_merged",
                                             "tests/test_hellowork_scanner.py::test_rescan_does_not_duplicate")),
        "pagination": Criterion(evidence=(HW + "max_jobs_per_slug_bounds_detail_fetches",)),
        "error_handling": Criterion(evidence=(HW + "listing_failure_recorded_others_continue",
                                              HW + "detail_page_failure_recorded",
                                              HW + "domain_fetch_failure_recorded_other_domains_continue",
                                              "tests/test_hellowork_scanner.py::test_collector_errors_do_not_fail_the_scan")),
        "source_enablement": _ENABLEMENT,
        "access_rules": Criterion(evidence=(SC + "every_public_page_collector_is_covered_here",
                                            SC + "no_public_page_collector_requests_a_url_robots_txt_disallows")),
    }, note="Public pages; staged source (scanned outside the pipeline)."),
    "linkedin": CollectorValidation("linkedin", True, {
        "coverage": Criterion(evidence=(LI + "unique_jobs_in_each_query_all_staged", LI + "stats_query_stopped_by_exhaustion"),
                              supervised=(_YIELD_LIVE,)),
        "reliability": Criterion(evidence=(LI + "blocked_empty_page_is_a_failure_not_exhaustion",
                                           LI + "incomplete_job_is_rediscovered_and_completed")),
        "latency": Criterion(supervised=(_LATENCY_LIVE,)),
        "deduplication": Criterion(evidence=(LI + "dedup_within_scan_query", LI + "rescan_does_not_duplicate",
                                             LI + "cross_query_dups_counted_in_stats")),
        "pagination": Criterion(evidence=(LI + "fetches_multiple_pages_when_new_jobs_found",
                                          LI + "pagination_stops_on_empty_results", LI + "safety_limit_max_pages")),
        "error_handling": Criterion(evidence=(LI + "scan_query_failure_is_non_fatal",
                                              LI + "query_failure_does_not_abort_remaining_queries",
                                              LI + "run_scan_fails_gracefully_when_jobspy_missing")),
        "source_enablement": _ENABLEMENT,
        "access_rules": Criterion(
            evidence=(LI + "disabled_by_default",),
            open=("Reads the site through a scraping library; whether its access rules allow this is not "
                  "assessed: the owner must accept or reject it explicitly.",)),
    }, note="Staged source read through the jobspy library; off by default."),
    "bundesagentur": CollectorValidation("bundesagentur", True, {
        "coverage": Criterion(evidence=(X2 + "successful_collection",), supervised=(_YIELD_LIVE,)),
        "reliability": Criterion(evidence=(X2 + "normalize_fields_missing_optional",
                                           JS + "malformed_response_is_captured_not_raised")),
        "latency": Criterion(supervised=(_LATENCY_LIVE,)),
        "deduplication": Criterion(evidence=(X2 + "deduplication_across_search_terms",)),
        "pagination": Criterion(open=("One page per search term; no test of what lies beyond it.",)),
        "error_handling": Criterion(evidence=(X2 + "network_error_per_term", X2 + "empty_response", *_ISOLATION)),
        "source_enablement": _ENABLEMENT,
        "access_rules": Criterion(evidence=(SC + "every_registered_collector_declares_a_valid_access_class",),
                                  open=("Unofficial API key documented by a third-party project; opt-in only.",)),
    }, note="Inactive in the shipped configuration."),
    "arbetsformedlingen": CollectorValidation("arbetsformedlingen", True, {
        "coverage": Criterion(evidence=(X2 + "successful_collection",), supervised=(_YIELD_LIVE,)),
        "reliability": Criterion(evidence=(X2 + "normalize_fields_missing_optional",
                                           JS + "malformed_response_is_captured_not_raised")),
        "latency": Criterion(supervised=(_LATENCY_LIVE,)),
        "deduplication": Criterion(evidence=(X2 + "deduplication_across_search_terms",)),
        "pagination": Criterion(open=("Bounded page size per search term; no test of what lies beyond it.",)),
        "error_handling": Criterion(evidence=(X2 + "network_error_per_term", X2 + "empty_response", *_ISOLATION)),
        "source_enablement": _ENABLEMENT,
        "access_rules": Criterion(evidence=(SC + "every_registered_collector_declares_a_valid_access_class",)),
    }, note="Inactive in the shipped configuration."),
}

# Registered collector types no shipped configuration uses: frozen, validation not started.
for _type in ("greenhouse", "lever", "smartrecruiters", "workingnomads", "remote", "rss"):
    COLLECTORS[_type] = CollectorValidation(_type, False, note="Not configured; validation not started.")


def report() -> list[dict[str, str]]:
    """One row per collector and criterion."""
    rows: list[dict[str, str]] = []
    for c in COLLECTORS.values():
        if not c.configured:
            rows.append({"collector": c.source_type, "criterion": "-", "status": "not configured"})
            continue
        for name in CRITERIA:
            rows.append({"collector": c.source_type, "criterion": name, "status": c.criteria[name].status})
    return rows


if __name__ == "__main__":  # pragma: no cover - a reading aid
    for row in report():
        print(f"{row['collector']:<20} {row['criterion']:<18} {row['status']}")
