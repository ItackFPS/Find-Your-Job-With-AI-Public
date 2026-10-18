"""Collection-stage profiler.

Runs the France Travail collector and reports:
  - Per-query timing
  - Per-HTTP-request latency (via httpx patch)
  - Duplicate analysis
  - Sequential vs concurrent assessment

Usage:
    python scripts/profile_collection.py

Does NOT run the full pipeline. No database writes. Read-only.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import httpx

# ---------------------------------------------------------------------------
# HTTP request interceptor
# ---------------------------------------------------------------------------

_http_log: list[dict] = []
_original_get = httpx.get
_original_post = httpx.post


def _timed_get(url: str, **kwargs: Any) -> httpx.Response:
    t0 = time.perf_counter()
    resp = _original_get(url, **kwargs)
    elapsed = time.perf_counter() - t0
    _http_log.append({
        "method": "GET",
        "url": url,
        "status": resp.status_code,
        "elapsed": elapsed,
        "params": kwargs.get("params", {}),
    })
    return resp


def _timed_post(url: str, **kwargs: Any) -> httpx.Response:
    t0 = time.perf_counter()
    resp = _original_post(url, **kwargs)
    elapsed = time.perf_counter() - t0
    _http_log.append({
        "method": "POST",
        "url": url,
        "status": resp.status_code,
        "elapsed": elapsed,
    })
    return resp


# ---------------------------------------------------------------------------
# Run collectors
# ---------------------------------------------------------------------------

def _run_collectors() -> dict:
    """Run collectors with HTTP timing patch active. Returns result dict."""
    from src.collectors.france_travail import FranceTravailCollector
    from src.discovery.query_expansion import DiscoveryQueryExpander
    from src.pipeline.filter import load_profile

    # Build discovery queries same way orchestrator does
    profile = load_profile("config/profile.yaml")
    expander = DiscoveryQueryExpander(llm_endpoint=None, llm_timeout=10)
    discovery_queries = expander.expand(profile, source="france_travail", language="fr")
    partition_vocab = expander.generate_partition_vocabulary(profile, "france_travail")

    results = {}

    # ---- France Travail ----
    print(f"\n[1/1] Running France Travail collector ({len(discovery_queries)} queries)...")
    http_before = len(_http_log)
    t0 = time.perf_counter()
    ft = FranceTravailCollector(config={
        "discovery_queries": discovery_queries,
        "partition_vocabulary": partition_vocab,
    })
    ft_result = ft.collect()
    ft_time = time.perf_counter() - t0
    ft_requests = _http_log[http_before:]
    results["france_travail"] = {
        "time": ft_time,
        "jobs": len(ft_result.jobs),
        "errors": ft_result.errors,
        "per_query_stats": ft_result.metadata.get("per_query_stats", {}),
        "per_query_status": ft_result.metadata.get("per_query_status", {}),
        "raw_count": ft_result.metadata.get("raw_count", 0),
        "unique_count": ft_result.metadata.get("unique_count", 0),
        "http_requests": ft_requests,
        "discovery_queries": discovery_queries,
        "partition_vocab": partition_vocab,
    }
    print(f"  → {len(ft_result.jobs)} jobs in {ft_time:.2f}s")

    # ---- pipeline dedup simulation ----
    from src.pipeline.normalize import normalize_jobs
    from src.pipeline.deduplicate import deduplicate_jobs

    normalized = normalize_jobs("france-travail", ft_result.jobs)
    pre_dedup = len(normalized)
    deduped, dupes_removed = deduplicate_jobs(normalized)

    results["dedup"] = {
        "pre": pre_dedup,
        "post": len(deduped),
        "removed": dupes_removed,
    }

    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt(s: float) -> str:
    return f"{s:.3f}s"


def main() -> None:
    print("=" * 72)
    print("  COLLECTION STAGE PROFILER")
    print("=" * 72)

    with patch("httpx.get", side_effect=_timed_get), \
         patch("httpx.post", side_effect=_timed_post):
        results = _run_collectors()

    ft  = results["france_travail"]
    dedup = results["dedup"]

    # ---- Per-request latency analysis (France Travail) ----
    ft_reqs = ft["http_requests"]
    auth_reqs = [r for r in ft_reqs if "connexion" in r["url"] or "access_token" in r["url"]]
    search_reqs = [r for r in ft_reqs if "offres/search" in r["url"]]

    search_latencies = [r["elapsed"] for r in search_reqs]
    avg_lat = sum(search_latencies) / len(search_latencies) if search_latencies else 0
    max_lat = max(search_latencies) if search_latencies else 0
    min_lat = min(search_latencies) if search_latencies else 0
    total_network = sum(search_latencies)
    rate_limit_spent = max(0.0, ft["time"] - total_network - sum(r["elapsed"] for r in auth_reqs))

    # ---- Duplicate analysis ----
    ft_pqs = ft["per_query_status"]
    ft_pq_stats = ft["per_query_stats"]
    total_raw_from_queries = sum(ft_pq_stats.values())  # unique-added per query
    cross_query_overlap = ft["raw_count"] - ft["unique_count"] if ft["raw_count"] else 0

    # ---- Print reports ----
    print("\n" + "=" * 72)
    print("  DISCOVERY QUERIES SENT TO FRANCE TRAVAIL API")
    print("=" * 72)
    queries = ft["discovery_queries"]
    print(f"  Base discovery queries ({len(queries)}):")
    for i, q in enumerate(queries, 1):
        status = ft_pqs.get(q, {})
        jobs_added = ft_pq_stats.get(q, "?")
        pages = status.get("pages", "?")
        api_total = status.get("api_total")
        saturated = status.get("saturated", False)
        sat_tag = " [SATURATED]" if saturated else ""
        api_tag = f"  (API total: {api_total:,})" if api_total else ""
        print(f"  {i:2d}. {q!r:35s}  jobs_added={jobs_added:>5}  pages={pages:>3}{sat_tag}{api_tag}")

    partition_queries = [q for q, s in ft_pqs.items() if s.get("type") == "partition"]
    if partition_queries:
        print(f"\n  Partition queries generated ({len(partition_queries)}):")
        for pq in partition_queries:
            status = ft_pqs.get(pq, {})
            parent = status.get("parent_query", "?")
            jobs_added = ft_pq_stats.get(pq, "?")
            pages = status.get("pages", "?")
            api_total = status.get("api_total")
            api_tag = f"  (API total: {api_total:,})" if api_total else ""
            print(f"       {pq!r:40s}  jobs_added={jobs_added:>5}  pages={pages:>3}  parent={parent!r}{api_tag}")

    print("\n" + "=" * 72)
    print("  PER-REQUEST HTTP LATENCY (France Travail search requests)")
    print("=" * 72)
    print(f"  Auth requests   : {len(auth_reqs)}")
    print(f"  Search requests : {len(search_reqs)}")
    print(f"  Avg latency     : {avg_lat:.3f}s")
    print(f"  Min latency     : {min_lat:.3f}s")
    print(f"  Max latency     : {max_lat:.3f}s")
    print(f"  Total network   : {total_network:.1f}s  (sum of all request latencies)")
    print(f"  Rate-limit idle : {rate_limit_spent:.1f}s  (estimated gap between requests)")
    print()

    # Show latency distribution
    buckets = {"<0.5s": 0, "0.5-1s": 0, "1-2s": 0, "2-3s": 0, "3-5s": 0, ">5s": 0}
    for lat in search_latencies:
        if lat < 0.5:   buckets["<0.5s"] += 1
        elif lat < 1.0: buckets["0.5-1s"] += 1
        elif lat < 2.0: buckets["1-2s"] += 1
        elif lat < 3.0: buckets["2-3s"] += 1
        elif lat < 5.0: buckets["3-5s"] += 1
        else:           buckets[">5s"] += 1
    print("  Latency distribution:")
    for bucket, count in buckets.items():
        bar = "█" * count
        print(f"    {bucket:8s} : {count:3d}  {bar}")

    # Slowest 5 requests
    slowest = sorted(search_reqs, key=lambda r: r["elapsed"], reverse=True)[:5]
    print(f"\n  Slowest 5 requests:")
    for r in slowest:
        params = r.get("params", {})
        rng = params.get("range", "?")
        kw = params.get("motsCles", "?")
        print(f"    {r['elapsed']:.3f}s  range={rng}  motsCles={kw!r}")

    print("\n" + "=" * 72)
    print("  CONCURRENCY ANALYSIS")
    print("=" * 72)
    n_queries = len(ft_pqs)
    n_search_reqs = len(search_reqs)
    print(f"  Total queries processed : {n_queries}  (sequential, one at a time)")
    print(f"  Total search requests   : {n_search_reqs}  (sequential within each query)")
    print(f"  Execution model         : FULLY SEQUENTIAL — no concurrency at any level")
    print()
    # Estimate savings from concurrency
    # Independent queries can run in parallel (no ordering dependency)
    # Within a query, pages MUST be sequential (need Content-Range from prev page)
    # But different queries ARE independent
    ft_query_times: dict[str, float] = {}
    per_req = avg_lat if search_reqs else 1.5
    for q, status in ft_pqs.items():
        pages = status.get("pages", 1) or 1
        ft_query_times[q] = pages * per_req

    if n_queries > 0:
        seq_time = sum(ft_query_times.values())
        # With concurrency of W workers, time ≈ max(critical_path, seq/W)
        # Critical path = longest single query (must finish before overall done)
        longest_query_time = max(ft_query_times.values()) if ft_query_times else 0
        print("  Theoretical speedup with bounded concurrency:")
        for workers in [2, 4, 6, 8]:
            # Simplified: distribute queries across W workers, critical path = longest queue
            # Conservative: assume perfect load balancing
            theo_time = max(longest_query_time, seq_time / workers)
            savings = seq_time - theo_time
            pct = 100 * savings / seq_time if seq_time else 0
            print(f"    {workers} workers : ~{theo_time:.0f}s  (save ~{savings:.0f}s, {pct:.0f}%)")

    print("\n" + "=" * 72)
    print("  DUPLICATE ANALYSIS")
    print("=" * 72)
    # Cross-query overlap within France Travail collector
    per_q_total_fetched = sum(
        (s.get("pages", 0) or 0) * 150
        for s in ft_pqs.values()
    )
    ft_unique = ft["unique_count"]
    ft_raw_fetched_approx = per_q_total_fetched  # upper bound (last page may be partial)
    ft_cross_query_overlap = ft_raw_fetched_approx - ft_unique

    print(f"  France Travail collector:")
    print(f"    Queries run             : {n_queries}")
    print(f"    Total pages fetched     : {n_search_reqs} requests × 150 max = {n_search_reqs*150} slots")
    print(f"    Unique FT jobs          : {ft_unique}")
    print(f"    Cross-query overlap     : ~{max(0, ft_cross_query_overlap)} jobs fetched but discarded as dups")
    print(f"    (FT collector deduplicates by FT job ID within collect())")
    print()
    print(f"  Pipeline deduplication:")
    print(f"    Before dedup            : {dedup['pre']} jobs")
    print(f"    After dedup             : {dedup['post']} jobs")
    print(f"    Removed by pipeline     : {dedup['removed']} duplicates")

    print("\n" + "=" * 72)
    print("  FRANCE TRAVAIL API BEHAVIOR")
    print("=" * 72)
    print(f"  Page size               : 150 (hard cap per API contract)")
    print(f"  Max offset              : 3000 (API hard limit — above 3000 returns HTTP 400)")
    print(f"  Max results per query   : 3000 (20 pages × 150)")
    print(f"  Rate limit configured   : 8 req/s (125ms min interval)")
    print(f"  Actual avg req latency  : {avg_lat:.3f}s")
    print(f"  Rate limiter fires?     : {'NO — requests are slower than rate limit' if avg_lat > 0.125 else 'YES'}")
    print(f"  Token reuse             : YES (token cached with expiry check)")
    print(f"  HTTP connection reuse   : NO — httpx.get() creates new connection each call")
    print(f"  Retry config            : max {3} attempts, exponential backoff 1s/2s/4s")
    print(f"  Retries observed        : {max(0, n_search_reqs - sum((s.get('pages',0) or 0) for s in ft_pqs.values()))} extra requests (vs expected pages)")

    print("\n" + "=" * 72)
    print("  SUMMARY TABLE")
    print("=" * 72)
    print(f"  {'Collector':<18} {'Requests':>9} {'Jobs':>6} {'Time':>8} {'Avg req':>8} {'Retries':>8}")
    print(f"  {'-'*18} {'-'*9} {'-'*6} {'-'*8} {'-'*8} {'-'*8}")
    print(f"  {'france-travail':<18} {len(search_reqs):>9} {ft['jobs']:>6} {ft['time']:>7.1f}s {avg_lat:>7.3f}s {'~0':>8}")
    auth_t = sum(r["elapsed"] for r in auth_reqs)
    total_t = ft["time"]
    total_jobs = ft["jobs"]
    total_reqs = len(search_reqs) + len(auth_reqs)
    print(f"  {'TOTAL':<18} {total_reqs:>9} {total_jobs:>6} {total_t:>7.1f}s")

    print()
    print("=" * 72)
    print("  ANSWERS")
    print("=" * 72)
    print(f"""
  1. Why does collection take {ft['time']:.0f}s?
     France Travail API has high per-request latency (~{avg_lat:.2f}s avg).
     With {len(search_reqs)} sequential search requests, wall-clock = sum(latencies).

  2. How much is actual network latency?
     ~{total_network:.0f}s ({100*total_network/(ft['time']) if ft['time'] else 0:.0f}% of FT time) is pure HTTP round-trip.

  3. How much is intentional rate limiting?
     ~{rate_limit_spent:.1f}s estimated idle time.
     Rate limiter does NOT fire (requests take >{0.125:.3f}s already).

  4. How much is retries?
     Estimated ~{max(0, n_search_reqs - sum((s.get('pages',0) or 0) for s in ft_pqs.values()))} extra requests due to retries.

  5. How much is redundant/duplicate network work?
     ~{max(0, ft_cross_query_overlap)} jobs fetched but discarded as FT cross-query dups.
     This represents ~{max(0, n_search_reqs - ft_unique//150):.0f} extra pages fetched for overlap.

  6. Are requests sequential?
     YES. Queries are sequential. Pages within a query are sequential.
     No async, no threading, no concurrency anywhere.

  7. Safest way to reduce collection time?
     Bounded query-level concurrency (process N queries in parallel, each
     still paginates sequentially internally). Independent queries have no
     ordering dependency. Rate limit is per-query, not global.

  8. Estimated runtime after optimization?
     With {min(4, n_queries)} workers: ~{max(max(ft_query_times.values()) if ft_query_times else 0, sum(ft_query_times.values())/4):.0f}s
     With {min(6, n_queries)} workers: ~{max(max(ft_query_times.values()) if ft_query_times else 0, sum(ft_query_times.values())/6):.0f}s
     (estimates assume {avg_lat:.2f}s per request; rate limit unaffected per-worker)
""")


if __name__ == "__main__":
    main()
