"""Local-first: job text goes only to an LLM server on this machine, unless explicitly opted into."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.ai.endpoint import ALLOW_REMOTE_ENV, RemoteLLMRefused, is_loopback_endpoint, require_local_endpoint
from src.ai.eligibility import EligibilityGate
from src.ai.interfaces import LocalLLMProvider
from src.models.job import Job

REMOTE = ("https://api.openai.com", "http://192.168.1.20:1234", "http://localhost.example.com:1234",
          "http://10.0.0.5:11434", "http://0.0.0.0:1234", "not a url")


@pytest.fixture(autouse=True)
def _no_opt_in(monkeypatch):
    monkeypatch.delenv(ALLOW_REMOTE_ENV, raising=False)


@pytest.fixture()
def no_network(monkeypatch):
    calls = []

    def _post(*a, **k):
        calls.append(a)
        raise AssertionError("an HTTP request was attempted")

    monkeypatch.setattr("requests.post", _post)
    monkeypatch.setattr("requests.Session.post", _post)
    return calls


@pytest.mark.parametrize("url", ["http://localhost:1234", "http://127.0.0.1:11434", "http://[::1]:1234",
                                 "http://127.0.1.1:1234", "HTTP://LOCALHOST:1234"])
def test_loopback_endpoints_are_accepted(url):
    assert is_loopback_endpoint(url)
    require_local_endpoint(url)
    assert LocalLLMProvider(endpoint=url).endpoint == url


@pytest.mark.parametrize("url", REMOTE)
def test_a_remote_endpoint_is_refused_with_a_clear_message(url, no_network):
    assert not is_loopback_endpoint(url)
    with pytest.raises(RemoteLLMRefused, match="not on this machine.*FYJ_ALLOW_REMOTE_LLM=1"):
        LocalLLMProvider(endpoint=url)
    assert not no_network


def test_remote_use_needs_the_explicit_opt_in(monkeypatch):
    monkeypatch.setenv(ALLOW_REMOTE_ENV, "true")          # only "1" opts in
    with pytest.raises(RemoteLLMRefused):
        require_local_endpoint("http://192.168.1.20:1234")
    monkeypatch.setenv(ALLOW_REMOTE_ENV, "1")
    assert LocalLLMProvider(endpoint="http://192.168.1.20:1234").endpoint == "http://192.168.1.20:1234"


def test_eligibility_gate_with_a_remote_endpoint_fails_open_and_says_why(monkeypatch, tmp_path, no_network):
    """The gate never sends text out and never rejects a job because its endpoint was refused."""
    monkeypatch.setenv("PHASE2_LLM_ENDPOINT", "https://api.openai.com")
    job = Job(id="j1", source="s", title="Développeur Python", company="Acme", description="python " * 20)
    profile = SimpleNamespace(matching=SimpleNamespace(llm_eligibility_enabled=True,
                                                       llm_eligibility_reject_confidence=0.75),
                              model_dump=lambda: {})
    result = EligibilityGate.run_stage([job], {}, profile, "h", str(tmp_path / "jobs.db"), use_ai=True)
    assert result.ran is False and result.survivors == [job] and result.rejected == 0
    assert "not on this machine" in (result.error or "")
    assert not no_network


def test_query_expansion_keeps_the_baseline_when_the_endpoint_is_remote(no_network):
    from src.discovery.query_expansion import DiscoveryQueryExpander

    exp = DiscoveryQueryExpander(llm_endpoint="https://api.openai.com")
    with pytest.raises(RemoteLLMRefused):
        exp._try_llm_expansion(["software"], ["python"], "fr")
    assert not no_network
