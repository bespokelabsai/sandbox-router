"""Deterministic fake-provider app used for manual browser acceptance."""

from __future__ import annotations

import math
import tempfile
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bespokelabs.sandbox.control_plane.api import create_app
from bespokelabs.sandbox.control_plane.errors import PolicyDeniedError
from bespokelabs.sandbox.control_plane.reconciliation import ProviderObservation
from bespokelabs.sandbox.control_plane.resource_metrics import normalize
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.exceptions import SandboxTimeoutError
from bespokelabs.sandbox.types import AgentRunResult, SandboxResult, Usage

_fixture_dir = tempfile.TemporaryDirectory(prefix="bespoke-dashboard-")
DB_PATH = f"{_fixture_dir.name}/browser.db"
OPERATOR_TOKEN = "bsk_live_phase5_operator_fixture_only"
READ_ONLY_TOKEN = "bsk_live_phase5_read_only_fixture_only"


class BrowserRuntime:

    def __init__(self, backend: str, index: int, **config: object) -> None:
        if config.get("app_name") == "failed-launch":
            raise SandboxTimeoutError(
                "Deterministic fake launch timed out",
                backend=backend,
                op="create",
                context={"cleanup_status": "deleted"},
            )
        self.backend_name = backend
        self.provider_resource_id = f"fake-{backend}-{index:03d}"
        self.destroyed = False

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        return SandboxResult(stdout=f"{language}:{code}", exit_code=0)

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        return SandboxResult(stdout=" ".join([command, *(args or [])]))

    def run_agent(self, prompt: str, **options) -> AgentRunResult:
        harness = options["harness"]
        if prompt == "No usage":
            return AgentRunResult(text="Usage unavailable", exit_code=1)
        usage = Usage(
            input_tokens=800000, output_tokens=50000, cache_read_tokens=200000
        )
        raw = {
            "usage": {
                "input_tokens": 1000000 if harness == "codex" else 800000,
                "output_tokens": 50000,
                "cached_input_tokens"
                if harness == "codex"
                else "cache_read_input_tokens": 200000,
            }
        }
        if harness == "grok":
            usage.llm_cost_usd = 1.25
            raw["total_cost_usd"] = 1.25
        return AgentRunResult(
            text="Fixture agent completed", usage=usage, raw=raw
        )

    def estimate_compute_cost(self, elapsed_secs: float) -> float:
        return elapsed_secs / 1800

    def resource_usage(self) -> dict:
        ticks = time.monotonic()
        return {
            "cpu_seconds": ticks,
            "monotonic_seconds": ticks,
            "cpu_capacity": 2,
            "counter_id": self.provider_resource_id,
            "memory_used_bytes": 2.4 * 1073741824,
            "memory_limit_bytes": 8 * 1073741824,
            "gpus": [
                {
                    "id": "fixture-gpu",
                    "name": "NVIDIA A100 (fixture)",
                    "utilization_percent": 64,
                    "memory_used_bytes": 12 * 1073741824,
                    "memory_total_bytes": 40 * 1073741824,
                }
            ],
        }

    def destroy(self) -> None:
        self.destroyed = True


class BrowserFactory:

    def __init__(self) -> None:
        self.created: list[BrowserRuntime] = []

    def __call__(self, backend: str, **config: object) -> BrowserRuntime:
        runtime = BrowserRuntime(backend, len(self.created) + 1, **config)
        self.created.append(runtime)
        return runtime


class BrowserReconciler:

    def __init__(self) -> None:
        self.by_org: dict[str, list[ProviderObservation]] = {}

    def list_resources(self, organization_id: str) -> list[ProviderObservation]:
        return self.by_org.get(organization_id, [])


now = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=516)


def current_time() -> datetime:
    return now


store = SQLiteStore(DB_PATH, key_pepper="phase5-browser-fixture")
factory = BrowserFactory()
reconciler = BrowserReconciler()
service = ControlPlane(
    store,
    sandbox_factory=factory,
    allowed_backends={"daytona", "e2b", "runpod"},
    provider_settings={
        "daytona": {"DAYTONA_API_KEY": "fixture-not-returned"},
        "e2b": {"E2B_API_KEY": "fixture-not-returned"},
        "runpod": {"RUNPOD_API_KEY": "fixture-not-returned"},
    },
    provider_health_checks={
        "daytona": lambda _settings: True,
        "runpod": lambda _settings: True,
    },
    provider_reconcilers={"daytona": reconciler},
    now=current_time,
)
_, operator_key = service.bootstrap_organization("Phase 5 browser fixture")
operator = store.authenticate(operator_key.secret)
read_key = service.issue_api_key(
    operator,
    name="Read-only observer",
    scopes=["sandboxes:read", "usage:read"],
)
with store._connect() as connection:
    connection.execute(
        "UPDATE api_keys SET key_digest=? WHERE id=?",
        (store._digest(OPERATOR_TOKEN), operator_key.id),
    )
    connection.execute(
        "UPDATE api_keys SET key_digest=? WHERE id=?",
        (store._digest(READ_ONLY_TOKEN), read_key.id),
    )

for index in range(8):
    archived = service.create_sandbox(
        operator,
        "runpod" if index % 2 else "daytona",
        {"preset": "archived-cpu", "timeout_secs": 3600},
    )
    now += timedelta(seconds=1)
    service.destroy_sandbox(operator, archived.id)
    now += timedelta(seconds=1)

live = service.create_sandbox(
    operator,
    "daytona",
    {
        "gpu": "A10G",
        "cpu": 4,
        "memory_mb": 16384,
        "timeout_secs": 600,
        "app_name": "operator-demo",
    },
)
service.execute(
    operator,
    live.id,
    code="print('fake provider ready')",
    request_id="browser-execution-1",
)
service.run_agent(
    operator,
    live.id,
    prompt="Explain",
    harness="codex",
    model="gpt-5.3-codex",
    request_id="browser-codex",
)
service.run_agent(
    operator,
    live.id,
    prompt="Explain",
    harness="grok",
    model="grok",
    request_id="browser-grok",
)
service.run_agent(
    operator,
    live.id,
    prompt="No usage",
    harness="codex",
    request_id="browser-unknown",
)
now += timedelta(seconds=500)
reconciler.by_org[operator.organization_id] = [
    ProviderObservation(
        "browser-observation-1",
        live.provider_resource_id or "",
        "running",
        now.isoformat(),
        Decimal("0.24"),
    )
]
service.reconcile(operator, "daytona")

try:
    service.create_sandbox(
        operator,
        "daytona",
        {"app_name": "failed-launch", "timeout_secs": 900},
    )
except SandboxTimeoutError:
    pass

service.set_policy(
    operator,
    max_concurrent_sandboxes=3,
    hourly_spend_limit_usd=Decimal("1"),
    daily_spend_limit_usd=Decimal("10"),
    allowed_backends=["daytona", "runpod"],
    allowed_gpu_types=["A10G"],
    max_sandbox_lifetime_secs=3600,
)
service.set_alert_configuration(
    operator,
    budget_threshold_percent=25,
    repeated_failures_count=1,
    repeated_failures_window_minutes=60,
    provider_degradation_enabled=True,
    long_running_secs=300,
    failed_cleanup_enabled=True,
)
service.check_provider_health(operator, "daytona")
service.check_provider_health(operator, "e2b")
service.check_provider_health(operator, "runpod")
try:
    service.create_sandbox(
        operator,
        "runpod",
        {"gpu": "A100", "timeout_secs": 600},
    )
except PolicyDeniedError:
    pass

service.alert_history(operator, offset=0, limit=20)

previous = None
for index in range(120):
    raw = factory.created[-1].resource_usage()
    raw.update(
        cpu_seconds=100 + index * 15 + math.sin(index / 5) * 10,
        monotonic_seconds=1000 + index * 15,
    )
    raw["memory_used_bytes"] = (2.4 + math.sin(index / 8) * 0.4) * 1073741824
    raw["gpus"][0]["utilization_percent"] = 60 + math.sin(index / 4) * 20
    raw["gpus"][0]["memory_used_bytes"] = (
        12 + math.sin(index / 9) * 2
    ) * 1073741824
    sample = normalize(raw, previous)
    sample["collected_at"] = (
        now - timedelta(seconds=(119 - index) * 15)
    ).isoformat()
    store.record_resource_sample(operator.organization_id, live.id, sample)
    previous = raw

app = create_app(
    service,
    session_cookie_secure=False,
    enable_local_dashboard_login=True,
)
