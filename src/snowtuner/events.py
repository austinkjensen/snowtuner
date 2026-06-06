"""Structured event logging for snowtuner.

``log_event(...)`` does two things at once:

1. Inserts a row into ``app.events`` so the event is queryable from
   anywhere the DB is — the UI's activity feed, the API's ``GET /events``,
   MCP tools.
2. Emits a JSON-line to stdout via the stdlib logger so external log
   aggregators (CloudWatch, Datadog, Vector) can ingest events natively.

Both sides are best-effort: if the DB insert fails (concurrent reset,
disk full) we still emit the stdout line; if logger emission fails
(unlikely) we still insert the DB row.  The caller never has to handle
logging-related exceptions.

What to log
-----------
Anything that changes state in snowtuner is a candidate:

  * **Operator actions** — accept/reject recommendation, propose/accept/
    reject/run/abort experiment, create/delete query group, set autonomous
    config, run sync/backfill/reset manually.
  * **Pipeline transitions** — AutomationLoop tick start + complete (with
    per-stage outcomes in the payload), sync completion (one event per
    source), experiment state changes, feature pipeline runs.
  * **Errors** — any failed stage or operation with the error in payload.
  * **Autonomous applies** — every auto-applied SQL with knobs in payload
    (the canonical record stays in ``app.autonomous_applications``; the
    event is just the timeline marker).

Read-only API calls (GET /recommendations etc.) are intentionally NOT
logged — they'd flood the table without producing useful audit data.

What NOT to put in payload
--------------------------
* Query bodies (could contain customer data).
* Credentials, RSA keys, raw bearer tokens.
* Snowflake password values (not currently a concern — we use key-pair auth).

Action namespace
----------------
Dotted ``domain.verb`` strings.  Consistent prefixes make filtering easy::

    recommendation.{accept,reject,supersede,apply}
    experiment.{propose,accept,reject,run.start,run.complete,abort,backfill}
    autonomous.{apply,rollback,config.update,circuit.trip,circuit.reset}
    sync.{start,complete,source.success,source.failure,backfill.start,backfill.complete}
    automation.{tick.start,tick.complete}
    features.{run.start,run.complete}
    query_group.{create,delete}
    reset.{start,complete}
    schema.drift.detected
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import duckdb

# A dedicated logger that we configure to emit JSON-line output to stdout
# regardless of the root logger's level.  The audit feed should never be
# silenced by operator-side verbosity tweaks (e.g. uvicorn --log-level warning).
# Each line is one event as a self-contained JSON object — ingestible by
# CloudWatch / Datadog / Vector / etc.
_event_logger = logging.getLogger("snowtuner.events")


def _configure_event_logger_once() -> None:
    """Idempotently attach a JSON-line stdout handler to the events logger.

    Called lazily on first ``log_event`` invocation so importing this
    module doesn't have side-effects on log configuration.
    """
    if getattr(_event_logger, "_snowtuner_configured", False):
        return
    import sys
    handler = logging.StreamHandler(sys.stdout)
    # No formatter beyond the message itself — the message IS the JSON line.
    handler.setFormatter(logging.Formatter("%(message)s"))
    _event_logger.addHandler(handler)
    _event_logger.setLevel(logging.INFO)
    # Don't propagate to root — root may have its own formatter that would
    # corrupt the JSON-line shape.
    _event_logger.propagate = False
    _event_logger._snowtuner_configured = True  # type: ignore[attr-defined]


def log_event(
    conn: duckdb.DuckDBPyConnection | None,
    *,
    actor: str,
    action: str,
    outcome: str = "success",
    subject: str | None = None,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Record one event in ``app.events`` and emit a JSON line to stdout.

    Parameters
    ----------
    conn
        DuckDB connection to write to.  ``None`` is permitted — only the
        stdout emission happens, useful for early-bootstrap events (logged
        before the DB is ready) or contexts where we don't have a handle.
    actor
        Who initiated the action.  Conventional values: ``'user'``,
        ``'automation'``, ``'engine'``, ``'sync'``, ``'autonomous'``, or
        ``'recommender:<name>'``.
    action
        Dotted namespace verb.  See module docstring for the conventions.
    outcome
        One of ``'success'``, ``'failed'``, ``'skipped'``, ``'started'``.
        ``'started'`` is used for long-running operations where you want
        an entry on both ends (sync.start / sync.complete).
    subject
        The entity the action targeted: warehouse name, recommendation id,
        experiment id, source name, etc.  Free-form; queried by exact match.
    payload
        Optional structured details.  Stored as JSON.  Avoid putting
        secrets or query text here.
    error
        Short error message when outcome='failed'.  The full stack trace
        belongs in regular logger.exception() calls, not here — this is
        meant to be a quick human-readable diagnostic in the audit feed.
    """
    ts = datetime.now(timezone.utc).replace(tzinfo=None)
    _configure_event_logger_once()

    # ── stdout JSON line ─────────────────────────────────────────
    # This part runs regardless of DB state so log aggregators get
    # everything even when DB writes fail.
    record = {
        "timestamp": ts.isoformat() + "Z",
        "actor": actor,
        "action": action,
        "outcome": outcome,
    }
    if subject:
        record["subject"] = subject
    if payload:
        record["payload"] = payload
    if error:
        record["error"] = error
    try:
        _event_logger.info(json.dumps(record, default=_json_default))
    except Exception:
        # Defensive — logger.info shouldn't raise but if it does, swallow.
        # Production should never silently drop events; this is for the
        # truly unexpected case (broken stdout, etc.).
        pass

    # ── DB insert ────────────────────────────────────────────────
    if conn is None:
        return
    try:
        conn.execute(
            """
            INSERT INTO app.events
              (timestamp, actor, action, subject, outcome, payload, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ts,
                actor,
                action,
                subject,
                outcome,
                json.dumps(payload, default=_json_default) if payload else None,
                error,
            ],
        )
    except Exception as e:
        # We don't want event-logging failures to break the operation
        # they're describing.  Surface via the regular logger so it shows
        # up in operator-facing logs even though it didn't make it to the
        # events table.
        logging.getLogger(__name__).warning(
            "could not persist event to app.events: %s; "
            "stdout JSON line was emitted, DB row was not",
            e,
        )


def _json_default(o: Any) -> Any:
    """Fallback serializer for objects ``json.dumps`` doesn't handle natively.

    Datetime → ISO string; enums → their value; anything else → str(o).
    """
    if isinstance(o, datetime):
        return o.isoformat() + ("Z" if o.tzinfo is None else "")
    if hasattr(o, "value"):
        return o.value
    return str(o)


# ── Retention ──────────────────────────────────────────────────────


def prune_events(
    conn: duckdb.DuckDBPyConnection,
    *,
    older_than_days: int,
) -> int:
    """Delete events older than the given window.  Returns the number
    of rows deleted.  Manually invoked via ``snowtuner events prune``;
    no auto-trim in v1.

    The archived JSON dumps written before a ``reset`` are not affected —
    those preserve history regardless of in-DB retention.
    """
    # DuckDB doesn't accept a bound parameter inside an INTERVAL literal,
    # so we have to format the integer into the SQL string.  Safe — the
    # value is constrained to int by the callers (CLI uses click.INT,
    # API uses Query(gt=0)) so there's no injection surface.
    days = int(older_than_days)
    row = conn.execute(
        f"""
        DELETE FROM app.events
        WHERE timestamp < (now() - INTERVAL {days} DAYS)
        RETURNING id
        """
    ).fetchall()
    return len(row)
