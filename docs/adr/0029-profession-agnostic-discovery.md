# 0029 — Discovery, filtering and scoring assume no profession; line-of-work vocabulary lives in presets

Status: accepted (2026-11-08). Amends ADR 0028 on one point: its statement that 2.0 is tuned for
IT and digital roles. Issue: #179.

## Context
ADR 0028 stated that 2.0 is tuned for IT and digital roles and listed the reason: the search
vocabulary, the default rejections and several defaults were IT-only. The owner then decided
that the app serves every line of work and that nothing profession-specific may be hardcoded.

An inspection of the code found IT assumptions in six places: the discovery query expander (static
IT queries sent to every source for every profile); `config/sources.yaml` and three collectors
(IT keyword lists, an `"informatique"` default, an IT occupation-code table); the always-on lists of
other lines of work in `filters.yaml`, which rejected a nurse's own jobs and scored them as
off-domain; the scorer's tech-role title hint; the Stats API's technology filter and IT title buckets;
and the shipped LinkedIn and HelloWork configurations.

## Decision
- **Search terms come from the profile.** Target domains, target occupations (a new optional
  profile field holding ESCO or custom entries, with a French label) and skills produce the queries.
  Contract types add neutral words from `config/discovery.yaml`. An empty profile produces no
  queries, never an IT baseline.
- **Line-of-work vocabulary is a preset.** `config/presets/<name>.yaml` holds queries, per-source
  collector settings, scoring hints and Stats groupings for one line of work, and applies when the
  profile's `target_domains` contains one of its `applies_to_domains`. `it.yaml` carries what used to
  be in code; an IT profile gets the same queries, settings and hints as before (asserted by tests).
  A user's own source settings always win over a preset's.
- **A line of work's own words are never off-domain.** A group of `excluded_title_keywords` /
  `non_it_title_functions` is skipped when it is the profile's own: its key is a selected domain, or
  one of its words appears as whole words in a target domain or occupation. The hard filter and the
  scorer share that rule. Skills are not read (a skill names a tool, not a line of work).
- **Collectors assume nothing.** France Travail, EURES, Bundesagentur and Arbetsförmedlingen search
  nothing when there is nothing to search. France Travail web reads any occupation whose published
  name matches a target domain or occupation, in addition to a preset's code table.
- **`config/filters.yaml` and `config/scoring.yaml` keep their bytes.** Their domain vocabulary only
  applies when its domain is selected, and strategy bundles fingerprint them.

## Consequences
- The README and the app no longer state an IT limit. They say that any line of work can be searched
  and that the remote boards list mostly digital roles.
- The G-MO-T baselines were re-recorded once, deliberately (28 offers in the user's own field moved
  from domain relevance 0.05 and score 0.40 to 1.0 and 0.9536; no accept or reject decision changed).
  The gate keeps its rule: a failure means fixing the vocabulary or narrowing the stated scope.
- **Not verified:** live recall for non-IT occupations on France Travail, EURES and Indeed. The tests
  use mocked collectors. A manual live run per occupation is the missing evidence; the ADR 0023
  criterion (L6) is unchanged and still returns with L6.
- Known limits: a profile that gives only skills (no domain or occupation) lifts no group of other
  lines of work; the LinkedIn and HelloWork scanners resolve their profile-derived settings at app
  start; the HelloWork collector still needs its own domain slugs for a non-IT profile; the
  enrichment prompt examples and the technology lexicons in `src/requirements` remain IT-shaped (inert
  for other titles).
