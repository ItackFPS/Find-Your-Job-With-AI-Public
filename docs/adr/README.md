# Architecture Decision Records

Short records of decisions that are hard to reverse, surprising without context, and
the result of a real trade-off. Format: `.agents/skills/domain-modeling/ADR-FORMAT.md`.
Each ADR cites where the decision lives in code; if code and an ADR disagree, the code
is the source of truth and the ADR must be updated or superseded.

| ADR | Decision |
|---|---|
| [0001](0001-deterministic-gates-before-optional-ai.md) | Deterministic gates decide eligibility; AI stages are opt-in and fail-open |
| [0002](0002-stored-score-is-deterministic.md) | The stored job score is the deterministic score; later stages rank per run |
| [0003](0003-hybrid-scoring-with-dealbreaker-gates.md) | Scoring is a weighted composite with multiplicative dealbreaker gates |
| [0004](0004-education-and-experience-ceilings.md) | Stated education and experience above the profile's maximum hard-reject |
| [0005](0005-ordered-location-preferences.md) | Location preferences are an ordered list resolved by one shared resolver |
| [0006](0006-location-first-notification-routing.md) | ~~Job notifications are routed by location, one channel per job, no fallback~~ (superseded: notifications removed in 2.0) |
| [0007](0007-evaluation-harness-outside-production-path.md) | The evaluation harness lives outside the production path and replays the real functions |
| [0008](0008-decision-journal.md) | Every job gets an auditable, versioned decision record (observability only, fail-open) |
| [0009](0009-feedback-journal.md) | Feedback is an append-only journal of facts, linked to the decision it reacts to |
| [0010](0010-disagreement-and-abstention.md) | Signals are compared; uncertainty becomes `review`, never a rejection |
| [0011](0011-experiment-lab.md) | Strategy changes are tested in a separate lab and promoted only by explicit human approval |
| [0012](0012-freshness-dates.md) | Listing age comes from the source date, else our first sighting — never the last collection |
| [0013](0013-distance-and-remote-policy-location.md) | Location is matched on distance from home and a typed remote policy (supersedes 0005 once a home is set) |
