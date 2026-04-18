"""DuckDB connection management."""
from __future__ import annotations

import os
from pathlib import Path

import duckdb

DEFAULT_DATA_DIR = Path.home() / ".sfo"
DEFAULT_DB_NAME = "sfo.duckdb"


def data_dir() -> Path:
    return Path(os.environ.get("SFO_DATA_DIR", str(DEFAULT_DATA_DIR)))


def db_path() -> Path:
    return data_dir() / DEFAULT_DB_NAME


_connection: duckdb.DuckDBPyConnection | None = None


def get_connection() -> duckdb.DuckDBPyConnection:
    global _connection
    if _connection is None:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _connection = duckdb.connect(str(path))
        from snowflake_optimizer.storage.schema import init_schema
        init_schema(_connection)
    return _connection


def set_connection(conn: duckdb.DuckDBPyConnection) -> None:
    global _connection
    _connection = conn


def close_connection() -> None:
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None
