"""Privacy actions API (ADR 0015).

GET  /api/privacy/learning        — {"paused": bool, "reset_at": str | null}
PUT  /api/privacy/learning/pause  — {"paused": true}: pause or resume learning
POST /api/privacy/learning/reset  — {"confirm": true}: set the reset marker, clear derived state
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.privacy import learning

router = APIRouter(prefix="/api/privacy", tags=["privacy"])


class LearningOut(BaseModel):
    paused: bool
    reset_at: str | None


class PauseIn(BaseModel):
    paused: bool


class ResetIn(BaseModel):
    confirm: bool = False


class ResetOut(LearningOut):
    cleared: dict[str, int]


@router.get("/learning", response_model=LearningOut)
def get_learning(request: Request) -> LearningOut:
    st = learning.state(request.app.state.db_path)
    return LearningOut(paused=st.paused, reset_at=st.reset_at)


@router.put("/learning/pause", response_model=LearningOut)
def pause_learning(body: PauseIn, request: Request) -> LearningOut:
    st = learning.set_paused(request.app.state.db_path, body.paused)
    return LearningOut(paused=st.paused, reset_at=st.reset_at)


@router.post("/learning/reset", response_model=ResetOut)
def reset_learning(body: ResetIn, request: Request) -> ResetOut:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="confirm_required")
    db = request.app.state.db_path
    _, cleared = learning.reset(db)
    st = learning.state(db)
    return ResetOut(paused=st.paused, reset_at=st.reset_at, cleared=cleared)
