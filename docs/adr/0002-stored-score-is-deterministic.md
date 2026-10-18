# The stored job score is the deterministic score; later stages rank per run

`src/pipeline/orchestrator.py` persists each job and its deterministic score
(`storage.upsert_jobs_batch`) immediately after scoring, **before** Phase 2 enrichment,
semantic matching and personalization run. Those later stages produce a per-run ranking
(final blended score, semantic score, personalization adjustment) recorded per pipeline
run in `job_scores`; they do not overwrite the job's stored score.

We chose this so a job's canonical score is reproducible from its content and the
profile alone, independent of embedding models, feedback history or whether AI ran on
that particular run. Semantic similarity is blended with a deliberately small weight
(`similarity.weight`, shipped at 0.06 in `config/semantic_config.yaml`), and
`cap_personalization_at_gates()` (`src/pipeline/scoring.py`) re-applies the domain and
seniority gates after personalization so feedback cannot lift a gated job back up.

## Consequences

- Anything that needs "the" score of a job (tiers via `score_to_tier()`, notifications,
  top-10 leaderboards) reads the stored deterministic score.
- The orchestrator module docstring still lists `store` last; the code order above is
  authoritative.
