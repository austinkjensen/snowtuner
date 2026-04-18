"""Thin wrapper around snowflake-connector-python.

Uses the same auth patterns as query_watchdog/snowflake_jobs.py (password or
OAuth via `authenticator='oauth_authorization_code'`).  Kept as a stub so the
rest of the system can run without the optional snowflake dependency installed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass
class SnowflakeAuth:
    account: str
    user: str
    password: str | None = None
    authenticator: str | None = None
    warehouse: str | None = None
    role: str | None = None

    @classmethod
    def from_env(cls) -> "SnowflakeAuth":
        return cls(
            account=os.environ["SFO_SNOWFLAKE_ACCOUNT"],
            user=os.environ["SFO_SNOWFLAKE_USER"],
            password=os.environ.get("SFO_SNOWFLAKE_PASSWORD"),
            authenticator=os.environ.get("SFO_SNOWFLAKE_AUTHENTICATOR"),
            warehouse=os.environ.get("SFO_SNOWFLAKE_WAREHOUSE"),
            role=os.environ.get("SFO_SNOWFLAKE_ROLE"),
        )


class SnowflakeClient:
    """Lazy-connects on first execute().  Re-raises connector errors unchanged."""

    def __init__(self, auth: SnowflakeAuth):
        self.auth = auth
        self._conn: Any = None

    def _connect(self) -> Any:
        if self._conn is not None:
            return self._conn
        try:
            import snowflake.connector  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "snowflake-connector-python not installed. "
                "Install the [snowflake] extra: `pip install snowflake-optimizer[snowflake]`."
            ) from e
        import snowflake.connector as sc
        params: dict[str, Any] = {"account": self.auth.account, "user": self.auth.user}
        if self.auth.password:
            params["password"] = self.auth.password
        if self.auth.authenticator:
            params["authenticator"] = self.auth.authenticator
        if self.auth.warehouse:
            params["warehouse"] = self.auth.warehouse
        if self.auth.role:
            params["role"] = self.auth.role
        self._conn = sc.connect(**params)
        return self._conn

    def execute(self, sql: str, params: list | None = None) -> list[tuple[Any, ...]]:
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute(sql, params or [])
            return cur.fetchall()
        finally:
            cur.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
