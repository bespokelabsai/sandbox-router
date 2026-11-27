r"""Operator seeding for a jobs-only deployment, without an HTTP admin route.

Run it against the authority store your host module builds, so the key is
digested with the same pepper the service uses::

    python -m bespokelabs.sandbox.control_plane.job_seed my_host:authority \\
        --organization "Example" --key-name ci

This creates access rows only. It does not enable the jobs API or any binding.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .job_interfaces import SeedableAuthorityStore
from .models import IssuedAPIKey

JOB_SCOPES = (
    "jobs:submit",
    "jobs:read",
    "jobs:result",
    "jobs:logs",
    "jobs:cancel",
)


@dataclass(frozen=True)
class SeededAccess:
    """Outcome of seeding. ``issued`` is None when the key already existed."""

    organization_id: str
    api_key_id: str
    issued: IssuedAPIKey | None


def seed_job_access(
    authority: SeedableAuthorityStore,
    *,
    organization: str,
    key_name: str,
    scopes: Iterable[str] = JOB_SCOPES,
) -> SeededAccess:
    """Ensure one organization and one active API key with this name exist.

    Repeating the call with the same names creates nothing new. A key's secret
    exists only in the ``issued`` value of the call that created it; an
    existing key is reported by ID and its scopes are left unchanged.
    """
    key_name = key_name.strip()
    if not key_name:
        raise ValueError("API-key name must not be empty")
    organization_id = authority.ensure_organization(organization)["id"]
    for key in authority.list_api_keys(organization_id):
        if key["name"] == key_name and key["revoked_at"] is None:
            return SeededAccess(organization_id, key["id"], None)
    issued = authority.issue_api_key(
        organization_id, name=key_name, scopes=tuple(scopes)
    )
    return SeededAccess(organization_id, issued.id, issued)


def _load(target: str) -> object:
    module_name, separator, attribute = target.partition(":")
    if not separator or not module_name or not attribute:
        raise SystemExit("The authority must be given as module:attribute")
    value: object = importlib.import_module(module_name)
    for part in attribute.split("."):
        value = getattr(value, part)
    return value


def main(argv: Sequence[str] | None = None) -> int:
    """Seed one organization and API key, printing a new key exactly once."""
    parser = argparse.ArgumentParser(
        prog="python -m bespokelabs.sandbox.control_plane.job_seed",
        description=(
            "Idempotently create one organization and one API key on the "
            "authority store built by a host module."
        ),
    )
    parser.add_argument(
        "authority",
        help="module:attribute naming the host module's authority store",
    )
    parser.add_argument("--organization", required=True)
    parser.add_argument("--key-name", required=True)
    parser.add_argument(
        "--scope",
        action="append",
        dest="scopes",
        metavar="SCOPE",
        help="repeatable; defaults to the five jobs:* scopes",
    )
    arguments = parser.parse_args(argv)
    authority = _load(arguments.authority)
    if not isinstance(authority, SeedableAuthorityStore):
        raise SystemExit(f"{arguments.authority} is not a seedable authority")
    seeded = seed_job_access(
        authority,
        organization=arguments.organization,
        key_name=arguments.key_name,
        scopes=arguments.scopes or JOB_SCOPES,
    )
    print(f"organization_id={seeded.organization_id}")
    print(f"api_key_id={seeded.api_key_id}")
    if seeded.issued is None:
        print(
            "An active API key with this name already exists; no key was "
            "issued and its secret cannot be shown again.",
            file=sys.stderr,
        )
    else:
        print(f"api_key={seeded.issued.secret}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
