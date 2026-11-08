# 0028 — 2.0 ships after P3, minimum P6 and P7; L6–L8 move to 2.x; a temporary multi-profile gate covers 2.0

Status: accepted (2026-11-08). Roadmap decision; no code. Supersedes, for release 2.0 only, the order
and one release gate of ADR 0027. Source:
`docs/plans/2026-11-08-2.0-release-decision-readiness.md` §4 and §14.

## Context
ADR 0027 ordered L1 to L8, then P3, P5, P6, P7, and added the multi-occupation self-check (ADR 0023)
to the P7 gates. L1–L5 are merged. What stands between the engine and a usable product is setup and
release qualification, which ADR 0027 scheduled after L8. L6 is gated on the ROME licence, the
appellation export and a corpus. No §P.5 release criterion needs L6, L7 or the rest of L8.

The multi-occupation self-check cannot exist without L6. Its underlying concern can: the France
Travail web collector reads IT occupations only, and no check runs a non-IT profile end to end.

## Decision
- **Order for 2.0:** P3 (without AI setup), minimum P6, P7, as milestones M0–M5 of the readiness
  report. L6, L7 and the rest of L8 move to 2.x. L9 stays optional.
- **ADR 0027's other P7 gates stay release gates:** health-monitor fault injection, INV-2 socket test,
  lane caps, reset bit-identity, erasure verification.
- **Temporary gate G-MO-T replaces the multi-occupation self-check for 2.0 only.** The six ADR 0023
  fixture profiles run through the deterministic pipeline against a recorded baseline. Blocking: 0
  strong-rule regressions and 0 clear-target offers rejected by a domain or off-domain rule, per
  profile. Reported, not gated: top-20 target share, labelled "deterministic baseline, not the ADR 0023
  criterion", and source yield per profile. It does not claim that L6 is complete. A failure means
  fixing the vocabulary or narrowing the stated scope, never changing a threshold.
- **The ADR 0023 gate is unchanged** and returns as an L6 exit criterion.
- **2.0 is stated as tuned for IT and digital roles**, in the app and the README, unless G-MO-T and a
  France Travail occupation extension show otherwise.
- **The 2.0 occupation picker is not built.** The wizard leaves room for a later step.
- **Unchanged:** L3 and L4 stay SHADOW; `activation_enabled` and `experimental_enabled` stay false; L5
  stays inert; deterministic eligibility and ranking stay authoritative (ADR 0002). Nothing here
  permits activation, and no release criterion depends on it.

Also decided for 2.0, recorded in the readiness report: user-facing AI is hidden and its backend kept,
with AI setup and analysis in 2.1 (amends the "AI optional" and "AI grounding" criteria of completion
plan §P.5 and R-C3 for 2.0); no France Travail credential field while the API is closed to new users;
a Today card instead of the ADR 0026 Home sections, which move to 2.1.

## Consequences
- ADR 0027 stays as written and governs the L6–L8 order after 2.0.
- A later occupation and salary contract means another profile version bump.
- ADR 0026's "Reconversion" lane cannot be complete before L6.
- `docs/plans/2026-10-23-roadmap-status.md` records this order.
- Any release still needs explicit owner approval.
