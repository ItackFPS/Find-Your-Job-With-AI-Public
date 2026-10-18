"""Focused Phase 2 (local AI) benchmark — AFTER the 2026-09-05 fix pass.

Measures the same metrics as the full-pipeline benchmark's Phase 2 section
(scripts/benchmark_full_pipeline_e2e.py) but scoped to just Phase 2 against
~100 real jobs already in data/jobs.db, so it doesn't require a full
20k-job collection run to get a like-for-like BEFORE/AFTER comparison.

Drives the real LocalLLMProvider (src/ai/interfaces.py) with the current
production Phase2Config (max_tokens=1024, the new evidence-based default;
truncation retry active) — same instrumentation pattern as
scripts/benchmark_eligibility_gate.py: a script-only timing wrapper around
_call_llm, no source changes.

Usage:
    python scripts/benchmark_phase2_focused.py [--limit 100]
"""

from __future__ import annotations

import argparse
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
REPORT_JSON = ROOT / "scripts" / "phase2_focused_benchmark_report.json"

load_dotenv(ROOT / ".env")

_llm_lock = threading.Lock()
_llm_calls: list[dict] = []


def _install_llm_timing_patch():
    from src.ai import interfaces as ai_interfaces

    original = ai_interfaces.LocalLLMProvider._call_llm

    def patched(self, prompt, max_tokens=None):
        t0 = time.perf_counter()
        wall_start = time.time()
        try:
            resp = original(self, prompt, max_tokens=max_tokens)
            return resp
        finally:
            dt = time.perf_counter() - t0
            with _llm_lock:
                _llm_calls.append({"wall_start": wall_start, "wall_end": wall_start + dt, "duration": dt})

    ai_interfaces.LocalLLMProvider._call_llm = patched
    return original


def _observed_max_concurrency(calls: list[dict]) -> int:
    events = []
    for c in calls:
        events.append((c["wall_start"], 1))
        events.append((c["wall_end"], -1))
    events.sort()
    cur = peak = 0
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
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


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
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
            util_str, mem_str = out.stdout.strip().split(",")
            return float(util_str), float(mem_str)
        except Exception:
            return None, None

    def _run(self):
        psutil.cpu_percent(interval=None)
        while not self._stop.is_set():
            cpu = psutil.cpu_percent(interval=None)
            gpu_util, vram_mb = self._query_gpu()
            self.samples.append({"t": time.time(), "cpu_pct": cpu, "gpu_util_pct": gpu_util, "vram_used_mb": vram_mb})
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
        gpu = [s["gpu_util_pct"] for s in self.samples if s["gpu_util_pct"] is not None]
        vram = [s["vram_used_mb"] for s in self.samples if s["vram_used_mb"] is not None]
        return {
            "samples": len(self.samples),
            "cpu_pct": {"min": min(cpu), "avg": mean(cpu), "max": max(cpu)},
            "gpu_util_pct": {"min": min(gpu), "avg": mean(gpu), "max": max(gpu)} if gpu else None,
            "vram_mb": {"min": min(vram), "avg": mean(vram), "max": max(vram)} if vram else None,
        }


def _load_sample_jobs(limit: int):
    from src.storage.sqlite import Storage

    storage = Storage(db_path=DB_PATH)
    storage.init_db()
    conn = storage._get_conn()
    rows = conn.execute(
        "SELECT id FROM jobs WHERE status = 'active' AND description IS NOT NULL "
        "ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    jobs = [storage.get_job_by_id(r[0]) for r in rows]
    jobs = [j for j in jobs if j is not None]
    storage.close()
    return jobs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    print("=" * 70)
    print("FOCUSED PHASE 2 BENCHMARK (AFTER — 2026-09-05 fix pass)")
    print("=" * 70)

    original_call_llm = _install_llm_timing_patch()

    from src.ai.config import load_phase2_config
    from src.ai.interfaces import LocalLLMProvider
    from src.pipeline.filter import load_profile

    phase2_cfg = load_phase2_config()
    profile = load_profile(str(Path(CONFIG_DIR) / "profile.yaml"))
    jobs = _load_sample_jobs(args.limit)
    print(f"Loaded {len(jobs)} real jobs from {DB_PATH}")
    print(f"max_tokens={phase2_cfg.llm_max_tokens}  max_workers={phase2_cfg.max_workers}  "
          f"endpoint={phase2_cfg.llm_endpoint}")

    provider = LocalLLMProvider(
        endpoint=phase2_cfg.llm_endpoint,
        model_name=phase2_cfg.llm_model_name,
        temperature=phase2_cfg.llm_temperature,
        max_tokens=phase2_cfg.llm_max_tokens,
        timeout_seconds=phase2_cfg.llm_timeout_seconds,
        thinking_enabled=phase2_cfg.llm_thinking_enabled,
        max_workers=phase2_cfg.max_workers,
    )

    mon = ResourceMonitor(interval=0.5)
    mon.start()

    progress = {"completed": 0}
    def on_progress(completed, total, failed):
        progress["completed"] = completed
        if completed % 10 == 0 or completed == total:
            print(f"  {completed}/{total} analyzed ({failed} failed so far)")

    t0 = time.time()
    batch_result = provider.analyze_batch(
        jobs, prompt_version=phase2_cfg.prompt_version,
        profile=profile.model_dump(), on_progress=on_progress,
    )
    total_wall = time.time() - t0
    mon.stop()

    durations = [c["duration"] for c in _llm_calls]
    failure_reasons = batch_result.failure_reasons
    retried = batch_result.retried

    latency = {}
    if durations:
        latency = {
            "n_calls": len(durations),
            "min": min(durations), "max": max(durations),
            "mean": mean(durations), "median": median(durations),
            "p95": _percentile(durations, 0.95), "p99": _percentile(durations, 0.99),
            "observed_max_concurrency": _observed_max_concurrency(_llm_calls),
            "throughput_jobs_per_sec": len(jobs) / total_wall if total_wall > 0 else 0.0,
        }

    report = {
        "total_jobs": batch_result.total_jobs,
        "successful": batch_result.successful,
        "failed": batch_result.failed,
        "skipped": batch_result.skipped,
        "retried": retried,
        "failure_reasons": failure_reasons,
        "total_wall_seconds": total_wall,
        "latency": latency,
        "resource_summary": mon.summary(),
        "config": {
            "max_tokens": phase2_cfg.llm_max_tokens,
            "max_workers": phase2_cfg.max_workers,
        },
    }

    REPORT_JSON.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print("\n" + "=" * 70)
    print(f"total_jobs={report['total_jobs']}  successful={report['successful']}  "
          f"failed={report['failed']}  retried={report['retried']}")
    print(f"failure_reasons={failure_reasons}")
    if latency:
        print(f"latency: mean={latency['mean']:.2f}s median={latency['median']:.2f}s "
              f"p95={latency['p95']:.2f}s p99={latency['p99']:.2f}s "
              f"min={latency['min']:.2f}s max={latency['max']:.2f}s")
        print(f"observed_max_concurrency={latency['observed_max_concurrency']}  "
              f"throughput={latency['throughput_jobs_per_sec']:.3f} jobs/sec")
    print(f"resource_summary={json.dumps(report['resource_summary'], indent=2)}")
    print(f"Report written to: {REPORT_JSON}")
    print("=" * 70)

    from src.ai import interfaces as ai_interfaces
    ai_interfaces.LocalLLMProvider._call_llm = original_call_llm

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
