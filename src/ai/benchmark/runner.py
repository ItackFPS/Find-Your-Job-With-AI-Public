"""Verify and benchmark a local Ollama model on synthetic offers (plan F8.3).

Loads the model, sends each fixture offer through an offer-analysis prompt whose
requirements must quote the offer verbatim, and scores the replies (``scoring.py``).
Only the bundled synthetic fixtures are sent, to a loopback Ollama (guarded).

Run it by hand: ``python -m src.ai.benchmark.runner qwen3.5:9b [--offers 8]``.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.ai.benchmark.scoring import (
    DEFAULT_OFFERS_PER_DAY,
    OfferScore,
    aggregate,
    score_offer,
    verdict,
)
from src.ai.runtime import ollama
from src.ai.runtime.jobs import JobCancelled, JobFailed

PROMPT_VERSION = "offer_analysis_bench_v1"
FIXTURES_PATH = Path(__file__).with_name("fixtures.json")
DEFAULT_OFFERS = 3
CHAT_OPTIONS: dict[str, Any] = {"temperature": 0, "seed": 42, "num_ctx": 8192,
                                "num_predict": 2048}

ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["mandatory", "preferred", "implicit"]},
                    "label": {"type": "string"},
                    "quote": {"type": "string"},
                    "profile_match": {"type": "string",
                                      "enum": ["met", "partly", "missing", "unknown"]},
                    "profile_entry_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["kind", "label", "quote", "profile_match", "profile_entry_ids"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["requirements", "summary"],
}

SYSTEM_PROMPT = """You analyse one job posting for a job seeker. Use ONLY the posting and the profile below; never invent facts.

Return ONLY a JSON object with:
- "requirements": 4 to 10 items, each with
  - "kind": "mandatory", "preferred" or "implicit";
  - "quote": the exact words of the posting that state this requirement, copied character for character (a short phrase or one line). Never paraphrase, translate or shorten words inside the quote;
  - "label": a short label for the requirement, written in French;
  - "profile_match": "met", "partly", "missing" or "unknown", judged only from the profile;
  - "profile_entry_ids": the ids (like "exp-1") of the profile entries that support the match, or [] if none;
- "summary": one or two sentences in French on how well the profile fits this posting."""


@lru_cache(maxsize=1)
def load_fixtures() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    return data


def build_messages(offer: dict[str, Any], profile: dict[str, Any]) -> list[dict[str, str]]:
    profile_lines = "\n".join(f"- [{e['id']}] {e['text']}" for e in profile["entries"])
    user = (f"PROFILE:\n{profile_lines}\n\nJOB POSTING: {offer['title']}\n<<<\n"
            f"{offer['text']}\n>>>")
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def run_benchmark(model: str, *, base_url: str | None = None, offers: int = DEFAULT_OFFERS,
                  think: bool | str | None = False,
                  offers_per_day: int = DEFAULT_OFFERS_PER_DAY,
                  smaller_tier_available: bool = True,
                  on_progress: Callable[[dict[str, Any]], None] = lambda _s: None,
                  cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    """Run the benchmark and return metrics plus the verdict. Raises ``JobFailed``/``JobCancelled``."""
    fixtures = load_fixtures()
    chosen = fixtures["offers"][:max(1, min(offers, len(fixtures["offers"])))]
    profile = fixtures["profile"]
    profile_ids = {e["id"] for e in profile["entries"]}

    started = time.monotonic()
    on_progress({"phase": "loading", "offers_done": 0, "offers_total": len(chosen)})
    ollama.chat(model, [{"role": "user", "content": "Réponds OK."}], base_url=base_url,
                think=think, options={**CHAT_OPTIONS, "num_predict": 8},  # same: no reload
                cancelled=cancelled)
    load_seconds = time.monotonic() - started

    scores: list[OfferScore] = []
    for i, offer in enumerate(chosen):
        if cancelled():
            raise JobCancelled()
        on_progress({"phase": "analysing", "offers_done": i, "offers_total": len(chosen)})
        t0 = time.monotonic()
        try:
            reply = ollama.chat(model, build_messages(offer, profile), base_url=base_url,
                                format_schema=ANALYSIS_SCHEMA, think=think,
                                options=CHAT_OPTIONS, cancelled=cancelled)
        except JobFailed as exc:
            if exc.code in (ollama.ERR_NOT_LOCAL, ollama.ERR_UNREACHABLE):
                raise
            scores.append(OfferScore(offer_id=offer["id"], json_valid=False,
                                     expected=len(offer.get("expected", ())),
                                     seconds=time.monotonic() - t0, error=exc.code))
            continue
        message = reply.get("message")
        raw = message.get("content", "") if isinstance(message, dict) else ""
        eval_count = reply.get("eval_count")
        eval_ns = reply.get("eval_duration")
        scores.append(score_offer(
            offer["id"], offer["text"], raw if isinstance(raw, str) else "", profile_ids,
            seconds=time.monotonic() - t0,
            eval_tokens=eval_count if isinstance(eval_count, int) else 0,
            eval_seconds=eval_ns / 1e9 if isinstance(eval_ns, int | float) else 0.0,
            expected=offer.get("expected", ())))
    on_progress({"phase": "done", "offers_done": len(chosen), "offers_total": len(chosen)})

    metrics = aggregate(scores)
    code, params = verdict(metrics, offers_per_day=offers_per_day,
                           smaller_tier_available=smaller_tier_available)
    summary = asdict(metrics)
    per_offer = summary.pop("per_offer")
    return {"model": model, "prompt_version": PROMPT_VERSION, "load_seconds": round(load_seconds, 2),
            "metrics": summary, "per_offer": per_offer, "verdict": code, "verdict_params": params}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("model")
    parser.add_argument("--offers", type=int, default=len(load_fixtures()["offers"]))
    parser.add_argument("--think", default="false",
                        help="false (default), true, or a level such as low")
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args(argv)
    think: bool | str = {"false": False, "true": True}.get(args.think.lower(), args.think)
    result = run_benchmark(args.model, base_url=args.base_url, offers=args.offers, think=think)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
