"""France Travail (formerly Pôle Emploi) job-board collector.

Uses the official "Offres d'emploi v2" REST API:
  https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search

Authentication: OAuth2 client-credentials flow.
Required environment variables:
  FRANCE_TRAVAIL_CLIENT_ID      — application client ID
  FRANCE_TRAVAIL_CLIENT_SECRET  — application client secret

Register at https://developer.francetravail.fr to obtain credentials.
Scope required: o2dsoffre api_offresdemploiv2
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import threading
import time
from typing import Any

import httpx

from src.collectors.base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
_SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
_JOB_URL_TEMPLATE = "https://candidat.francetravail.fr/offres/recherche/detail/{}"
_SCOPE = "o2dsoffre api_offresdemploiv2"

# Maximum results per API request (France Travail hard cap: 150 per range).
_PAGE_SIZE = 150

# France Travail API hard offset limit: start positions > 3000 return HTTP 400.
# "La position de début doit être inférieure ou égale à 3000."
# Effectively: at most 3,000 results are retrievable per distinct query.
_MAX_API_OFFSET = 3000

# Matches the French department code prefix of lieuTravail.libelle, e.g.
# "75 - Paris" -> "75", "2A - Ajaccio" -> "2A", "971 - ..." -> "971"
# (Corsica uses 2A/2B; overseas departments use 3-digit codes).
_DEPARTMENT_RE = re.compile(r"^(2[AB]|\d{2,3})\s*-")

# HTTP status codes that indicate transient server-side errors worth retrying.
# 429 is a rate-limit response; 5xx are server errors.
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
_MAX_RETRY_ATTEMPTS: int = 3
_RETRY_BACKOFF_BASE: float = 1.0  # seconds; doubles on each successive retry

# Default keyword for the motsCles parameter.  "informatique" is the broadest
# French IT category on France Travail, covering software, systems, networking,
# data, and support roles.  Override via sources.yaml config.keywords.
_DEFAULT_SEARCH_KEYWORDS = "informatique"


class _RateLimiter:
    """Enforces a minimum interval between calls to stay within the API rate limit.

    The France Travail API allows up to 10 requests/second.  The default
    ``max_rps=8`` leaves a 20 % safety margin (125 ms minimum interval).

    Thread-safe: uses a slot-reservation pattern so the lock is held only
    briefly to update the last-call timestamp; the actual sleep happens
    outside the lock, allowing other threads to reserve their own slots
    without blocking each other's sleep.
    """

    def __init__(self, max_rps: float = 8.0) -> None:
        self._min_interval: float = 1.0 / max_rps if max_rps > 0 else 0.0
        self._last_call: float = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            to_wait = self._min_interval - (now - self._last_call)
            if to_wait > 0:
                # Reserve this slot by setting last_call forward in time.
                # The sleep happens OUTSIDE the lock so other threads can
                # reserve their own consecutive slots concurrently.
                self._last_call = now + to_wait
            else:
                self._last_call = now
        if to_wait > 0:
            time.sleep(to_wait)

# France Travail contract code → generic employment type
_CONTRACT_TYPE_MAP: dict[str, str] = {
    "CDI": "full-time",
    "CDD": "contract",
    "MIS": "contract",   # intérim / mission
    "SAI": "contract",   # saisonnier
    "LIB": "contract",   # libéral
    "REP": "contract",   # remplacement
    "DDI": "contract",   # dispositif d'insertion
    "FRA": "contract",   # franchise
    "CCE": "contract",   # commercial agent
}


class FranceTravailCollector(BaseCollector):
    """Collects jobs from the France Travail Offres d'emploi API.

    Exhaustive by default: every discovery query is fully paginated until
    France Travail reports no more results.  Jobs returned by multiple queries
    are deduplicated by source_job_id so the pipeline sees each offer once.

    ``discovery_queries`` (list[str]) in config takes priority over the legacy
    ``keywords`` (str) config key.  Defaults to ``[_DEFAULT_SEARCH_KEYWORDS]``.

    ``max_jobs`` (optional int): safety ceiling on total unique jobs collected.
    When unset (the default), collection is exhaustive — every available result
    for every discovery query is fetched.

    ``rate_limit_rps`` (float, default 8.0): maximum search API requests per
    second.  The France Travail API allows 10 req/s; the default leaves a 20 %
    safety margin.

    ``max_workers`` (int, default 4): number of concurrent worker threads for
    query-level parallelism.  Discovery queries run concurrently (up to this
    many at once); pages within a single query are always sequential.
    Set to 1 to disable concurrency entirely (sequential, deterministic order).
    """

    # official: France Travail's documented Offres d'emploi API (francetravail.io), with the
    # user's own OAuth2 credentials. Its own client-side rate limiter paces requests.
    access_class = "official"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        max_jobs: int | None = None,
        _page_size: int | None = None,
    ) -> None:
        super().__init__()
        config = config or {}
        self.client_id: str = (
            config.get("client_id")
            or os.environ.get("FRANCE_TRAVAIL_CLIENT_ID", "")
        )
        self.client_secret: str = (
            config.get("client_secret")
            or os.environ.get("FRANCE_TRAVAIL_CLIENT_SECRET", "")
        )
        # max_jobs: optional safety ceiling; None = exhaustive (default)
        _cfg_max = config.get("max_jobs")
        if _cfg_max is not None:
            self.max_jobs: int | None = int(_cfg_max)
        elif max_jobs is not None:
            self.max_jobs = int(max_jobs)
        else:
            self.max_jobs = None

        # search_keywords: legacy single-keyword config key (kept for backward compat)
        self.search_keywords: str = (
            config.get("keywords") or _DEFAULT_SEARCH_KEYWORDS
        )
        # discovery_queries: list of broad search terms; overrides search_keywords
        cfg_queries: list[str] = list(config.get("discovery_queries") or [])
        if cfg_queries:
            self.discovery_queries: list[str] = cfg_queries
        else:
            self.discovery_queries = [self.search_keywords]

        # _page_size is exposed for testing; production code uses _PAGE_SIZE
        self._page_size: int = _page_size or _PAGE_SIZE
        self._token: str | None = None
        self._token_expires_at: float = 0.0

        rate_limit_rps: float = float(config.get("rate_limit_rps", 8.0))
        self._rate_limiter = _RateLimiter(rate_limit_rps)

        # partition_vocabulary: terms appended to saturated broad queries to
        # build compound queries (e.g. "informatique" + "développeur" →
        # "informatique développeur").  Injected by the orchestrator from the
        # profile-derived vocabulary.
        self.partition_vocabulary: list[str] = list(
            config.get("partition_vocabulary") or []
        )
        # Maximum compound partition queries generated per saturated query.
        self.max_partitions_per_query: int = int(
            config.get("max_partitions_per_query", 12)
        )
        # max_workers: concurrency for independent queries (1 = sequential).
        self._max_workers: int = int(config.get("max_workers", 4))

    def collect(self) -> CollectorResult:
        start_time = time.time()
        errors: list[str] = []
        jobs: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}

        if not self.client_id or not self.client_secret:
            errors.append(
                "France Travail collector requires FRANCE_TRAVAIL_CLIENT_ID and "
                "FRANCE_TRAVAIL_CLIENT_SECRET to be set (environment variables or "
                "source config keys 'client_id'/'client_secret'). "
                "Register at https://developer.francetravail.fr to obtain credentials. "
                "Skipping collection."
            )
            duration = time.time() - start_time
            self._log_collection("france_travail", 0, errors, duration)
            return CollectorResult(
                source_name="france_travail",
                jobs=jobs,
                errors=errors,
                duration_seconds=duration,
            )

        try:
            with httpx.Client(timeout=self.timeout) as client:
                token = self._get_or_refresh_token(client)
                raw_jobs, per_query_stats, query_errors, per_query_status = (
                    self._search_all_queries(token, client)
                )

            errors.extend(query_errors)

            raw_count = sum(per_query_stats.values())
            unique_count = len(raw_jobs)
            logger.info(
                "France Travail: %d queries, %d raw results, %d unique",
                len(self.discovery_queries), raw_count, unique_count,
            )
            metadata["per_query_stats"] = per_query_stats
            metadata["per_query_status"] = per_query_status
            metadata["raw_count"] = raw_count
            metadata["unique_count"] = unique_count

            for raw in raw_jobs:
                if not isinstance(raw, dict):
                    errors.append(f"Skipping non-dict entry: {type(raw)}")
                    continue
                jobs.append(self._normalize_fields(raw))
        except (httpx.HTTPError, RuntimeError) as exc:
            errors.append(f"Collection failed: {exc}")

        duration = time.time() - start_time
        self._log_collection("france_travail", len(jobs), errors, duration)
        return CollectorResult(
            source_name="france_travail",
            jobs=jobs,
            errors=errors,
            duration_seconds=duration,
            metadata=metadata,
        )

    # -- OAuth2 ---------------------------------------------------------------

    def _get_or_refresh_token(self, client: httpx.Client) -> str:
        """Return a valid bearer token, refreshing if it has expired."""
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        self._token, expires_in = self._fetch_token(client)
        self._token_expires_at = time.time() + expires_in
        return self._token

    def _fetch_token(self, client: httpx.Client) -> tuple[str, int]:
        """Fetch a new OAuth2 access token via the client-credentials flow."""
        try:
            response = client.post(
                _TOKEN_URL,
                params={"realm": "/partenaire"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": _SCOPE,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"France Travail OAuth2 token request failed "
                f"(HTTP {exc.response.status_code}): {exc.response.text[:300]}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"France Travail OAuth2 token request failed: {exc}"
            ) from exc

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"France Travail OAuth2 token response was not valid JSON: {exc}"
            ) from exc
        token: str = data.get("access_token", "")
        expires_in: int = int(data.get("expires_in", 1440))
        if not token:
            raise RuntimeError(
                f"France Travail OAuth2 response missing 'access_token': {data}"
            )
        return token, expires_in

    # -- API search -----------------------------------------------------------

    def _search_all_queries(
        self, token: str, client: httpx.Client
    ) -> tuple[list[dict[str, Any]], dict[str, int], list[str], dict[str, dict]]:
        """Run _search_jobs_for_query for every discovery query, with automatic
        partition recovery for saturated queries.

        When a query reaches the API's 3000-result offset limit while the API
        reports more than 3000 total results, the query is marked *saturated*
        and compound partition queries are generated (e.g. "informatique" →
        ["informatique développeur", "informatique data", …]).  Partition
        queries are one level deep only — partitions never generate further
        partitions.

        Discovery queries run concurrently (up to max_workers threads).
        Partition queries likewise run concurrently after all discovery queries
        finish.  Pages within a single query are always sequential.

        Cross-query deduplication by ``id`` ensures each France Travail offer
        appears at most once in the returned list.

        Partial results from a failed query are preserved.  A query error
        stops only that query; remaining queries continue normally.

        Returns:
            (unique_jobs, per_query_stats, query_errors, per_query_status)
            where per_query_stats maps query → unique jobs added by that query
            and per_query_status maps query → rich diagnostic dict.
        """
        all_jobs: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        per_query_stats: dict[str, int] = {}
        per_query_status: dict[str, dict] = {}
        query_errors: list[str] = []
        total_requests = 0
        queries_processed = 0
        run_start = time.monotonic()

        cap_label = "unlimited" if self.max_jobs is None else f"safety cap {self.max_jobs:,}"
        logger.info(
            "France Travail discovery: %d %s — %s (workers=%d)",
            len(self.discovery_queries),
            "query" if len(self.discovery_queries) == 1 else "queries",
            cap_label,
            self._max_workers,
        )

        # Build discovery work list.
        discovery_work: list[tuple[str, bool, str | None]] = [
            (q, False, None) for q in self.discovery_queries
        ]
        queued_keys: set[str] = {q.lower().strip() for q in self.discovery_queries}

        # Phase 1: run all discovery queries concurrently.
        # Pass max_jobs as a per-query upper bound so the first page's range
        # parameter is sized correctly (same as the old sequential behavior).
        phase1_raw = self._execute_query_batch(
            token, client, discovery_work, max_to_collect=self.max_jobs
        )

        # Identify saturated discovery queries and generate partition work.
        partition_work: list[tuple[str, bool, str | None]] = []
        for query, _is_partition, _parent in discovery_work:
            result = phase1_raw[query]
            if result["saturated"] and not _is_partition and self.partition_vocabulary:
                partition_qs = self._generate_partition_queries(query, queued_keys)
                for pq in partition_qs:
                    queued_keys.add(pq.lower().strip())
                    partition_work.append((pq, True, query))

        # Phase 2: run partition queries concurrently (if any).
        phase2_raw: dict[str, dict] = {}
        if partition_work:
            phase2_raw = self._execute_query_batch(
                token, client, partition_work, max_to_collect=self.max_jobs
            )

        # Merge results in deterministic order: discovery queries first,
        # then partition queries.  Cross-query dedup applied during merge.
        all_work_in_order = discovery_work + partition_work
        all_raw = {**phase1_raw, **phase2_raw}

        for query, is_partition, parent_query in all_work_in_order:
            result = all_raw[query]
            raw = result["raw"]
            pages = result["pages"]
            api_total = result["api_total"]
            query_error = result["error"]
            query_saturated = result["saturated"]

            total_requests += pages
            queries_processed += 1

            # Global safety cap: record status but skip adding jobs once reached.
            if self.max_jobs is not None and len(all_jobs) >= self.max_jobs:
                logger.info(
                    "France Travail: safety cap max_jobs=%d reached, skipping remaining queries",
                    self.max_jobs,
                )
                per_query_stats[query] = 0
                per_query_status[query] = {
                    "status": "completed" if query_error is None else "failed",
                    "type": "partition" if is_partition else "discovery",
                    "parent_query": parent_query,
                    "jobs": 0,
                    "pages": pages,
                    "api_total": api_total,
                    "saturated": query_saturated,
                    "partitions_generated": 0,
                    "stop_reason": "cap_reached",
                    "error": query_error,
                }
                continue

            # Cross-query dedup by job ID.
            new_unique: list[dict[str, Any]] = []
            for job in raw:
                if not isinstance(job, dict):
                    new_unique.append(job)
                    continue
                job_id = job.get("id") or ""
                if job_id and job_id in seen_ids:
                    continue
                if job_id:
                    seen_ids.add(job_id)
                new_unique.append(job)

            # Per-query cap when max_jobs is set.
            if self.max_jobs is not None:
                remaining = self.max_jobs - len(all_jobs)
                new_unique = new_unique[:remaining]

            per_query_stats[query] = len(new_unique)
            all_jobs.extend(new_unique)

            query_type = "partition" if is_partition else "discovery"

            if query_error is not None:
                if new_unique:
                    status = "partial"
                    stop_reason = "error"
                    msg = f"France Travail: query '{query}' partial: {query_error}"
                    logger.warning(msg)
                    query_errors.append(msg)
                else:
                    status = "failed"
                    stop_reason = "error"
                    msg = f"France Travail: query '{query}' failed: {query_error}"
                    logger.warning(msg)
                    query_errors.append(msg)
            elif query_saturated:
                status = "saturated"
                stop_reason = "api_offset_limit"
            else:
                status = "completed"
                stop_reason = "exhausted"

            # Count partitions generated for this discovery query.
            partitions_generated = (
                sum(1 for (_pq, _is_p, _par) in partition_work if _par == query)
                if not is_partition
                else 0
            )

            per_query_status[query] = {
                "status": status,
                "type": query_type,
                "parent_query": parent_query,
                "jobs": len(new_unique),
                "pages": pages,
                "api_total": api_total,
                "saturated": query_saturated,
                "partitions_generated": partitions_generated,
                "stop_reason": stop_reason,
                "error": query_error,
            }

            # Log saturation + partition info for discovery queries.
            if query_saturated and not is_partition and self.partition_vocabulary:
                logger.info(
                    "France Travail [%s]: saturated (api_total=%s) — "
                    "queued %d partition queries",
                    repr(query),
                    f"{api_total:,}" if api_total is not None else "?",
                    partitions_generated,
                )

            total_label = f"{api_total:,}" if api_total is not None else "?"
            status_tag = f"  [{status}]" if status != "completed" else ""
            parent_tag = f"  (partition of '{parent_query}')" if is_partition else ""
            logger.info(
                "France Travail — Query: %s  Results: %s  Pages: %d  Unique added: %d"
                "  (running total: %d)%s%s",
                repr(query), total_label, pages, len(new_unique),
                len(all_jobs), status_tag, parent_tag,
            )

        elapsed = time.monotonic() - run_start
        total_raw = sum(per_query_stats.values())
        logger.info(
            "France Travail discovery complete — queries: %d  raw: %d  unique: %d  "
            "API requests: %d  duration: %.1fs",
            queries_processed, total_raw, len(all_jobs),
            total_requests, elapsed,
        )

        return all_jobs, per_query_stats, query_errors, per_query_status

    def _execute_query_batch(
        self,
        token: str,
        client: httpx.Client,
        work: list[tuple[str, bool, str | None]],
        max_to_collect: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Run a batch of independent queries, returning results keyed by query.

        Sequential when max_workers <= 1 or len(work) <= 1.
        Concurrent otherwise, using a ThreadPoolExecutor with max_workers.
        The shared httpx.Client and rate limiter are both thread-safe.
        Pages within each query remain sequential regardless of concurrency.
        """
        results: dict[str, dict[str, Any]] = {}

        def _run_one(query: str) -> tuple[str, dict[str, Any]]:
            raw, pages, api_total, error, saturated = self._search_jobs_for_query(
                token, query, client, max_to_collect=max_to_collect
            )
            return query, {
                "raw": raw,
                "pages": pages,
                "api_total": api_total,
                "error": error,
                "saturated": saturated,
            }

        if self._max_workers <= 1 or len(work) <= 1:
            for query, _, _ in work:
                q, data = _run_one(query)
                results[q] = data
            return results

        effective_workers = min(self._max_workers, len(work))
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=effective_workers,
            thread_name_prefix="ft_query",
        ) as executor:
            futures = {
                executor.submit(_run_one, query): query
                for query, _, _ in work
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    q, data = future.result()
                    results[q] = data
                except Exception as exc:
                    query = futures[future]
                    logger.error(
                        "France Travail: unexpected exception in worker for %r: %s",
                        query, exc,
                    )
                    results[query] = {
                        "raw": [], "pages": 0, "api_total": None,
                        "error": str(exc), "saturated": False,
                    }

        return results

    def _generate_partition_queries(
        self, saturated_query: str, queued_keys: set[str]
    ) -> list[str]:
        """Build compound queries to recover results beyond the API offset limit.

        Combines *saturated_query* with each term from ``partition_vocabulary``
        (e.g. "informatique" + "développeur" → "informatique développeur").
        Already-queued queries (case-insensitive) are skipped.  Output is
        capped at ``max_partitions_per_query``.
        """
        partitions: list[str] = []
        for term in self.partition_vocabulary:
            if len(partitions) >= self.max_partitions_per_query:
                break
            candidate = f"{saturated_query} {term}"
            if candidate.lower() not in queued_keys:
                partitions.append(candidate)
        return partitions

    def _search_jobs_for_query(
        self,
        token: str,
        query: str,
        client: httpx.Client,
        max_to_collect: int | None = None,
    ) -> tuple[list[dict[str, Any]], int, int | None, str | None, bool]:
        """Paginate a single motsCles query until all results are fetched.

        When ``max_to_collect`` is None (the default), pagination continues
        until the API reports no more results — exhaustive collection.  When
        set, it acts as a hard ceiling (used for the optional safety cap).

        A rate-limit wait is applied before every HTTP request to stay within
        the documented 10 req/s limit (default: 8 req/s).

        Transient HTTP errors (408, 429, 500, 502, 503, 504) are retried up
        to _MAX_RETRY_ATTEMPTS times with exponential back-off.  On a 429
        the Retry-After header is honoured when present.  Permanent errors
        and network failures stop pagination immediately and return whatever
        was already collected — partial results are never discarded.

        Returns:
            (jobs, page_count, api_total, error, saturated)
            where api_total is the last known Content-Range total (or None),
            error is None on clean completion or an error string on failure,
            and saturated is True when the API offset limit was reached while
            api_total > _MAX_API_OFFSET (meaning the API has more results
            that are inaccessible via pagination).
        """
        all_jobs: list[dict[str, Any]] = []
        start = 0
        page_count = 0
        api_total: int | None = None
        _hit_api_limit = False

        while True:
            if max_to_collect is not None and len(all_jobs) >= max_to_collect:
                break

            if start >= _MAX_API_OFFSET:
                _hit_api_limit = True
                logger.info(
                    "France Travail [%s]: API offset limit (%d) reached after "
                    "%d results — stopping (API may have more)",
                    query, _MAX_API_OFFSET, len(all_jobs),
                )
                break

            if max_to_collect is not None:
                batch_size = min(self._page_size, max_to_collect - len(all_jobs))
            else:
                batch_size = self._page_size
            end = start + batch_size - 1

            # Per-page retry loop for transient failures.
            page_error: str | None = None
            response: httpx.Response | None = None
            for attempt in range(_MAX_RETRY_ATTEMPTS + 1):
                if attempt > 0:
                    retry_after: float | None = None
                    if response is not None:
                        ra_header = response.headers.get("Retry-After", "")
                        if ra_header:
                            try:
                                retry_after = float(ra_header)
                            except ValueError:
                                pass
                    delay = (
                        retry_after
                        if retry_after is not None
                        else _RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                    )
                    logger.info(
                        "France Travail [%s]: retrying range %d-%d "
                        "(attempt %d/%d, waiting %.1fs)",
                        query, start, end, attempt, _MAX_RETRY_ATTEMPTS, delay,
                    )
                    time.sleep(delay)

                self._rate_limiter.wait()
                try:
                    response = client.get(
                        _SEARCH_URL,
                        params={
                            "sort": 1,
                            "range": f"{start}-{end}",
                            "motsCles": query,
                        },
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/json",
                        },
                        timeout=self.timeout,
                    )
                except httpx.RequestError as exc:
                    page_error = f"France Travail search request failed: {exc}"
                    logger.warning(
                        "France Travail [%s]: network error on range %d-%d: %s",
                        query, start, end, exc,
                    )
                    break  # Network errors are not retried

                if response.status_code in _RETRYABLE_STATUS_CODES:
                    page_error = (
                        f"France Travail search API failed "
                        f"(HTTP {response.status_code}): {response.text[:300]}"
                    )
                    if attempt < _MAX_RETRY_ATTEMPTS:
                        continue  # will retry
                    logger.warning(
                        "France Travail [%s]: gave up on range %d-%d after %d retries (HTTP %d)",
                        query, start, end, _MAX_RETRY_ATTEMPTS, response.status_code,
                    )
                    break
                elif not response.is_success:
                    page_error = (
                        f"France Travail search API failed "
                        f"(HTTP {response.status_code}): {response.text[:300]}"
                    )
                    logger.warning(
                        "France Travail [%s]: permanent error on range %d-%d "
                        "(HTTP %d) — stopping pagination",
                        query, start, end, response.status_code,
                    )
                    break  # Permanent error, no retry
                else:
                    page_error = None
                    break  # Success

            if page_error is not None:
                if max_to_collect is not None:
                    return all_jobs[:max_to_collect], page_count, api_total, page_error, False
                return all_jobs, page_count, api_total, page_error, False

            assert response is not None
            page_count += 1
            try:
                data = response.json()
            except Exception as exc:
                page_error = f"France Travail: failed to parse JSON response: {exc}"
                logger.warning(
                    "France Travail [%s]: JSON parse error on range %d-%d: %s",
                    query, start, end, exc,
                )
                if max_to_collect is not None:
                    return all_jobs[:max_to_collect], page_count - 1, api_total, page_error, False
                return all_jobs, page_count - 1, api_total, page_error, False

            batch = data.get("resultats", [])
            if not isinstance(batch, list):
                page_error = f"Unexpected France Travail API response format: {type(batch)}"
                logger.warning(
                    "France Travail [%s]: unexpected response format on range %d-%d",
                    query, start, end,
                )
                if max_to_collect is not None:
                    return all_jobs[:max_to_collect], page_count - 1, api_total, page_error, False
                return all_jobs, page_count - 1, api_total, page_error, False

            if not batch:
                logger.debug(
                    "France Travail [%s]: empty page at range %d-%d — stopping",
                    query, start, end,
                )
                break

            all_jobs.extend(batch)

            # Content-Range: "offres 0-149/5000" → api_total = 5000
            content_range = response.headers.get("Content-Range", "")
            api_total = _parse_content_range_total(content_range)
            if api_total is not None:
                logger.debug(
                    "France Travail [%s]: page %d, range %d-%d → %d jobs "
                    "(API total: %d, collected: %d)",
                    query, page_count, start, end, len(batch), api_total, len(all_jobs),
                )
                if len(all_jobs) >= api_total:
                    break
            else:
                logger.debug(
                    "France Travail [%s]: page %d, range %d-%d → %d jobs (collected: %d)",
                    query, page_count, start, end, len(batch), len(all_jobs),
                )

            # Fewer results than requested → end of available data
            if len(batch) < batch_size:
                break

            start += batch_size

        saturated = _hit_api_limit and api_total is not None and api_total > _MAX_API_OFFSET
        if max_to_collect is not None:
            return all_jobs[:max_to_collect], page_count, api_total, None, saturated
        return all_jobs, page_count, api_total, None, saturated

    # -- Field normalization --------------------------------------------------

    # -- Field normalization --------------------------------------------------

    @staticmethod
    def _normalize_fields(raw: dict[str, Any]) -> dict[str, Any]:
        """Translate France Travail fields to the generic normalizer contract.

        The pipeline normalizer (src/pipeline/normalize.py) reads 'company',
        'title', 'description', 'location', 'countries', 'remote', and 'type'
        (employment type) from the raw dict. France Travail uses different
        field names, so we rename and map them here without touching the
        normalizer contract.

        France Travail is a French job board — all listed jobs are in France,
        so we always inject "France" into the countries list.

        Remote detection: France Travail has no dedicated boolean remote field,
        but "télétravail" appears in location strings or job descriptions when
        applicable. We check both.
        """
        job_id: str = raw.get("id") or ""
        title: str = raw.get("intitule") or ""
        description: str = raw.get("description") or ""

        # Company name
        enterprise = raw.get("entreprise")
        company: str = (
            enterprise.get("nom") or ""
            if isinstance(enterprise, dict)
            else ""
        )

        # Location: "75 - Paris", "69 - Lyon", "FRANCE ENTIÈRE", etc.
        lieu = raw.get("lieuTravail")
        location: str = (
            lieu.get("libelle") or ""
            if isinstance(lieu, dict)
            else ""
        )

        # French INSEE department code. lieuTravail.libelle consistently
        # starts with the department code ("75 - Paris"); codePostal is a
        # fallback for the rarer cases where libelle omits it (e.g. plain
        # city names, "Télétravail", "FRANCE ENTIÈRE"). No department can be
        # derived from those fallback-less cases — left as None (unknown).
        department: str | None = None
        if isinstance(lieu, dict):
            libelle_match = _DEPARTMENT_RE.match(lieu.get("libelle") or "")
            if libelle_match:
                department = libelle_match.group(1)
            else:
                code_postal = str(lieu.get("codePostal") or "")
                if len(code_postal) >= 2:
                    department = code_postal[:2]

        # Remote detection via location string and first 500 chars of description
        location_lower = location.lower()
        remote: bool = (
            "télétravail" in location_lower      # télétravail
            or "teletravail" in location_lower
            or "france entière" in location_lower     # france entière
            or "télétravail" in description.lower()[:500]
        )

        # Employment type: map France Travail codes → generic values the
        # pipeline normalizer understands ("full-time", "contract", "internship").
        # Key is 'type' because normalize_job reads raw_data.get("type").
        alternance: bool = bool(raw.get("alternance", False))
        type_contrat: str = raw.get("typeContrat") or ""
        if alternance:
            employment_type: str | None = "internship"
        else:
            employment_type = _CONTRACT_TYPE_MAP.get(type_contrat)

        # Canonical URL: prefer the origin URL when the offer came via a partner;
        # fall back to the France Travail candidate portal.
        origine_offre = raw.get("origineOffre")
        url_origine: str | None = (
            origine_offre.get("urlOrigine")
            if isinstance(origine_offre, dict)
            else None
        )
        url: str = url_origine or (
            _JOB_URL_TEMPLATE.format(job_id) if job_id else ""
        )

        # Skills from the structured competences list
        competences = raw.get("competences") or []
        skills: list[str] = [
            c["libelle"]
            for c in competences
            if isinstance(c, dict) and c.get("libelle")
        ]

        return {
            # Generic normalizer contract fields
            "source_job_id": job_id,
            "title": title,
            "company": company,
            "description": description,
            "location": location,
            "countries": ["France"],
            "department_code": department,
            "remote": remote,
            "type": employment_type,    # read by normalize_job as employment_type
            "url": url,
            "skills": skills,
            "published_at": raw.get("dateCreation"),
            # Pass through the full original payload for debugging
            "_france_travail_raw": raw,
        }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _parse_content_range_total(content_range: str) -> int | None:
    """Parse the total result count from a France Travail Content-Range header.

    Example: ``"offres 0-149/5000"`` → ``5000``
    Returns ``None`` if the header is absent, malformed, or uses ``*`` as total.
    """
    if not content_range:
        return None
    try:
        _, rest = content_range.split(" ", 1)
        _, total_str = rest.split("/", 1)
        total_str = total_str.strip()
        if total_str == "*":
            return None
        return int(total_str)
    except (ValueError, IndexError):
        return None
