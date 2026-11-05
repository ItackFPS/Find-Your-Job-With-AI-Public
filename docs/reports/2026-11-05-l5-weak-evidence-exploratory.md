# L5.4 Weak evidence: exploratory evaluation (2026-11-05)

Stage L5.4 of the learning architecture (plan `docs/plans/2026-11-05-l5-weak-evidence-plan.md`). This
report applies the pre-registered AD-2b test (`docs/lab/preregistered/2026-11-05-ad2-weak-evidence.json`,
sha256 `2029332684e971f21d7167aaa8c9307eac5d9c447ce8be223af71f84abfcfafe`) to **exploratory** data only.
It decides nothing. The decisive measurement is the real-v3 held-out part, run once after the owner
reports labelling finished.

## How it was run

```
python -m src.evaluation weak-evidence <labelling>/data/calibration/real-v2 \
    --profile <labelling>/data/lab/decision-2026-11-03/profile.yaml --split tuning
python -m src.evaluation weak-evidence tests/fixtures/evaluation/datasets/synthetic-ai-filter-50 --split all
```

- **Baseline:** the code and config of this branch, every check `reject`.
- **Candidate:** the same config with `published_age`, `title_keywords` and `seniority` set to `rank`.
  Penalty, ramp and lane values are the pre-registered ones.
- **Profile:** the frozen copy used for the 2026-11-03 decision run. It is personal and not committed.
- **Data:** real-v2 labels version 5, read-only, tuning split only (69 labels, 7 `accept`). The held-out
  part is not read: the CLI refuses `--split heldout`.
- **Score:** offline runs use the deterministic score, the proxy every evaluation report uses for
  production's final score. The main list is the accepted, unflagged offers in the `relevant` tier or
  above.
- **Output:** counts only. No job content is reported.

## Results

### real-v2, tuning split (exploratory)

| | Baseline | Candidate |
|---|---|---|
| main | 15 | 15 |
| outside your criteria | 0 | 2 |
| below threshold | 36 | 213 |
| rejected | 259 | 80 |

| Criterion | Result | Verdict |
|---|---|---|
| 1. relevant offers in main ∪ outside, ≥ 2× baseline | 1 → 1 (candidate: 1 main, 0 outside) | **fails** |
| 2. main precision not lower beyond the paired 95 % CI | 1/3 → 1/3; difference CI [0, 0] | holds |
| 3. outside lane ≤ 10 | 2 | holds |
| 4. no STRONG change | 179 verdict changes, all weak-flagged acceptances; 62 offers rejected by a later check; 0 other changes | holds |
| 5. runtime ≤ +25 % on a replay of the owner's database | not computed offline | — |

Offline run time on this dataset was 2.7 s for the baseline and 2.4 s for the candidate. The two runs
are not comparable enough to measure criterion 5.

The proposal's "baseline 2 of 7" counted the relevant offers the gates accept. Under criterion 1's
definition (main ∪ outside), the baseline is 1 of 7: the other accepted relevant offer scores below
the `relevant` tier.

### Where the 7 relevant tuning offers land

| Baseline | Candidate | Offers | Why the candidate does not surface it |
|---|---|---|---|
| main | main | 1 | — |
| below threshold | below threshold | 1 | score 0.56, unflagged |
| rejected by a weak check (title / age) | below threshold | 4 | `domain_relevance` 0.35 < 0.7 floor (one also scores 0.66 ≥ 0.60) |
| rejected by `published_age` | below threshold | 1 | score 0.50 < 0.60 floor (`domain_relevance` 1.0) |

The candidate does what it should mechanically: the 5 relevant offers rejected by weak checks now pass
the filter and the requirements gate. None reaches the outside lane:

- **Domain floor.** The 4 offers whose titles failed `title_keywords` (or whose titles do not name the
  target role) also score `domain_relevance` 0.35. The same title signal drives both, so the lane's
  0.7 domain floor removes what ranking `title_keywords` let through.
- **Score floor.** The 5th offer misses the 0.60 floor.

### Synthetic benchmark (criterion 4)

`synthetic-ai-filter-50`:

- 14 verdict changes, all weak-flagged acceptances;
- 4 offers rejected by a later check (requirements gate or `contract_type`);
- 0 other changes; main list unchanged (19);
- outside lane 0.

The committed golden decisions, which hold the ADR 0004 safety cases, are unchanged: the shipped
config is all-`reject`.

## Reading

Exploratory evidence only (7 relevant labels; README rule 6 needs 30 for adoption). It suggests that,
as pre-registered, the candidate would also fail criterion 1 on real-v3 if real-v3 shows the same
pattern. The blocking parameters are the lane floors, mostly the domain floor, not the filter
mechanics.

The pre-registration fixes the values. Changing them (for example the lane domain floor, or dropping
`title_keywords` from the candidate) needs a new dated, hashed file before the real-v3 held-out part
is read. That choice is the owner's.

## Not covered

- Criterion 5 (replay of a copy of the owner's database).
- The semantic blend: offline scores are deterministic only.
- real-v3: nothing on its held-out part was computed.
