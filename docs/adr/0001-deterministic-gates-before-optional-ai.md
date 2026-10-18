# Deterministic gates decide eligibility; AI stages are opt-in and fail-open

Hard filtering (`src/pipeline/filter.py`) and the requirements gate
(`src/requirements/gate.py`) always run first and are the authority on whether a job
is rejected. The two local-LLM stages are additive and off by default: the eligibility
gate (`src/ai/eligibility.py`) is a no-op unless `use_ai` **and**
`profile.matching.llm_eligibility_enabled` are true, and Phase 2 enrichment
(`src/ai/pipeline.py`) runs only with `use_ai` (`--ai` / API field, not an env var).
Both default to `False` in `src/models/profile.py`. When they run, they can only remove
a job on a high-confidence REJECT; UNCERTAIN, low-confidence and any LLM failure pass
the job through.

We chose this because the product's worst failure is silently hiding a viable job:
deterministic rules are explainable, testable and reproducible, while local-LLM output
varies with the model, server and context size. The AI path is measured against the
deterministic baseline (`tests/test_ai_filter_benchmark_50.py`,
`scripts/` benchmark tooling) before anyone considers turning it on.

## Consequences

- Enabling AI must never be a silent default change; it is a profile/run decision.
- LLM results never mutate the deterministic gate's evidence model (`JobRequirements`
  / `GateResult`); they are a separate signal.
