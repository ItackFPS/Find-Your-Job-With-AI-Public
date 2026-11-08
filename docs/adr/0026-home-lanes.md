# 0026 — Home shows three labelled lanes; exploration and "outside criteria" offers are capped and explained

Status: accepted (2026-11-05). Not implemented yet (Home sections in P6; lane inputs arrive with
stages L4–L6). Source: `docs/design/learning-architecture-decisions.md` §4.11 (AD-11),
with the v1 proposal's AD-11.

## Context
A single ranked list hides the offers the user would not find alone: near-misses outside a soft
criterion, reconversion paths, and exploration picks that the personal model needs to learn from
(ADR 0018). It also hides the cases where the engine and the user's behaviour disagree.

## Decision
- **Home sections:**
  1. **"Meilleures offres"**: the main ranked list.
  2. **"À regarder"**: the outside-criteria lane (ADR 0017, at most 10), "Reconversion possible"
     (ADR 0023 entry rule: middle similarity band, at least 2 transferable skills, no conflict on a
     mandatory requirement) and labelled exploration picks (ADR 0018, at most 2 inside the top 20 of
     the main list).
  3. **"À vous de juger"**: disagreement review items (ADR 0010, after its own calibration) and the
     optional active question (ADR 0016).
- Each section has a one-line explanation, and each offer a "pourquoi cette offre" link.
- **Hidden offers** (ADR 0017, hiding) are excluded from every lane.
- Lanes only reorder and surface offers that passed strong constraints; no lane accepts an offer a
  strong constraint rejected (INV-1).
- Lane sizes and the automatic shrink rule (shrink "À regarder" to 5 when its open rate stays below
  25 % of the main list's after 30 days, measured as an O-plane metric) are initial values, not
  approved parameters.

## Consequences
- Tests enforce lane caps and the exclusion of hidden and strong-rejected offers.
- A usability test (R-A3): 3 of 3 testers can say why an offer is in "À regarder".
- The open-rate metric is computed by self-monitoring (ADR 0025).
