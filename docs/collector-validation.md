# Collector validation before leaving the collection freeze

Status: Phase B B8-G, 2026-11-15. Owner decision D7 and ADR 0032 keep every external collector off until it
passes its own validation and the owner approves its reactivation. This page says what "its own validation"
means. The list itself is code: `src/collectors/validation.py` (`python -m src.collectors.validation` prints
it), and `tests/test_collector_validation.py` keeps it complete and checks that every test it cites exists.

## The eight criteria

They are the ones D7 names. Each one is judged on three kinds of evidence.

| Criterion | What it must show |
|---|---|
| coverage | the collector asks for what the profile needs and reaches the offers it should (query plan, page plan) |
| reliability | answers are parsed as intended, and a changed page format is detected (fixture, canary) |
| latency | requests are spaced as the source asks; the collection time per run is known and acceptable |
| deduplication | the same offer is kept once, within a query, across queries and across runs |
| pagination | the collector stops at the end of a listing, respects its page cap and says why it stopped |
| error handling | HTTP errors, network failures and malformed answers are reported, never raised, and do not stop the run |
| source enablement | the collector runs only when the freeze allows it and the user turned it on (one rule, B4-A) |
| access rules | robots.txt, crawl delay, request cap, credentials and terms of use are respected |

Evidence kinds:

- **offline**: tests that run without contacting the source (fixtures, mocks, replays);
- **supervised**: what only a live run can show (yield per request, real collection time, credentials). A
  supervised run needs the owner's explicit authorisation for that one collector; none is authorised today
  (OQ-2: replay only);
- **open**: a known gap or finding that must be resolved, or explicitly accepted by the owner, first.

## How a collector becomes validated

1. Every criterion has offline evidence and no open item remains (`ready_for_supervised_check()`).
2. The owner authorises one supervised run for that collector; its results (yield per request and per second,
   collection seconds, errors) are recorded in a validation record.
3. A code change adds the collector to `VALIDATED_COLLECTORS` with a reference to that record.
4. The user turns it back on in the application. An update never does it.

## State on 2026-11-24

`ok` = offline evidence present; `ok + S` = offline evidence present, supervised check pending; `ok + R` =
offline evidence and supervised check recorded (see the validation record below); `none` = no offline evidence
yet; **open** = open item.

| Collector | coverage | reliability | latency | deduplication | pagination | error handling | enablement | access rules |
|---|---|---|---|---|---|---|---|---|
| france_travail | ok + R | ok | ok + R | ok | ok | ok | ok | ok + R |
| france_travail_web | **open** | ok | ok + S | ok | ok | ok | ok | ok |
| eures | ok + R | ok | ok + R | ok | ok | ok | ok | ok |
| indeed | ok + R | ok | ok + R | ok | ok | ok | ok | ok + R |
| hellowork | ok + R | ok | ok + R | ok | ok | ok | ok | ok |
| linkedin | ok + S | ok | none | ok | ok | ok | ok | **open** |
| bundesagentur | ok + S | ok | none | ok | **open** | ok | ok | **open** |
| arbetsformedlingen | ok + S | ok | none | ok | **open** | ok | ok | ok |

Not configured (frozen, validation not started): greenhouse, lever, smartrecruiters, workingnomads, remote,
rss, jooble (official API; needs the user's own France key, `JOOBLE_API_KEY`). RemoteOK, Jobicy and We Work Remotely are removed (owner decision D10).

### Open items

- **france_travail_web, coverage**: the preset occupation codes are broad. On the 363 replayed offers, 62 pass
  the filter and 1 is useful (B8-A replay). The occupation boost (B5-E) was not shipped for that reason.
- **linkedin, access rules**: read through a scraping library; whether its access rules allow this is not
  assessed. The owner must accept or reject it.
- **bundesagentur, pagination** and **arbetsformedlingen, pagination**: one bounded page per search term; no
  test of what lies beyond it.
- **bundesagentur, access rules**: the API key is documented by a third-party project; the source is opt-in.

### Supervised checks

Done for france_travail, eures, indeed and hellowork (validation record below). Still not done for the
others: newly discovered useful offers per request and per second (`src/pipeline/counts.py`), and collection
seconds per run (`by_source.seconds`) within a range agreed for that source.

## Validation record 2026-11-24

Owner-authorised supervised runs (collector mission, 2026-11-24) on an isolated installation (separate
checkout and data directory, a test profile: nurse, home Clermont-Ferrand, 60 km), with this change and the
collector PRs #304, #306, #308, #309, #311, #313, #315 and #316 merged locally. The owner reactivated the four
collectors in that installation; this change does not reactivate anything.

Run `726e9c1a` (all four sources, outcome `successful`, 248 s, 0 errors; 7,692 collected, 1,069 removed as
duplicates, 5,101 kept):

| Source | Requests | Seconds | Collected | Accepted | New useful | Useful per request | Useful per second |
|---|---|---|---|---|---|---|---|
| france_travail | 63 | 12.1 | 5,580 | 3,487 | 3,068 | 48.7 | 254.6 |
| eures | 5 | 42.5 | 250 | 81 | 81 | 16.2 | 1.9 |
| indeed | 27 | 40.3 | 1,853 | 1,526 | 86 | 3.2 | 2.1 |
| hellowork (scan) | 12 pages (3 listings + 9 offers, scan log) | 11.9 | 9 | 7 | 0 | 0 | 0 |

HelloWork reads at most `max_jobs_per_slug` (3) offers per listing page: 9 of the 60 listed offers. Its
yield is bounded by that setting, not by the source.

Run `0758e848` (failure injection: a wrong France Travail secret): the preflight is DEGRADED with
`ft_auth_invalid_client`, France Travail is skipped with that reason and its 3,486 stored offers are kept,
EURES reads deeper (500 collected, 10 requests), Indeed 1,879 collected, 0 errors, outcome `successful`. The
secret appears nowhere in the log.

Accepted by the owner: Indeed's terms-of-use risk (opt-in source; its switch states the risk; the first 403
or 429 stops the run's searches). France Travail API access is granted to the installation (the owner's own
credentials).

## Limits

- Every "ok" without "+ R" is offline evidence only. Live behaviour is recorded for the four collectors of
  the validation record, on one profile and one day; it is unverified for the others.
- The table is a snapshot; the module is the source of truth.
