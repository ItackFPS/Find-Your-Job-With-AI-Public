"""Backend <-> n8n contract test for the weekly digest email.

Parses the REAL, shipped config/n8n_weekly_digest.json (not a synthetic
copy) and replays its HTTP Request node's exact URL + query parameters
against the real /api/jobs/digest/weekly-combined endpoint via TestClient.
This is the one place that would catch the two systems drifting apart again
(e.g. a future endpoint change renaming a query param without updating the
workflow, or vice versa) — every other digest test uses synthetic params
chosen by the test author, not the workflow's own committed configuration.

Single-node design (Location Priority feature, 2026-09-10 — see Format
Digest's own comment in the workflow JSON): the workflow fetches ONE global,
unsectioned Top-N ranking rather than two department/remote-scoped calls.
Discord's real-time channels already cover "best jobs per preferred
location"; this weekly email is deliberately the broader, complementary
"best overall opportunities this week" surface.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage

_WORKFLOW_PATH = Path(__file__).resolve().parents[1] / "config" / "n8n_weekly_digest.json"


def _load_workflow() -> dict:
    with _WORKFLOW_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _http_request_nodes(workflow: dict) -> list[dict]:
    return [n for n in workflow["nodes"] if n["type"] == "n8n-nodes-base.httpRequest"]


def _query_params(node: dict) -> dict[str, str]:
    qp = node["parameters"].get("queryParameters", {}).get("parameters", [])
    return {p["name"]: p["value"] for p in qp}


def _header_params(node: dict) -> dict[str, str]:
    hp = node["parameters"].get("headerParameters", {}).get("parameters", [])
    return {p["name"]: p["value"] for p in hp}


@pytest.fixture()
def tmp_db(tmp_path):
    db = str(tmp_path / "test.db")
    s = Storage(db_path=db)
    s.init_db()
    s.close()
    rs = RunStorage(db_path=db)
    rs.init_db()
    rs.close()
    return db


@pytest.fixture()
def client(tmp_db):
    app.state.db_path = tmp_db
    app.state.config_dir = str(Path("config"))
    return TestClient(app)


class TestWorkflowFileStructure:
    def test_workflow_file_is_valid_json(self):
        _load_workflow()  # must not raise

    def test_exactly_one_http_request_node(self):
        workflow = _load_workflow()
        nodes = _http_request_nodes(workflow)
        assert len(nodes) == 1

    def test_http_node_targets_the_weekly_combined_endpoint(self):
        workflow = _load_workflow()
        node = _http_request_nodes(workflow)[0]
        parsed = urlsplit(node["parameters"]["url"])
        assert parsed.path == "/api/jobs/digest/weekly-combined"

    def test_http_node_requests_no_department_or_remote_filter(self):
        """Deliberately global/unsectioned — see module docstring."""
        workflow = _load_workflow()
        params = _query_params(_http_request_nodes(workflow)[0])
        assert "department" not in params
        assert "remote" not in params

    def test_http_node_requests_a_true_top_10(self):
        """Preserves the intended product behavior explicitly: a curated
        Top 10, not the endpoint's own default of 50."""
        workflow = _load_workflow()
        params = _query_params(_http_request_nodes(workflow)[0])
        assert params.get("top_limit") == "10"

    def test_format_digest_references_the_http_node(self):
        workflow = _load_workflow()
        fmt = next(n for n in workflow["nodes"] if n["name"] == "Format Digest")
        # Format Digest reads the HTTP node's output via $input.first(), the
        # implicit "previous node" reference for a single-node chain, so it
        # need not name the node explicitly — only assert the chain wires
        # the HTTP node directly into Format Digest (see the connections
        # test below).
        assert "$input.first()" in fmt["parameters"]["jsCode"]

    def test_connections_form_a_single_unbranching_chain_into_format_digest(self):
        """IF(false) -> HTTP node -> Format Digest — sequential, so
        Format Digest can rely on the HTTP call having already run."""
        workflow = _load_workflow()
        connections = workflow["connections"]
        http_name = _http_request_nodes(workflow)[0]["name"]
        assert connections[http_name]["main"][0][0]["node"] == "Format Digest"

    def test_http_node_sends_the_n8n_api_key_header(self):
        """N8N_CALLER_API_KEY auth (2026-09-12): the header must be present
        so the workflow keeps working the moment an operator sets
        N8N_CALLER_API_KEY on both sides — never hardcoded, sourced from an
        n8n instance variable so the secret itself never lives in this
        committed JSON file. Deliberately NOT named N8N_API_KEY — that name
        is already used by this project's .env for n8n's own, unrelated
        server/public-API key."""
        workflow = _load_workflow()
        headers = _header_params(_http_request_nodes(workflow)[0])
        assert "X-N8N-API-Key" in headers
        assert "$vars.N8N_CALLER_API_KEY" in headers["X-N8N-API-Key"]
        # The secret's literal value must never be committed here.
        assert "webhook" not in headers["X-N8N-API-Key"].lower()

    def test_recipient_is_an_instance_variable_not_an_address(self):
        """The digest recipient is the operator's own address: it comes from
        an n8n instance variable, so no personal address is committed (one
        was, until the public-release audit)."""
        workflow = _load_workflow()
        send = next(n for n in workflow["nodes"] if "toEmail" in n["parameters"])
        assert "$vars.DIGEST_TO_EMAIL" in send["parameters"]["toEmail"]
        text = json.dumps(workflow)
        for address in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text):
            assert address.endswith(".local"), address

    def test_no_auto_apply_node_anywhere(self):
        """The workflow must never itself apply to a job — email delivery
        only."""
        workflow = _load_workflow()
        node_types = {n["type"] for n in workflow["nodes"]}
        assert "n8n-nodes-base.httpRequest" in node_types  # sanity: nodes did load
        for node in workflow["nodes"]:
            haystack = json.dumps(node).lower()
            assert "apply" not in node["name"].lower()
            assert "/apply" not in haystack or "apply_url" in haystack  # apply_url is just a data field, not an action


class TestWorkflowParamsAgainstRealEndpoint:
    """Replays the HTTP node's exact captured params against the real
    endpoint (via TestClient, no live n8n needed) and checks the response
    shape a real n8n Format Digest step depends on."""

    def _seed(self, tmp_db: str):
        s = Storage(db_path=tmp_db)
        s.init_db()
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            """INSERT INTO jobs (id, title, location, department, countries, remote,
                                  work_mode, source, status, discovered_at, canonical_url, apply_url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "var-job", "Backend Dev", "Toulon", "83", json.dumps(["France"]), 0,
                "on_site", "test", "active", datetime.now(UTC).isoformat(), None, None,
            ),
        )
        conn.execute(
            """INSERT INTO jobs (id, title, location, department, countries, remote,
                                  work_mode, source, status, discovered_at, canonical_url, apply_url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "remote-job", "Remote Dev", "Anywhere", None, json.dumps(["France"]), 1,
                "remote", "test", "active", datetime.now(UTC).isoformat(), None, None,
            ),
        )
        conn.commit()
        conn.close()
        s.close()

        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.store_job_scores(run_id, [
            {"job_id": "var-job", "final_score": 0.85, "deterministic_score": 0.85, "semantic_score": 0.0},
            {"job_id": "remote-job", "final_score": 0.90, "deterministic_score": 0.90, "semantic_score": 0.0},
        ])
        rs.complete_run(run_id, {"personalized": 2})
        rs.close()

    def test_workflow_http_node_returns_200_with_expected_fields(self, client, tmp_db):
        self._seed(tmp_db)
        workflow = _load_workflow()
        node = _http_request_nodes(workflow)[0]
        params = _query_params(node)
        resp = client.get("/api/jobs/digest/weekly-combined", params=params)
        assert resp.status_code == 200, f"{node['name']} params {params} -> {resp.status_code}"
        body = resp.json()
        assert "top_jobs" in body
        assert "run_id" in body
        assert "run_completed_at" in body

    def test_workflow_http_node_returns_both_department_and_remote_jobs(self, client, tmp_db):
        """The global, unsectioned ranking must include jobs regardless of
        department/remote status — that's the entire point of dropping the
        old two-call split."""
        self._seed(tmp_db)
        workflow = _load_workflow()
        params = _query_params(_http_request_nodes(workflow)[0])
        resp = client.get("/api/jobs/digest/weekly-combined", params=params)
        top_ids = {j["job_id"] for j in resp.json()["top_jobs"]}
        assert top_ids == {"var-job", "remote-job"}

    def test_response_fields_needed_by_format_digest_jscode_are_present(self, client, tmp_db):
        """Format Digest reads job.rank/title/company/location/remote/
        work_mode/score_100/tier/canonical_url/apply_url/skills/source —
        every field it touches must actually exist on a WeeklyDigestJobOut."""
        self._seed(tmp_db)
        workflow = _load_workflow()
        params = _query_params(_http_request_nodes(workflow)[0])
        resp = client.get("/api/jobs/digest/weekly-combined", params=params)
        job = resp.json()["top_jobs"][0]
        required_fields = {
            "rank", "title", "company", "location", "remote", "work_mode",
            "score_100", "tier", "canonical_url", "apply_url", "skills", "source",
        }
        assert required_fields <= job.keys()
