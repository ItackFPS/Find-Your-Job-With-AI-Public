# Pre-registered lab candidates

These candidates were frozen on 2026-10-17, **before any independently labelled data existed to test them on**.
Freezing first keeps a later result from being tuned to the data that judges it. Each file is an
ordinary `fyj-lab-proposal/1` (validated by `src/lab/proposal.py`). The hashes below are of the files' exact
bytes (kept LF by `.gitattributes`). A proposal's measurement records the same hash (`proposal_sha256`), so a
reviewer can check that the measured candidate is this one.

| file | sha256 | what it tests | fits ADR 0003 order? |
|---|---|---|---|
| `2026-10-h1-location-first-in-order.json` | `ee9f6171464440e371e555ef9c2de3aeb45be3db1b44fdb8b500cabd2bb72b33` | location weighted up, strict priority order kept | yes |
| `2026-10-h2-location-up-seniority-down.json` | `70f055efd640398d5e1d559467dcc78e959d2b0446b727170f4fa3398f8c2a12` | the real-v1 exploratory winner | **no** (seniority below experience) |

## Where they came from

- On `real-v1` (all accept labels assistant-assisted), `location_fit` was the most discriminating factor (factor
  AUC 0.73), and `seniority_match` ranked slightly backwards (0.47). 96 of the 116 reject-labelled jobs the gates
  pass cite location. Location ranks and never rejects (ADR 0005), so only the ranking can judge this.
- H2 won among four variants tried on `real-v1` (AUC 0.736 → 0.791, paired CI95 [+0.019, +0.103]). Because it
  was **selected on that data**, `real-v1` cannot confirm it.
- H1 was written from the ordering rule alone and **never measured** on `real-v1`.

## Decision rule (fixed now)

1. Test only on a dataset labelled **after** this date, with blind labels (`real-v2`, docs/development.md:
   "Collecting independent evidence"). Never test on `real-v1` again.
2. Measure each file unchanged: `python -m src.lab proposal docs/lab/preregistered/<file> data/benchmarks/real-v2
   --profile <profile>`. Read `comparison.ranking.delta`, computed on the jobs both sides accept.
3. A candidate **supports** its hypothesis only if all three hold:
   - the lower bound of `comparison.ranking.delta.auc_diff_ci95` is above 0;
   - no guardrail fails;
   - the uniform cohort does not rank worse: `sampling.representative.ranking.auc` from
     `python -m src.evaluation run data/benchmarks/real-v2 --config-dir <bundle> --profile <profile> --sampling`
     is not below the same figure with `--config-dir config`. `<bundle>` is the directory `proposal` created
     under its `--store`, named by the bundle hash.

   Two candidates are tested, so report both, whatever the outcome.
4. Below 30 independent accept labels in the cohort, the result is **exploratory**: report it, do not adopt.
5. Adopting H2 would also change the product priority in ADR 0003 (seniority below experience). That is the
   user's decision, separate from the evidence.

A ranking result does not make a candidate eligible under the lab's verdict. The verdict counts gate decisions
only (ADR 0011). Promotion of a scoring-only change therefore needs an explicit human decision either way.
