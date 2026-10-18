# Listing age comes from the source date, else from our first sighting — never from the last collection

Status: accepted (issue #105)

A job carries three dates that were used interchangeably. They now have one meaning each:

| field | meaning | set by |
|---|---|---|
| `published_at` | the date the **source** claims the listing was published | normalisation (unparseable → `None`) |
| `first_seen_at` | the first time **this pipeline stored** the listing | `upsert_jobs_batch`, preserved on every later upsert |
| `discovered_at` | the time the listing was **last collected** | `Job` construction, re-stamped on every collection |

The `published_age` hard filter used `published_at or discovered_at`. Because `discovered_at` is
re-stamped on every collection, an undated listing re-collected today always looked new, however
long it had been online — the filter could never age it out. `discovered_at` is therefore **not an
age signal** and is no longer read by the filter.

`freshness_reference` (`src/pipeline/filter.py`) picks the reference date and records its basis:

1. `published_at`, when plausible: not before 2000-01-01 (epoch-zero / placeholder artefacts) and
   at most one day in the future (timezones, clock skew). Out-of-range values are ignored, not
   clamped.
2. else the stored `first_seen_at`: the listing has been online at least since we first stored it.
   `run_pipeline` reads it from `jobs` (`Storage.get_first_seen_at`) just before the hard filter;
   the lookup is fail-open (on error, undated listings are treated as new, the previous behaviour).
3. else **unknown**, which passes — missing data is not a rejection reason. A listing never stored
   before is new to us, whatever its true age.

The bound stays inclusive (`reference >= now - posted_within_days`); the 30-day default is
unchanged. The rule is versioned (`FRESHNESS_RULE_VERSION`, part of `rule_versions()` and so of the
production `strategy_version`), and the decision journal's `published_age` evidence records
`first_seen_at`, the reference and its basis.

## Consequences

- An undated listing is shown for `posted_within_days` after we first stored it, then ages out on
  later runs exactly like a dated one. A genuinely new undated listing still passes.
- Only accepted jobs are written to `jobs`, so a listing rejected for another reason on every run
  has no `first_seen_at`; if the profile later accepts it, it is aged from that first storage.
- Offline evaluation replays the same rule: `export` writes each stored job's `first_seen_at` (when
  not after the dataset's `as_of`), and the replay clock is frozen at `as_of`, so an undated listing
  first stored more than `posted_within_days` before `as_of` is rejected offline exactly as in
  production. Pipeline-rejected jobs (`export --rejected`) were never stored and carry none, as in
  production. `feedback-dataset` applies the same rule to the jobs it derives labels for (a job
  rejected on every run has none). `discovered_at` is never exported (jobs are rebuilt with `discovered_at = as_of`).
  Datasets exported before this field existed (`real-v1@2`: 39 of 250 jobs are undated) cannot
  replay the fallback: they keep identical decisions, and `audit` re-exports them without the field
  (the private sidecar records which format was used).
- `discovered_at` keeps its name and storage for compatibility. Endpoints that order or window by
  it (`GET /api/jobs/digest/weekly`, list ordering) mean "recently collected", not "new"; switching
  the digest to `first_seen_at` is a separate product decision.
