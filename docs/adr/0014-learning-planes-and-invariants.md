# 0014 — Learning is split into four planes bound by shared invariants

Status: accepted (2026-11-05). Not implemented yet: the code-level checks below land with stage L1.

## Context
The owner approved the learning architecture v5
(`docs/design/learning-architecture-decisions.md`, §2–§3, with the four-plane table of the
v1 proposal §2). The application is to learn from the user's behaviour, from the collected market
and from its own operation, with no labels asked of the user, no server and no paid AI. Learning
that mixes those sources, or that can reject offers or move the stored score, would break
ADR 0001, 0002 and 0011. Every later learning ADR (0015 onwards) cites this one.

## Decision
Four planes. Each owns its own tables and influences the others only through the listed flows.

| Plane | Question | Owns (writes) | May influence | Must never |
|---|---|---|---|---|
| **P Personal** | What does *this user* want? | personal model state, ranking shadow, per-run `job_scores.personal_*` | ranking order, lanes, profile *suggestions* | accept or reject; change the stored score; leave the install; feed E or M |
| **E Engine** | What makes a good match in general? | config bundles, knowledge packs, code (shipped in releases); `active_strategy` via the lab | scoring, evidence rules, occupation relations, defaults | learn at runtime from one user's signals |
| **M Market** | What does the collected market look like? | `market_*` aggregate tables, from stored offers only | one weighted input of occupation relatedness; insights; suggestions | decide acceptance; read feedback or the profile |
| **O Operational** | Is the pipeline doing its job? | `ops_metrics_daily`, `ops_alerts` | alerts, existing source pacing, proposals | silently change filters, scoring or rankings |

Allowed flows, and no others: E → P (prior, feature definitions, caps); M → E at run time
(co-occurrence as one weighted, minimum-support input of relatedness); M → P none (suggestions only,
confirmed by the user); P → O aggregate counts only, which are **personal data** under the privacy
ADR; O → E developer reports, exported by the user on request, never sent automatically.

Invariants, true for every later learning decision:

- **INV-1** Learning never overrides a user's strong constraint, and never rejects an offer.
- **INV-1b** Offer validity (closed, expired, duplicate, malformed) is operational, not a
  preference: no learning, lane or user setting shows an invalid offer as open.
- **INV-2** Nothing personal leaves the install (profile, signals, learned state, aggregates).
- **INV-3** The stored score stays deterministic (ADR 0002); learned effects live in per-run
  ranking and their own derived tables.
- **INV-4** Learned state is derived from journals and can always be rebuilt or deleted.
- **INV-5** Every learned effect is visible, explainable and undoable from the UI, with no
  terminal or file editing.
- **INV-6** A new learned behaviour runs in shadow first; without sufficient positive evidence the
  production ranking stays. Absence of negative evidence is not enough.
- **INV-7** Cold start is today's ranking, bit-identical.
- **INV-8** The planes stay separate, as above.
- **INV-9** No claim stronger than its evidence: a local sample is never "the market", a skill
  the profile does not mention is never "a skill you lack", a heuristic is never "unbiased".
- **INV-10** Erase means erased in every copy the application controls (database, WAL, its own
  backups); copies outside its control are named to the user; an erasure is never reported
  complete before it is verified; writes created before an erasure are never re-inserted after it
  (server-controlled erasure generation, not client time).

We chose planes over one learning component because each source has a different trust level and
privacy class: market data is public, personal signals are private and biased by what was shown,
engine changes must stay human-approved (ADR 0011). Keeping them apart lets each be erased,
rebuilt, paused or measured on its own.

## Consequences
- Enforcement uses the technique of `tests/test_evaluation_isolation.py`: import-boundary tests (the
  personal plane imports no network client; market and operational code read feedback only through
  the one flow-4 aggregate function; production still imports neither `src/evaluation` nor
  `src/lab`), plus a socket-guard test for INV-2 and a bit-identity test for INV-7. They ship with
  the first code of stage L1, not with this ADR.
- Existing packages map onto planes: `src/personalization/` and `src/feedback/` are P (the
  personalization engine is replaced, its scaffolding kept); scoring, filters, `src/requirements/`,
  `src/esco/` and `src/lab/` are E; `src/skillgraph/` becomes M's view layer. Package names for new
  code are fixed by the ADRs that introduce it.
- ADR 0001, 0002, 0009 and 0011 are amended by the later ADRs that change them (0009's "feedback
  never changes ranking by default" and 0011's lab scope), not by this one.
- No parameter is set here. Every threshold in the plan stays unapproved until measured.
