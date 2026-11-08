# 0020 — Personal learning updates itself within bounds; global strategy changes still need the lab

Status: accepted (2026-11-05). Amends ADR 0011. Not implemented yet (stage L4).
Source: `docs/design/learning-architecture-decisions.md` §4.4 (AD-4), with the v1
proposal's AD-4.

## Context
ADR 0011 makes every strategy change a lab experiment promoted by explicit human approval, with an
evidence floor (30 labelled offers, 5 discordant). One user's behaviour can never meet that floor,
and asking for labels is ruled out (labels ADR). A bounded personal adjustment is a different kind of
change from a new global filter or scoring rule.

## Decision
- **Personal plane (layer 1).** The personal posterior (ADR 0018) updates continuously within
  `cap(n)`. It is not a strategy candidate and needs no lab record or approval, because it is
  bounded, explainable, reversible from the UI, and guarded by the shadow evaluation and health
  monitor (ADR 0019).
- **Personal algorithm or config changes** (new feature, new weights, new cap formula) arrive only in
  releases. On each install they first run as a shadow challenger and become active only under the
  ADR 0019 activation rule. v1's "after 30 days with no evidence against it" is **removed**: nothing
  activates on time alone.
- **Engine plane (layer 2)** — filters, scoring, evidence rules, occupation relations, knowledge-pack
  weights — is **unchanged**: pre-registration, held-out developer benchmarks, lab experiment, human
  `approve` on the CLI (ADR 0011).
- Held-out labels remain the developer release gate for the personal **algorithm** (offline
  criteria); per-install activation is governed by behaviour (ADR 0019). This replaces completion
  plan item C10 ("personalization measured on held-out labels before being on by default").

## Consequences
- ADR 0011 carries an amendment: layer-1 personal learning is outside the lab's scope.
- A property test over random signal sequences checks that no personal change accepts an offer
  violating a strong constraint or rejects any offer (INV-1); every global change still has a lab
  record.
- Rollback: personal — pause, reset or the health monitor (ADR 0018/0019); engine — lab rollback
  (ADR 0011).
