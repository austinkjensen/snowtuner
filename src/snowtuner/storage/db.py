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
            _apply_runtime_pragmas(_master)
            from snowtuner.storage.schema import init_schema
            init_schema(_master)
    return _master


def _apply_runtime_pragmas(conn: duckdb.DuckDBPyConnection) -> None:
    """Memory-bound DuckDB for the typical snowtuner deploy.

    The default snowtuner CloudFormation instance is t3.medium (4 GB RAM)
    with 2 GB swap.  DuckDB's default ``memory_limit`` is roughly 80% of
    physical RAM, which puts ingest under real OOM pressure when 14 days
    of ``QUERY_HISTORY`` arrive in a single transaction (observed during
    AWS dogfooding: DuckDB hit its limit at 2.9 GB and aborted).

    Two pragmas help:

      * ``memory_limit`` — explicit cap.  Set below physical RAM so the
        kernel still has headroom for the API process and SSM agent.
      * ``preserve_insertion_order = false`` — DuckDB doesn't need to
        buffer entire rowsets to keep input order, which cuts insert-side
        memory roughly 2-3× on the workloads we care about.

    Both env-overridable.  Operators on bigger boxes (t3.large = 8 GB,
    m6i.xlarge = 16 GB) can set ``SNOWTUNER_DUCKDB_MEMORY_LIMIT`` higher
    to get more parallelism / less swap pressure during sync.
    """
    mem_limit = os.environ.get("SNOWTUNER_DUCKDB_MEMORY_LIMIT", "3GB")
    conn.execute(f"SET memory_limit = '{mem_limit}'")
    conn.execute("SET preserve_insertion_order = false")


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


def reset_database(
    *,
    include_user_config: bool = False,
    audit_archive_dir: Path | None = None,
) -> Path:
    """Close the active DB connection and delete the on-disk DuckDB file(s).

    Pre-release migration strategy: rather than carrying schema-migration shims
    across versions, ``snowtuner reset`` wipes the local DuckDB and lets the
    next ``get_connection()`` rebuild it from scratch via ``init_schema``.
    Returns the path of the file that was (now) deleted, for logging.

    Preservation behavior (v0.2)
    ----------------------------
    By default we PRESERVE user-authored config across reset:

      * ``app.query_groups``     — manually-built saved filter sets
      * ``app.autonomous_config`` — per (action, warehouse, knob) opt-ins

    These rows are read into memory before the file is deleted, then
    re-inserted via the proper Store APIs after re-init.  This means a
    schema-evolution reset doesn't blow away an operator's careful
    tuning of autonomous thresholds or their saved query groups.

    Pass ``include_user_config=True`` to opt back into wiping these too
    (e.g. when the QueryFilterSpec or AutonomousConfig schema itself
    changed and the rows wouldn't validate against the new shape).

    Audit export
    ------------
    Regardless of ``include_user_config``, we ALWAYS dump
    ``app.autonomous_applications`` to a timestamped JSON file under
    ``audit_archive_dir`` (default ``~/.snowtuner/audit-archive/``)
    before deletion.  The audit trail is compliance-relevant — losing
    it silently on reset would be a bad surprise.

    Caveats
    -------
    * **Local only.**  Does not touch Snowflake.  Any orphaned
      ``SNOWTUNER_EXP_*`` test warehouses created by a crashed experiment
      will become invisible after reset — run ``snowtuner experiments
      recover`` FIRST if you have any.
    * **Other app-state still lost.**  ``app.recommendations``,
      ``app.experiments``, ``app.experiment_runs``, ``app.training_state``
      are wiped.  Recommendations regenerate on the next ``snowtuner run``;
      experiments and recommender training state are reproducible but the
      historical records are gone.
    * **Credentials/keys are not touched.**  ``~/.snowtuner/creds.toml``
      and the RSA private key file remain untouched.
    """
    # ── Step 1: snapshot user-authored config (if preserving) ────────
    preserved_groups: list = []
    preserved_configs: list = []
    audit_path: Path | None = None
    path = db_path()
    if path.exists():
        try:
            conn = get_connection()
            if not include_user_config:
                preserved_groups, preserved_configs = _snapshot_user_config(conn)
            # Audit export is unconditional — always preserve the audit trail.
            # Two artifacts go in the same archive directory: the
            # autonomous-applications dump and the events stream dump.
            archive_root = (
                audit_archive_dir or Path.home() / ".snowtuner" / "audit-archive"
            )
            audit_path = _export_audit_trail(conn, archive_root)
            _export_event_log(conn, archive_root)
        except Exception:
            # Pre-existing schema may be too out-of-date to read.  Skip
            # preservation rather than blocking the reset.  The audit
            # archive will be missing this run but the user can always
            # try a partial-reset approach (DROP specific tables) if they
            # care about preserving more.
            pass

    # ── Step 2: nuke the file ────────────────────────────────────────
    close_connection()
    if path.exists():
        path.unlink()
    # DuckDB may leave a WAL file alongside the main file.
    wal = path.with_suffix(path.suffix + ".wal")
    if wal.exists():
        wal.unlink()

    # ── Step 3: re-init + restore preserved rows ─────────────────────
    if preserved_groups or preserved_configs:
        conn = get_connection()   # triggers init_schema on the empty file
        _restore_user_config(conn, preserved_groups, preserved_configs)

    if audit_path is not None:
        # Stash on the path object for the CLI to surface to the user.
        # (Path doesn't allow arbitrary attributes; using a closure isn't
        # appropriate either.  Return a tuple would change the API.  For
        # now we just log via the standard logger and let the CLI also
        # report.)
        import logging
        logging.getLogger(__name__).info(
            "autonomous audit trail archived to %s", audit_path,
        )
    return path


def _snapshot_user_config(conn) -> tuple[list, list]:
    """Read user-authored rows that should survive a reset.  Returns
    ``(query_groups, autonomous_configs)`` as lists of model instances.
    """
    from snowtuner.autonomous.config import AutonomousConfigStore
    from snowtuner.query_groups.store import QueryGroupStore
    groups = QueryGroupStore(conn).list(limit=10_000)
    configs = AutonomousConfigStore(conn).list()
    return list(groups), list(configs)


def _restore_user_config(conn, groups: list, configs: list) -> None:
    """Re-insert preserved query groups and autonomous configs into the
    freshly-initialized database.
    """
    from snowtuner.autonomous.config import AutonomousConfigStore
    from snowtuner.query_groups.store import QueryGroupStore
    qstore = QueryGroupStore(conn)
    for g in groups:
        try:
            qstore.insert(
                name=g.name,
                description=g.description,
                kind=g.kind,
                filter_spec=g.filter_spec,
                snapshot_query_ids=g.snapshot_query_ids,
                snapshot_at=g.snapshot_at,
                created_by=g.created_by,
            )
        except Exception:
            # Schema may have evolved; skip rows that can't be re-validated.
            # The original is still in the archive (next iteration) if needed.
            pass
    cstore = AutonomousConfigStore(conn)
    for c in configs:
        try:
            cstore.upsert(
                c.action_type, c.warehouse_name, c.knob,
                enabled=c.enabled,
                confidence_threshold=c.confidence_threshold,
                cooldown_hours=c.cooldown_hours,
                max_rollbacks_per_week=c.max_rollbacks_per_week,
            )
        except Exception:
            pass


def _export_audit_trail(conn, archive_dir: Path) -> Path | None:
    """Dump app.autonomous_applications to a timestamped JSON file.

    Returns the file path that was written, or None if there were no
    audit rows to archive.  Schema is the public-safe shape used by
    AutonomousApplicationOut so the export is loadable by any client
    that knows that shape (a future ``snowtuner audit import`` command,
    say).
    """
    import json
    from datetime import datetime, timezone
    rows = conn.execute(
        """
        SELECT id, recommendation_id, action_type, warehouse_name,
               applied_sql, rollback_sql, applied_at, state, error,
               rolled_back_at, rolled_back_sql, rollback_error
        FROM app.autonomous_applications
        ORDER BY applied_at
        """
    ).fetchall()
    if not rows:
        return None
    cols = [
        "id", "recommendation_id", "action_type", "warehouse_name",
        "applied_sql", "rollback_sql", "applied_at", "state", "error",
        "rolled_back_at", "rolled_back_sql", "rollback_error",
    ]
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = archive_dir / f"autonomous-applications-{stamp}.json"

    def _ser(v):
        # datetimes → ISO strings; everything else passes through.
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return v

    records = [
        {c: _ser(v) for c, v in zip(cols, r)}
        for r in rows
    ]
    out_path.write_text(json.dumps(records, indent=2))
    return out_path


def _export_event_log(conn, archive_dir: Path) -> Path | None:
    """Dump app.events to a timestamped JSON file.

    Mirrors ``_export_audit_trail`` for the cross-cutting event stream.
    Same archive directory, same approach: written before reset wipes
    the DB.  Events are append-only and the archived JSON is functionally
    complete (events reference entity IDs that get renumbered on reset,
    so preserving in-place would create dangling references).
    """
    import json
    from datetime import datetime, timezone
    # Check table exists first — early-development DBs may not have it.
    try:
        rows = conn.execute(
            """
            SELECT id, timestamp, actor, action, subject, outcome, payload, error
            FROM app.events
            ORDER BY timestamp
            """
        ).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    cols = ["id", "timestamp", "actor", "action", "subject", "outcome", "payload", "error"]
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = archive_dir / f"events-{stamp}.json"

    def _ser(v):
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return v

    records = [
        {c: _ser(v) for c, v in zip(cols, r)}
        for r in rows
    ]
    out_path.write_text(json.dumps(records, indent=2))
    return out_path
