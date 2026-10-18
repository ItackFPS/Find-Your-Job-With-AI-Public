# Location preferences are an ordered list resolved by one shared resolver

`profile.geography.location_preferences` is an ordered list: list position is priority
and each entry is `preferred` or `acceptable`. `resolve_job_location()`
(`src/pipeline/location_match.py`) maps a job to at most one entry. Scoring
(`_location_preference_score` in `src/pipeline/scoring.py`) gives the top preferred
entry 1.0, decays 0.03 per lower position (floor 0.70), caps `acceptable` 0.15 below the
equivalent `preferred` score, and adds a small `top_location_bonus` for the single
top-priority preferred location — applied before the domain/seniority gates so it can
never rescue a dealbreaker. The legacy `target_department` branch is consulted only when
the list is empty.

We chose an ordered list over per-location weights because it is what a user can state
reliably ("this department first, then that one, remote acceptable"), and a bounded linear
decay keeps location a strong but predictable signal on top of its 0.30 weight.

## Consequences

- Reordering the list changes rankings and notification routing (ADR 0006) together,
  because both use the same resolver.
