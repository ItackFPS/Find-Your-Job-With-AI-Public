# 0027 — The learning stages L1–L9 replace Phase 4 and run before the remaining P3, P5–P7 work; P7 gains learning gates

Status: accepted (2026-11-05). Roadmap decision; no code. Source:
`docs/plans/2026-11-05-learning-architecture-decisions.md` §4.13 (AD-13) and §6, the v1 proposal's
AD-13, and the owner's mandate of 2026-11-05.

## Context
The 2.0 completion plan (`docs/plans/2026-10-30-completion-plan.md`) had Phase 4 "ranking quality"
built on owner labels. The approved learning architecture (ADRs 0014–0026) replaces it and adds
stages that touch the same pages as P3, P5, P6 and P7. The order between them was left open in the
roadmap status record. On 2026-11-05 the owner asked to implement the architecture first, install
and test it in a fresh copy, then continue the rest of the roadmap without stopping between items.

## Decision
- **Phase 4 is replaced** by stages L1–L4 (signals and privacy; structured evidence and ops base;
  model in shadow with the simulator; personal ranking eligible). Completion-plan item C10 is
  replaced by ADR 0020.
- **Order**, sequential (owner preference, no parallel streams):
  1. L1 → L2 → L3 → L4 → L5 → L6 → L7 → L8. L9 (estimated position correction) is optional and is
     done only if offline validation on logged data supports it.
  2. Install and test in a **fresh separate copy** (own empty `data/`, example profile), never the
     V1 install, the development database or the labelling environment.
  3. Then P3 (profile v2 UI, AI setup UI, embedding download, wizard, language, CV import), P5 (AI
     analysis on demand, unchanged; the LLM stays optional and no learning decision uses it), P6
     (polish, with Home sections per ADR 0026), P7 (release qualification).
- **Exceptions to the order**, because a stage needs them: the occupation picker
  (`targets.occupations`, ADR 0023) lands with L6 in the existing Profile page and is reused by the
  P3 wizard; the `matches_salary` correction (ADR 0024) lands before L7.
- **P7 release gates gain**: health-monitor fault-injection tests (ADR 0019); the INV-2 socket test
  (ADR 0014); the multi-occupation self-check (ADR 0023); lane caps (ADR 0026); reset
  bit-identity (INV-7, ADR 0018); the erasure verification test (ADR 0015).
- The EXPERIMENTAL state ships disabled and is enabled only after simulator scenarios SR-4 and SR-5
  pass (owner decision, ADR 0019).
- Stage exit criteria in v5 §6 are proposals, confirmed when each stage is planned. Activation of
  personal ranking is never a stage exit criterion; it happens only when the evidence exists.

## Consequences
- `docs/plans/2026-10-23-roadmap-status.md` records this order, and marks L0 done when this ADR
  merges.
- Each stage gets its own plan before code (CLAUDE.md §11.4), since every stage changes a contract
  or a schema.
- Any release still needs explicit owner approval.
