"""Model benchmark (plan F8.3): scoring on canned outputs, verdicts, mocked Ollama runs."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from src.ai.benchmark import runner
from src.ai.benchmark.runner import load_fixtures, run_benchmark
from src.ai.benchmark.scoring import (
    MAX_ON_DEMAND_SECONDS,
    BenchmarkMetrics,
    OfferScore,
    aggregate,
    is_grounded,
    looks_french,
    normalise,
    parse_analysis,
    score_offer,
    verdict,
)
from src.ai.catalogue import load_catalogue
from src.ai.endpoint import ALLOW_REMOTE_ENV
from src.ai.runtime import detect, ollama
from src.ai.runtime.detect import RuntimeDetection, RuntimeModel
from src.ai.runtime.jobs import JobCancelled, JobFailed, JobRegistry
from src.api.routes import ai as ai_routes

OFFER = ("Profil recherché :\n- Minimum 2 ans  d'expérience en développement Python.\n"
         "- Une première expérience avec Docker est indispensable.")
PROFILE_IDS = {"exp-1", "skill-1"}


def _analysis(*quotes: str, label: str = "Expérience Python exigée",
              summary: str = "Le profil correspond bien à la plupart des exigences du poste.",
              refs: list[str] | None = None) -> str:
    return json.dumps({"requirements": [
        {"kind": "mandatory", "label": label, "quote": q, "profile_match": "met",
         "profile_entry_ids": refs if refs is not None else ["exp-1"]} for q in quotes],
        "summary": summary}, ensure_ascii=False)


# --- grounding and language --------------------------------------------------------------


def test_grounding_is_an_exact_substring_after_whitespace_normalisation():
    assert is_grounded("Minimum 2 ans d'expérience en développement Python", OFFER)
    assert is_grounded("Minimum 2 ans\n d'expérience", OFFER)  # whitespace differs only
    assert not is_grounded("Minimum deux ans d'expérience", OFFER)  # paraphrase
    assert not is_grounded("minimum 2 ans", OFFER)  # case matters: quotes are verbatim
    assert not is_grounded("Py", OFFER)  # too short to prove anything
    assert not is_grounded(None, OFFER)


def test_normalise_composes_unicode():
    assert normalise("expérience  x") == "expérience x"


def test_looks_french():
    assert looks_french("Le profil correspond à la plupart des exigences du poste")
    assert not looks_french("The profile matches most of the requirements for this role")
    assert not looks_french("Python Docker SQL")


def test_parse_analysis_validity():
    assert parse_analysis(_analysis("Docker")) is not None
    assert parse_analysis("```json\n" + _analysis("Docker") + "\n```") is not None
    assert parse_analysis("not json") is None
    assert parse_analysis('{"requirements": []}') is None
    assert parse_analysis('{"requirements": [{"label": "x"}]}') is None
    assert parse_analysis('[1, 2]') is None


def test_score_offer_counts_grounded_quotes_and_profile_refs():
    raw = _analysis("Minimum 2 ans d'expérience en développement Python",
                    "Docker est obligatoire", refs=["exp-1", "exp-9"])
    score = score_offer("o1", OFFER, raw, PROFILE_IDS, seconds=4.0, eval_tokens=200,
                        eval_seconds=2.0)
    assert (score.json_valid, score.requirements, score.grounded) == (True, 2, 1)
    assert (score.profile_refs, score.profile_refs_valid) == (4, 2)
    assert score.french


def test_score_offer_recall_counts_expected_phrases_in_grounded_quotes_only():
    raw = _analysis("Minimum 2 ans d'expérience en développement Python",
                    "Docker est obligatoire")  # second quote is not grounded
    score = score_offer("o1", OFFER, raw, PROFILE_IDS,
                        expected=["2 ans d'expérience", "Docker", "pyth"])
    assert (score.expected, score.found) == (3, 1)  # "pyth" is not a whole word
    m = aggregate([score])
    assert m.recall == pytest.approx(1 / 3)


def test_score_offer_flags_english_labels():
    raw = _analysis("Docker", label="Docker experience required",
                    summary="The candidate is a strong match for the role.")
    assert not score_offer("o1", OFFER, raw, PROFILE_IDS).french


def test_score_offer_invalid_json():
    score = score_offer("o1", OFFER, "Sure! Here is the analysis: {", PROFILE_IDS, seconds=3.0)
    assert not score.json_valid and score.requirements == 0 and score.seconds == 3.0


# --- aggregation and verdict -------------------------------------------------------------


def _metrics(json_rate=1.0, grounding=1.0, french=1.0, spo=10.0) -> BenchmarkMetrics:
    return BenchmarkMetrics(offers=3, json_valid_rate=json_rate, grounding_rate=grounding,
                            french_rate=french, profile_ref_valid_rate=1.0, recall=None,
                            requirements_per_offer=5.0, seconds_per_offer=spo,
                            tokens_per_second=50.0)


def test_aggregate_rates():
    scores = [
        OfferScore("a", True, requirements=4, grounded=4, french=True, seconds=10,
                   eval_tokens=500, eval_seconds=10),
        OfferScore("b", True, requirements=6, grounded=3, french=False, seconds=20,
                   eval_tokens=500, eval_seconds=10),
        OfferScore("c", False, seconds=30),
    ]
    m = aggregate(scores)
    assert m.json_valid_rate == pytest.approx(2 / 3)
    assert m.grounding_rate == pytest.approx(0.7)
    assert m.french_rate == 0.5
    assert m.seconds_per_offer == 20.0
    assert m.tokens_per_second == 50.0
    assert m.profile_ref_valid_rate is None
    assert aggregate([]).json_valid_rate == 0.0


def test_verdict_ok_on_demand_with_daily_minutes():
    code, params = verdict(_metrics(spo=12.0), offers_per_day=100)
    assert code == "ok_on_demand"
    assert params == {"seconds_per_offer": 12.0, "offers_per_day": 100, "minutes_per_day": 20.0}


@pytest.mark.parametrize(("metrics", "reason"), [
    (_metrics(json_rate=0.5), "json"), (_metrics(grounding=0.6), "grounding"),
    (_metrics(french=0.2), "french"),
])
def test_verdict_invalid_output(metrics, reason):
    code, params = verdict(metrics)
    assert code == "invalid_output_try_other" and params["reason"] == reason


def test_verdict_invalid_beats_slow():
    assert verdict(_metrics(json_rate=0.0, spo=500))[0] == "invalid_output_try_other"


def test_verdict_too_slow():
    slow = _metrics(spo=MAX_ON_DEMAND_SECONDS + 1)
    assert verdict(slow)[0] == "too_slow_try_smaller"
    assert verdict(slow, smaller_tier_available=False)[0] == "too_slow_continue_without_ai"


# --- fixtures ----------------------------------------------------------------------------


def test_fixtures_are_synthetic_bilingual_and_well_formed():
    fx = load_fixtures()
    offers = fx["offers"]
    assert 6 <= len(offers) <= 10
    assert {o["language"] for o in offers} == {"fr", "en"}
    assert len({o["id"] for o in offers}) == len(offers)
    assert all(o["title"] and len(o["text"]) > 300 for o in offers)
    for offer in offers:  # recall phrases must exist in the offer, or recall is meaningless
        assert offer["expected"]
        assert all(p.lower() in offer["text"].lower() for p in offer["expected"])
    assert {e["id"] for e in fx["profile"]["entries"]} >= {"exp-1", "edu-1"}
    assert "Synthetic" in fx["note"]


def test_messages_carry_the_offer_and_the_profile_ids():
    fx = load_fixtures()
    messages = runner.build_messages(fx["offers"][0], fx["profile"])
    assert messages[0]["role"] == "system" and "character for character" in messages[0]["content"]
    assert fx["offers"][0]["text"] in messages[1]["content"]
    assert "[exp-1]" in messages[1]["content"]


# --- runner with a fake Ollama -----------------------------------------------------------


class _FakeChat:
    """Answers each offer with a quote copied from it (or a canned bad reply)."""

    def __init__(self, mode: str = "good") -> None:
        self.mode = mode
        self.calls: list[dict] = []

    def __call__(self, model, messages, *, base_url=None, format_schema=None, think=False,
                 options=None, timeout=None, cancelled=None):
        self.calls.append({"model": model, "format": format_schema, "think": think,
                           "base_url": base_url})
        if format_schema is None:  # the warm-up load
            return {"message": {"content": "OK"}}
        text = messages[1]["content"].split("<<<\n", 1)[1].rsplit("\n>>>", 1)[0]
        line = next(ln.strip("- ") for ln in text.splitlines() if len(ln.strip("- ")) > 20)
        content = {"good": _analysis(line), "invalid": "I cannot do that.",
                   "ungrounded": _analysis("Une expérience inventée de dix ans")}[self.mode]
        return {"message": {"content": content}, "eval_count": 100,
                "eval_duration": 2_000_000_000}


def test_run_benchmark_scores_and_gives_a_verdict(monkeypatch):
    fake = _FakeChat()
    monkeypatch.setattr(ollama, "chat", fake)
    progress: list[dict] = []
    result = run_benchmark("m:1", offers=3, think="low", on_progress=progress.append)
    assert result["metrics"]["offers"] == 3
    assert result["metrics"]["json_valid_rate"] == 1.0
    assert result["metrics"]["grounding_rate"] == 1.0
    assert result["metrics"]["tokens_per_second"] == 50.0
    assert result["verdict"] == "ok_on_demand"
    assert result["prompt_version"] == runner.PROMPT_VERSION
    assert len(fake.calls) == 4 and all(c["think"] == "low" for c in fake.calls)
    assert fake.calls[1]["format"] == runner.ANALYSIS_SCHEMA
    assert progress[0]["phase"] == "loading" and progress[-1] == {
        "phase": "done", "offers_done": 3, "offers_total": 3}


@pytest.mark.parametrize(("mode", "reason"), [("invalid", "json"), ("ungrounded", "grounding")])
def test_run_benchmark_bad_outputs(monkeypatch, mode, reason):
    monkeypatch.setattr(ollama, "chat", _FakeChat(mode))
    result = run_benchmark("m:1", offers=2)
    assert result["verdict"] == "invalid_output_try_other"
    assert result["verdict_params"]["reason"] == reason


def test_run_benchmark_counts_a_failed_call_as_invalid(monkeypatch):
    fake = _FakeChat()

    def flaky(model, messages, **kwargs):
        if kwargs.get("format_schema") is not None and len(fake.calls) == 1:
            fake.calls.append({})
            raise JobFailed("timeout")
        return fake(model, messages, **kwargs)

    monkeypatch.setattr(ollama, "chat", flaky)
    result = run_benchmark("m:1", offers=2)
    assert result["per_offer"][0]["error"] == "timeout"
    assert result["metrics"]["json_valid_rate"] == 0.5


def test_run_benchmark_stops_when_ollama_is_unreachable(monkeypatch):
    def down(*a, **k):
        raise JobFailed("unreachable")

    monkeypatch.setattr(ollama, "chat", down)
    with pytest.raises(JobFailed):
        run_benchmark("m:1")


# --- routes ------------------------------------------------------------------------------


@pytest.fixture()
def client(isolated_app, monkeypatch):
    monkeypatch.delenv(ALLOW_REMOTE_ENV, raising=False)
    monkeypatch.setattr(ai_routes, "BENCHMARK_JOBS", JobRegistry("benchmark"))
    monkeypatch.setattr(detect, "probe_ollama", lambda base: RuntimeDetection(
        runtime="ollama", base_url=base, status="running", reachable=True,
        models=[RuntimeModel(name="m:1"), RuntimeModel(name="mine:latest")]))
    return TestClient(isolated_app)


def test_benchmark_route_runs_a_job(client, monkeypatch):
    fake = _FakeChat()
    monkeypatch.setattr(ollama, "chat", fake)
    small = load_catalogue().default("small")
    resp = client.post("/api/ai/benchmark", json={"model": small.ollama_tag, "offers": 2,
                                                  "offers_per_day": 60})
    assert resp.status_code == 202
    job_id = resp.json()["id"]
    ai_routes.BENCHMARK_JOBS.wait(job_id, 5)
    status = client.get(f"/api/ai/benchmark/{job_id}").json()
    assert status["status"] == "succeeded" and status["phase"] == "done"
    result = status["result"]
    assert result["verdict"] == "ok_on_demand"
    assert result["verdict_params"]["offers_per_day"] == 60
    assert fake.calls[0]["think"] == small.think


def test_benchmark_route_refuses_a_remote_ollama(client, monkeypatch):
    calls = []
    monkeypatch.setattr(ollama, "chat", lambda *a, **k: calls.append(a))
    resp = client.post("/api/ai/benchmark", json={"model": "m:1",
                                                  "base_url": "http://10.0.0.5:11434"})
    assert resp.status_code == 400 and resp.json()["detail"]["error"] == "endpoint_not_local"
    assert calls == []


def test_benchmark_route_cancel(client, monkeypatch):
    import threading

    gate = threading.Event()
    fake = _FakeChat()

    def slow(*a, **k):
        gate.wait(5)
        return fake(*a, **k)

    monkeypatch.setattr(ollama, "chat", slow)
    job_id = client.post("/api/ai/benchmark", json={"model": "m:1", "offers": 3}).json()["id"]
    assert client.delete(f"/api/ai/benchmark/{job_id}").status_code == 200
    gate.set()
    ai_routes.BENCHMARK_JOBS.wait(job_id, 5)
    assert client.get(f"/api/ai/benchmark/{job_id}").json()["status"] == "cancelled"


def test_benchmark_status_unknown_job(client):
    assert client.get("/api/ai/benchmark/nope").status_code == 404
    assert client.delete("/api/ai/benchmark/nope").status_code == 404


def test_benchmark_route_only_for_catalogue_or_installed_models(client, monkeypatch):
    calls = []
    monkeypatch.setattr(ollama, "chat", lambda *a, **k: calls.append(a) or {})
    resp = client.post("/api/ai/benchmark", json={"model": "not-installed:7b"})
    assert resp.status_code == 404 and resp.json()["detail"]["error"] == "model_not_available"
    assert calls == []
    fake = _FakeChat()
    monkeypatch.setattr(ollama, "chat", fake)
    job_id = client.post("/api/ai/benchmark", json={"model": "mine", "offers": 1}).json()["id"]
    ai_routes.BENCHMARK_JOBS.wait(job_id, 5)
    assert client.get(f"/api/ai/benchmark/{job_id}").json()["status"] == "succeeded"


def test_benchmark_route_one_job_at_a_time(client, monkeypatch):
    gate = threading.Event()
    fake = _FakeChat()

    def slow(*a, **k):
        gate.wait(5)
        return fake(*a, **k)

    monkeypatch.setattr(ollama, "chat", slow)
    first = client.post("/api/ai/benchmark", json={"model": "m:1", "offers": 1}).json()["id"]
    small = load_catalogue().default("small")
    resp = client.post("/api/ai/benchmark", json={"model": small.ollama_tag})
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "benchmark_busy", "job_id": first}
    gate.set()
    ai_routes.BENCHMARK_JOBS.wait(first, 5)
    resp = client.post("/api/ai/benchmark", json={"model": small.ollama_tag, "offers": 1})
    assert resp.status_code == 202
    ai_routes.BENCHMARK_JOBS.wait(resp.json()["id"], 5)


# --- streamed chat with cancel against a fake Ollama ------------------------------------


class _ChatServer:
    """``/api/chat`` that waits ``delay`` s (a model load), then streams ``lines``."""

    def __init__(self, lines: list[dict], delay: float = 0.0, line_delay: float = 0.0) -> None:
        self.closed = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                time.sleep(delay)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                try:
                    for line in lines:
                        self.wfile.write(json.dumps(line).encode() + b"\n")
                        self.wfile.flush()
                        time.sleep(line_delay)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    server.closed.set()

            def log_message(self, *args) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def _local(monkeypatch):
    monkeypatch.delenv(ALLOW_REMOTE_ENV, raising=False)


def test_chat_assembles_the_streamed_message_and_timings(_local):
    srv = _ChatServer([{"message": {"content": '{"a": '}, "done": False},
                       {"message": {"content": "1}"}, "done": False},
                       {"message": {"content": ""}, "done": True, "eval_count": 7,
                        "eval_duration": 1000}])
    try:
        reply = ollama.chat("m:1", [{"role": "user", "content": "x"}], base_url=srv.url,
                            cancelled=lambda: False)
    finally:
        srv.close()
    assert reply["message"]["content"] == '{"a": 1}' and reply["eval_count"] == 7


def test_chat_reports_an_error_line(_local):
    srv = _ChatServer([{"error": "model 'm:1' not found"}])
    try:
        with pytest.raises(JobFailed) as exc:
            ollama.chat("m:1", [], base_url=srv.url)
    finally:
        srv.close()
    assert exc.value.code == "ollama_error"


def test_chat_cancel_returns_at_once_while_the_model_loads(_local):
    srv = _ChatServer([{"done": True}], delay=5.0)
    started = time.monotonic()
    try:
        with pytest.raises(JobCancelled):
            ollama.chat("m:1", [], base_url=srv.url,
                        cancelled=lambda: time.monotonic() - started > 0.3)
    finally:
        elapsed = time.monotonic() - started
        srv.close()
    assert elapsed < 2.0


def test_chat_cancel_closes_the_stream_mid_answer(_local):
    lines = [{"message": {"content": "x"}, "done": False}] * 200 + [{"done": True}]
    srv = _ChatServer(lines, line_delay=0.02)
    started = time.monotonic()
    try:
        with pytest.raises(JobCancelled):
            ollama.chat("m:1", [], base_url=srv.url,
                        cancelled=lambda: time.monotonic() - started > 0.3)
        assert time.monotonic() - started < 2.0
        assert srv.closed.wait(3)  # the server saw the connection go away
    finally:
        srv.close()
