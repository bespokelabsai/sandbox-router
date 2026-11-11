# Development setup

[Back to README](../README.md)

Set up a source checkout, run checks, and find the implementation modules.

## Development

Use Python 3.11+ to include the server modules and tests:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,server]'

# Unit/API tests using local execution and mocked providers.
python -m pytest -q --ignore=tests/test_regression.py

# Repository formatting and lint configuration.
python -m pyink --check src tests examples
python -m ruff check src tests examples
```

On macOS, prefix the test command with `COPYFILE_DISABLE=1` to prevent the
system tar from adding AppleDouble `._*` files to directory-transfer fixtures:

```bash
COPYFILE_DISABLE=1 python -m pytest -q --ignore=tests/test_regression.py
```

`tests/test_regression.py` probes installed providers at collection time and
can create real resources when SDKs/authentication are available. Run
`python -m pytest -q` when you intend to exercise those integrations. Mocked
Daytona adapter tests also need the `[daytona]` extra and skip without it.
The test suite exercises Python/API behavior and dashboard asset contracts;
`tests/dashboard_browser_app.py` is a separate deterministic browser fixture.

### Codebase map

| Path | Responsibility |
|---|---|
| `src/bespokelabs/sandbox/sandbox.py` | Direct SDK, lifecycle, workspace setup, structured output, agent cost tracking |
| `src/bespokelabs/sandbox/aio.py` | Thread-backed async client and session wrapper |
| `src/bespokelabs/sandbox/backends/` | Lazy-loaded provider clients and sessions, plus the job-only Google Cloud Batch client |
| `src/bespokelabs/sandbox/_jobs.py` | Job backend lookup and the shared `wait_job` loop |
| `src/bespokelabs/sandbox/protocols.py`, `types.py`, `exceptions.py` | Backend contracts and shared public types/errors |
| `src/bespokelabs/sandbox/workspace.py`, `_transfer.py` | Manifest entries and directory transfer strategies |
| `src/bespokelabs/sandbox/agents.py`, `_agent_runtime.py`, `_usage.py` | Agent placement, command preparation, Claude/OpenCode usage parsing |
| `src/bespokelabs/sandbox/presets.py`, `pricing.py`, `pricing.json` | Preset configuration and bundled compute rates |
| `src/bespokelabs/sandbox/remote.py` | Standard-library HTTP client for the control plane |
| `src/bespokelabs/sandbox/control_plane/` | FastAPI routes, tenant service, SQLite migrations/ledgers, supervision, dashboard assets |
| `images/`, `.github/workflows/build-images.yml` | Preset Dockerfiles and image publishing workflow |
| `tests/`, `examples/`, `docs/` | Regression coverage, usage examples, API contract and operations runbook |

See [DEVELOPMENT.md](../DEVELOPMENT.md) for design direction and compatibility
rules. It includes proposed integrations as well as implemented concepts;
the current public API is exported by `src/bespokelabs/sandbox/__init__.py`.
For server deployments, read the [operations runbook](CONTROL_PLANE_OPERATIONS.md).
