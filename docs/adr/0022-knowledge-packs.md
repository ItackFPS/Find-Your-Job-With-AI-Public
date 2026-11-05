# 0022 — Shared intelligence travels only in versioned, read-only knowledge packs shipped with releases

Status: accepted (2026-11-05). Not implemented yet (stage L6). Source:
`docs/plans/2026-11-05-learning-architecture-decisions.md` §1.1–§1.5 and §4.1 (AD-1), with the v1
proposal's AD-1.

## Context
The application must work for every occupation and profile, France first (objective O-4), with no
server and nothing personal leaving the install (INV-2). Engine knowledge — occupation taxonomies,
vocabularies, default weights, classifier prototypes, calibration tables — still has to improve over
time and reach every install.

## Decision
- **No channel between installs** in 2.x: no telemetry, no server, no federated training. Opt-in
  aggregate telemetry is out of scope and would need its own ADR with a privacy design.
- Everything shared ships as a **knowledge pack**: a read-only SQLite file `knowledge/occupations.db`
  plus a manifest (`pack_version`, per-table SHA-256, licences, source URLs, download dates, scheme
  versions). It is never part of `jobs.db` and holds no user data.
- Bundled with the installer when ≤ 30 MB compressed, otherwise downloaded at first run with the
  R-Q4 pattern (size shown, SHA-256 checked, resumable, the app works without it).
- `pack_version` enters the strategy fingerprint (`src/decisions/strategy.py`), so engine results
  are attributable to a pack.
- **Pack v1 = ROME 4.0 only** (arborescence, fiches, appellations, savoir-faire, savoirs, precomputed
  label embeddings; Licence Ouverte / Etalab). **ESCO is not in the first pack** (owner decision); it
  can be reconsidered only after its legal notice and package size are verified. No ROME↔ESCO
  crosswalk is shipped unless developer-built and measured (`kind = "derived"`, never "official": no
  official one was found). No mobility links unless they prove machine-readable.
- Pack changes are engine changes: validated by the developer and the lab (ADR 0011), shipped in
  releases.

## Consequences
- **Implementation preconditions** (not ADR blockers): read the ROME licence PDF (if it forbids
  redistributing derived tables, ship only the open-data arborescence and build the rest at first
  run); confirm the bulk export holds the API's fields; build the pack and measure its size
  (fallback: float16 vectors, ≈ 11 MB, or the download path).
- A missing or corrupt pack is detected by hash; the app falls back to the current keyword
  vocabularies with a visible notice. A bad pack is rolled back by shipping the previous one.
- The 30 MB budget and ≤ 1 s load time are initial values, not approved parameters.
- `src/esco/` stays as the pattern for a separate read-only DB.
