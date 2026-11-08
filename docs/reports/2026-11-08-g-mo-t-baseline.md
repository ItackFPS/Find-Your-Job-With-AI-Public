# G-MO-T baseline (2026-11-08)

G-MO-T is the temporary multi-occupation gate of 2.0 (ADR 0028). It is **not** the ADR 0023
criterion, which returns with L6.

- **Datasets:** `tests/fixtures/evaluation/datasets/g-mo-t-*`, built by
  `scripts/build_g_mo_t_datasets.py`. 72 synthetic offers (12 per occupation) seen from six profiles.
- **Gate:** `tests/test_g_mo_t_gate.py`, part of the suite CI runs.

## Blocking results

| Profile | Clear-target offers kept | Rejected by a domain rule | Other occupations' offers accepted | Baseline moved |
|---|---|---|---|---|
| Nurse | 12/12 | 0 | 0/60 | no |
| Accountant | 12/12 | 0 | 0/60 | no |
| Warehouse operative | 12/12 | 0 | 0/60 | no |
| Sales representative | 12/12 | 0 | 0/60 | no |
| Systems administrator | 12/12 | 0 | 0/60 | no |
| Chef | 11/12 | 0 | 0/60 | no |

The chef profile's one rejection is "Head Chef", through the career-stage rule (`filter:seniority`,
"head" under the excluded `director` level). It is not a domain rule, so it is reported, not gated.

## What the gate found

The first run, before any fix, kept far fewer target offers:

| Profile | First run | Cause |
|---|---|---|
| Warehouse operative | 1/12 | `manual_labor` had no warehouse titles, although the picker offers "Manual Labor & Warehouse" |
| Chef | 0/12 | The picker offers `hospitality`, which had no vocabulary. A domain with none matches only its own name |
| Systems administrator | 6/12 | Titles such as "Administrateur VMware" had no match, and `systems` had no description terms |
| Nurse | 10/12 | "IDE" titles |

The fix is vocabulary only, in `config/filters.yaml`: `manual_labor`, `healthcare` and `systems`
extended, `hospitality` and `logistics` added. `logistics` had the same defect as `hospitality`. No
threshold or engine code changed. The 50-offer benchmark's decisions did not move.

## Reported, not gated

**Top-20 target share:** 100 % for every profile, because every accepted offer is the profile's own.
Deterministic baseline on a small synthetic corpus, not the ADR 0023 criterion. The vocabulary was
fixed on this same corpus, so these figures do not measure performance on unseen real offers. Real
non-IT postings remain unmeasured.

**Sources returning offers with no credentials and no opt-in:**

| Profile | France Travail website | EURES, remote boards |
|---|---|---|
| Systems administrator | yes | yes |
| Nurse, accountant, warehouse operative, sales representative, chef | **no**: the collector reads only IT occupations (`OCCUPATIONS` in `src/collectors/france_travail_web.py`) | yes |

This is why 2.0 is stated as tuned for IT and digital roles.
