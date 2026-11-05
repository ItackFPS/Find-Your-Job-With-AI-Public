# 0024 — Market figures describe the collected sample, carry their coverage, and compare salaries only inside one comparability class

Status: accepted (2026-11-05). Not implemented yet (stage L7; the `matches_salary` correction is a
prerequisite that lands first). Source: `docs/plans/2026-11-05-learning-architecture-decisions.md`
§4.8 (AD-8), with the v1 proposal's AD-8.

## Context
Aggregates over stored offers (demand per occupation, skills, salaries) feed the Skill Graph,
Insights, profile suggestions and, with low weight, occupation similarity (ADR 0023). The offers are
those this installation collected: shaped by the user's sources, queries, geography and history.
They are a sample, never a census. Salary data is also thin and heterogeneous: in a 1,161-offer
France Travail sample an amount is stated on 477 offers (41.1 %), with annual, monthly and hourly
periods, gross or net almost never stated, and contract types mixed. `Job` stores only
`salary_min`, `salary_max` and `salary_currency`, and `matches_salary` compares values of unknown
period directly (a monthly offer against an annual profile minimum).

## Decision
- **Aggregates, kept beyond offer deletion.** A scheduled job maintains market tables (occupation ×
  period × area; skill × occupation × period; occupation relations) from all stored offers, accepted
  or not, computed before cleanup removes expired offers. They hold no personal data (plane M,
  ADR 0014).
- **Coverage descriptor on every insight**: `n_offers`, sources and shares, geography, period and run
  count, `collection_scope` with an `in_scope` flag, `scope_changes`, and `field_coverage`.
- **Three levels of claim**, each with its own wording and minimum conditions (initial values, not
  approved parameters):
  1. *Observed* ("Dans les N offres collectées par l'application…"): n ≥ 5, in scope.
  2. *Observed trend*: as Observed, no scope change across the compared periods, n ≥ 20 per period.
  3. *Market indication* ("Indication sur le marché (échantillon partiel)…"): n ≥ 30 in the cell,
     in scope, field coverage ≥ 50 %, regular collection over the period.
  Figures below the Observed floor are suppressed. No insight claims a market share until an
  external calibration source is verified.
- **Uses.** Salary advice only at Market-indication level, always "parmi les offres affichant un
  salaire", never "le salaire du marché". Occupation suggestions are ordered by similarity and
  transferable skills (ADR 0023); demand is context only and never ranks an out-of-scope occupation
  above an in-scope one. Out-of-scope occupations or areas get no demand statement. Engine use
  (flow M→E: co-occurrence, centroids) needs minimum support (initial value 20 offers), in-scope
  pairs only, with a lab-tuned weight.
- **Salary comparability.** A derived, rebuildable `salary_obs` record per offer, never overwriting
  raw fields: `low`, `high`, `kind` (fixed, range, from, up_to, not_stated), `currency`, `period`,
  `basis` (gross, net, unknown) plus `basis_presumed`, `months_per_year`, `working_time`,
  `weekly_hours`, `contract_stratum`, `parse_status`, `exclusion_reason`, `source_field`,
  `parser_version`. Unparsed strings are counted, never guessed.
  - Conversions allowed: year as is; month × `months_per_year` (default 12, recorded); part-time
    monthly or annual × 35 / weekly hours only when hours are stated. Hourly amounts stay in their
    own stratum unless full-time hours are stated. Day rates and weekly amounts are never converted.
  - Gross and net are never converted into each other. `basis = unknown` compares only with
    `unknown` from the same source, labelled "base (brut/net) non précisée".
  - Contract strata are never pooled; apprenticeship and internship pay is excluded. Range low and
    high ends are summarised separately; a midpoint only when `high / low ≤ 1.5`. Non-EUR amounts
    are excluded from French figures (no exchange rate). Implausible annual equivalents outside
    [P_min, P_max] are excluded and logged. Benefits and variable pay are flags, never added.
  - A statistic is computed only inside one **comparability class** (currency, basis, period,
    contract stratum, working-time treatment) within an occupation × area × period cell; the
    descriptor shows the class, n inside it and n excluded by reason. No statistic mixes classes.
- **Profile salary contract** (owner decision §8 item 7): the expectation carries amount, currency,
  period, basis, working-time treatment (with weekly hours for part-time) and months per year for a
  monthly period. No profile-to-market comparison unless the expectation is in the same class.
- **Prerequisite, in this order**: `matches_salary` compares `salary_obs` within one class and
  returns UNKNOWN for incomparable values (ADR 0017, salary check); only then the profile salary
  contract change, any salary-based filtering, or any new collector salary mapping.

## Consequences
- Schema: `salary_obs` (derived, keyed by `job_id`, added to cleanup `DERIVED_TABLES`);
  `market_build` gains `coverage_json` and `scope_hash`.
- Assumptions to verify before relying on them: France Travail amounts are gross by convention
  (until verified, `basis = unknown`, `basis_presumed = gross`); the FT `libelle` pattern holds for
  other occupations; the 35 h legal week. Other sources' salary fields are not yet measured.
- External calibration (France Travail match counts, the data.gouv offers dataset) is a possible
  later reference, not inspected; no design depends on it.
- Tests: conversion and exclusion rules per class, no mixed-class statistic, suppression below the
  floor, wording per claim level, and `matches_salary` returning UNKNOWN across classes.
