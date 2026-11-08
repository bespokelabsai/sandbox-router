"""Hosted agent metering, persistence, isolation, and cost provenance."""

from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from bespokelabs.sandbox import RemoteSandbox, RemoteSandboxClient, Sandbox
from bespokelabs.sandbox.control_plane.agent_usage import agent_usage_record
from bespokelabs.sandbox.control_plane.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.exceptions import SandboxExecutionError
from bespokelabs.sandbox.types import (
    AgentRunResult,
    SandboxConfig,
    SandboxResult,
    Usage,
)

try:
    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane.api import create_app
except ImportError:
    TestClient = create_app = None


CODEX_OUTPUT = "\n".join(
    json.dumps(e)
    for e in [
        {"type": "thread.started", "thread_id": "demo-thread"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Done."},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1000000,
                "cached_input_tokens": 200000,
                "output_tokens": 50000,
            },
        },
    ]
)
GROK_OUTPUT = json.dumps(
    {
        "text": "Grok done.",
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_input_tokens": 300,
        },
        "total_cost_usd": 1.25,
    }
)


class AgentDashboardTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "agent.db"
        self.store = SQLiteStore(self.path, key_pepper="test")
        self.sessions = []

        def factory(backend, **config):
            session = Mock()
            session.execute_command.return_value = SandboxResult(
                stdout=CODEX_OUTPUT
            )
            self.sessions.append(session)
            runtime = Sandbox._from_session(
                backend, session, SandboxConfig(backend=backend)
            )
            runtime.estimate_compute_cost = lambda elapsed: 0.5
            return runtime

        self.service = ControlPlane(
            self.store, sandbox_factory=factory, allowed_backends=["local"]
        )
        _, self.key = self.service.bootstrap_organization("One")
        self.principal = self.store.authenticate(self.key.secret)
        _, other_key = self.service.bootstrap_organization("Other")
        self.other = self.store.authenticate(other_key.secret)
        self.sb = self.service.create_sandbox(self.principal, "local", {})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def run_agent(self, **kwargs):
        return self.service.run_agent(
            self.principal,
            self.sb.id,
            prompt="Explain",
            harness="codex",
            model="gpt-5.3-codex",
            **kwargs,
        )

    def test_estimate_cache_split_persistence_and_idempotency(self):
        first = self.run_agent(request_id="once")
        second = self.run_agent(request_id="once")
        self.assertEqual(first, second)
        self.sessions[0].execute_command.assert_called_once()
        usage = first["agent_usage"]
        self.assertEqual(usage["input_tokens"], 800000)
        self.assertEqual(usage["cache_read_tokens"], 200000)
        self.assertEqual(usage["total_tokens"], 1050000)
        self.assertEqual(Decimal(usage["cost_usd"]), Decimal("2.135"))
        self.assertEqual(usage["cost_state"], "estimated")
        self.assertEqual(usage["pricing_as_of"], "2026-11-07")
        self.assertEqual(usage["rates_per_million"]["cached_input"], "0.175")
        self.assertEqual(first["usage"]["provider_cost_usd"], "0.5")
        reopened = SQLiteStore(self.path, key_pepper="test")
        fresh_service = ControlPlane(reopened, allowed_backends=["local"])
        replay = fresh_service.run_agent(
            self.principal,
            self.sb.id,
            prompt="Explain",
            harness="codex",
            model="gpt-5.3-codex",
            request_id="once",
        )
        self.assertEqual(replay, first)
        detail = fresh_service.sandbox_detail(self.principal, self.sb.id)
        self.assertEqual(detail["agent_usage"]["runs"], 1)
        self.assertEqual(detail["agent_runs"][0]["cost_state"], "estimated")
        self.assertEqual(detail["agent_usage"]["estimated_cost_usd"], "2.135")
        with reopened._connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM agent_usage"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM usage_events"
                ).fetchone()[0],
                1,
            )

    def test_mixed_reported_estimated_unknown_and_date_filters(self):
        self.run_agent()
        self.sessions[0].execute_command.return_value = SandboxResult(
            stdout=GROK_OUTPUT, exit_code=1
        )
        response = self.service.run_agent(
            self.principal, self.sb.id, prompt="Explain", harness="grok"
        )
        self.assertEqual(response["exit_code"], 1)
        self.assertEqual(response["agent_usage"]["cost_state"], "reported")
        self.sessions[0].execute_command.return_value = SandboxResult(
            stdout="CLI crashed", exit_code=1
        )
        self.run_agent()
        for group in ("sandbox", "backend", "day"):
            rows = self.service.cost_summary(self.principal, group_by=group)
            usage = next(
                r["agent_usage"] for r in rows if r["agent_usage"]["runs"]
            )
            self.assertEqual(usage["runs"], 3)
            self.assertEqual(usage["total_tokens"], 1051500)
            self.assertEqual(usage["reported_cost_usd"], "1.25")
            self.assertEqual(usage["estimated_cost_usd"], "2.135")
            self.assertEqual(usage["unknown_cost_runs"], 1)
            self.assertEqual(usage["unknown_usage_runs"], 1)
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE agent_usage SET created_at='2026-01-02T00:00:00+00:00'"
            )
        inclusive = self.store.summarize_agent_usage(
            self.principal.organization_id,
            start="2026-01-02T00:00:00Z",
            end="2026-01-03T00:00:00Z",
        )
        self.assertEqual(inclusive[self.sb.id]["runs"], 3)
        exclusive = self.store.summarize_agent_usage(
            self.principal.organization_id, end="2026-01-02T00:00:00Z"
        )
        self.assertEqual(exclusive, {})
        with self.assertRaises(ValueError):
            self.store.summarize_agent_usage(
                self.principal.organization_id,
                start="2026-01-03T00:00:00Z",
                end="2026-01-02T00:00:00Z",
            )

    def test_isolation_read_scope_and_request_conflicts(self):
        self.run_agent(request_id="once")
        with self.assertRaises(NotFoundError):
            self.service.sandbox_detail(self.other, self.sb.id)
        with self.assertRaises(NotFoundError):
            self.service.run_agent(self.other, self.sb.id, prompt="Explain")
        self.assertEqual(self.service.cost_summary(self.other), [])
        key = self.service.issue_api_key(
            self.principal, name="Inventory only", scopes=["sandboxes:read"]
        )
        reader = self.store.authenticate(key.secret)
        detail = self.service.sandbox_detail(reader, self.sb.id)
        self.assertIsNone(detail["agent_usage"])
        self.assertEqual(detail["agent_runs"], [])
        self.assertNotIn("agent_usage", detail["executions"][0]["response"])
        with self.assertRaises(AuthorizationError):
            self.service.run_agent(reader, self.sb.id, prompt="Explain")
        with self.assertRaises(ConflictError):
            self.service.run_agent(
                self.principal,
                self.sb.id,
                prompt="Different",
                harness="codex",
                model="gpt-5.3-codex",
                request_id="once",
            )
        with self.assertRaises(ConflictError):
            self.service.execute(
                self.principal, self.sb.id, command="echo", request_id="once"
            )
        self.assertEqual(self.sessions[0].execute_command.call_count, 1)

    def test_provider_failure_is_unknown_and_not_rerun(self):
        self.sessions[0].execute_command.side_effect = RuntimeError(
            "Disconnected"
        )
        with self.assertRaises(SandboxExecutionError):
            self.run_agent(request_id="failed")
        usage = self.store.agent_runs(self.principal.organization_id)[0]
        self.assertEqual(usage["cost_state"], "unavailable")
        self.assertFalse(usage["tokens_known"])
        self.assertIsNone(usage["cost_usd"])
        with self.assertRaises(ConflictError):
            self.run_agent(request_id="failed")
        self.assertEqual(self.sessions[0].execute_command.call_count, 1)

    @unittest.skipIf(TestClient is None, "server dependencies missing")
    def test_http_and_remote_client_end_to_end(self):
        client = TestClient(create_app(self.service))
        remote = RemoteSandboxClient("https://testserver", self.key.secret)

        def request(method, path, payload=None, *, idempotency_key=None):
            headers = {"Authorization": f"Bearer {self.key.secret}"}
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            response = client.request(
                method, path, json=payload, headers=headers
            )
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        with patch.object(remote, "_request", side_effect=request):
            sb = RemoteSandbox(remote, self.sb.id, "local")
            result = sb.run_agent(
                "Explain",
                harness="codex",
                model="gpt-5.3-codex",
                idempotency_key="http-once",
            )
            self.assertEqual(result.text, "Done.")
            self.assertEqual(result.agent_usage["cost_state"], "estimated")
            self.assertEqual(
                sb.run_agent(
                    "Explain",
                    harness="codex",
                    model="gpt-5.3-codex",
                    idempotency_key="http-once",
                ),
                result,
            )
            costs = remote.costs()
            self.assertEqual(
                costs["items"][0]["agent_usage"]["estimated_cost_runs"], 1
            )
        self.assertEqual(
            client.post(
                f"/v1/sandboxes/{self.sb.id}/agent", json={"prompt": "x"}
            ).status_code,
            401,
        )
        response = client.post(
            f"/v1/sandboxes/{self.sb.id}/agent",
            json={"prompt": "x", "harness": "bad"},
            headers={"Authorization": f"Bearer {self.key.secret}"},
        )
        self.assertEqual(response.status_code, 422)
        client.close()


class EstimateTests(unittest.TestCase):

    def result(self, raw=None):
        return AgentRunResult(
            usage=Usage(
                input_tokens=100, output_tokens=20, cache_read_tokens=50
            ),
            raw=raw or {"usage": {"input_tokens": 100, "output_tokens": 20}},
        )

    def test_unknown_models_incomplete_usage_and_overrides_are_not_priced(self):
        for model, kwargs in [
            (None, {}),
            ("future-model", {}),
            ("gpt-5.3-codex", {"extra_args": ["-m", "other"]}),
            (
                "gpt-5.3-codex",
                {"extra_args": ['--config=model_provider="custom"']},
            ),
        ]:
            row = agent_usage_record(
                self.result(), harness="codex", model=model, **kwargs
            )
            self.assertIsNone(row["cost_usd"])
            self.assertEqual(row["cost_state"], "unavailable")
        row = agent_usage_record(
            self.result(
                {"usage": {"input_tokens": 100}, "usage_is_incomplete": True}
            ),
            harness="codex",
            model="gpt-5.3-codex",
        )
        self.assertIsNone(row["cost_usd"])
        self.assertTrue(row["usage_is_incomplete"])

    def test_reported_zero_and_missing_cost_are_different(self):
        for value, expected in [(0, "reported"), (None, "unavailable")]:
            row = agent_usage_record(
                self.result(
                    {"usage": {"input_tokens": 100}, "total_cost_usd": value}
                ),
                harness="grok",
                model=None,
            )
            self.assertEqual(row["cost_state"], expected)
        row = agent_usage_record(
            self.result(
                {
                    "usage": {"input_tokens": 100},
                    "total_cost_usd": 1,
                    "cost_is_partial": True,
                }
            ),
            harness="grok",
            model=None,
        )
        self.assertIsNone(row["cost_usd"])

    def test_malformed_or_missing_source_cost_is_not_reported_zero(self):
        from bespokelabs.sandbox._usage import parse_grok_result

        for cost in (None, True, float("nan"), float("inf"), -1, "unknown"):
            raw = parse_grok_result(
                json.dumps({"text": "Done", "total_cost_usd": cost})
            )
            row = agent_usage_record(
                self.result(raw), harness="grok", model=None
            )
            self.assertEqual(row["cost_state"], "unavailable")
            self.assertIsNone(row["cost_usd"])

    def test_incomplete_codex_run_has_no_full_cost_estimate(self):
        raw = {
            "events": [
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                },
                {"type": "turn.started"},
                {"type": "turn.failed"},
            ]
        }
        row = agent_usage_record(
            self.result(raw), harness="codex", model="gpt-5.3-codex"
        )
        self.assertTrue(row["usage_is_incomplete"])
        self.assertIsNone(row["cost_usd"])

    def test_reported_cost_wins_over_codex_estimate(self):
        result = self.result(
            {"usage": {"input_tokens": 100}, "total_cost_usd": 0.75}
        )
        row = agent_usage_record(result, harness="codex", model="gpt-5.3-codex")
        self.assertEqual(row["cost_state"], "reported")
        self.assertEqual(row["cost_usd"], "0.75")
