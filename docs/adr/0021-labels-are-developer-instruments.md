# 0021 — Labels are developer and release instruments, never a user prerequisite

Status: accepted (2026-11-05). Mostly a rule on product flows; UI test lands with stage L1.
Source: `docs/plans/2026-11-05-learning-architecture-decisions.md` §4.10 (AD-10, approved unchanged
from the v1 proposal).

## Context
The 2.0 plan treated the owner's 100 blind labels (F0.2, R-Q5) as a step toward personal quality.
Asking every user to label offers before the application improves would block most users and turn a
measurement tool into a chore. Personal learning (ADR 0018) works from behaviour instead.

## Decision
- The application **never asks for a minimum number of labels** and never conditions quality or a
  feature on labels. No counter, quota or "label N more offers" prompt exists.
- Quality › Calibration stays available as an optional "Aider à mesurer la qualité" tool.
- Labels are used for:
  1. developer regression and release gates (synthetic sets, volunteer blind sets, owner sets);
  2. pre-registered decisions on engine changes (ADR 0011);
  3. offline simulated-user tests of the personal algorithm;
  4. the user's own **tuning-split** calibration answers, which count as explicit signals (ADR
     0018). **Held-out answers are never read by the personal plane.**
- A volunteer blind-label programme (process, not code): three or more volunteers in different
  occupations each label one export of 100 offers; these become developer datasets on the developer
  machine, never shipped.

## Consequences
- Roadmap F0.2 / R-Q5 are reframed: the owner's 100 labels are the first developer dataset, not a
  product prerequisite. R-C1 (held-out evidence before engine changes) stands.
- A UI test checks that no string or flow requires labels; `tests/test_evaluation_isolation.py` is
  unchanged.
