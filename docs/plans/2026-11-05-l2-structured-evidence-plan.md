# L2 — Structured evidence and the operations base (plan)

Stage L2 of the learning architecture v5 (`2026-11-05-learning-architecture-decisions.md` §6;
ADR 0023 step 1, ADR 0025 partial). Owner mandate of 2026-11-05: implement L1–L9 sequentially.

## Exit criteria (v1 proposal §9, unchanged in v5)

- ≥ 99 % France Travail ROME coverage, measured and shown;
- first extractor precision/recall published in Quality;
- detector fault tests pass (a source returning 0, a completeness drop).

## Slices (one PR each)

| Slice | Contents | Contract change |
|---|---|---|
| L2.1 | `src/requirements/structured.py`: typed ROME occupation, experience, education and salary from the stored France Travail payload (pure functions). `src/evaluation/extractor_agreement.py` (read-only, child process) and `GET /api/quality/extractor-agreement`: ROME coverage, extractor precision / recall / disagreement for experience and education | new API response (additive) |
| L2.2 | Quality page: "Requirement extraction" card showing that report | frontend only |
| L2.3 | `ops_metrics_daily`, `ops_alerts` (operational); after each pipeline run, per-source metrics (offers collected, new offers, field completeness of title, description, company, place, date, contract) and two detectors: source volume (robust z < −3 vs the trailing 14-day median/MAD on 2 consecutive runs, minimum volume, or 0 offers when the median ≥ 5) and field completeness (drop ≥ 20 points vs the median). `GET /api/quality/alerts`, `POST /api/quality/alerts/{id}/resolve` | new tables (additive), new API |
| L2.4 | Quality › Santé: open alerts with their sentence, evidence and suggested action; resolve | frontend only |

## Not in L2 (stated)

- Structured evidence does **not** change any gate or score in L2. "A disagreeing extraction is
  treated as UNKNOWN" (ADR 0025's allowed automatic action) lands with evidence strengths in L5,
  where the gate consumes typed evidence; L2 measures first.
- No stored copy of the structured values: they are derived from `raw_payload` on read. The
  occupation table (`job_occupations`) arrives with the classifier in L6.
- Duplicate, freshness, systematic-filtering and ranking detectors are L8.

## Tests

Pure-function tests with synthetic payloads; reports and detectors on temporary databases with
injected faults; API tests through `TestClient`; Settings/Quality page tests in vitest.
