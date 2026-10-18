# The evaluation harness lives outside the production path and replays the real functions

`src/evaluation/` measures the pipeline offline: versioned datasets (JSONL + hashed
manifest), a decision-level runner, metrics, immutable run results and "golden"
decisions. It calls the production functions (`filter_jobs`, `RequirementExtractor`,
`gate_jobs`, later `score_jobs_with_breakdown`) instead of re-implementing them, and
`tests/test_evaluation_isolation.py` fails if any other `src/` module imports it.
Ground-truth labels are joined only after inference; results never touch `jobs.db`;
no network or LLM is needed (an LLM is opt-in per run).

We chose this because evolving the matching safely requires measuring it first, and a
harness that shared code paths with production could not detect production changes (or
could itself change production). Time-dependent checks are frozen to the dataset's
`as_of` so replays are reproducible.

## Consequences

- The golden file is a **change detector, not a quality target**: a diff means "a
  decision changed — review it". It must never be tuned against, and sets no
  precision/recall floor (consistent with `tests/test_ai_filter_benchmark_50.py`).
  The only hard gate is the safety invariant `unsafe_rejection_rate = 0` on jobs
  labelled `accept`, enforced by `tests/test_hard_filter_benchmark.py` on its corpus.
- Real-job datasets exported from `jobs.db` contain only jobs accepted at the time
  (rejected jobs keep no description). They therefore expose false positives but cannot
  measure false negatives; runs on them carry an explicit survival-bias warning.
- The runner mirrors the orchestrator's requirements-gate wiring and builds the scoring
  config through the shared `scoring.build_scoring_config`.
  `tests/test_evaluation_orchestrator_equivalence.py` runs the real `run_pipeline()` on
  the same jobs and requires identical accepted sets, scores and factors, so drift fails
  a test. A future refactor could still extract the gate wiring.
- ESCO synonym enrichment is off by default in evaluation runs (results must not depend
  on a developer's `data/esco.db`); `--esco-db` enables the production lookup and joins
  the strategy fingerprint.
