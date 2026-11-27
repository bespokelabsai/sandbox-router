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


def tasks(**changes):
    return {
        "count": 4,
        "succeeded": 2,
        "failed": 1,
        "other": 1,
        "failures": [{"index": 3, "exit_code": 7, "reason": "exit_code"}],
        "truncated": False,
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
        status(result=terminal(reason="zone-secret has no capacity")),
        status(result=terminal(tasks={"count": 2})),
        status(result=terminal(tasks=tasks(private="raw-secret"))),
        status(result=terminal(tasks=tasks(count=-1))),
        status(result=terminal(tasks=tasks(truncated=1))),
        status(
            result=terminal(
                tasks=tasks(
                    failures=[
                        {"index": 0, "exit_code": 1, "reason": "raw-secret"}
                    ]
                )
            )
        ),
        status(
            result=terminal(
                tasks=tasks(
                    failures=[
                        {
                            "index": 0,
                            "exit_code": 1,
                            "reason": None,
                            "message": "raw-secret",
                        }
                    ]
                )
            )
        ),
        status(
            result=terminal(
                tasks=tasks(
                    failures=[
                        {"index": i, "exit_code": 1, "reason": None}
                        for i in range(21)
                    ]
                )
            )
        ),
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


def call_every_method(client, queued):
    queued.extend(
        [
            (202, status()),
            (200, status()),
            (200, {"result": None}),
            (200, {"logs": ""}),
            (200, status(cancel_requested=True)),
        ]
    )
    client.submit(JobSpec("true"), binding_id="batch", idempotency_key="key-1")
    client.status("hjob_synthetic")
    client.result("hjob_synthetic")
    client.logs("hjob_synthetic")
    client.cancel("hjob_synthetic")


def test_absent_extra_headers_send_exactly_the_existing_headers(transport):
    client, calls, queued = transport
    call_every_method(client, queued)
    assert [list(r.header_items()) for r, _ in calls] == [
        [
            ("Authorization", "Bearer test-key"),
            ("Accept", "application/json"),
            ("Content-type", "application/json"),
            ("Idempotency-key", "key-1"),
        ],
        *[
            [
                ("Authorization", "Bearer test-key"),
                ("Accept", "application/json"),
            ]
        ]
        * 4,
    ]


def test_static_extra_headers_sent_on_every_method(transport):
    _, calls, queued = transport
    supplied = {"X-Platform-Token": "platform-secret", "x-trace": "a b"}
    client = RemoteJobClient(
        "https://jobs.example.test", "test-key", extra_headers=supplied
    )
    supplied["X-Later"] = "ignored"  # The mapping is copied at construction.
    call_every_method(client, queued)
    assert len(calls) == 5
    for request, _ in calls:
        assert request.get_header("X-platform-token") == "platform-secret"
        assert request.get_header("X-trace") == "a b"
        assert request.get_header("X-later") is None
        assert request.get_header("Authorization") == "Bearer test-key"
        assert request.get_header("Accept") == "application/json"
    assert calls[0][0].get_header("Content-type") == "application/json"
    assert calls[0][0].get_header("Idempotency-key") == "key-1"
    assert "platform-secret" not in repr(client)


def test_callable_extra_headers_evaluated_per_request_and_on_retry(transport):
    _, calls, queued = transport
    issued = []

    def fresh():
        issued.append(f"token-{len(issued) + 1}")
        return {"X-Platform-Token": issued[-1]}

    client = RemoteJobClient(
        "https://jobs.example.test", "test-key", extra_headers=fresh
    )
    assert issued == []  # Not evaluated until a request is made.
    call_every_method(client, queued)
    # The client never retries; a caller's replay gets a fresh evaluation.
    queued.extend([TimeoutError("synthetic"), (202, status())])
    for _ in range(2):
        try:
            client.submit(
                JobSpec("true"), binding_id="batch", idempotency_key="key-1"
            )
        except RemoteJobError as exc:
            assert exc.retryable
    assert [r.get_header("X-platform-token") for r, _ in calls] == [
        f"token-{n}" for n in range(1, 8)
    ]
    assert issued[-1] not in repr(client)


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "authorization",
        "AUTHORIZATION",
        "aUtHoRiZaTiOn",
        "Idempotency-Key",
        "idempotency-key",
        "IDEMPOTENCY-KEY",
        "Content-Type",
        "content-type",
        "Accept",
        "ACCEPT",
        "Host",
        "content-length",
        "Transfer-Encoding",
        "connection",
        "User-Agent",
    ],
)
def test_extra_headers_cannot_override_client_owned_headers(transport, name):
    _, calls, queued = transport
    supplied = {"X-Ok": "fine", name: "override-secret"}
    with pytest.raises(RemoteJobError) as caught:
        RemoteJobClient(
            "https://jobs.example.test", "test-key", extra_headers=supplied
        )
    assert name.lower() in str(caught.value)
    assert "override-secret" not in "".join(
        traceback.format_exception(caught.value)
    )
    client = RemoteJobClient(
        "https://jobs.example.test", "test-key", extra_headers=lambda: supplied
    )
    with pytest.raises(RemoteJobError) as caught:
        client.status("hjob_synthetic")
    assert caught.value.code == "extra_headers" and not caught.value.retryable
    assert "override-secret" not in "".join(
        traceback.format_exception(caught.value)
    )
    assert calls == []


@pytest.mark.parametrize(
    "supplied",
    [
        {"X-Token": "secret-value\r\nX-Injected: 1"},
        {"X-Token": "secret-value\n"},
        {"X-Token": "secret-value\r"},
        {"X-Token\r\nX-Injected": "secret-value"},
        {"X-Token\n": "secret-value"},
        {"X Token": "secret-value"},
        {"X-Token:": "secret-value"},
        {"": "secret-value"},
        {"X-Token": ""},
        {"X-Token": "secret-value\x00"},
        {"X-Token": "secret-value\u00e9"},
        {"X-Token": b"secret-value"},
        {"X-Token": None},
        {b"X-Token": "secret-value"},
        {"X-Token": "secret-value", "x-token": "secret-value"},
        {"N" * 257: "secret-value"},
        {"X-Token": "secret-value" + "v" * 8192},
        {f"X-Token-{n}": "secret-value" for n in range(33)},
        [("X-Token", "secret-value")],
        "X-Token: secret-value",
        7,
    ],
)
def test_invalid_extra_headers_rejected_without_reflection(transport, supplied):
    _, calls, queued = transport
    with pytest.raises(RemoteJobError) as caught:
        RemoteJobClient(
            "https://jobs.example.test", "test-key", extra_headers=supplied
        )
    static = "".join(traceback.format_exception(caught.value))
    client = RemoteJobClient(
        "https://jobs.example.test", "test-key", extra_headers=lambda: supplied
    )
    with pytest.raises(RemoteJobError) as caught:
        client.cancel("hjob_synthetic")
    assert isinstance(caught.value, RemoteSandboxError)
    dynamic = "".join(traceback.format_exception(caught.value))
    for text in (static, dynamic):
        assert "secret-value" not in text and "X-Token" not in text
    assert calls == []


def test_extra_headers_bounds_are_inclusive(transport):
    _, calls, queued = transport
    client = RemoteJobClient(
        "https://jobs.example.test",
        "test-key",
        extra_headers={
            "N" * 256: "v" * 8192,
            **{f"X-{n}": "\tv" for n in range(31)},
        },
    )
    queued.append((200, status()))
    client.status("hjob_synthetic")
    assert calls[0][0].get_header("N" + "n" * 255) == "v" * 8192
    assert RemoteJobClient(
        "https://jobs.example.test", "test-key", extra_headers={}
    )


def test_extra_headers_callable_failure_is_sanitized_and_sends_nothing(
    transport,
):
    _, calls, queued = transport

    class TokenUnavailable(Exception):
        pass

    def failing():
        raise TokenUnavailable("credential-secret")

    client = RemoteJobClient(
        "https://jobs.example.test", "test-key", extra_headers=failing
    )
    with pytest.raises(RemoteJobError) as caught:
        client.submit(
            JobSpec("true"), binding_id="batch", idempotency_key="key-1"
        )
    assert "TokenUnavailable" in str(caught.value)
    assert caught.value.code == "extra_headers" and not caught.value.retryable
    assert caught.value.__suppress_context__
    assert "credential-secret" not in "".join(
        traceback.format_exception(caught.value)
    )
    assert "credential-secret" not in repr(caught.value) + repr(client)
    assert calls == []


def test_extra_headers_reach_a_real_server_with_owned_headers_intact():
    seen = []

    class Platform(BaseHTTPRequestHandler):

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append(self.headers)
            body = json.dumps(status()).encode()
            self.send_response(202 if self.path == "/v1/jobs" else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST

        def log_message(self, *args):
            pass

    tokens = iter(["token-1", "token-2"])
    with ThreadingHTTPServer(("127.0.0.1", 0), Platform) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = RemoteJobClient(
                f"http://127.0.0.1:{server.server_port}",
                "synthetic-key",
                trust_env=False,
                extra_headers=lambda: {"X-Platform-Token": next(tokens)},
            )
            job = client.submit(
                JobSpec("true"), binding_id="batch", idempotency_key="key-1"
            )
            client.status(job.id)
        finally:
            server.shutdown()
            worker.join(5)
    assert [h.get_all("X-Platform-Token") for h in seen] == [
        ["token-1"],
        ["token-2"],
    ]
    assert [h.get_all("Authorization") for h in seen] == [
        ["Bearer synthetic-key"]
    ] * 2
    assert seen[0].get_all("Idempotency-Key") == ["key-1"]
    assert seen[0].get_all("Content-Type") == ["application/json"]
    assert seen[0].get_all("Host") == [f"127.0.0.1:{server.server_port}"]


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


@pytest.mark.parametrize("reason", ["quota_exceeded", "capacity_unavailable"])
def test_capacity_reasons_are_surfaced(transport, reason):
    client, _, queued = transport
    queued.append((200, {"result": terminal(state="failed", reason=reason)}))
    result = client.result("hjob_synthetic")
    assert result.reason == reason and result.exit_code is None
    # A single-task response has no tasks key at all.
    assert result.tasks is None


def test_multi_task_result_is_typed(transport):
    client, _, queued = transport
    failures = [
        {"index": 1, "exit_code": 7, "reason": "exit_code"},
        {"index": 3, "exit_code": None, "reason": "capacity_unavailable"},
    ]
    summary = tasks(failed=2, other=0, failures=failures, truncated=True)
    queued.extend(
        [
            (200, {"result": terminal(state="failed", tasks=summary)}),
            (
                200,
                status(
                    phase="terminal",
                    state="failed",
                    result=terminal(state="failed", tasks=summary),
                ),
            ),
        ]
    )
    result = client.result("hjob_synthetic")
    assert (result.tasks.count, result.tasks.succeeded) == (4, 2)
    assert (result.tasks.failed, result.tasks.other) == (2, 0)
    assert result.tasks.truncated is True
    assert [
        (f.index, f.exit_code, f.reason) for f in result.tasks.failures
    ] == [(1, 7, "exit_code"), (3, None, "capacity_unavailable")]
    assert client.status("hjob_synthetic").result == result
