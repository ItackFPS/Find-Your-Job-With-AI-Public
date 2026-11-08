"""M3: work history saved by the Profile page comes back unchanged and drives the computed years."""

from __future__ import annotations

from datetime import date

from src.models.profile import ExperienceEntry, compute_experience_years


def test_filled_history_gives_the_years_used_by_matching():
    entries = [
        ExperienceEntry(id="exp-1", role="Infirmier", start="2020-01", end="2021-12", relevant=True),
        ExperienceEntry(id="exp-2", role="Vendeur", start="2022-01", end="2022-12", relevant=False),
    ]
    assert compute_experience_years(entries, date(2026, 11, 8)) == 2.0


def test_an_empty_end_means_a_current_job():
    entries = [ExperienceEntry(id="exp-1", role="Comptable", start="2026-01", end=None)]
    assert compute_experience_years(entries, date(2026, 12, 15)) == 1.0
