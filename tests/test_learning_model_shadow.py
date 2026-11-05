"""The personal model and its shadow journal (ADR 0018, ADR 0019; stage L3.2)."""

from __future__ import annotations

import itertools
import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src.feedback.models import FeedbackEvent
from src.feedback.storage import FeedbackStorage
from src.learning.config import PersonalModelConfig
from src.learning.model import fit, structured_values
from src.learning.shadow import compute_shadow, current_state
from src.learning.signals import ALL_FEATURES, CONTENT, OfferSignal
from src.models.job import Job
from src.storage.sqlite import Storage

CFG = PersonalModelConfig()
NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
E1, E2 = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]
_ids = itertools.count()


def sig(job: str, y: float, features=ALL_FEATURES, weight: float = 1.0) -> OfferSignal:
    return OfferSignal(job, y, frozenset(features), NOW.isoformat(), weight, "test")


# -- model --------------------------------------------------------------------------------------

def test_no_signal_gives_exactly_zero():
    m = fit([], {}, {}, E1, CFG)
    assert m.n == 0 and m.cap == 0.0
    assert m.adjustment(E1, {"source": "x"}) == 0.0
    assert m.adjustment(None, {}) == 0.0


def test_a_positive_raises_similar_offers_only():
    values = {"a": {"source": "x"}}
    m = fit([sig("a", 1.0)], {"a": E1}, values, E2, CFG)
    near = m.adjustment([0.9, 0.1, 0.0], {"source": "x"})
    far = m.adjustment(E2, {"source": "y"})
    assert near > 0 and far == pytest.approx(0.0, abs=1e-12)
    assert abs(near) <= m.cap


def test_a_reason_routed_negative_moves_only_its_feature():
    s = sig("a", -1.2, {"distance"})
    m = fit([s], {"a": E1}, {"a": {"distance": "low", "source": "x"}}, None, CFG)
    assert m.delta is None and set(m.tables) == {"distance"}
    assert m.adjustment(E1, {"distance": "mid", "source": "x"}) == 0.0
    assert m.adjustment(E2, {"distance": "low"}) < 0


def test_content_only_signal_touches_no_structured_feature():
    m = fit([sig("a", 0.25, CONTENT)], {"a": E1}, {"a": {"source": "x"}}, None, CFG)
    assert m.tables == {} and m.delta is not None


def test_cap_bounds_every_adjustment():
    signals = [sig(f"j{i}", 2.0) for i in range(40)]
    m = fit(signals, {s.job_id: E1 for s in signals}, {s.job_id: {"source": "x"} for s in signals}, None, CFG)
    assert m.cap == pytest.approx(CFG.cap(40))
    assert 0 < m.adjustment(E1, {"source": "x"}) <= m.cap


def test_version_changes_with_inputs_and_config():
    a = fit([sig("a", 1.0)], {"a": E1}, {}, None, CFG)
    b = fit([sig("a", 1.5)], {"a": E1}, {}, None, CFG)
    c = fit([sig("a", 1.0)], {"a": E1}, {}, None, PersonalModelConfig(half_life_days=30))
    assert len({a.version, b.version, c.version}) == 3
    assert a.version == fit([sig("a", 1.0)], {"a": E1}, {}, None, CFG).version


def test_structured_values():
    v = structured_values(source="ft", work_mode=None, remote=True, employment_type=" CDI ",
                          salary_min=3000, factors={"location_fit": 0.9, "seniority_match": 0.2})
    assert v == {"source": "ft", "remote": "remote", "contract": "cdi", "salary": "30-45k",
                 "distance": "high", "seniority": "low"}


# -- shadow journal ------------------------------------------------------------------------------

def _job(i: int, vec: list[float]) -> Job:
    return Job(id=f"j{i}", source="src", title=f"Dev {i}", description="d", company="C", location="Paris")


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "jobs.db")
    st = Storage(db_path=path)
    st.init_db()
    jobs = [_job(i, E1 if i < 3 else E2) for i in range(6)]
    for j in jobs:
        st.upsert_job(j)
    st.store_embeddings([(j.id, "h", NOW.isoformat(), E1 if i < 3 else E2) for i, j in enumerate(jobs)], "m")
    st.close()
    fs = FeedbackStorage(db_path=path)
    fs.init_db()
    fs.close()
    production = [(j, 0.9 - 0.0001 * i) for i, j in enumerate(jobs)]     # close scores: small moves show
    return path, production


def _save(path: str, job: str, at: datetime) -> None:
    fs = FeedbackStorage(db_path=path)
    fs.append(FeedbackEvent(event_id=f"s{next(_ids)}", job_id=job, signal_class="list", type="saved",
                            source="ui", occurred_at=at.isoformat(timespec="seconds")))
    fs.close()


def _shadow(path: str, production, run_id: str = "r1"):
    return compute_shadow(path, run_id, production, {}, lambda prod, chal: chal, embedding_model="m",
                          started_at=NOW, config_path=Path("missing.yaml"), now=NOW)


def _rows(path: str, run_id: str = "r1") -> dict[str, tuple[int, int, float]]:
    conn = sqlite3.connect(path)
    try:
        return {j: (p, c, a) for j, p, c, a in conn.execute(
            "SELECT job_id, production_position, challenger_position, personal_adjustment FROM ranking_shadow "
            "WHERE run_id = ?", (run_id,))}
    finally:
        conn.close()


def test_shadow_without_signal_is_production(db):
    path, production = db
    before = list(production)
    adjustments, version = _shadow(path, production)
    assert production == before
    assert set(adjustments.values()) == {0.0}
    assert all(p == c for p, c, _ in _rows(path).values())
    assert current_state(path)["signals"] == 0 and version.startswith("pm1+")


def test_shadow_journals_a_challenger_without_touching_production(db):
    path, production = db
    _save(path, "j5", NOW - timedelta(days=1))          # an E2 offer at the bottom of production
    before = [(j.id, s) for j, s in production]
    adjustments, version = _shadow(path, production)
    assert [(j.id, s) for j, s in production] == before
    rows = _rows(path)
    # j3-j5 share j5's vector and source; j0-j2 share only the source (every offer here has it)
    assert adjustments["j5"] == adjustments["j3"] > adjustments["j0"] > 0
    assert rows["j5"][0] == 6 and rows["j5"][1] == 3        # the challenger would lift the E2 offers
    state = current_state(path)
    assert state["model_version"] == version and state["signals"] == 1 and state["coverage"] == 1.0


def test_signals_after_the_run_started_are_not_used(db):
    path, production = db
    _save(path, "j5", NOW + timedelta(minutes=5))
    adjustments, _ = _shadow(path, production)
    assert set(adjustments.values()) == {0.0}


def test_reset_gives_the_ranking_of_a_copy_without_pre_reset_signals(db, tmp_path):
    from src.privacy.learning import reset

    path, production = db
    clean = str(tmp_path / "clean.db")
    shutil.copy(path, clean)
    _save(path, "j4", NOW - timedelta(days=3))            # before the reset: must be ignored
    _shadow(path, production, run_id="r0")
    with patch("src.privacy.learning.time.sleep"):
        marker, cleared = reset(path)
    assert cleared["ranking_shadow"] == 6 and cleared["personal_model_state"] == 1
    after = datetime.fromisoformat(marker) + timedelta(seconds=1)
    for p in (path, clean):
        _save(p, "j5", after)
    started = after + timedelta(minutes=1)
    got = {p: compute_shadow(p, "r1", production, {}, lambda a, b: b, embedding_model="m", started_at=started,
                             config_path=Path("missing.yaml"), now=started) for p in (path, clean)}
    assert got[path][0] == got[clean][0]
    assert _rows(path) == _rows(clean)


# -- pipeline -----------------------------------------------------------------------------------

def _pipeline(db: Path, cfg: Path):
    from src.evaluation.dataset import load_dataset
    from src.pipeline.orchestrator import run_pipeline
    from tests.test_decision_journal_orchestrator import DS, _FakeEmbeddingService

    with patch("src.embeddings.embedding.EmbeddingService", _FakeEmbeddingService):
        result = run_pipeline(db_path=str(db), config_dir=str(cfg), run_id="run",
                              prefetched_linkedin_jobs=load_dataset(DS).build_jobs())
    assert result.status == "completed", result.error
    return result


def test_pipeline_bit_identity_and_fail_open(tmp_path):
    from tests.test_decision_journal_orchestrator import _config_dir

    cfg = _config_dir(tmp_path, None)
    a = tmp_path / "a"
    a.mkdir()
    with_shadow = _pipeline(a / "jobs.db", cfg)
    b = tmp_path / "b"
    b.mkdir()
    with patch("src.learning.shadow.compute_shadow", side_effect=RuntimeError("boom")):
        without = _pipeline(b / "jobs.db", cfg)
    assert [(s.job_id, s.final_score, s.deterministic_score) for s in with_shadow.scored_jobs] == \
        [(s.job_id, s.final_score, s.deterministic_score) for s in without.scored_jobs]
    assert {s.personal_adjustment for s in with_shadow.scored_jobs} == {0.0}
    assert {s.personal_adjustment for s in without.scored_jobs} == {None}
    conn = sqlite3.connect(a / "jobs.db")
    assert conn.execute("SELECT COUNT(*) FROM ranking_shadow WHERE run_id = 'run'").fetchone()[0] == \
        len(with_shadow.scored_jobs)
    conn.close()


def test_pipeline_feedback_moves_the_challenger_not_production(tmp_path):
    from src.feedback.service import FeedbackService
    from tests.test_decision_journal_orchestrator import _config_dir

    cfg = _config_dir(tmp_path, None)
    db = tmp_path / "jobs.db"
    before = _pipeline(db, cfg)
    liked = before.scored_jobs[-1].job_id
    FeedbackService(str(db), config_dir=str(cfg)).record_explicit(liked, "interested", source="ui")
    after = _pipeline(db, cfg)
    assert [(s.job_id, s.final_score) for s in after.scored_jobs] == \
        [(s.job_id, s.final_score) for s in before.scored_jobs]
    adj = {s.job_id: s.personal_adjustment for s in after.scored_jobs}
    assert adj[liked] > 0
    assert len({s.personal_model_version for s in after.scored_jobs}) == 1
    state = json.loads(sqlite3.connect(db).execute(
        "SELECT summary_json FROM personal_model_state").fetchone()[0])
    assert state["n"] == 1
