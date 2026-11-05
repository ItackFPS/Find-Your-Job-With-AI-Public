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
| [0014](0014-learning-planes-and-invariants.md) | Learning is split into four planes (personal, engine, market, operational) bound by shared invariants |
| [0015](0015-privacy-retention-and-erasure.md) | Personal data has four in-app actions; erasure is a resumable job verified before success (supersedes 0009's owner-side erasure) |
| [0016](0016-signal-capture-and-delivery.md) | Only visible exposure is logged; durable actions go through an acknowledged, ordered outbox |
| [0017](0017-validity-constraints-and-hiding.md) | Offer validity is operational; only strong user constraints reject; weak ones rank; Hide is list management |
| [0018](0018-personal-preference-model.md) | Personal preferences are a bounded Bayesian model that only reorders accepted offers |
| [0019](0019-shadow-evaluation-and-health-monitor.md) | Personal ranking activates only on positive shadow evidence and pauses itself more easily than it activates |
| [0020](0020-two-promotion-regimes.md) | Personal learning updates itself within bounds; global strategy changes still need the lab (amends 0011) |
| [0021](0021-labels-are-developer-instruments.md) | Labels are developer and release instruments, never a user prerequisite |
| [0022](0022-knowledge-packs.md) | Shared intelligence travels only in versioned, read-only knowledge packs shipped with releases |
| [0023](0023-occupation-model.md) | Offers get a ROME occupation (structured first, then a calibrated per-code classifier); similarity, transferable skills and feasibility stay separate |
| [0024](0024-market-plane-and-salary-comparability.md) | Market figures describe the collected sample with their coverage; salaries compare only inside one comparability class |
| [0025](0025-self-monitoring.md) | The application monitors its own pipeline with detectors and alerts; it never modifies itself silently |
