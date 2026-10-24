# Location is matched on distance from home and a typed remote policy

When the profile has `geography.home`, location is no longer an ordered list of
départements (ADR 0005) but a home place, a maximum distance, a `strict_distance`
switch and a `remote_policy` (Remote France / EU / worldwide, hybrid). One assessment per
job (`src/pipeline/location_distance.py`) feeds the hard filter, the location factor, the
decision journal and explanations: the offer's place comes from `resolve_place`, its remote
eligibility from `classify_remote` (both `src/geo/`), and the road distance is estimated as
great-circle × 1.25.

- Hard filter: the only distance exclusion is on-site work beyond the radius with
  `strict_distance` on (a commune, or a département whose nearest plausible point is still
  beyond it). A remote offer is excluded only when it states explicitly that it is
  restricted to another area and is not itself placed in France. Unknown places and
  unknown remote areas never reject (ADR 0004's rule).
- Location factor: `1 − 0.35·(d/R)²` inside the radius (1.0 at home, 0.65 at the edge),
  then linear down to 0.30 at 2R; 0.50 when only "France" is known, 0.40 when nothing is.
  An accepted remote kind scores at least 0.90; remote with an unknown area at least 0.55
  ("worth a look"); an unaccepted kind or an inferred restriction gets the place score only.
  The v1 top-location bonus becomes `top_location_bonus × (1 − d/R)` inside the radius.

We chose distance because users state "where I live and how far I will go" reliably, and
départements are a poor proxy near their borders; a typed remote eligibility replaces "a
remote job without countries is Remote France", which ranked a Hong Kong office job first.

## Consequences

- v1 profiles are converted on first use (`src/geo/profile_migration.py`, backup
  `profile.v1-backup.local.yaml`); the v1 fields are kept until the profile v2 migration.
  A profile without any département (remote-only) keeps the ADR 0005 behaviour.
- `preferred_countries` still filters by country (unchanged); it is a separate preference.
- The France Travail web collector reads the départements within the radius plus home's.
- No new scoring gate, so `cap_personalization_at_gates` is unchanged.
