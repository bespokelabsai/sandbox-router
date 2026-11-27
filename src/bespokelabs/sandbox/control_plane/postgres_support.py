"""Shared PostgreSQL plumbing: caller-owned connections and migrations.

``psycopg`` is imported only when a store is used, so importing this package
without the ``postgres`` extra still works. Nothing here reads environment
variables or keeps module-level connection state.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Union

# Either a zero-argument factory returning a new ``psycopg.Connection`` (closed
# after each operation) or a pool exposing ``connection()`` as a context manager,
# such as ``psycopg_pool.ConnectionPool``.
ConnectionSource = Union[Callable[[], Any], Any]

Migration = tuple[int, Sequence[str]]

_IDENTIFIER = re.compile(r"[a-z_]+")


def _dict_row() -> Any:
    try:
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise ImportError(
            "PostgreSQL stores require psycopg; install the 'postgres' extra"
        ) from exc
    return dict_row


@contextmanager
def transaction(source: ConnectionSource) -> Iterator[Any]:
    """Yield a cursor with mapping rows inside exactly one transaction."""
    row_factory = _dict_row()
    if hasattr(source, "connection"):
        with source.connection() as connection:
            with connection.transaction():
                with connection.cursor(row_factory=row_factory) as cursor:
                    yield cursor
        return
    if not callable(source):
        raise ValueError("A connection factory or connection pool is required")
    connection = source()
    try:
        with connection.transaction():
            with connection.cursor(row_factory=row_factory) as cursor:
                yield cursor
    finally:
        connection.close()


def apply_migrations(
    source: ConnectionSource,
    journal: str,
    migrations: Sequence[Migration],
    *,
    requires_table: str | None = None,
    missing_message: str = "",
) -> None:
    """Apply unapplied versions once, even when several processes start together.

    DDL, the version stamp, and the journal itself share one transaction. A
    transaction-scoped advisory lock derived from the journal name serializes
    concurrent starters; the later one then finds every version recorded.
    """
    if not _IDENTIFIER.fullmatch(journal):
        raise ValueError(f"not a journal table name: {journal!r}")
    lock_key = int.from_bytes(
        hashlib.sha256(journal.encode()).digest()[:8], "big", signed=True
    )
    with transaction(source) as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        if requires_table is not None:
            cursor.execute(
                "SELECT to_regclass(%s) AS relation", (requires_table,)
            )
            if cursor.fetchone()["relation"] is None:
                raise ValueError(missing_message)
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {journal} ("
            "version INTEGER PRIMARY KEY, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cursor.execute(f"SELECT version FROM {journal}")
        applied = {row["version"] for row in cursor.fetchall()}
        for version, statements in migrations:
            if version in applied:
                continue
            for statement in statements:
                cursor.execute(statement)
            cursor.execute(
                f"INSERT INTO {journal}(version) VALUES (%s)", (version,)
            )
