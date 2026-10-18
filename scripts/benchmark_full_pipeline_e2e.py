"""Full production-like end-to-end pipeline benchmark, AI (Phase 2) enabled.

Measurement-only harness. Drives the REAL production code path:
    PipelineRunner (src/api/runner.py) -> run_pipeline (src/pipeline/orchestrator.py)
exactly as the running FastAPI backend does for POST /api/pipeline/run
{"use_ai": true}, with the same NotificationEngine wiring app.py uses.

2026-09-05 update: this now calls runner.start_run(use_ai=True) directly —
the Issue 1 fix gave PipelineRunner a real use_ai parameter, so the
class-level monkeypatch of run_pipeline this script used before (working
around the API's inability to request an AI-enabled run) is no longer
needed and has been removed.

Instrumentation added here, in this script only (never in src/):
  - per-call LLM latency capture, via a class-level monkeypatch of
    LocalLLMProvider._call_llm (same pattern as scripts/benchmark_eligibility_gate.py)
  - a background CPU/RAM/GPU/VRAM sampling thread (psutil + nvidia-smi)

No pipeline/orchestrator/AI source file is modified. Do not run this while
another pipeline run is in progress (checked before starting).

Note: write REPORT_JSON's parent to a path OUTSIDE this repo when invoking
via a redirected shell log too — writing a new/changed file inside the repo
while uvicorn --reload is watching it restarts the API process mid-run (see
Issue 2 in the engineering report); the recover_stale_runs fix means that no
longer falsely fails an external run, but avoiding the restart entirely is
still cleaner for a benchmark.

Usage:
    python scripts/benchmark_full_pipeline_e2e.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).parent.parent))

import psutil  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

ROOT = Path(__file__).parent.parent
DB_PATH = str(ROOT / "data" / "jobs.db")
CONFIG_DIR = str(ROOT / "config")
import tempfile

# Written OUTSIDE the repo — see the module docstring on why (uvicorn
# --reload watches the repo tree; a file write here mid-run restarts it).
REPORT_JSON = Path(tempfile.gettempdir()) / "full_pipeline_benchmark_report_after.json"

# src/main.py and src/api/app.py both load .env before importing the
# orchestrator/runner; this script bypasses both entry points, so it must
# do the same or FRANCE_TRAVAIL_CLIENT_ID/SECRET etc. are invisible to it.
load_dotenv(ROOT / ".env")

# ---------------------------------------------------------------------------
# Instrumentation: per-call LLM latency (class-level monkeypatch, script-only)
# ---------------------------------------------------------------------------
_llm_lock = threading.Lock()
_llm_calls: list[dict] = []


def _install_llm_timing_patch():
    from src.ai import interfaces as ai_interfaces

    original = ai_interfaces.LocalLLMProvider._call_llm

    def patched(self, prompt, max_tokens=None):
        t0 = time.perf_counter()
        wall_start = time.time()
        ok = True
        err = None
        resp_len = None
        try:
            resp = original(self, prompt, max_tokens=max_tokens)
            resp_len = len(resp)
            return resp
        except Exception as exc:  # noqa: BLE001 - re-raised unchanged below
            ok = False
            err = type(exc).__name__
            raise
        finally:
            dt = time.perf_counter() - t0
            with _llm_lock:
                _llm_calls.append(
                    {
                        "wall_start": wall_start,
                        "wall_end": wall_start + dt,
                        "duration": dt,
                        "ok": ok,
                        "error": err,
                        "prompt_chars": len(prompt),
                        "response_chars": resp_len,
                    }
                )

    ai_interfaces.LocalLLMProvider._call_llm = patched
    return original


def _observed_max_concurrency(calls: list[dict]) -> int:
    events = []
    for c in calls:
        events.append((c["wall_start"], 1))
        events.append((c["wall_end"], -1))
    events.sort()
    cur = 0
    peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


# ---------------------------------------------------------------------------
# Resource monitor
# ---------------------------------------------------------------------------
class ResourceMonitor:
    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @staticmethod
    def _query_gpu():
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
            util_str, mem_str = out.stdout.strip().split(",")
            return float(util_str), float(mem_str)
        except Exception:
            return None, None

    def _run(self):
        psutil.cpu_percent(interval=None)  # prime the counter
        while not self._stop.is_set():
            cpu = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            gpu_util, vram_mb = self._query_gpu()
            self.samples.append(
                {
                    "t": time.time(),
                    "cpu_pct": cpu,
                    "ram_used_gb": vm.used / 1e9,
                    "ram_pct": vm.percent,
                    "gpu_util_pct": gpu_util,
                    "vram_used_mb": vram_mb,
                }
            )
            self._stop.wait(self.interval)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def summary(self) -> dict:
        if not self.samples:
            return {}
        cpu = [s["cpu_pct"] for s in self.samples]
        ram = [s["ram_used_gb"] for s in self.samples]
        gpu = [s["gpu_util_pct"] for s in self.samples if s["gpu_util_pct"] is not None]
        vram = [s["vram_used_mb"] for s in self.samples if s["vram_used_mb"] is not None]
        return {
            "samples": len(self.samples),
            "cpu_pct": {"min": min(cpu), "avg": mean(cpu), "max": max(cpu)},
            "ram_gb": {"min": min(ram), "avg": mean(ram), "max": max(ram)},
            "gpu_util_pct": {"min": min(gpu), "avg": mean(gpu), "max": max(gpu)} if gpu else None,
            "vram_mb": {"min": min(vram), "avg": mean(vram), "max": max(vram)} if vram else None,
        }


def main() -> int:
    print("=" * 70)
    print("FULL PIPELINE E2E BENCHMARK — Phase 2 local AI enabled")
    print("=" * 70)

    from src.storage.runs import RunStorage

    rs = RunStorage(db_path=DB_PATH)
    rs.init_db()
    if rs.get_current_running_run() is not None:
        print("ABORT: a pipeline run is already in progress. Not starting a second one.")
        return 1

    original_call_llm = _install_llm_timing_patch()

    from src.api.runner import PipelineRunner
    from src.notifications.engine import NotificationEngine

    notification_engine = NotificationEngine(db_path=DB_PATH, config_dir=CONFIG_DIR)
    runner = PipelineRunner(
        db_path=DB_PATH,
        config_dir=CONFIG_DIR,
        notification_engine=notification_engine,
    )

    mon = ResourceMonitor(interval=1.0)
    mon.start()

    t_start = time.time()
    try:
        run_id = runner.start_run(use_ai=True)  # Issue 1 fix — native support, no monkeypatch
    except RuntimeError as exc:
        print(f"ABORT: {exc}")
        mon.stop()
        return 1

    print(f"Run started: {run_id} at {time.strftime('%Y-%m-%d %H:%M:%S')}")

    last_stage = None
    last_report = time.time()
    while runner.is_running():
        time.sleep(2)
        row = rs.get_run(run_id)
        stage = (row.get("stats") or {}).get("current_stage") if row else None
        now = time.time()
        if stage != last_stage or now - last_report > 30:
            elapsed = now - t_start
            n_llm = len(_llm_calls)
            print(f"  [{elapsed:7.1f}s] stage={stage!r}  llm_calls_so_far={n_llm}")
            last_stage = stage
            last_report = now

    t_end = time.time()
    mon.stop()
    total_duration = t_end - t_start

    final = rs.get_run(run_id)
    stats = (final or {}).get("stats", {})
    status = (final or {}).get("status")
    error = (final or {}).get("error")

    scored_rows = rs.get_jobs_for_run(run_id, limit=20, offset=0)
    rejections = rs.get_run_rejections(run_id, limit=100000)

    rejection_by_stage: dict[str, int] = {}
    rejection_reasons: dict[str, dict[str, int]] = {}
    for r in rejections:
        st = r.get("rejection_stage") or "unknown"
        rejection_by_stage[st] = rejection_by_stage.get(st, 0) + 1
        detail = (r.get("rejection_detail") or "")[:120]
        rejection_reasons.setdefault(st, {})
        rejection_reasons[st][detail] = rejection_reasons[st].get(detail, 0) + 1

    # Enrich top jobs with job details + AI analysis (cache lookup requires the
    # job's current content_hash and this run's profile_version, or it's a
    # deliberate miss by design — see AIAnalysisStorage.get_analysis).
    from src.ai.storage import AIAnalysisStorage
    from src.storage.sqlite import Storage

    ai_storage = AIAnalysisStorage(db_path=DB_PATH)
    job_storage = Storage(db_path=DB_PATH)
    job_storage.init_db()
    profile_hash = stats.get("profile_hash", "")

    top_jobs_enriched = []
    for row in scored_rows:
        job_id = row["job_id"]
        entry = {
            "job_id": job_id,
            "final_score": row.get("final_score"),
            "deterministic_score": row.get("deterministic_score"),
            "semantic_score": row.get("semantic_score"),
            "score_breakdown": row.get("score_breakdown"),
        }
        job = job_storage.get_job_by_id(job_id)
        if job is not None:
            entry["title"] = job.title
            entry["company"] = job.company
            entry["source"] = job.source
            entry["location"] = job.location
            try:
                ai_result = ai_storage.get_analysis(
                    job_id,
                    content_hash=job.content_hash or "",
                    profile_version=profile_hash,
                )
            except Exception:
                ai_result = None
            if ai_result is not None and ai_result.status == "success":
                a = ai_result.analysis
                entry["ai"] = {
                    "domain": a.domain,
                    "seniority_level": a.seniority_level,
                    "profile_match_score": a.profile_match_score,
                    "profile_match_reasons": a.profile_match_reasons,
                    "profile_mismatch_reasons": a.profile_mismatch_reasons,
                }
        top_jobs_enriched.append(entry)
    job_storage.close()

    # Phase 2 latency analysis from captured calls
    durations = [c["duration"] for c in _llm_calls]
    ok_calls = [c for c in _llm_calls if c["ok"]]
    failed_calls = [c for c in _llm_calls if not c["ok"]]
    phase2_latency = {}
    if durations:
        phase2_latency = {
            "n_calls": len(durations),
            "n_success": len(ok_calls),
            "n_failed": len(failed_calls),
            "min": min(durations),
            "max": max(durations),
            "mean": mean(durations),
            "median": median(durations),
            "p95": _percentile(durations, 0.95),
            "p99": _percentile(durations, 0.99),
            "observed_max_concurrency": _observed_max_concurrency(_llm_calls),
            "avg_prompt_chars": mean([c["prompt_chars"] for c in _llm_calls]),
            "avg_response_chars": mean(
                [c["response_chars"] for c in ok_calls if c["response_chars"] is not None]
            )
            if ok_calls
            else None,
            "error_types": {
                et: sum(1 for c in failed_calls if c["error"] == et)
                for et in {c["error"] for c in failed_calls}
            },
        }

    resource_summary = mon.summary()

    report = {
        "run_id": run_id,
        "status": status,
        "error": error,
        "total_wall_duration_seconds": total_duration,
        "pipeline_reported_duration_seconds": stats.get("duration_seconds"),
        "stats": stats,
        "top_jobs": top_jobs_enriched,
        "rejection_by_stage": rejection_by_stage,
        "rejection_reasons_sample": rejection_reasons,
        "phase2_latency": phase2_latency,
        "resource_summary": resource_summary,
        "resource_samples": mon.samples,
    }

    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print("\n" + "=" * 70)
    print(f"DONE. status={status}  total_wall_duration={total_duration:.1f}s")
    print(f"Report written to: {REPORT_JSON}")
    print("=" * 70)

    # restore patched symbol (best-effort; process exits right after anyway)
    from src.ai import interfaces as ai_interfaces

    ai_interfaces.LocalLLMProvider._call_llm = original_call_llm

    return 0 if status == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
