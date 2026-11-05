# 0023 — Offers get a ROME occupation (structured first, then a calibrated per-code classifier); similarity, transferable skills and feasibility stay separate

Status: accepted (2026-11-05). Not implemented yet (stage L2 for structured fields, L6 for the
classifier). Design, data splits and evaluation tables:
`docs/plans/2026-11-05-learning-architecture-decisions.md` §1 and §4.7 (AD-7).

## Context
Matching today rests on keyword vocabularies and the embedding. Professional potential (objective
O-1) needs occupations: what job an offer is, how close two jobs are, and what of the user's
evidenced experience transfers. France Travail offers carry `romeCode` on 100 % of a 1,161-offer
sample; other sources carry nothing. A nearest-neighbour vote over prototypes favours codes with many
appellations (by construction), and ROME codes have very unequal appellation counts.

## Decision
- **Scheme.** ROME 4.0 is primary (knowledge pack, ADR 0022); ESCO optional and not in the first
  pack; tables stay scheme-agnostic. Occupation groups (domaine professionnel, grand domaine) come
  from the ROME arborescence.
- **Assignment cascade.**
  1. *Structured*: the source's ROME code, confidence 1.0.
  2. *Classifier*: equal prototype budget per code (≤ 3 pack prototypes from appellation labels by
     k-medoids, ≤ 3 local prototypes from the user's structured FT offers for codes with ≥ 5 local
     offers); one **per-code score** (mean of the best two similarities), not neighbour votes; a
     pooled count-offset correction if the score still rises with prototype count (by count bucket,
     shrunk code → domaine → grand domaine); softmax over the top 10 with a fitted temperature;
     **isotonic calibration** at code, domaine and grand-domaine level. No threshold is ever used
     uncalibrated.
  3. *Decision cascade*: assign the code if calibrated and not ambiguous across domaines; else the
     domaine professionnel; else the grand domaine; else unknown with the keyword fallback. The
     level and probability are stored in `job_occupations`.
- **Data separation.** Developer corpus stratified by grand domaine, split by employer,
  near-duplicates removed across splits: **D-design** (method choices), **D-calib-A** (offsets,
  temperature), **D-calib-B** (isotonic maps, thresholds; cross-fitting by employer when a domaine is
  small), **D-test** from a later period, used once — any change after seeing it needs a fresh
  D-test. On an install, 20 % of local structured offers, drawn by employer, are never prototypes
  and are the only ones the accuracy monitor measures.
- **Three separate concepts** (replacing one "relatedness and reachability" score):
  *occupation similarity* (same code, domaine, grand domaine, ROME skill overlap, market
  co-occurrence as one weighted input); *transferable skills* (skills the profile evidences that the
  fiche or offer asks for); *transition feasibility* (which stated requirements are met, unconfirmed
  or in conflict, from structured FT fields and the extractor).
- **Wording rule (INV-9).** A skill the profile does not mention is "not stated", never "missing":
  "7 compétences de votre profil correspondent à ce métier. 3 autres demandées ne figurent pas dans
  votre profil — à vérifier." Profile completeness is shown next to any feasibility statement.
  Feasibility is never a gate; mandatory requirements keep ADR 0004.
- **Scoring use**, through the lab only: `domain_relevance` from similarity; the ADR 0003 domain
  gate fires only at calibrated confidence ≥ 0.8, low similarity, and an untargeted occupation.
  Profile occupations come from a picker (`targets.occupations`).

Every M, L, threshold, band and budget is an initial value, fitted or confirmed by measurement.

## Consequences
- **Implementation preconditions**: the ROME licence PDF, the per-code appellation distribution in
  the ROME export, and the developer corpus with its four splits.
- Evaluation per grand domaine and overall, on D-test and on a small labelled non-FT sample: top-1/3
  accuracy, domaine-level accuracy, abstention, calibration error, accuracy by prototype-count bucket,
  confusion inside vs across domaines (never merged into one figure), against the top-k vote
  baseline. No target is set before that baseline exists; a domaine that fails gets the group
  fallback only.
- A UI test forbids "manque", "manquant" or "missing" for skills; a multi-occupation self-check (6
  fixture profiles, 600 fixture offers) guards every occupation, not just IT.
- Assignment runs on new offers only, batched, within ≤ 5 % of run time (to measure).
- A "Reconversion possible" lane uses a middle similarity band, ≥ 2 transferable skills and no
  conflict on a mandatory requirement (lanes ADR).
