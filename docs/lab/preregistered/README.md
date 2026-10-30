# Pre-registered lab candidates (2.0)

These candidates are frozen on 2026-10-30, **before any `real-v2` label exists**: the export has 0
labels, and labelling starts with the in-app calibration (Quality page). Freezing first keeps a later
result from being tuned to the labels that judge it. The hashes below are the sha256 of each file's
exact bytes, kept as committed by `.gitattributes` (`-text`). `tests/test_preregistered_candidates.py`
fails if a file changes or no longer applies to `config/`.

Each file is the exact `--overrides` value of `python -m src.lab candidate`.

| file | sha256 | what it tests | fits ADR 0003 order? |
|---|---|---|---|
| `2026-10-30-h1-distance-first-in-order.json` | `1a283eec7b944ffffd41949371380c7c18bcda8519e6620dbdca3c1892cf081f` | location (distance) weighted up to 0.38, domain 0.24, skills 0.16, seniority 0.12, experience 0.10 | yes |
| `2026-10-30-h2-distance-up-seniority-down.json` | `3c7100911ccccd4115215dbcd27d4ae01f7a010a006d747e58a6b2698ec8d2b2` | location (distance) 0.40, seniority 0.05, others unchanged | **no** (seniority below experience) |

## Where they came from

They re-express H1 and H2 of 2026-10-17 (kept unchanged in `history-2026-10-17/`, with their original
README and hashes `ee9f6171…` and `70f055ef…`) for the 2.0 pipeline:

- In 2.0 `location_fit` is the distance and remote-policy factor (ADR 0013), not v1's ordered location
  list. The weight key is the same, so the overrides are the same numbers; what they weight is the new
  factor. Both candidates still sum to 1.00.
- On `real-v1` (assistant-assisted labels) location was the most discriminating v1 factor (factor AUC
  0.73), and seniority ranked slightly backwards (0.47). H2 was **selected on that data** (AUC 0.736 →
  0.791 among four variants), so `real-v1` cannot confirm it. H1 was written from the ordering rule
  alone and never measured. Neither was ever measured under the distance factor.
- Not pre-registered: changes to the distance decay. Its constants are code
  (`src/pipeline/location_distance.py`), not data, so they cannot be a lab candidate. Testing one needs
  its own pre-registration file committed before its held-out run.

## Decision rule (fixed now)

1. **Labels.** Only `real-v2` labels made blind in the in-app calibration (`label_source: human`,
   labeler `in-app calibration`). Never `real-v1`.
2. **Split.** Only the **held-out** part (`--split heldout`: 30 % of the jobs, chosen by a hash of the
   dataset id and job id, see `src/evaluation/heldout.py`). The tuning part and the lab agent never
   decide a pre-registered candidate.
3. **Profile.** One copy of the owner's profile, the same file for the baseline and both candidates.
   Its hash is recorded in each run's strategy fingerprint.
4. **Measure each file unchanged:**
   `python -m src.lab hypothesis "<what the table says>"`, then
   `python -m src.lab candidate <hypothesis_id> --overrides "<the file's contents>"`, then
   `python -m src.lab run <candidate_id> <data>/calibration/real-v2 --profile <profile copy> --split heldout`.
   Read `comparison.ranking.delta`, computed on the jobs both sides accept.
5. **A candidate supports its hypothesis only if all three hold:**
   - the lower bound of `comparison.ranking.delta.auc_diff_ci95` is above 0;
   - no guardrail fails;
   - the uniform cohort does not rank worse: `sampling.representative.ranking.auc` from
     `python -m src.evaluation run <dataset> --config-dir <bundle> --profile <profile copy> --sampling --split heldout`
     is not below the same figure with `--config-dir config`.
6. **Evidence threshold.** Below 30 accept labels in the held-out cohort, the result is
   **exploratory**: report it, do not adopt it. With the first 100 labels the held-out part holds about
   30 jobs, so the first result is expected to be exploratory. Labelling more (up to 310) raises it.
7. **Report both, whatever the outcome.** Any further candidate measured on the held-out part needs a
   new dated, hashed file here, committed before that run.
8. Adopting H2 would also change the product priority in ADR 0003 (seniority below experience). That is
   the owner's decision, separate from the evidence.

A ranking result does not make a candidate eligible under the lab's verdict, which counts gate
decisions only (ADR 0011). A scoring-only change flips no gate decision, so its verdict is
`insufficient_evidence`. Promoting one needs an explicit human decision either way.
