"""Hosted ownership checks around the public native Cloud Batch API.

Credentials are exclusively supplied by a trusted lazy client factory. The
factory must resolve to the configured account; task service accounts are
not credential bindings. Factories own and reuse their clients, including
closing them at host shutdown. This module never falls back to ambient ADC.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import replace
from typing import Callable

from bespokelabs.sandbox.exceptions import SandboxNotFoundError
from bespokelabs.sandbox.protocols import SandboxJobBackend
from bespokelabs.sandbox.types import JobSpec, JobState, JobStatus

from ..job_models import (
    MAX_LOG_BYTES,
    MAX_LOG_TAIL,
    CancellationUnavailable,
    HostedJob,
    JobBinding,
    JobObservation,
    OwnershipConflict,
    ProviderMissing,
    bounded_log_text,
    bounded_tail,
)

OWNERSHIP_LABEL = "hosted-job-owner"
_RESERVED_OPTIONS = {"project", "region", "job_id", "job_id_prefix"}
# Empty/partial pages may carry continuation tokens without making progress.
_MAX_LOG_PAGES = 20


class GcpBatchHostedAdapter:

    def __init__(
        self,
        binding: JobBinding,
        client_factory: Callable[[], SandboxJobBackend],
        *,
        cancel_client_factory: Callable[[], object] | None = None,
        logging_client_factory: Callable[[], object] | None = None,
    ) -> None:
        if binding.backend != "gcp_batch" or not all(
            re.fullmatch(r"[a-zA-Z0-9_.:-]+", part)
            for part in (binding.project, binding.region)
        ):
            raise ValueError("Invalid Cloud Batch binding")
        if not callable(client_factory):
            raise ValueError("An explicit bound client factory is required")
        self.binding = binding
        self._factory = client_factory
        # Separate explicit public SDK handle: never inspect a native client's
        # private attributes or call its delete-capable cancel_job wrapper.
        self._cancel_factory = cancel_client_factory
        self._logging_factory = logging_client_factory

    def resource_name(self, binding: JobBinding, token: str) -> str:
        if binding != self.binding or not re.fullmatch(
            r"[a-z]([a-z0-9-]{0,61}[a-z0-9])?", token
        ):
            raise OwnershipConflict()
        return f"projects/{binding.project}/locations/{binding.region}/jobs/{token}"

    def _check_record(self, record: HostedJob) -> None:
        if (
            record.binding != self.binding
            or record.resource_name
            != self.resource_name(
                self.binding, record.resource_name.rsplit("/", 1)[-1]
            )
            or not re.fullmatch(r"[a-f0-9]{32}", record.ownership_marker)
        ):
            raise OwnershipConflict()

    def validate(self, spec: JobSpec, binding: JobBinding) -> None:
        """Validate hosted routing purely, before reserving an operation key."""
        if binding != self.binding:
            raise OwnershipConflict()
        if _RESERVED_OPTIONS.intersection(spec.backend_options):
            raise ValueError("Hosted resource routing is server-controlled")
        # Match the public backend's label normalization so a differently
        # spelled request label cannot collide with the reserved marker.
        for key in spec.labels:
            normalized = re.sub(r"[^a-z0-9_-]", "_", key.lower())[:63]
            if not normalized or not normalized[0].isalpha():
                normalized = ("l_" + normalized)[:63]
            if normalized == OWNERSHIP_LABEL:
                raise ValueError(
                    "Hosted ownership labels are server-controlled"
                )

    def submit(self, record: HostedJob, spec: JobSpec) -> str:
        self._check_record(record)
        # Revalidation after dispatch authorization fails closed if policy
        # changed. It must never reset the consumed authorization for retry.
        self.validate(spec, record.binding)
        options = copy.deepcopy(spec.backend_options)
        options.update(
            project=self.binding.project,
            region=self.binding.region,
            job_id=record.resource_name.rsplit("/", 1)[-1],
        )
        runtime_spec = replace(
            spec,
            backend_options=options,
            labels={**spec.labels, OWNERSHIP_LABEL: record.ownership_marker},
        )
        accepted = self._factory().submit_job(runtime_spec)
        if accepted != record.resource_name:
            raise OwnershipConflict()
        return accepted

    def _verified(self, record: HostedJob, status: JobStatus) -> JobObservation:
        self._check_record(record)
        if status.state is JobState.UNKNOWN:
            raise ProviderMissing()
        uid = status.details.get("uid")
        labels = status.details.get("labels")
        if (
            status.job_id != record.resource_name
            or not isinstance(uid, str)
            or not uid
            or not isinstance(labels, dict)
            or labels.get(OWNERSHIP_LABEL) != record.ownership_marker
            or (record.uid is not None and record.uid != uid)
        ):
            raise OwnershipConflict()
        return JobObservation(status, uid)

    def observe(self, record: HostedJob) -> JobObservation:
        self._check_record(record)
        try:
            status = self._factory().job_status(record.resource_name)
        except SandboxNotFoundError:
            raise ProviderMissing() from None
        return self._verified(record, status)

    def cancel(self, record: HostedJob) -> JobObservation:
        self._check_record(record)
        if record.uid is None or not record.cancel_requested:
            raise OwnershipConflict()
        client = self._factory()
        try:
            before = self._verified(
                record, client.job_status(record.resource_name)
            )
            if before.status.done:
                return before
            if self._cancel_factory is None:
                raise CancellationUnavailable()
            cancel = getattr(self._cancel_factory(), "cancel_job", None)
            if not callable(cancel):
                raise CancellationUnavailable()
            # Only the official CancelJob RPC, never DeleteJob. SDK versions
            # without this method are deliberately unsupported for cancellation.
            try:
                cancel(name=record.resource_name, retry=None, timeout=30)
            except NotImplementedError:
                raise CancellationUnavailable() from None
            except Exception as exc:
                # Import only on a failed SDK RPC. Native fake clients and
                # installations without the optional SDK remain import-safe.
                try:
                    from google.api_core import exceptions as google_errors
                except ImportError:
                    raise exc from None
                if isinstance(exc, google_errors.NotFound):
                    raise ProviderMissing() from None
                if isinstance(exc, google_errors.MethodNotImplemented):
                    raise CancellationUnavailable() from None
                raise
            # An Operation acknowledgement is not a terminal job outcome.
            return self._verified(
                record, client.job_status(record.resource_name)
            )
        except SandboxNotFoundError:
            raise ProviderMissing() from None

    def logs(self, record: HostedJob, tail: int | None) -> str:
        tail = bounded_tail(tail)
        self._check_record(record)
        if record.uid is None:
            raise OwnershipConflict()
        client = self._factory()
        try:
            self._verified(record, client.job_status(record.resource_name))
            if tail == 0:
                return ""
            if self._logging_factory is None:
                raise RuntimeError(
                    "An explicit bound logging factory is required"
                )
            # Use the public GAPIC client, not native job_logs or the high-level
            # Logging iterator: its max_results check can fetch an extra page.
            logging = self._logging_factory()
            remaining = tail
            token = ""
            text = ""
            for _ in range(_MAX_LOG_PAGES):
                pager = logging.list_log_entries(
                    request={
                        "resource_names": [f"projects/{self.binding.project}"],
                        "filter": (
                            f'logName="projects/{self.binding.project}/logs/batch_task_logs" '
                            f"AND labels.job_uid={json.dumps(record.uid)}"
                        ),
                        "order_by": "timestamp desc",
                        "page_size": min(remaining, MAX_LOG_TAIL),
                        "page_token": token,
                    },
                    retry=None,
                    timeout=30,
                )
                # Consume only the already-fetched response. Iterating the
                # pager (even with islice) cannot shrink later page requests
                # after partial pages. Never ask it for the next response.
                page = next(iter(pager.pages))
                if len(page.entries) > remaining:
                    raise RuntimeError(
                        "Logging response exceeds requested budget"
                    )
                for entry in page.entries:
                    if "text_payload" in entry:
                        line = entry.text_payload
                    elif "json_payload" in entry:
                        from google.protobuf.json_format import MessageToDict

                        value = MessageToDict(
                            type(entry).pb(entry).json_payload
                        )
                        line = str(value.get("message", value))
                    else:
                        line = str(entry.proto_payload or "")
                    # Entries arrive newest first; retain only the chronological
                    # prefix that can fit in the final response, not N MiB lines.
                    line = line[:MAX_LOG_BYTES]
                    if not line.endswith("\n"):
                        line += "\n"
                    text = bounded_log_text(line + text)
                remaining -= len(page.entries)
                token = page.next_page_token
                if remaining == 0 or not token:
                    break
            self._verified(record, client.job_status(record.resource_name))
            return text
        except SandboxNotFoundError:
            raise ProviderMissing() from None
