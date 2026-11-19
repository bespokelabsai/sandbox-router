"""Hosted-job client tests with no optional server/provider dependency."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import traceback
import urllib.error
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from bespokelabs.sandbox import (
    JobSpec,
    JobState,
    RemoteJobClient,
    RemoteJobError,
    RemoteJobResult,
    RemoteJobStatus,
    RemoteSandboxError,
    remote_jobs,
)


def status(**changes):
    return {
        "id": "hjob_synthetic",
        "backend": "gcp_batch",
        "phase": "active",
        "state": "running",
        "confidence": "verified",
        "cancel_requested": False,
        "error_code": None,
        "result": None,
        **changes,
    }


def terminal(**changes):
    return {
        "state": "succeeded",
        "exit_code": None,
        "reason": None,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": None,
        **changes,
    }


class Response(io.BytesIO):
    status = 200


@pytest.fixture
def transport(monkeypatch):
    calls = []
    queued = []

    def open_request(request, *, timeout):
        calls.append((request, timeout))
        value = queued.pop(0)
        if isinstance(value, Exception):
            raise value
        code, value = value
        response = Response(
            value if isinstance(value, bytes) else json.dumps(value).encode()
        )
        response.status = code
        return response

    opener = SimpleNamespace(open=open_request)
    monkeypatch.setattr(
        remote_jobs.urllib.request, "build_opener", lambda *args: opener
    )
    return (
        RemoteJobClient("https://jobs.example.test/gateway", "test-key"),
        calls,
        queued,
    )


def test_typed_routes_payload_and_explicit_headers(transport):
    client, calls, queued = transport
    spec = JobSpec(["printf", "hello"], env_vars={"TOKEN": "runtime-secret"})
    queued.extend(
        [
            (202, status()),
            (200, status()),
            (200, {"result": terminal()}),
            (200, {"logs": "workload output"}),
            (200, status(cancel_requested=True)),
        ]
    )
    job = client.submit(spec, binding_id="batch", idempotency_key="operation-1")
    assert isinstance(job, RemoteJobStatus) and job.state is JobState.RUNNING
    assert client.status(job.id) == job
    result = client.result(job.id)
    assert isinstance(result, RemoteJobResult) and result.exit_code is None
    assert client.logs(job.id, tail=10) == "workload output"
    cancellation = client.cancel(job.id)
    assert cancellation.cancel_requested and cancellation.phase == "active"
    assert cancellation.result is None
    paths = [
        "",
        "/hjob_synthetic",
        "/hjob_synthetic/result",
        "/hjob_synthetic/logs?tail=10",
        "/hjob_synthetic/cancel",
    ]
    for (request, timeout), suffix in zip(calls, paths, strict=True):
        assert (
            request.full_url
            == "https://jobs.example.test/gateway/v1/jobs" + suffix
        )
        assert timeout == 60
        assert request.get_header("Authorization") == "Bearer test-key"
        assert request.get_header("Cookie") is None
        assert request.get_header("X-csrf-token") is None
    assert [r.method for r, _ in calls] == ["POST", "GET", "GET", "GET", "POST"]
    assert json.loads(calls[0][0].data) == {
        "binding_id": "batch",
        "spec": asdict(spec),
    }
    assert calls[0][0].get_header("Idempotency-key") == "operation-1"
    assert all(r.get_header("Idempotency-key") is None for r, _ in calls[1:])


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("credential-secret"),
        urllib.error.URLError("url-secret"),
        ConnectionResetError("body-secret"),
    ],
)
def test_transport_failure_unknown_no_retry_or_cancel(transport, failure):
    client, calls, queued = transport
    queued.append(failure)
    with pytest.raises(RemoteJobError) as caught:
        client.submit(
            JobSpec("true"), binding_id="batch", idempotency_key="saved-key"
        )
    assert isinstance(caught.value, RemoteSandboxError)
    assert caught.value.outcome == "unknown" and caught.value.retryable
    assert len(calls) == 1
    assert caught.value.__suppress_context__
    assert "secret" not in "".join(traceback.format_exception(caught.value))
    queued.append(
        (
            202,
            status(
                phase="uncertain",
                state=None,
                confidence="missing",
                error_code="submit_unconfirmed",
            ),
        )
    )
    replay = client.submit(
        JobSpec("true"), binding_id="batch", idempotency_key="saved-key"
    )
    assert (
        replay.phase == "uncertain"
        and replay.state is None
        and replay.result is None
    )
    assert [r.get_header("Idempotency-key") for r, _ in calls] == [
        "saved-key",
        "saved-key",
    ]


def test_http_errors_are_bounded_closed_and_not_reflected(transport):
    client, calls, queued = transport

    class ErrorBody(io.BytesIO):
        read_size = None

        def read(self, size=-1):
            self.read_size = size
            return super().read(size)

    body = ErrorBody(b"credential-secret" * 10000)
    queued.append(
        urllib.error.HTTPError(
            "https://url-secret", 409, "header-secret", {}, body
        )
    )
    with pytest.raises(RemoteJobError) as caught:
        client.status("hjob_synthetic")
    assert caught.value.status_code == 409 and caught.value.outcome == "unknown"
    assert body.read_size == 4096 and body.closed
    assert "secret" not in "".join(traceback.format_exception(caught.value))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json-secret",
        b"[]",
        b"null",
        b'{"id": "body-secret"}',
        status(id="hjob_synthetic\n"),
        status(cancel_requested=1),
        status(state="invented"),
        status(phase="invented"),
        status(confidence="invented"),
        status(error_code="raw-secret"),
        status(private="metadata-secret"),
        status(result=terminal(exit_code=True)),
        status(result=terminal(exit_code=2**31)),
        status(result=terminal(started_at="2026-01-01")),
        status(result=terminal(state="unknown")),
        status(result=terminal(private="raw-secret")),
    ],
)
def test_malformed_or_extra_response_is_sanitized(transport, payload):
    client, _, queued = transport
    queued.append((200, payload))
    with pytest.raises(RemoteJobError) as caught:
        client.status("hjob_synthetic")
    assert (
        caught.value.code == "invalid_response"
        and caught.value.outcome == "unknown"
    )
    assert "secret" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "state,phase,confidence,error_code",
    [
        (None, "submitting", "unobserved", None),
        (None, "uncertain", "missing", "provider_missing"),
        ("running", "active", "unavailable", "provider_unavailable"),
        ("unknown", "uncertain", "ownership_conflict", "ownership_conflict"),
    ],
)
def test_observation_uncertainty_and_null_result_preserved(
    transport, state, phase, confidence, error_code
):
    client, _, queued = transport
    queued.extend(
        [
            (
                200,
                status(
                    state=state,
                    phase=phase,
                    confidence=confidence,
                    error_code=error_code,
                ),
            ),
            (200, {"result": None}),
        ]
    )
    job = client.status("hjob_synthetic")
    assert (
        job.phase == phase
        and job.confidence == confidence
        and job.error_code == error_code
    )
    assert (job.state.value if job.state else None) == state
    assert job.result is None and not hasattr(job, "done")
    assert client.result(job.id) is None


@pytest.mark.parametrize(
    "value",
    [
        "../x",
        "a/b",
        "%2e%2e",
        "%252f",
        "x?tail=0",
        "x#f",
        "https://other.test",
        "x\\y",
        "x\n",
        "",
        "x" * 256,
        None,
    ],
)
def test_invalid_ids_never_reach_transport(transport, value):
    client, calls, _ = transport
    for operation in (client.status, client.result, client.logs, client.cancel):
        with pytest.raises(ValueError):
            operation(value)
    assert calls == []


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ftp://host",
        "https://user:secret@host",
        "https://host/a/../b",
        "https://host/%2e%2e",
        "https://host?token=secret",
        "https://host#fragment",
        "https://host?",
        "https://host#",
        "https://host:bad",
        "https://host:0",
        "https://host\\other",
        "https://host\n",
        " https://host",
        "https://",
        "https://host//prefix",
    ],
)
def test_invalid_base_url(value):
    with pytest.raises(ValueError, match="Invalid base_url"):
        RemoteJobClient(value, "key")


@pytest.mark.parametrize(
    "timeout", [0, -1, float("inf"), float("nan"), True, "60"]
)
def test_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        RemoteJobClient(
            "https://jobs.example.test", "key", timeout_secs=timeout
        )


@pytest.mark.parametrize(
    "key",
    ["", "spaces disallowed", "\r\nInjected: secret", "é", "x" * 513, None],
)
def test_submit_requires_existing_header_semantics(transport, key):
    client, calls, _ = transport
    with pytest.raises(ValueError):
        client.submit(JobSpec("true"), binding_id="batch", idempotency_key=key)
    assert calls == []


def test_submit_key_required_and_payload_bounded(transport):
    client, calls, _ = transport
    with pytest.raises(TypeError):
        client.submit(JobSpec("true"), binding_id="batch")
    for spec in (
        JobSpec("x" * 65536),
        JobSpec("true", backend_options={"x": float("nan")}),
    ):
        with pytest.raises(ValueError):
            client.submit(spec, binding_id="batch", idempotency_key="key")
    for tail in (-1, 1001, True, "1", None):
        with pytest.raises(ValueError):
            client.logs("hjob_synthetic", tail=tail)
    assert calls == []


def test_response_and_log_byte_limits(transport):
    client, _, queued = transport
    queued.append((200, b" " * (remote_jobs._MAX_RESPONSE_BYTES + 1)))
    with pytest.raises(RemoteJobError, match="oversized"):
        client.status("hjob_synthetic")
    queued.append((200, {"logs": "é" * (1024 * 1024)}))
    with pytest.raises(RemoteJobError, match="oversized"):
        client.logs("hjob_synthetic")
    queued.append((200, {"logs": "\x00" * (1024 * 1024)}))
    assert len(client.logs("hjob_synthetic")) == 1024 * 1024


def test_proxy_options_and_redirect_handler(monkeypatch):
    captured = []
    monkeypatch.setenv("https_proxy", "http://proxy.example.test:8080")
    monkeypatch.setattr(
        remote_jobs.urllib.request,
        "build_opener",
        lambda *handlers: captured.append(handlers),
    )
    RemoteJobClient("https://jobs.example.test", "key", trust_env=False)
    RemoteJobClient("https://jobs.example.test", "key")
    assert captured[0][0].proxies == {}
    assert captured[1][0].proxies["https"] == "http://proxy.example.test:8080"
    assert (
        captured[0][1].redirect_request(
            None, None, 307, "", {}, "https://other.test"
        )
        is None
    )


@pytest.mark.parametrize("cross_origin", [False, True])
def test_real_redirects_never_forward_credentials_or_replay(cross_origin):
    requests = []
    target_requests = []

    class Target(BaseHTTPRequestHandler):

        def do_POST(self):
            target_requests.append(self.headers.get("Authorization"))
            self.send_response(500)
            self.end_headers()

        do_GET = do_POST

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Target) as target:
        target_worker = threading.Thread(
            target=target.serve_forever, daemon=True
        )
        target_worker.start()

        class Redirect(Target):
            code = 307

            def do_POST(self):
                requests.append(self.headers.get("Authorization"))
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(self.code)
                destination = (
                    target.server_port
                    if cross_origin
                    else self.server.server_port
                )
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{destination}/redirect-target",
                )
                self.send_header("Content-Length", "0")
                self.end_headers()

        with ThreadingHTTPServer(("127.0.0.1", 0), Redirect) as source:
            worker = threading.Thread(target=source.serve_forever, daemon=True)
            worker.start()
            try:
                client = RemoteJobClient(
                    f"http://127.0.0.1:{source.server_port}",
                    "synthetic-key",
                    trust_env=False,
                )
                for code in (301, 302, 303, 307, 308):
                    Redirect.code = code
                    with pytest.raises(RemoteJobError) as caught:
                        client.submit(
                            JobSpec("true"),
                            binding_id="batch",
                            idempotency_key="same-key",
                        )
                    assert caught.value.status_code == code
                assert requests == ["Bearer synthetic-key"] * 5
                assert target_requests == []
            finally:
                source.shutdown()
                worker.join(5)
                target.shutdown()
                target_worker.join(5)


def test_cancel_timeout_is_unknown_not_completion(transport):
    client, calls, queued = transport
    queued.append(TimeoutError("synthetic-secret"))
    with pytest.raises(RemoteJobError) as caught:
        client.cancel("hjob_synthetic")
    assert caught.value.outcome == "unknown" and len(calls) == 1


def test_unexpected_success_status_is_not_accepted(transport):
    client, calls, queued = transport
    queued.append((200, status()))  # Submission requires the existing 202 DTO.
    with pytest.raises(RemoteJobError) as caught:
        client.submit(
            JobSpec("true"), binding_id="batch", idempotency_key="same-key"
        )
    assert caught.value.status_code == 200 and caught.value.outcome == "unknown"
    assert len(calls) == 1


def test_default_import_and_new_client_are_server_provider_free():
    script = """
import sys
before = set(sys.modules)
from bespokelabs.sandbox import RemoteJobClient, RemoteJobStatus, JobSpec
RemoteJobClient('https://jobs.example.test', 'synthetic-key', trust_env=False)
assert not any(n.startswith('bespokelabs.sandbox.control_plane') for n in sys.modules)
assert not any(n.split('.')[0] in {'fastapi', 'uvicorn', 'google', 'modal', 'daytona', 'tensorlake', 'e2b_code_interpreter'} for n in set(sys.modules) - before)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
