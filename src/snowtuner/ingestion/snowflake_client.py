"""Thin wrapper around snowflake-connector-python.

Accepts a :class:`SnowflakeCredentials` instance (produced by the credential
resolver).  Lazy-connects on first ``execute()`` so callers can build a client
without triggering a connection until actually needed.
"""
from __future__ import annotations

from typing import Any

from snowtuner.credentials import CredentialResolver, SnowflakeCredentials


class SnowflakeClient:
    """Lazy-connects on first ``execute``.  Re-raises connector errors unchanged."""

    def __init__(self, credentials: SnowflakeCredentials):
        self.credentials = credentials
        self._conn: Any = None

    @classmethod
    def from_resolver(
        cls, resolver: CredentialResolver | None = None
    ) -> "SnowflakeClient":
        """Build from the default tiered resolver (env → keyring → file)."""
        resolver = resolver or CredentialResolver()
        result = resolver.load()
        if result is None:
            raise RuntimeError(
                "No Snowflake credentials found.  Run `snowtuner init` to set them up."
            )
        return cls(result.credentials)

    def _connect(self) -> Any:
        if self._conn is not None:
            return self._conn
        try:
            import snowflake.connector  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "snowflake-connector-python not installed.  "
                "Install with: pip install snowtuner[snowflake]"
            ) from e
        import snowflake.connector as sc
        self._conn = sc.connect(**self.credentials.to_connector_kwargs())
        return self._conn

    def execute(self, sql: str, params: list | None = None) -> list[tuple[Any, ...]]:
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute(sql, params or [])
            return cur.fetchall()
        finally:
            cur.close()

    def execute_with_columns(
        self, sql: str, params: list | None = None,
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Execute and return (lowercased column names, rows)."""
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute(sql, params or [])
            cols = [d[0].lower() for d in (cur.description or [])]
            return cols, cur.fetchall()
        finally:
            cur.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
