"""Explicitly opt-in Novita-only acceptance test; collection is offline.

Run only this file, never the provider-probing regression suite. Requires
NOVITA_RUN_E2E=1, NOVITA_API_KEY, NOVITA_DOMAIN and NOVITA_E2E_TEMPLATE.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NOVITA_RUN_E2E") != "1",
    reason="Live Novita test requires explicit NOVITA_RUN_E2E=1",
)


@pytest.mark.parametrize(
    "protected", [False, True], ids=["public", "protected"]
)
def test_novita_live_acceptance(tmp_path, record_property, protected):
    # An opt-in without credentials is still unexecuted, never acceptance.
    for name in ("NOVITA_API_KEY", "NOVITA_DOMAIN", "NOVITA_E2E_TEMPLATE"):
        if not os.environ.get(name):
            pytest.skip(f"Live E2E requires {name}; acceptance not executed")

    import httpx
    from novita_sandbox import Novita, SandboxNotFoundException

    from bespokelabs.sandbox import Sandbox, SandboxClient, SandboxSessionState

    domain = os.environ["NOVITA_DOMAIN"]
    template = os.environ["NOVITA_E2E_TEMPLATE"]
    record_property("sdk_version", importlib.metadata.version("novita-sandbox"))
    record_property("domain", domain)
    record_property("template", template)
    owner = f"sandbox-router-e2e-{uuid.uuid4().hex}"
    provider = Novita()
    sandbox = None
    sandbox_id = None
    acceptance_ready = False
    try:
        sandbox = SandboxClient("novita").create(
            template=template,
            timeout_secs=300,
            backend_options={
                "metadata": {"sandbox-router-e2e": owner},
                "network": {"allow_public_traffic": not protected},
                "lifecycle": {"on_timeout": "kill", "auto_resume": False},
            },
        )
        sandbox_id = sandbox.provider_resource_id
        record_property("sandbox_id", sandbox_id)
        result = sandbox.execute_code(
            "router_marker = 41\nprint(router_marker + 1)"
        )
        assert result.exit_code == 0 and result.stdout.strip() == "42"
        assert sandbox.execute_code("raise ValueError('e2e')").exit_code != 0
        result = sandbox.execute_command("printf shell")
        assert result.exit_code == 0 and result.stdout == "shell"
        result = sandbox.execute_command("printf out; printf err >&2; exit 7")
        assert (result.stdout, result.stderr, result.exit_code) == (
            "out",
            "err",
            7,
        )

        for name, payload in (("binary", b"\x00\xff\x80\xfe"), ("empty", b"")):
            remote = f"/tmp/router-e2e/{name}"
            sandbox.write_file(remote, payload)
            assert sandbox.read_file(remote) == payload
            source, target = tmp_path / f"{name}-in", tmp_path / f"{name}-out"
            source.write_bytes(payload)
            sandbox.upload_file(str(source), remote + "-upload")
            sandbox.download_file(remote + "-upload", str(target))
            assert target.read_bytes() == payload
        entries = sandbox.list_files("/tmp/router-e2e")
        assert any(
            entry.path.endswith("/binary")
            and not entry.is_dir
            and entry.size == 4
            for entry in entries
        )
        assert any(
            entry.path.endswith("/router-e2e") and entry.is_dir
            for entry in sandbox.list_files("/tmp")
        )

        sandbox.write_file("/tmp/router-e2e/index.html", "router-service-ready")
        result = sandbox.execute_command(
            "nohup python3 -m http.server 8080 --bind 0.0.0.0 "
            "--directory /tmp/router-e2e >/tmp/router-e2e-http.log 2>&1 </dev/null &"
        )
        assert result.exit_code == 0

        def check_service(attached):
            exposed = attached.expose_port(8080)
            if protected:
                assert bool(exposed.headers.get("Sandbox-Traffic-Access-Token"))
                assert set(exposed.headers) == {"Sandbox-Traffic-Access-Token"}
            deadline = time.monotonic() + 30
            with httpx.Client(timeout=5) as http:
                while time.monotonic() < deadline:
                    try:
                        response = http.get(
                            exposed.url, headers=exposed.headers
                        )
                        if (
                            response.status_code == 200
                            and response.text == "router-service-ready"
                        ):
                            if protected:
                                for headers in (
                                    {},
                                    {
                                        "Sandbox-Traffic-Access-Token": "invalid-e2e-token"
                                    },
                                ):
                                    denied = http.get(
                                        exposed.url, headers=headers
                                    )
                                    assert denied.status_code in (
                                        401,
                                        403,
                                    ), "Protected service accepted missing or invalid token"
                            else:
                                public_response = http.get(exposed.url)
                                assert public_response.status_code == 200
                                assert (
                                    public_response.text
                                    == "router-service-ready"
                                )
                            return
                    except httpx.TransportError:
                        pass
                    time.sleep(0.5)
            pytest.fail(
                "Sandbox service did not become reachable", pytrace=False
            )

        check_service(sandbox)
        before = provider.sandbox.get_info(sandbox_id).end_at
        state = SandboxSessionState.from_json(sandbox.session_state().to_json())
        assert set(state.data) == {"sandbox_id", "domain"}
        record_property("end_at_before_connect", before.isoformat())
        # Sandbox.resume constructs a new Novita client; state carries no tokens.
        resumed = Sandbox.resume(state)
        assert resumed.provider_resource_id == sandbox_id
        after = provider.sandbox.get_info(sandbox_id).end_at
        record_property("end_at_after_connect", after.isoformat())
        record_property(
            "expiry_change_seconds", (after - before).total_seconds()
        )
        assert (
            resumed.execute_code("print(router_marker + 1)").stdout.strip()
            == "42"
        )
        check_service(resumed)
        # A fresh interpreter receives only the non-sensitive state payload.
        # Credentials come from its inherited environment, never argv or JSON.
        child = subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "protected" if protected else "public",
            ],
            input=json.dumps(state.data),
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        assert child.returncode == 0, "Separate-process acceptance failed"
        evidence = json.loads(child.stdout)
        assert evidence["sandbox_id"] == sandbox_id
        assert evidence["pid"] != os.getpid()
        assert evidence["python_state"] == "42"
        assert evidence["service_status"] == 200
        if protected:
            assert len(evidence["denied_statuses"]) == 2
            assert all(
                status in (401, 403) for status in evidence["denied_statuses"]
            )
        else:
            assert evidence["denied_statuses"] == []
        record_property("separate_process", json.dumps(evidence))
        acceptance_ready = True
    except Exception as exc:
        # Provider exception strings may contain response bodies or URLs.
        raise AssertionError(
            f"Novita live acceptance failed ({type(exc).__name__})"
        ) from None
    finally:
        if sandbox is not None:
            # Explicit destroy is mandatory: __exit__ suppresses cleanup errors.
            # Only the resource returned by this test is ever targeted.
            try:
                sandbox.destroy()
                try:
                    provider.sandbox.get_info(sandbox_id)
                except SandboxNotFoundException:
                    record_property("cleanup", "confirmed_absent")
                    if acceptance_ready:
                        record_property("acceptance", "passed")
                else:
                    pytest.fail(
                        f"Cleanup unconfirmed; sandbox ID: {sandbox_id}",
                        pytrace=False,
                    )
            except Exception:
                record_property("cleanup", "failed")
                pytest.fail(
                    f"Cleanup failed; residual sandbox ID: {sandbox_id}",
                    pytrace=False,
                )
        else:
            record_property(
                "cleanup", "creation outcome unknown; no ID returned"
            )
            record_property("ownership_marker", owner)


def _separate_process_acceptance():
    """Reattach without sharing a client, handle, or token with the parent."""
    import httpx

    from bespokelabs.sandbox import Sandbox, SandboxSessionState

    logging.disable(logging.CRITICAL)
    data = json.loads(sys.stdin.read())
    assert set(data) == {"sandbox_id", "domain"}
    attached = Sandbox.resume(SandboxSessionState(backend="novita", data=data))
    assert attached.provider_resource_id == data["sandbox_id"]
    result = attached.execute_code("print(router_marker + 1)")
    assert result.exit_code == 0 and result.stdout.strip() == "42"
    exposed = attached.expose_port(8080)
    protected = sys.argv[1] == "protected"
    denied_statuses = []
    with httpx.Client(timeout=10) as http:
        response = http.get(exposed.url, headers=exposed.headers)
        assert response.status_code == 200
        assert response.text == "router-service-ready"
        if protected:
            assert set(exposed.headers) == {"Sandbox-Traffic-Access-Token"}
            assert exposed.headers["Sandbox-Traffic-Access-Token"]
            for headers in (
                {},
                {"Sandbox-Traffic-Access-Token": "invalid-e2e-token"},
            ):
                denied = http.get(exposed.url, headers=headers)
                assert denied.status_code in (401, 403)
                denied_statuses.append(denied.status_code)
        else:
            public = http.get(exposed.url)
            assert public.status_code == 200
            assert public.text == "router-service-ready"
    print(
        json.dumps(
            {
                "sandbox_id": attached.provider_resource_id,
                "pid": os.getpid(),
                "python_state": result.stdout.strip(),
                "service_status": response.status_code,
                "denied_statuses": denied_statuses,
            }
        )
    )


if __name__ == "__main__":
    try:
        _separate_process_acceptance()
    except Exception:
        # Do not emit SDK errors, response bodies, URLs, or local variables.
        sys.exit(1)
