"""Example host module for the jobs-only service.

Serve it with ``uvicorn jobs_service_host:app``; the service image does this
when ``APP_MODULE=jobs_service_host:app``. Copy it and replace the binding,
the admission policy, and the limits with your own. Every value below comes
from the environment of the process and every name is a placeholder.

Required environment:

    JOBS_DATABASE_URL            PostgreSQL connection string
    JOBS_API_KEY_PEPPER          secret; identical on every instance
    JOBS_HMAC_SECRET_HEX         secret, at least 64 hex characters; identical
                                 on every instance
    JOBS_BATCH_PROJECT           project the batch jobs run in
    JOBS_BATCH_REGION            region the batch jobs run in
    JOBS_BATCH_ACCOUNT           non-secret name of the identity this service
                                 calls the batch API as

Optional environment:

    JOBS_ALLOWED_ORGANIZATIONS   comma-separated organization IDs; empty
                                 (the default) allows nobody to submit
    JOBS_ALLOWED_IMAGE_PREFIX    only images starting with this may run
    JOBS_POOL_MAX_SIZE           connections per instance (default 10)
"""

from __future__ import annotations

import functools
import os

from psycopg_pool import ConnectionPool

from bespokelabs.sandbox.control_plane.job_adapters.gcp_batch import (
    GcpBatchHostedAdapter,
)
from bespokelabs.sandbox.control_plane.job_api import HostedJobConfiguration
from bespokelabs.sandbox.control_plane.job_app import create_job_app
from bespokelabs.sandbox.control_plane.job_models import (
    ConfiguredJobBinding,
    JobBinding,
)
from bespokelabs.sandbox.control_plane.job_store import hosted_job_hmac
from bespokelabs.sandbox.control_plane.job_store_postgres import (
    PostgresHostedJobRepository,
)
from bespokelabs.sandbox.control_plane.models import Principal
from bespokelabs.sandbox.control_plane.store_postgres import (
    PostgresAuthorityStore,
)
from bespokelabs.sandbox.types import JobSpec

HMAC_SECRET = bytes.fromhex(os.environ["JOBS_HMAC_SECRET_HEX"])
ALLOWED_ORGANIZATIONS = frozenset(
    item.strip()
    for item in os.environ.get("JOBS_ALLOWED_ORGANIZATIONS", "").split(",")
    if item.strip()
)
ALLOWED_IMAGE_PREFIX = os.environ.get("JOBS_ALLOWED_IMAGE_PREFIX", "")
MAX_CPU = 4
MAX_MEMORY_MB = 8192
MAX_DISK_MB = 51200
MAX_TIMEOUT_SECS = 3600

# Never repoint an existing version; add version 2 and keep 1 while its jobs
# still need status, logs, or cancellation.
BINDING = JobBinding(
    id="batch",
    version=1,
    backend="gcp_batch",
    account=os.environ["JOBS_BATCH_ACCOUNT"],
    project=os.environ["JOBS_BATCH_PROJECT"],
    region=os.environ["JOBS_BATCH_REGION"],
)


# The three clients below use the identity attached to this service, which
# must be the account named in BINDING. Each is built once, on first use.
@functools.lru_cache(maxsize=1)
def _batch_client():
    from bespokelabs.sandbox.backends.gcp_batch import GcpBatchClient

    return GcpBatchClient()


@functools.lru_cache(maxsize=1)
def _cancel_client():
    from google.cloud import batch_v1

    return batch_v1.BatchServiceClient()


@functools.lru_cache(maxsize=1)
def _logging_client():
    from google.cloud.logging_v2.services.logging_service_v2 import (
        LoggingServiceV2Client,
    )

    return LoggingServiceV2Client()


def admit(principal: Principal, binding: JobBinding, spec: JobSpec) -> bool:
    """Allow only small, CPU-only jobs running an approved image.

    Pure and replay-safe: it reads nothing and changes nothing outside its
    arguments. Anything it does not recognise is denied.
    """
    return (
        binding == BINDING
        and principal.organization_id in ALLOWED_ORGANIZATIONS
        and bool(ALLOWED_IMAGE_PREFIX)
        and isinstance(spec.image, str)
        and spec.image.startswith(ALLOWED_IMAGE_PREFIX)
        and spec.gpu is None
        and spec.cpu <= MAX_CPU
        and spec.memory_mb <= MAX_MEMORY_MB
        and (spec.disk_mb is None or spec.disk_mb <= MAX_DISK_MB)
        and spec.timeout_secs <= MAX_TIMEOUT_SECS
        and not spec.backend_options
    )


# Bounded waits keep requests from queueing behind an unreachable database;
# the check discards connections that died while idle.
pool = ConnectionPool(
    os.environ["JOBS_DATABASE_URL"],
    kwargs={"connect_timeout": 5},
    min_size=1,
    max_size=int(os.environ.get("JOBS_POOL_MAX_SIZE", "10")),
    timeout=5,
    check=ConnectionPool.check_connection,
    open=True,
)
# The authority store comes first: the job repository needs its tables.
authority = PostgresAuthorityStore(
    pool, key_pepper=os.environ["JOBS_API_KEY_PEPPER"]
)
repository = PostgresHostedJobRepository(pool, hosted_job_hmac(HMAC_SECRET))

app = create_job_app(
    authority=authority,
    repository=repository,
    configuration=HostedJobConfiguration(
        bindings=(
            ConfiguredJobBinding(
                binding=BINDING,
                organizations=ALLOWED_ORGANIZATIONS,
                adapter_factory=lambda: GcpBatchHostedAdapter(
                    BINDING,
                    client_factory=_batch_client,
                    cancel_client_factory=_cancel_client,
                    logging_client_factory=_logging_client,
                ),
            ),
        ),
        submit_bindings={BINDING.id: BINDING.version},
        hmac_secret=HMAC_SECRET,
        admit=admit,
    ),
    on_shutdown=pool.close,
)
