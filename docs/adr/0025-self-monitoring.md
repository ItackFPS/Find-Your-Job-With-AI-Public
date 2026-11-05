# 0025 — The application monitors its own pipeline with detectors and alerts; it never modifies itself silently

Status: accepted (2026-11-05). Not implemented yet (base in stage L2, full set in stage L8).
Source: `docs/plans/2026-11-05-learning-architecture-decisions.md` §4.9 (AD-9), with the v1
proposal's AD-9.

## Context
Sources change format, collectors degrade, extractors drift, duplicates grow and rules start
rejecting offers the user wants (objective O-3). Today these show up only as scattered figures
(`pipeline_runs.stats_json`, `source_breakers`, the canary, the Quality page's false positives by
reason and source, #104) or not at all. Personal learning (ADR 0018) and shadow evaluation
(ADR 0019) add their own health signals.

## Decision
- **Operational plane (O).** Daily metrics go into `ops_metrics_daily(day, scope, metric, value, n)`;
  alerts into `ops_alerts(alert_id, raised_at, scope, code, severity, evidence_json, status,
  resolved_at)`. Both hold counts and codes only, no offer text and no personal content (ADR 0014,
  ADR 0015). Retention 400 days.
- **Detectors**: robust z-score against the trailing 14-day median (MAD), a minimum volume per
  metric, a 2-run persistence rule, and fixed thresholds where stated. Concerns covered: source
  degradation; field extraction completeness; requirement extraction against France Travail
  structured fields; occupation classifier accuracy on held-out local offers (ADR 0023); exact and
  near-duplicate growth (near-duplicates are linked, never auto-merged); systematic filtering errors
  (share of user positives carrying a weak flag); strong-rule safety (any positive on a
  strong-rejected offer); ranking regressions (ADR 0019 health metrics, the synthetic self-check);
  freshness and expiry.
- **v5 additions**:
  - *exposure logging health*: visible exposures per session and the share of sessions with zero
    logged exposures while lists were viewed (detects a broken observer, ADR 0016);
  - *shadow status metrics*: days in shadow, sessions, positives, discordant count, posterior P,
    shown on Quality, never as a request to the user (ADR 0021);
  - validity rules (ADR 0017) are monitored separately from user-constraint checks.
- **Allowed automatic actions** are limited to: existing breakers and pacing; treating disagreeing
  extractions as UNKNOWN; ADR 0019 pauses. Everything else is an alert with a plain sentence, its
  evidence and a suggested action, or a proposal that goes through the lab (ADR 0011, ADR 0020).
- Alerts appear in Quality › Santé and Settings › Sources. A daily digest groups them.
- Every threshold is an initial value, not an approved parameter.

## Consequences
- Each detector has an injected-fault test (a source returns 0, completeness drops, duplicates
  spike, the positive-with-flag share crosses its threshold, the extractor degrades) and must raise
  within one run.
- A replay of 30 days of run history must give at most one false alarm per week before the
  detectors are on by default.
- Detectors fail open and log; a detector crash never stops a run. Rollback: flag off, tables
  ignored.
- The Quality page's per-reason and per-source false-positive figures (#104) fold into this surface.
