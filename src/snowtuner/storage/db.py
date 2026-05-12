"""DuckDB connection management — thread-safe via per-thread cursors.

DuckDB's Python ``DuckDBPyConnection`` is NOT thread-safe; concurrent calls
from multiple threads will SIGSEGV the process.  Uvicorn happily runs sync
FastAPI handlers in a worker thread pool, so we hit this immediately under
real load.

The fix DuckDB documents: open one *master* connection and call
``master.cursor()`` to get a per-thread "duplicate" connection that shares
the underlying database.  Each cursor has the full ``DuckDBPyConnection``
API (``execute``, ``fetchall``, etc.) and is safe to use from its own
thread concurrently with other cursors.

We use ``threading.local`` to lazily mint one cursor per thread.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import duckdb

DEFAULT_DATA_DIR = Path.home() / ".snowtuner"
DEFAULT_DB_NAME = "snowtuner.duckdb"


def naive_utcnow() -> datetime:
    """Return *now* as a naive datetime whose wall-clock components are UTC.

    DuckDB's Python binding silently converts timezone-aware datetimes to
    local time before stripping the tz when binding to a TIMESTAMP column —
    so passing ``datetime.now(timezone.utc)`` ends up storing the local
    wall-clock value.  Use this helper at every DuckDB write site so the
    stored value really is UTC; on read-back, the naive datetime can be
    treated as UTC by convention.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def data_dir() -> Path:
    return Path(os.environ.get("SNOWTUNER_DATA_DIR", str(DEFAULT_DATA_DIR)))


def db_path() -> Path:
    return data_dir() / DEFAULT_DB_NAME


_master: duckdb.DuckDBPyConnection | None = None
_master_lock = threading.Lock()
_thread_local = threading.local()


def _ensure_master() -> duckdb.DuckDBPyConnection:
    global _master
    if _master is not None:
        return _master
    with _master_lock:
        if _master is None:
            path = db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            _master = duckdb.connect(str(path))
            from snowtuner.storage.schema import init_schema
            init_schema(_master)
    return _master


def get_connection() -> duckdb.DuckDBPyConnection:
    """Return a thread-local cursor over the shared DuckDB.

    First call on a given thread creates a cursor; subsequent calls reuse it.
    The returned object has the full ``DuckDBPyConnection`` API.
    """
    master = _ensure_master()
    cursor = getattr(_thread_local, "cursor", None)
    if cursor is None:
        cursor = master.cursor()
        _thread_local.cursor = cursor
    return cursor


def set_connection(conn: duckdb.DuckDBPyConnection) -> None:
    """Inject a connection (used by tests).  Treats ``conn`` as the master and
    pins it for the current thread; other threads will get their own cursors
    off this master via ``conn.cursor()`` on first access."""
    global _master, _thread_local
    _master = conn
    _thread_local = threading.local()
    _thread_local.cursor = conn


def close_connection() -> None:
    """Close the master and clear all per-thread cursors.  Mostly for tests."""
    global _master, _thread_local
    if _master is not None:
        try:
            _master.close()
        except Exception:
            pass
        _master = None
    _thread_local = threading.local()
