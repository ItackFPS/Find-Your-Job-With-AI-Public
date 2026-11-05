# L5 Weak evidence: implementation plan (2026-11-05)

Stage L5 of the learning architecture (decisions v5, AD-2b; proposal AD-2). It changes the meaning of
the hard filter for some checks, which is a global strategy change (ADR 0011). So it ships behind a
flag that defaults to today's behaviour, and the switch is decided by a pre-registered evaluation
and the owner, never by this plan.

## Invariants

- `filters.weak_evidence_mode` defaults to `reject` for every check. With the default, every run
  rejects, scores and ranks exactly as before (tests pin this).
- STRONG violations still reject in every mode. AD-2a validity rules (closed, duplicate, malformed)
  are not user constraints and are never weak.
- `salary` is not switchable until the `matches_salary` correction lands (v5 AD-2b prerequisite).
- Weak-flagged offers never enter the main list. The "outside your criteria" lane never notifies.
- Nothing is discarded: weak-flagged offers outside the lane are stored and journaled as
  `below_threshold`.
- The parameters (penalty 0.10 / 0.25, age ramp 1×–3×, lane 10, score floor 60, domain floor 0.7)
  are the pre-registered values, not approved values.

## Slices (one PR each, in order)

| Slice | What | Files | Tests |
|---|---|---|---|
| **L5.0** | This plan; the dated, hashed pre-registration `docs/lab/preregistered/2026-11-05-ad2-weak-evidence.json` | plan, README, `tests/test_preregistered_candidates.py` | the frozen-hash test |
| **L5.1** | Per-check strength (STRONG / WEAK / UNKNOWN) from the AD-2 table, computed next to `FilterCheckOutcome`; the decision journal records `strength` on each evidence item (additive `schema_version` bump, missing field = legacy) | `src/pipeline/filter.py`, the journal builder | classification table cases; journal round trip; full pipeline unchanged with the default |
| **L5.2** | `filters.weak_evidence_mode` per check (`reject` default, `rank`); in `rank`, a WEAK failure passes the filter with a `weak_flags` entry; the scorer subtracts the bounded penalty before the ADR 0003 gates; `score_breakdown` gains `weak_flags`, `weak_penalty` | `config/filters.yaml`, `src/pipeline/filter.py`, `src/scoring/`, orchestrator | default = byte-identical rejections; `rank` passes only WEAK; STRONG still rejects; penalty caps; salary refused |
| **L5.3** | Lanes: main list excludes weak-flagged offers; "outside your criteria" (≤ 10, score ≥ 60, `domain_relevance` ≥ 0.7, best first) served by its own endpoint and shown collapsed; the rest is `below_threshold` in the journal `exposure_json.lane` | `src/storage/runs.py`, `src/api/routes/jobs.py`, frontend Jobs page | lane cap in 100 % of runs; main never holds a weak flag; no notification |
| **L5.4** | Exploratory evaluation on real-v2 tuning (criteria 1–2 counts, criterion 4 self-check, criterion 5 runtime on a synthetic replay); report under `docs/reports/` | `src/evaluation/` (lane-aware counts only) | evaluation unit tests |

The decisive real-v3 held-out run happens once, after the owner reports labelling finished, as the
pre-registration says. It is not part of these PRs.

## Rollout after a pass

One check at a time (`published_age`, then `title_keywords`, then `seniority`), each by a human
promotion of a lab bundle that sets that check to `rank`. Rollback: the check back to `reject`, or
the lab `rollback`.

## Out of scope

- AD-7 occupation proximity (the lane uses `domain_relevance` ≥ 0.7 until then).
- The learned promotion of a weak-flagged offer into the main list (needs AD-3 active; activation
  stays off).
- Salary under AD-2b (blocked on the `matches_salary` correction).
