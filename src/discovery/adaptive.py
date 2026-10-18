"""Phase LATER / I15: bounded, deterministic adaptive discovery.

Reorders (never removes or invents) a source's discovery queries by their
OWN measured historical yield — jobs actually collected per past pipeline
run that used that exact query — so proven-productive queries get whatever
implicit priority/budget a source's collection order gives them (e.g. a
per-source time budget, or being tried before a saturation cutoff kicks in).

Design constraints (see the phase's own guardrails):
  - Deterministic: the same historical stats always produce the same order.
    No randomness, no ML, no learned weights.
  - Bounded: this NEVER changes the number of queries a source runs, only
    their order — the existing max_queries cap in
    src.discovery.query_expansion is completely untouched. A bad/noisy
    query cannot cause more queries to be generated; it can only end up
    sorted later.
  - Reversible: nothing here is persisted as new state. Every call
    recomputes the plan fresh from whatever run history is passed in — a
    single bad run's numbers age out naturally as soon as newer, better
    runs replace it in the lookback window, and passing no history at all
    (or history with no signal for a given query) reproduces the exact
    pre-I15 query order.
  - Explainable: `QueryYieldStats.average_yield` is a single, auditable
    number (jobs collected / times the query was actually run) — not a
    black-box score.
  - Source fairness: yield is per query USE, not a source's raw total
    volume — a source that runs 3 queries and a source that runs 12 are
    compared on the same footing (jobs per query), not by whichever
    happens to issue more queries.

Data source: `PipelineStats.by_source[source]["per_query_stats"]`
(dict[query, jobs_added_by_that_query]), already computed and persisted by
collectors that support per-query attribution (currently France Travail —
see src/collectors/france_travail.py) inside each run's `pipeline_runs.
stats_json`. Sources with no such attribution simply produce no history
here, and their queries keep their original (static + profile-derived)
order — this module never fabricates a signal that doesn't exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QueryYieldStats:
    """Aggregated historical yield for one discovery query, for one source."""

    query: str
    total_jobs: int
    run_count: int

    @property
    def average_yield(self) -> float:
        """Jobs collected per run this query was actually issued in.

        0.0 when run_count is 0 (should not normally be constructed that
        way, but guards against division by zero defensively).
        """
        return self.total_jobs / self.run_count if self.run_count else 0.0


def compute_query_yield_history(
    historical_runs: list[dict[str, Any]],
    source: str,
    *,
    lookback_runs: int = 10,
) -> dict[str, QueryYieldStats]:
    """Aggregate per-query yield for `source` across up to `lookback_runs`
    historical runs (most-recent-first order assumed, matching
    ``RunStorage.list_runs()``'s own ordering).

    Best-effort and defensive throughout: a run with missing/malformed
    stats, a non-dict ``by_source`` entry, a missing/non-dict
    ``per_query_stats``, or a non-numeric count for one query is simply
    skipped for that run/query — this NEVER raises, since a corrupted or
    unexpected shape in historical data must never break discovery, only
    reduce it to "no signal available" for the affected query.

    Returns an empty dict when there is no usable history at all — callers
    (``prioritize_queries``) treat that identically to "brand new, never
    seen this source before": the original query order is preserved.
    """
    totals: dict[str, int] = {}
    counts: dict[str, int] = {}

    for run in historical_runs[:lookback_runs]:
        if not isinstance(run, dict):
            continue
        stats = run.get("stats")
        if not isinstance(stats, dict):
            continue
        by_source = stats.get("by_source")
        if not isinstance(by_source, dict):
            continue
        source_stats = by_source.get(source)
        if not isinstance(source_stats, dict):
            continue
        per_query_stats = source_stats.get("per_query_stats")
        if not isinstance(per_query_stats, dict):
            continue

        for query, jobs in per_query_stats.items():
            if not isinstance(query, str) or not query.strip():
                continue
            if not isinstance(jobs, (int, float)) or isinstance(jobs, bool) or jobs < 0:
                continue
            totals[query] = totals.get(query, 0) + int(jobs)
            counts[query] = counts.get(query, 0) + 1

    return {
        query: QueryYieldStats(query=query, total_jobs=totals[query], run_count=counts[query])
        for query in totals
    }


def prioritize_queries(
    queries: list[str],
    yield_history: dict[str, QueryYieldStats],
    *,
    min_history_runs: int = 2,
) -> list[str]:
    """Reorder `queries` so historically higher-yield ones run first.

    A query only participates in the reordering once it has been observed
    across at least ``min_history_runs`` past runs — a single lucky/unlucky
    run must not be enough to move a query, avoiding "one noisy run
    reshuffles everything" instability. Queries below that threshold (which
    includes every query the very first time a source/profile combination
    is ever run — the "zero historical data" case) keep their ORIGINAL
    relative order and are placed after the history-backed group.

    This NEVER adds or removes a query — the output is always a permutation
    of the input list (same length, same members), so it cannot affect
    query count/bounds, only order. Ties within the history-backed group
    are broken by the original order (stable sort) for determinism.
    """
    original_index = {q: i for i, q in enumerate(queries)}

    with_history: list[str] = []
    without_history: list[str] = []
    for q in queries:
        stats = yield_history.get(q)
        if stats is not None and stats.run_count >= min_history_runs:
            with_history.append(q)
        else:
            without_history.append(q)

    with_history.sort(key=lambda q: (-yield_history[q].average_yield, original_index[q]))

    return with_history + without_history
