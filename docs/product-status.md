# Product status

The state of the product as observed by running it, not as described by merged pull requests.
Last verified 2026-11-09, on a copy of a real database, with the open branches listed below merged
locally. Update a line when its proof changes; a line without proof is not done.

Status values: **done** (works in the application and verified), **partial**, **missing**, **blocked**
(needs something outside the code), **obsolete**, **to confirm**.

Origin: **asked** is something the owner requested; **proposed** is a recommendation from the review.

## What was measured on 2026-11-09

Everything below ran on a copy of a real database with real collection, unless stated. The open branches
were merged locally only: none of this is in `main` or in an installed application yet.

**Matching, independent human labels** (real-v2 tuning split, 69 offers, 7 accepted, diagnostic view):
recall 0.25 to 0.75, unsafe rejection rate 0.75 to 0.25, recall@20 1/7 to 3/7. AUC among scored offers went
from 0.615 to 0.513 on 3 accepted and 13 rejected: no gain in ordering. With the profile frozen before the
domain change, precision went from 0.143 to 0.118 and recall stayed at 0.5.

**Score distribution.** Reported run: maximum 0.78, 14 offers at 0.7 or more. After: maximum 0.885, 79 at
0.7 or more. The sources differ between the two, so this shows the cap is gone, not that quality rose.

**Pipeline.** 7,664 offers collected, 6,941 after deduplication, 856 accepted, 12 min 51 s from start to
finish. Two defects appeared only in real runs and are fixed: a date in a raw offer failed the whole run at
storage (#192); counting open audits held the end of a run for more than 18 minutes (#193).

| Source | Collected | Accepted | Score 0.6 or more | Note |
|---|---|---|---|---|
| Employers' boards | 3,873 | 120 | 16 | 41 to 62 s; one board 404 |
| EURES | 2,495 | 117 | 67 | |
| France Travail public pages | 216 | 189 | 143 | extracts only; 60 pages at most; 219 occupations published |
| HelloWork | 817 | 209 | 43 | from a previous scan, not collected in this run |
| LinkedIn | 263 | 221 | 26 | from a previous scan, not collected in this run |

**Behaviour, on the same copy.** 8 explicit signals through the API (interested in "support" offers, not
interested in "embarqué" offers):

| Offers not signalled | Mean position before | After |
|---|---|---|
| "support" | 92.0 | 83.0 |
| "embarqué" | 95.7 | 116.4 |

Stored scores changed: 0. Forgetting `embarque` reduced the shift from 24.7 to 16.7 places; related terms stay
learned until forgotten too. Paused: production order, nothing journaled. This proves the mechanism and its
control. It does not prove a better order: AUC 0.607 to 0.631 and precision@5 unchanged on 67 human labels.

**Non-IT check.** A nurse profile (typed domains, skills) on the same copy: 103 offers scored, all of the top 20
and 48 of the top 50 have a nursing title. That is a title pattern, not a human judgement; recall is unknown, and
the employers' boards were discovered from an IT database (1 offer accepted).

## Matching

| Item | Origin | Status | Proof | User impact | Depends on | Action | Priority |
|---|---|---|---|---|---|---|---|
| Good offers capped near 70 | asked | partial (#189 open) | Typed domains matched no vocabulary key: domain relevance 0.35/0.45 on all 1,927 offers of the reported run, final max 0.78. Measured after the fix on human labels, below. | Good offers were ranked as mediocre | merge #189 | Merge | P0 |
| Clear titles rejected by exact keywords | asked | partial (#189 open) | 1,165 offers with clear titles rejected in that run; `src/pipeline/domain_vocabulary.py` matches by stems | Offers disappeared with no trace in the list | merge #189 | Merge | P0 |
| Skills found only past 2,000 characters | asked | partial (#189 open) | 776 of 1,923 offers named profile skills only after the old window; window is now 12,000 | Skills score too low on long descriptions | merge #189 | Merge | P0 |
| "Bac à Bac+2 (BTS…)" read as a BTS minimum | asked | partial (#189 open) | `src/requirements/education.py`, `tests/test_education_range_examples.py` | Eligible offers rejected | merge #189 | Merge | P0 |
| Extract read as a full description | asked | partial (#189 open) | `tests/test_partial_description_scoring.py`: no skill in an extract is unknown (0.5), not absent | France Travail public pages were penalised for their length | merge #189 | Merge | P0 |
| In-progress BTS against an offer asking Bac+2 | proposed | to confirm | About 180 rejections in the last real run; current behaviour follows an earlier owner confirmation | Possible false rejections | owner | Owner decides | P1 |
| Occupation evidence from one shared word | proposed | partial (#184, #189 open) | Embedded and industrial offers ranked 5 to 7 in a real run; `named_domains` now requires the whole label | False positives near the top | merge #184, #189 | Merge | P0 |
| Ambiguous vocabulary terms | proposed | partial | "ingénieur systèmes" still matches embedded titles at 1.0 | Some false positives remain | none | Review the family lists with labels | P1 |
| Extracts scored 0.5 on skills | proposed | to confirm | France Travail extracts: 143 of 189 at 0.6 or more; no human label covers extracts | These pages may rank too well | labels on extracts | Label extracts, then adjust | P1 |
| Title equivalents and synonyms | asked | partial | Stems and typed labels only. `scripts/setup_esco.py` seeds an IT subset; installed `esco.db` is 45 KB | Equivalent job titles are missed unless typed | ESCO import for all occupations | Import the full ESCO set | P1 |
| Transferable skills, indirect experience, missions versus title | asked | missing | No such stage in the pipeline; only 6% semantic similarity touches it | A career change is scored as a mismatch | design, labels | Design with labelled evaluation | P1 |
| Contradictions inside an offer | asked | partial | Requirements gate reads explicit requirements only | Rare wrong decisions | none | Measure before building | P3 |

## Behaviour learning

| Item | Origin | Status | Proof | User impact | Depends on | Action | Priority |
|---|---|---|---|---|---|---|---|
| Observe views, opens, saves, hides, explicit feedback | asked | done | `feedback_events`, `exposure_log` hold real events | Basis of the loop | none | Keep | none |
| Dwell time | asked | partial | Only exposure `visible_ms`; time spent on an open offer is not recorded | A weak signal is unavailable | none | Add as a weak signal if labels show value | P2 |
| Actions change what is shown | asked | partial (#191 open) | Before #191: `ranking_shadow` absent, `personal_adjustment` NULL everywhere, journal bridge off. Scenario below. | Nothing a person did reached the list | merge #191 | Merge | P0 |
| Explain and correct a learned preference | asked | partial (#191 open) | Settings panel, per-offer note, forget and restore endpoints | Control over the model | merge #191 | Merge | P0 |
| One view or one rejection is never absolute | asked | partial (#191 open) | Opens never count towards the 3 strong signals; effect capped at `0.15*n/(n+20)`; gates re-applied | Bounded effect | merge #191 | Merge | P0 |
| Proof the personal order is better | asked | blocked | 5-fold on 67 human labels: AUC 0.607 to 0.631, precision@5 unchanged at 0.2; 7 accepted offers, within noise | Effect visible, benefit not established | more labels | Repeat with `src.evaluation.learning_lift` | P1 |
| Implicit jobs, missions, environments | asked | partial | Model learns words of titles and skills only | Preferences are coarse | #191 | Extend after labels | P2 |
| Readable learned preferences | proposed | partial | Panel shows `niveau : mid`, stems and company names | Harder to understand and correct | #191 | Label values, drop company names | P2 |
| Learning on the owner's installation | proposed | blocked | Paused by its owner since 2026-11-08 | The personal order stays off there | owner | Owner resumes | P1 |

## Any line of work

| Item | Origin | Status | Proof | User impact | Depends on | Action | Priority |
|---|---|---|---|---|---|---|---|
| Profile to ranking without IT assumptions | asked | partial | ADR 0029, #189 vocabulary; nurse profile above, no human labels | Non-IT people depend on what they type | merge #184, #189 | Merge | P0 |
| Line-of-work presets | asked | partial | `config/presets/` holds `it.yaml` only | Other professions get no preset | none | Add presets from measured queries | P2 |
| Requirements lexicons | asked | partial | `src/requirements/principal_skill.py` lists technologies only | Principal-skill gate has no effect elsewhere | labels on other professions | Generalise with a labelled set | P1 |
| AI prompts | asked | partial | `src/ai/prompt.py` examples are programming ones | Optional stage biased when turned on | none | Replace examples | P2 |
| Non-IT evaluation set | proposed | missing | Human labels cover one IT profile | No non-IT quality measure exists | people to label | Build a second labelled set | P1 |

## Sources

See ADR 0031 for the evidence on terms and access.

| Item | Origin | Status | Proof | User impact | Depends on | Action | Priority |
|---|---|---|---|---|---|---|---|
| France Travail without a personal key | asked | partial (#184 open) | Without #184 the public-page collector selected 0 pages for typed domains; with it, 216 offers | Main French source for someone with no key | merge #184 | Merge | P0 |
| France Travail public pages cover few occupations | proposed | partial | Sitemap publishes 219 occupations; nothing for sales, logistics or human resources under those words; 216 offers against 8,589 through the API in the earlier run | Thin French coverage for someone with no access | none | Request application access, widen other sources | P1 |
| France Travail application access | proposed | blocked | API access restricted | Would give full descriptions | owner request | Owner contacts France Travail | P2 |
| LinkedIn | asked | to confirm | §8.2 forbids scraping; no search API; 0 offers, HTTP 429 | Opt-in, gives almost nothing | owner | Recommend removal | P1 |
| HelloWork | asked | to confirm | CGU art. 1, 9.2, 9.3 forbid extraction; 46 minutes for 35 accepted offers | Opt-in, slow | owner | Recommend removal | P1 |
| Indeed | proposed | to confirm | Reads the mobile application's API; terms not verified | Second-best source by good offers | owner | Check terms, then decide | P1 |
| Employers' boards | proposed | partial (#192 open) | 3,873 offers in about a minute, 120 accepted | New coverage with no key; follows the boards already seen | merge #192 | Merge | P0 |
| RemoteOK, Jobicy, We Work Remotely | asked | partial (#190 open) | Removed from code, settings, UI, tests, docs | Scope reduced | merge #190 | Merge | P0 |

## Release

| Item | Origin | Status | Proof | User impact | Depends on | Action | Priority |
|---|---|---|---|---|---|---|---|
| A run can fail or stall on real data | proposed | partial (#192, #193 open) | Date in raw offer failed a run; audit count held one for 18 minutes or more | A daily run could end with nothing new | merge #192, #193 | Merge | P0 |
| Installed application is out of date | proposed | blocked | 2.0.0rc1 from 6008093, without #180 to #186 or the open branches | The owner sees none of this | merges, rebuild | Rebuild after merges | P0 |
| 2.0 qualification | asked | partial | 22 met, 10 partly, 4 not met of 36 lines | Release held | owner checks | Owner-only checks | P1 |
| Held-out labels for the next round | asked | blocked | real-v3: 0 of 500 labelled | No clean final measure | owner labels | Owner labels | P1 |
| Occupation layers L6 to L8 | asked | blocked | Wait on labelled data | Deferred | labels | Keep deferred | P3 |
| Saved offers about to expire | asked | missing | `TODO(F6.6)` for `saved_expiring` | No reminder | none | Implement | P3 |
| Open pull requests #176, #177, #178, #187, #188 | asked | to confirm | Open at 2026-11-09 | Mixed | owner | Review and merge | P2 |
