# Ruff baseline

Configuration: `[tool.ruff]` in [`pyproject.toml`](../../pyproject.toml) — rules
`E4, E7, E9, F`, `target-version = "py312"`, `line-length = 88`.
CI: [`lint.yml`](../../.github/workflows/lint.yml), **advisory** (reports, never fails)
until this baseline reaches zero; tracked in issue #14.

Measured with Ruff 0.16.2: `ruff check src tests --statistics`

**30 findings** — 9 in `tests/`, 21 in `src/` (initial baseline on 42ece22,
2026-09-16: 301; burned down since by #39, #43, #49, #50 and the follow-up that
removed the last unused test imports, issue #14).

| Rule | Count | Meaning | Notes |
|---|---:|---|---|
| E402 | 29 | import not at top of file | `src/api/app.py` (15) imports after `load_dotenv()`/`configure_logging()` on purpose; `src/main.py` (1); `src/personalization/engine.py` (4, imports placed after `logger`, style only); tests: `test_france_travail.py` (5), `test_departments.py` (2), `test_wttj_collector.py` (2) |
| F401 | 1 | unused import | `src/esco/loader.py`'s deliberate availability check |

By area: `tests` 9, `src/api` 15, `src/personalization` 4, `src/main.py` 1,
`src/esco` 1.

## Formatting

Not enforced. `ruff format --check src tests` would rewrite 227 of 269 files
(~25k changed lines at 88 columns), so formatting is deferred to incremental,
behavior-neutral PRs (issue #14) rather than one unreviewable diff.

## Updating this baseline

When a burn-down PR lands, re-run the command above and update the counts in the
same PR. Do not widen `select` until the count is zero.
