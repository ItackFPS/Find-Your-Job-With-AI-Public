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

## State on 2026-11-15 (offline only)

`ok` = offline evidence present; `ok + S` = offline evidence present, supervised check pending; `none` = no
offline evidence yet; **open** = open item.

| Collector | coverage | reliability | latency | deduplication | pagination | error handling | enablement | access rules |
|---|---|---|---|---|---|---|---|---|
| france_travail | **open** | ok | ok + S | ok | ok | ok | ok | **open** |
| france_travail_web | **open** | ok | ok + S | ok | ok | ok | ok | ok |
| indeed | **open** | ok | ok + S | ok | ok | ok | ok | **open** |
| hellowork | **open** | ok | ok + S | ok | ok | ok | ok | ok |
| bundesagentur | ok + S | ok | none | ok | **open** | ok | ok | **open** |
| arbetsformedlingen | ok + S | ok | none | ok | **open** | ok | ok | ok |

Not configured (frozen, validation not started): greenhouse, lever, smartrecruiters, workingnomads, remote,
rss. RemoteOK, Jobicy and We Work Remotely are removed (owner decision D10). EURES and LinkedIn are removed
(owner decision, 2026-11-24).

### Open items

- **france_travail, coverage**: a saturated query (for example "informatique") reaches the API's result
  ceiling and is incomplete (B4-D). A narrower query plan is needed.
- **france_travail, access rules**: its requests use its own HTTP client and are not counted (B8-A), so no
  request yield can be computed until they are. API access is restricted and not set up on this installation.
- **france_travail_web, coverage**: the preset occupation codes are broad. On the 363 replayed offers, 62 pass
  the filter and 1 is useful (B8-A replay). The occupation boost (B5-E) was not shipped for that reason.
- **indeed, coverage**: listing completeness cannot be known; the answer gives no total (B4-D).
- **indeed, access rules**: read through a scraping library. Its terms-of-use risk is documented, not
  resolved; the owner must accept or reject it.
- **hellowork, coverage**: each listing page exposes 20 offers and robots.txt forbids query-string paging;
  breadth comes only from the number of occupation pages read.
- **bundesagentur, pagination** and **arbetsformedlingen, pagination**: one bounded page per search term; no
  test of what lies beyond it.
- **bundesagentur, access rules**: the API key is documented by a third-party project; the source is opt-in.

### Supervised checks (not done; collection is frozen)

For every configured collector: newly discovered useful offers per request and per second
(`src/pipeline/counts.py`), and collection seconds per run (`by_source.seconds`) within a range agreed for that
source. For France Travail (API): access granted to the installation.

## Limits

- Every "ok" above is offline evidence. Live behaviour is unverified for every collector.
- The table is a snapshot; the module is the source of truth.
