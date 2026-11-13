# Installation

[Back to README](../README.md)

Install the core SDK, provider extras, or the self-hosted server. For a first run, start with the [quickstart](../README.md#quickstart).

## Install

Use Python **3.10+** for the core SDK and **3.11+** for the control plane
and its tests.
The only required core dependency is Pydantic; provider SDKs are optional.

```bash
pip install bespokelabs-sandbox
```

For the server and a provider adapter, install the matching extras together:

```bash
pip install 'bespokelabs-sandbox[server,e2b]'
```

<details>
<summary>Using Sandbox alongside Curator or other Bespoke packages</summary>

Sandbox can share an environment with Curator:

```bash
python -m pip install bespokelabs-curator bespokelabs-sandbox
```

```python
from bespokelabs import curator, sandbox
```

Each distribution owns its own subpackage. Sandbox installs only
`bespokelabs/sandbox`; it does not install `bespokelabs/__init__.py`.
The separate `bespokelabs` distribution is not needed for these imports.
The Nimble SDK uses the `bespokelabs-nimble` distribution.

For upgrades from older releases that shared the root file, use a fresh virtual
environment with namespace-compatible versions of all installed Bespoke packages.
Use `python -m pip` with the interpreter used by your app or notebook, and check
for old editable checkouts or a local `bespokelabs.py` if imports fail.

</details>

<details>
<summary>Provider installation options</summary>

```bash
pip install 'bespokelabs-sandbox[docker]'
pip install 'bespokelabs-sandbox[monty]'       # local Python subset, no shell
pip install 'bespokelabs-sandbox[daytona]'
pip install 'bespokelabs-sandbox[tensorlake]'
pip install 'bespokelabs-sandbox[modal]'
pip install 'bespokelabs-sandbox[e2b]'
pip install 'bespokelabs-sandbox[novita]'      # Novita Code Interpreter
pip install 'bespokelabs-sandbox[ray]'
pip install 'bespokelabs-sandbox[boxd]'
pip install 'bespokelabs-sandbox[gcp-batch]'   # detached jobs on Google Cloud Batch
pip install 'bespokelabs-sandbox[all]'
```

`[all]` installs all optional provider SDKs, but does not include `[server]`
or `[dev]`. Install `'bespokelabs-sandbox[server,e2b]'`, for example, to run
the API with E2B support. The remote HTTP client needs only the core package.

The Novita extra installs `novita-sandbox>=2.1.3,<2.2`, the inspected SDK baseline
for the Code Interpreter adapter. Set `NOVITA_API_KEY`; optionally select an
endpoint with `NOVITA_DOMAIN`. See [Novita setup and limitations](BACKENDS.md#novita-code-interpreter),
including native SDK connect lifecycle effects and verified protected-service access.

The RunPod backend has no Python extra; it uses the system OpenSSH client.
The Safehouse backend also has no Python extra. Install its CLI separately on
macOS:

```bash
brew install eugene1g/safehouse/agent-safehouse
```

</details>
