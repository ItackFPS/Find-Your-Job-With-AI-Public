# Stated education and experience above the profile's maximum hard-reject

The requirements gate (`src/requirements/gate.py`) treats the profile's
`max_education` and experience bounds as ceilings, not preferences. An education
level above `max_education`, with attributable evidence, hard-rejects the job only
when the posting makes it a **requirement**:

| Wording around the level | Importance | Above `max_education` |
|---|---|---|
| mandatory cue: "exigé", "requis", "minimum", "obligatoire", "required" | MANDATORY | hard reject |
| stated as the bar, softened only by equivalence: "Bac+4 ou équivalent" | PREFERRED, `is_desirable` false | hard reject |
| desirability cue: "souhaité", "apprécié", "idéalement", "un plus", "preferred" | PREFERRED, `is_desirable` true | 20% soft penalty |
| explicitly non-eliminatory, in the same sentence: "non éliminatoire", "pas obligatoire", "facultatif", "not required", "optional" | OPTIONAL | nothing |
| LLM-only reading (CORE), or a PREFERRED reading with no equivalence evidence | CORE / PREFERRED | 20% soft penalty |
| no qualifier, contradicted, or uncorroborated | UNKNOWN | nothing |

Mandatory years of experience above the maximum hard-reject; preferred excess is a 20%
soft penalty. A degree offered as an alternative to experience
(`EducationRequirement.is_experience_alternative`) is not a ceiling violation.

We chose ceilings because the candidate cannot apply credibly to roles whose stated
bar is above their level, and scoring alone left such jobs ranked highly. The UNKNOWN
exemption and the "in-progress diploma" handling (e.g. "vous préparez un Bac+N",
commit 8859322) keep the gate from rejecting apprenticeships and internships aimed at
students still studying.

## Amendment 2026-10: a wish is not a ceiling

From commit fe67427 until this amendment, any PREFERRED level above the maximum
hard-rejected, desirability wording included. That rejected the synthetic benchmark job
bm46 ("Bac+2 en informatique. Un niveau Bac+5 est apprécié mais absolument pas un
critère éliminatoire") for a Bac+2 profile, and "Bac+3 souhaité ou Bac+5 bienvenu"
for a Bac+3 profile: postings that say in words that the higher level is not required.
That contradicts the project's first safety rule (no unsafe rejection; a negative
decision needs explicit evidence), so the ceiling now applies to requirements only:

- A desirability cue makes the level a preference (soft penalty), never a ceiling.
- An explicit non-eliminatory cue in the mention's sentence makes it OPTIONAL.
  Negated mandatory words ("not required", "non obligatoire") are no longer read as
  mandatory cues; before, "Diplôme Bac+5 non obligatoire" hard-rejected.
- Competing cues around one mention are resolved by distance, ties going to the
  reading that cannot reject: "Bac+5 requis, anglais souhaité" and "Bac+5 requis,
  permis B non obligatoire" stay MANDATORY.
- When a posting states several levels, the enforceable one is checked first
  ("Bac+3 ou équivalent. Bac+5 apprécié" checks Bac+3): a wish never hides a
  requirement.
- "Bac+4 ou équivalent" still rejects: it states the bar and accepts equivalent
  diplomas, it does not lower it (the owner's documented case).

Rejection reasons carry `rule_version = "hard-filter.v3"` from this change on, so
journaled decisions made under the old rule stay distinguishable.

Two consistency rules follow from the table:

- A level above the maximum with **no qualifier** ("Profil recherché : Un Master avec
  une spécialisation…") is not known to be required, nor known not to be. The gate
  records the education constraint as UNKNOWN (the job passes with uncertainty, visible
  as `uncertain` in the decision journal), never as satisfied; still no reject and no
  penalty. Explicit wordings (desirable, non-eliminatory) stay satisfied.
- A rejection message names why the level counts as required: "(mandatory)" or
  "(stated level, equivalence accepted)" — never the raw "preferred" importance, which
  contradicted the decision it explained.

Enforced by `tests/test_education_requirement_strength.py`, the hard-filter safety
corpus (`tests/test_hard_filter_benchmark.py`) and the end-to-end safety gate on the
synthetic benchmark (`tests/test_rejection_safety_gate.py`).

## Consequences

- Extraction precision matters more than recall: a misclassified importance becomes a
  false rejection (known case: issue #19). The distance rule bounds, but does not
  remove, the risk of a cue being attributed to the wrong requirement.
- Jobs that only *wish* for a higher level now reach scoring with a 20% penalty instead
  of being dropped; they rank lower but stay visible.
- Each constraint can be switched off per profile
  (`matching.education_hard_constraint`, `principal_skill_gate`,
  `mandatory_central_skill_gate`).

## Amendment (2026-10, profile v2 F5.2): career stage from several signals

With profile v2 target levels (`targets.levels`), a seniority word in the title is weak evidence and no
longer a hard-filter rejection. The requirements gate infers the offer's level from weighted signals
(`src/requirements/career_level.py`): title word (1), stated years (3 mandatory, 2 otherwise), people
management in the description (2), internship or apprenticeship contract (3). It compares them with the
highest target, plus one level with `stretch` (scoring prefers the same levels). It rejects only when
people management conflicts, the conflicting weight reaches 3, and no non-title signal fits. Any other conflict, including a title
alone or a title contradicted by the stated years, is `UNKNOWN`: the offer passes with uncertainty and
a "Career level" flag (review), never a rejection. Years above `max_years` stay with the experience
ceiling above. A v2 profile with no target level shows every level. A v1 profile keeps the keyword
rejection.

## Amendment (2026-10, profile v2 F5.3): education by French/EQF level

A v2 profile declares qualifications at the French national levels 3–8, which share their numbers
with EQF levels 3–8 (décret n° 2019-14). Examples: CAP 3, Bac 4, BTS/DUT/DEUG 5,
Licence/Licence pro/BUT and the maîtrise 6, Master/ingénieur 7, Doctorat 8. A foreign degree takes
the level the user picks. Offers keep their `bac+N` reading and map it to a level (Bac+1 → 4,
Bac+4 → 6, Bac+6/+7 → 7). The gate receives the highest Bac+N the candidate's highest held level
covers (`level_to_max_bac_plus`), so every rule above (mandatory vs preferred vs "or equivalent
experience", enrolment, soft penalty) is unchanged and compares levels. One consequence is intended:
"Bac+4 minimum" no longer rejects a Licence or BUT holder, because both are level 6. Messages name
levels ("level 7 (Bac+5) required; you declared level 6 (BUT)") and never call two qualifications
equivalent. In-progress qualifications are not held. A v2 profile without a held level, and any v1
profile, keeps `max_education`.
