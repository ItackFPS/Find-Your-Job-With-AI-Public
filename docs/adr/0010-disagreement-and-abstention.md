# Signals are compared, uncertainty becomes `review`, never a rejection

The pipeline has several heterogeneous opinions about a job (deterministic fit, semantic
similarity, Phase 2 profile match, the LLM eligibility verdict) but no way to say "these
disagree" or "I don't know". `src/decisions/disagreement.py` compares them and answers
`accept | review | reject` with an agreement-based `decision_confidence`, kept strictly separate
from `fit_score`, `unknowns` (the UNKNOWN evidence dimensions) and `conflicts`.

Rules that are not negotiable: uncertainty is never turned into a rejection (a rejected job can
become `review`, never `accept`; an accepted job can become `review`, never `reject`); `review`
never removes a job from ranking or notifications; the confidence an LLM states about itself is
kept only as a categorical signal (`eligibility`), never as the decision confidence. The default
mode is **shadow**: the assessment is journaled next to the unchanged production decision
(`signals.assessment`, `decision_confidence`, `confidence_calibrated = false`). `active` only
labels the journaled outcome `review` — the pipeline itself is unchanged — and must be switched
on by the owner in `config/disagreement.yaml` after reading the shadow metrics.

## Consequences

- A rejection resting only on LLM evidence (no text/rule-backed violation) is flagged `review`
  in the journal rather than standing silently; a REJECT the LLM issued but the pipeline
  overrode (low confidence / unsupported) is a recorded conflict.
- The confidence is an uncalibrated heuristic (agreement × completeness × (1 − UNKNOWN density)
  × (1 − conflicts)); it becomes meaningful only after calibration on labelled data (ECE/Brier).
- `python -m src.evaluation run <dataset> --assess` evaluates the engine offline on
  deterministic evidence (no semantic/Phase 2/LLM signals offline) and reports review rate,
  capture rate and lift. **On the 50-job synthetic dataset the result is
  `insufficient_evidence` and the engine flags nothing**: 4 wrong decisions, none captured, so
  there is no evidence yet that the flag is useful and `active` should stay off.
- Thresholds live in `config/disagreement.yaml`, whose hash is recorded in every assessment (`config_hash`). It is not part of the strategy fingerprint, which tracks only what changes accept/reject decisions or scores.
- **Engine v2 — user feedback is a signal.** The feedback journal (ADR 0009) gives each job a
  *stance*: the latest explicit `interested` / `not_interested`, or an application status showing the
  user pursued it (`to_apply`, `applied`, `interview`, `offer`, `accepted_offer`,
  `employer_rejected`); `withdrawn` clears it; views and clicks never count. A rejection of a job the
  user wants is `review` (reason + conflict), whatever evidence backs it; an accepted job the user
  declined is a recorded conflict (`review`). Production decisions are unchanged (shadow), and the
  stance is journaled with its basis in `signals.assessment.signals.user`.
  Switch: `review_on_user_feedback` in `config/disagreement.yaml`.

## Review queue: when feedback answers a doubtful decision

`GET /api/decisions/review-queue` lists jobs whose latest decision is flagged `review` and not yet
answered (`DecisionStorage.review_queue`). A decision is asked about once per *episode*, the run of the
job's latest decisions with the same outcome: explicit feedback linked to any decision of that episode
answers it, so a job rejected run after run is not asked about after every run. Unlinked explicit
feedback answers it only when given strictly after the latest decision. Feedback linked to an earlier
episode never does, so a job the user wanted comes back when it is rejected again after having been
accepted. Behavioural signals (views, clicks) never answer and never create a stance.
