# 0034 — The personal learning module is removed

Status: accepted (2026-11-16, owner decision, issue #250). Supersedes ADR 0018, ADR 0019 and
ADR 0033.

## Context
The personal learning module (`src/learning`) fitted a personal preference model on the journals
(ADR 0018), compared it in shadow with the production ranking and activated it through a health
monitor (ADR 0019), and from 2026-11-15 used an anytime-valid sequential test (ADR 0033). It also drew
the Découverte (S4) slots. The rule was safe: worst false activation 4.9 %. It was also ineffective: on
synthetic volumes, a 0.70 challenger activated in at most 1.8 % of histories within 90 days
(`docs/reports/2026-11-15-sequential-activation.md`). Earlier work found no design that was both valid
and powerful with the evidence one user produces (activation reports of 2026-11-06 and 2026-11-07).

## Decision
Delete the module and everything that exists only for it:

- `src/learning/` (signals, model, shadow, gates, health, exploration, simulator) and
  `src/evaluation/learning_curve.py`;
- the pipeline stage that drew Découverte, journaled the shadow ranking, updated the health record
  and applied the personal order;
- the endpoints `GET /api/jobs/decouverte`, `GET /api/privacy/learning/status`,
  `POST /api/privacy/learning/resume` and `GET /api/quality/learning-curve`, and their pages;
- the `personal_model` section of `config/personalization.yaml`.

The visible order is the production ranking again: deterministic score, semantic matching and the
classic personalization stage, capped at the gates.

Kept, because other parts use them:

- the personalization stage, with Pause and Reset learning (ADR 0015), which still govern it;
- the feedback journal, the exposure log and their surface and position context (ADR 0016), which feed
  personalization, the review queue and evaluation;
- in existing databases, the tables `personal_model_state`, `ranking_shadow`, `shadow_positive`,
  `personal_health`, `s4_draw` and the `job_scores` columns `personal_adjustment`,
  `personal_model_version`, `visible_position`. Nothing writes them any more and new databases do not
  create the tables. They stay classified (`src/storage/data_classes.py`), so "Erase my activity" and
  "Reset learning" still clear old values. Dropping them would be a data migration nobody approved.

## Consequences
- An installed `personalization.yaml` the user edited may keep a `personal_model` section: it is
  ignored.
- Bringing personal ranking back needs a new ADR and a design with enough power for one user's
  evidence. ADRs 0018, 0019 and 0033 and the reports stay as the record of what was tried.
