# Stated education and experience above the profile's maximum hard-reject

The requirements gate (`src/requirements/gate.py`) treats the profile's
`max_education` and experience bounds as ceilings, not preferences. An education
requirement classified MANDATORY **or PREFERRED** above `max_education`, with
attributable evidence, hard-rejects the job (commit fe67427); CORE above the maximum is
a 20% soft penalty; UNKNOWN / ambiguous / contradicted evidence never hard-rejects.
Mandatory years of experience above the maximum hard-reject; preferred excess is a 20%
soft penalty. A degree offered as an alternative to experience
(`EducationRequirement.is_experience_alternative`) is not a ceiling violation.

We chose ceilings because the candidate cannot apply credibly to roles whose stated
bar is above their level, and scoring alone left such jobs ranked highly. The UNKNOWN
exemption and the "in-progress diploma" handling (e.g. "vous préparez un Bac+N",
commit 8859322) keep the gate from rejecting apprenticeships and internships aimed at
students still studying.

## Consequences

- Extraction precision matters more than recall: a misclassified importance becomes a
  false rejection (known case: issue #19).
- Each constraint can be switched off per profile
  (`matching.education_hard_constraint`, `principal_skill_gate`,
  `mandatory_central_skill_gate`).
