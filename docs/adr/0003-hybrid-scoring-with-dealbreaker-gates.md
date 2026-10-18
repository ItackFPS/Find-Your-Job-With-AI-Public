# Scoring is a weighted composite with multiplicative dealbreaker gates

`src/pipeline/scoring.py` (`_apply_weights`) combines five factors additively —
`location_fit` 0.30, `domain_relevance` 0.25, `skills_match` 0.20, `seniority_match`
0.15, `experience_fit` 0.10 (defaults; configurable in `config/scoring.yaml`) — then
applies two gates: a confirmed off-domain job (`domain_relevance ≤ 0.05`) is capped at
`domain_cap`, and a confirmed seniority mismatch (`seniority_match == 0`) is multiplied
by `seniority_multiplier`.

This replaced a 7-factor mean that clustered almost every job in a narrow 78–83 band
(commit 32bae50). A pure weighted average lets strong location or skills compensate for
a dealbreaker; the gates make a confirmed domain or seniority mismatch
non-compensable, while the additive part keeps "matter of degree" factors rankable.

## Consequences

- Weight order encodes product priority: location > domain > skills > seniority >
  experience. Changing it changes which jobs surface first.
- Any stage that adjusts scores afterwards must re-apply the gates (see ADR 0002).
- `score_to_tier()` is the single tier implementation; `frontend/src/utils/scoreToTier.ts`
  is a manual mirror.
