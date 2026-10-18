"""Dashboard API route.

GET /api/dashboard — aggregated stats for the latest pipeline run.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from src.api.routes.pipeline import _row_to_run_out
from src.api.schemas import DashboardOut
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardOut)
def get_dashboard(request: Request) -> DashboardOut:
    runner = request.app.state.runner
    db_path = request.app.state.db_path

    rs = RunStorage(db_path=db_path)
    rs.init_db()
    s = Storage(db_path=db_path)
    s.init_db()

    try:
        latest_run_row = rs.get_latest_completed_run()
        latest_run_out = _row_to_run_out(latest_run_row) if latest_run_row else None
        total_jobs = s.count_jobs()
        is_running = runner.is_running()
        current_run_id = runner.current_run_id()
    finally:
        rs.close()
        s.close()

    return DashboardOut(
        latest_run=latest_run_out,
        total_jobs_in_db=total_jobs,
        is_running=is_running,
        current_run_id=current_run_id,
    )
