# Location is matched on distance from home and a typed remote policy

When the profile has `geography.home`, location is no longer an ordered list of
départements (ADR 0005) but a home place, a maximum distance, a `strict_distance`
switch and a `remote_policy` (Remote France / EU / worldwide, hybrid). One assessment per
job (`src/pipeline/location_distance.py`) feeds the hard filter, the location factor, the
decision journal and explanations: the offer's place comes from `resolve_place` (the job's
canonical département column wins over a text resolution that disagrees with it), its
remote eligibility from `classify_remote` (both `src/geo/`), and the road distance is
estimated as great-circle × 1.25.

- Hard filter, ADR 0004's rule applied (uncertainty never rejects):
  - `distance`: on-site work beyond the radius, only with `strict_distance` on, only for an
    exactly resolved commune (a homonym picked as "likely" never rejects); each end known
    only at département precision, offer or home, adds a 60 km margin.
  - `remote_policy`: only an offer that is actually remote, has no French place and no
    French remote wording, and whose explicit residency/authorisation wording names areas
    that are ALL outside France and the EU. Time-zone wording ("overlap with EST hours")
    and "Remote - US" style labels never reject.
- Location factor: `1 − 0.35·(d/R)²` inside the radius (1.0 at home, 0.65 at the edge),
  then linear down to 0.30 at 2R; 0.50 when only "France" is known, 0.40 when nothing is.
  An accepted remote kind scores at least 0.90; remote with an unknown area at least 0.55
  ("worth a look"); an unaccepted kind or an inferred restriction gets the place score only.
  The v1 top-location bonus becomes `top_location_bonus × (1 − d/R)` inside the radius.
  An offer in an `excluded_departments` entry (a v1 exclusion) earns no location credit
  there (capped at 0.50, no bonus) and is not read by France Travail; it is never rejected.

We chose distance because users state "where I live and how far I will go" reliably, and
départements are a poor proxy near their borders; a typed remote eligibility replaces "a
remote job without countries is Remote France", which ranked a Hong Kong office job first.

## Consequences

- v1 personal profiles are converted at pipeline start or on the first save
  (`src/geo/profile_migration.py`, backup `profile.v1-backup.local.yaml`, writes serialised
  with `PUT /api/profile`); `GET /api/profile` shows the converted view without writing.
  The v1 fields are kept until the profile v2 migration. A profile without any département
  (remote-only), and the tracked default profile (no home until the user sets one), keep
  the ADR 0005 behaviour.
- `preferred_countries` still filters by country (unchanged); it is a separate preference.
- The France Travail web collector reads the départements within the radius plus home's.
- No new scoring gate, so `cap_personalization_at_gates` is unchanged.
