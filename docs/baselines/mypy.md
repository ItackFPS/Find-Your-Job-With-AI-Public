# mypy baseline

Configuration: `[tool.mypy]` in [`pyproject.toml`](../../pyproject.toml)
(`disallow_untyped_defs`, `warn_return_any`, per-module `ignore_missing_imports` for
untyped third-party packages).
CI: the `mypy (advisory)` step of [`tests.yml`](../../.github/workflows/tests.yml) (reports,
never fails) until a decision is made to make it blocking.

**Status (2026-09-17): 0 errors** — `mypy src` is clean after #36, #37, #38 and
the typing-boundary fixes for `requirements/extractor.py` and
`pipeline/orchestrator.py`. The CI job stays advisory until a decision is made to
make it blocking. The measurement below is the historical starting point.

Measured on `main` at 42ece22 (2026-09-16) with mypy 2.3.0: `mypy src`

**52 errors in 25 files** — identical count locally (Python 3.13) and in CI
(Python 3.12, `pip install -e ".[dev,mcp_admin]"`).

## By error code

| Code | Count | Group |
|---|---:|---|
| no-any-return | 13 | annotation |
| assignment | 8 | possible defect |
| arg-type | 7 | possible defect (6) / annotation (1) |
| union-attr | 5 | possible defect |
| no-untyped-def | 4 | annotation |
| name-defined | 3 | annotation |
| index | 2 | possible defect |
| valid-type | 2 | annotation |
| call-overload | 2 | annotation |
| import-not-found / import-untyped | 2 | annotation — `crawl4ai` (optional `browser` extra): "not found" in CI, "untyped" when installed |
| return-value | 1 | possible defect |
| call-arg | 1 | possible defect |
| attr-defined | 1 | possible defect |
| misc | 1 | annotation |

## Groups and follow-up issues

- **Possible defects — issue #16.** Errors on code paths that may misbehave at
  runtime and need a verdict (bug with regression test, or false positive with a
  precise annotation): `pipeline/orchestrator.py` (unexpected `config=` kwarg to
  `BaseCollector`; `str` used where `HardRejectionReason` is assigned; `Job | None`
  assigned to `Job`), `hellowork/scanner.py:129`, `pipeline/normalize.py:185`,
  `api/app.py:182,186`, `notifications/engine.py:223-232`,
  `requirements/extractor.py:483,665`, `discord_admin/reconciler.py:138-171`,
  `ai/eligibility.py:369`, `collectors/_jsonld.py:149`.
- **Annotation-only — issue #17.** No expected runtime effect: `JobSearchProfile`
  string annotations without a `TYPE_CHECKING` import, `callable` used as a type,
  missing return annotations, pydantic `Field` overloads, `crawl4ai` stubs override,
  and `Any` returned from JSON/YAML/`app.state` accessors.

## By file

| File | Errors |
|---|---:|
| `src/pipeline/orchestrator.py` | 6 |
| `src/api/routes/pipeline.py` | 5 |
| `src/pipeline/normalize.py` | 4 |
| `src/notifications/engine.py` | 4 |
| `src/discord_admin/reconciler.py` | 4 |
| `src/pipeline/filter.py` | 3 |
| `src/api/app.py` | 3 |
| `src/scheduler/service.py`, `src/requirements/extractor.py`, `src/notifications/discord_reconciler_worker.py`, `src/api/routes/jobs.py`, `src/ai/config.py` | 2 each |
| `src/collectors/base_browser.py`, `src/collectors/wttj.py`, `src/personalization/engine.py`, `src/linkedin/staging.py`, `src/linkedin/scanner.py`, `src/hellowork/scanner.py`, `src/esco/loader.py`, `src/discord_admin/lock.py`, `src/collectors/base.py`, `src/collectors/_jsonld.py`, `src/api/routes/scheduler.py`, `src/api/routes/departments.py`, `src/ai/eligibility.py` | 1 each |

## Updating this baseline

A PR that fixes mypy errors updates the counts here in the same PR. New code should
not add errors; the CI job summary shows the current count on every PR. Make the job
blocking once the count is zero.
