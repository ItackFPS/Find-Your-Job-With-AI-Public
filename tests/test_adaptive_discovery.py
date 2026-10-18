"""Phase LATER / I15 regression tests: bounded, deterministic adaptive discovery.

Scope guard for every test here: adaptive discovery may only REORDER an
existing query list — it must never add, remove, or invent a query, never
touch the profile, never use randomness/ML, and must produce the exact same
output every time given the exact same historical input (deterministic).
"""

from __future__ import annotations


from src.discovery.adaptive import (
    QueryYieldStats,
    compute_query_yield_history,
    prioritize_queries,
)


def _run(source: str, per_query_stats: dict[str, int]) -> dict:
    """Build a run dict shaped like RunStorage.list_runs()'s own output."""
    return {"stats": {"by_source": {source: {"per_query_stats": per_query_stats}}}}


class TestZeroHistoricalData:
    def test_empty_run_list_yields_empty_history(self) -> None:
        assert compute_query_yield_history([], "france_travail") == {}

    def test_runs_with_no_matching_source_yield_empty_history(self) -> None:
        runs = [_run("apec", {"informatique": 5})]
        assert compute_query_yield_history(runs, "france_travail") == {}

    def test_prioritize_with_no_history_preserves_original_order(self) -> None:
        queries = ["informatique", "data", "cybersécurité"]
        assert prioritize_queries(queries, {}) == queries


class TestNormalHistoricalData:
    def test_aggregates_total_jobs_and_run_count_across_runs(self) -> None:
        runs = [
            _run("france_travail", {"informatique": 10, "data": 2}),
            _run("france_travail", {"informatique": 8, "data": 4}),
        ]
        history = compute_query_yield_history(runs, "france_travail")
        assert history["informatique"] == QueryYieldStats("informatique", 18, 2)
        assert history["data"] == QueryYieldStats("data", 6, 2)
        assert history["informatique"].average_yield == 9.0
        assert history["data"].average_yield == 3.0

    def test_lookback_runs_caps_how_many_runs_are_considered(self) -> None:
        runs = [_run("france_travail", {"q": 100})] * 5
        history = compute_query_yield_history(runs, "france_travail", lookback_runs=2)
        assert history["q"].run_count == 2
        assert history["q"].total_jobs == 200


class TestHighYieldQueryPromoted:
    def test_high_yield_query_moves_ahead_of_low_yield_query(self) -> None:
        queries = ["low_yield", "high_yield"]
        history = {
            "low_yield": QueryYieldStats("low_yield", 2, 3),   # avg 0.67
            "high_yield": QueryYieldStats("high_yield", 30, 3),  # avg 10.0
        }
        assert prioritize_queries(queries, history) == ["high_yield", "low_yield"]


class TestLowYieldQueryDeprioritized:
    def test_zero_yield_query_sinks_below_any_positive_yield_query(self) -> None:
        queries = ["dead_query", "good_query"]
        history = {
            "dead_query": QueryYieldStats("dead_query", 0, 4),
            "good_query": QueryYieldStats("good_query", 5, 4),
        }
        assert prioritize_queries(queries, history) == ["good_query", "dead_query"]


class TestNoisyQuery:
    def test_a_single_lucky_run_does_not_move_a_query(self) -> None:
        """min_history_runs guards against one noisy/lucky run reshuffling
        everything — a query with only 1 run of history stays in place."""
        queries = ["a", "b"]
        history = {"b": QueryYieldStats("b", 1000, 1)}  # huge yield, but only 1 run
        assert prioritize_queries(queries, history, min_history_runs=2) == ["a", "b"]

    def test_malformed_per_query_stats_entries_are_skipped_not_crashed(self) -> None:
        runs = [
            {"stats": {"by_source": {"france_travail": {"per_query_stats": {
                "good": 5,
                "bad_negative": -3,
                "bad_type": "not-a-number",
                123: 5,  # non-string key
            }}}}},
        ]
        history = compute_query_yield_history(runs, "france_travail")
        assert set(history.keys()) == {"good"}
        assert history["good"].total_jobs == 5


class TestBoundedExpansion:
    def test_prioritize_never_changes_the_query_set_or_count(self) -> None:
        queries = ["a", "b", "c", "d"]
        history = {
            "b": QueryYieldStats("b", 100, 5),
            "d": QueryYieldStats("d", 1, 5),
        }
        result = prioritize_queries(queries, history)
        assert len(result) == len(queries)
        assert set(result) == set(queries)

    def test_prioritize_is_a_pure_permutation_never_adds_new_strings(self) -> None:
        queries = ["only_one"]
        history = {"only_one": QueryYieldStats("only_one", 50, 10)}
        assert prioritize_queries(queries, history) == ["only_one"]


class TestDuplicateQueryElimination:
    def test_duplicate_query_strings_in_input_are_each_kept_in_place(self) -> None:
        """prioritize_queries doesn't dedupe (that's query_expansion's own
        job, before this runs) — it must not crash or drop a legitimate
        repeated entry, just reorder deterministically."""
        queries = ["a", "a", "b"]
        history = {"b": QueryYieldStats("b", 10, 3)}
        result = prioritize_queries(queries, history)
        assert result.count("a") == 2
        assert result[0] == "b"


class TestDeterministicOutput:
    def test_same_inputs_always_produce_the_same_order(self) -> None:
        queries = ["a", "b", "c"]
        history = {
            "a": QueryYieldStats("a", 5, 3),
            "b": QueryYieldStats("b", 5, 3),  # tie with "a"
        }
        result1 = prioritize_queries(queries, history)
        result2 = prioritize_queries(queries, history)
        assert result1 == result2 == ["a", "b", "c"]  # tie broken by original order

    def test_repeated_compute_query_yield_history_calls_are_idempotent(self) -> None:
        runs = [_run("france_travail", {"q": 7})]
        h1 = compute_query_yield_history(runs, "france_travail")
        h2 = compute_query_yield_history(runs, "france_travail")
        assert h1 == h2


class TestCorruptedOrMissingHistoricalMetrics:
    def test_non_dict_run_entries_are_skipped(self) -> None:
        runs = ["not-a-dict", None, 42, _run("france_travail", {"q": 3})]
        history = compute_query_yield_history(runs, "france_travail")
        assert history["q"].total_jobs == 3

    def test_missing_stats_key_is_skipped(self) -> None:
        runs = [{"no_stats_key": True}]
        assert compute_query_yield_history(runs, "france_travail") == {}

    def test_non_dict_by_source_is_skipped(self) -> None:
        runs = [{"stats": {"by_source": "not-a-dict"}}]
        assert compute_query_yield_history(runs, "france_travail") == {}

    def test_non_dict_source_stats_is_skipped(self) -> None:
        runs = [{"stats": {"by_source": {"france_travail": "not-a-dict"}}}]
        assert compute_query_yield_history(runs, "france_travail") == {}

    def test_missing_per_query_stats_is_skipped(self) -> None:
        runs = [{"stats": {"by_source": {"france_travail": {"collected": 10}}}}]
        assert compute_query_yield_history(runs, "france_travail") == {}

    def test_non_dict_per_query_stats_is_skipped(self) -> None:
        runs = [{"stats": {"by_source": {"france_travail": {"per_query_stats": "oops"}}}}]
        assert compute_query_yield_history(runs, "france_travail") == {}
