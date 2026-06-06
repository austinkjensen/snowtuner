"""Admin MCP server.

Exposes the snowtuner FastAPI as MCP tools so a data platform admin can ask
Claude Desktop (or any MCP client) about optimizer state.

Operating model
---------------
The admin runs ``snowtuner api`` first (background service).  Claude Desktop
launches ``snowtuner mcp`` as a stdio subprocess.  This server forwards each
tool call to the API over HTTP — single source of truth for state stays in
one place (the API → DuckDB) and we don't fight the DuckDB single-writer
constraint.

Configure Claude Desktop with::

    {
      "mcpServers": {
        "snowtuner": {
          "command": "/path/to/snowtuner",
          "args": ["mcp"],
          "env": {"SNOWTUNER_API_URL": "http://127.0.0.1:8770"}
        }
      }
    }
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


def _api_url() -> str:
    return os.environ.get("SNOWTUNER_API_URL", "http://127.0.0.1:8770").rstrip("/")


def _auth_headers() -> dict[str, str]:
    """Mirror the API's bearer-token convention.

    When the API runs in ``SNOWTUNER_AUTH_MODE=token`` the MCP server has
    to authenticate too.  Same token plumbing as the rest of snowtuner:
    ``SNOWTUNER_API_TOKEN`` env var first, then ``~/.snowtuner/api_token``.
    In ``none`` mode the header is harmless (server ignores it).
    """
    from snowtuner.api.auth import get_or_create_token
    try:
        return {"Authorization": f"Bearer {get_or_create_token()}"}
    except Exception:
        # If the token file can't be read for some reason, fall through —
        # the API will reject the request with a clear 401 the user can
        # diagnose.
        return {}


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=_api_url(), timeout=30.0, headers=_auth_headers(),
    )


def _get(path: str, **params) -> Any:
    with _client() as c:
        r = c.get(path, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json()


def _post(path: str, json: dict | None = None) -> Any:
    with _client() as c:
        r = c.post(path, json=json or {})
        r.raise_for_status()
        return r.json()


def _put(path: str, json: dict | None = None) -> Any:
    with _client() as c:
        r = c.put(path, json=json or {})
        r.raise_for_status()
        return r.json()


def _delete(path: str) -> Any:
    with _client() as c:
        r = c.delete(path)
        r.raise_for_status()
        return r.json()


mcp = FastMCP(
    "snowtuner-admin",
    instructions=(
        "Tools to inspect snowtuner — a locally-hosted Snowflake cost & "
        "performance advisor.  Use these to look at recommendations, warehouses, "
        "the autonomous-mode audit log; or to accept / reject / rollback "
        "specific proposals.  All operations target the running snowtuner API "
        "(default http://127.0.0.1:8770)."
    ),
)


@mcp.tool()
def get_status() -> dict:
    """Snapshot of ingested-data freshness, per-warehouse activity, recommender
    training state, and recommendation counts by status.

    Use this to answer 'what's the state of my snowtuner installation?' or
    'is the data fresh?'
    """
    return _get("/status")


@mcp.tool()
def list_warehouses() -> list[dict]:
    """List every warehouse snowtuner knows about, with current size,
    auto_suspend setting, and recent activity counts.
    """
    return _get("/warehouses")


@mcp.tool()
def get_warehouse_summary(name: str) -> dict:
    """Fetch the summary for one warehouse by name.

    Returns the same per-warehouse shape as ``list_warehouses`` (size,
    auto_suspend_seconds, auto_resume, queries_in_window,
    suspend_resume_events) — convenience wrapper so an agent doesn't have
    to fetch the whole list to inspect one warehouse.

    Raises a clear error if the warehouse isn't in raw.warehouses (run
    ``run_orchestrator(skip_sync=False)`` to refresh from Snowflake).
    """
    needle = name.upper()
    for wh in _get("/warehouses"):
        if (wh.get("name") or "").upper() == needle:
            return wh
    raise ValueError(
        f"warehouse {name!r} not found in raw.warehouses; "
        f"run sync to refresh from Snowflake or check the name"
    )


@mcp.tool()
def list_recommendations(status: str = "PROPOSED", limit: int = 50) -> list[dict]:
    """List recommendations filtered by status (default PROPOSED).

    Valid status values: PROPOSED, ACCEPTED, REJECTED, APPLIED, ROLLED_BACK,
    SUPERSEDED.  Each item has the proposed action, expected impact, and
    target resource.
    """
    return _get("/recommendations", status=status, limit=limit)


@mcp.tool()
def get_recommendation(rec_id: int) -> dict:
    """Fetch full detail for a single recommendation by id, including the SQL
    that would run, the rollback SQL, rationale, evidence, and confidence."""
    return _get(f"/recommendations/{rec_id}")


@mcp.tool()
def accept_recommendation(rec_id: int, note: str | None = None) -> dict:
    """Mark a recommendation ACCEPTED (advisory).  Does NOT execute against
    Snowflake — accepting is a record-keeping action.  Use the rec's `sql`
    field to apply manually, or enable autonomous mode to apply automatically."""
    return _post(f"/recommendations/{rec_id}/accept", {"note": note})


@mcp.tool()
def reject_recommendation(rec_id: int, note: str | None = None) -> dict:
    """Mark a recommendation REJECTED.  This signals 'don't suggest this
    again' to future runs (subject to the recommender's deduplication logic)."""
    return _post(f"/recommendations/{rec_id}/reject", {"note": note})


@mcp.tool()
def list_recommenders() -> list[dict]:
    """List the recommenders snowtuner has registered.  Each entry includes
    name, version, the action type it produces, and the feature tables it
    requires."""
    return _get("/recommenders")


@mcp.tool()
def list_autonomous_config() -> list[dict]:
    """List per (action_type, warehouse) autonomous-mode configuration.

    Each row says whether autonomous apply is enabled, the confidence
    threshold required, the cooldown window, the rollback budget, and
    whether the circuit breaker is currently open.
    """
    return _get("/autonomous/config")


@mcp.tool()
def enable_autonomous(
    action_type: str,
    warehouse_name: str,
    knob: str = "*",
    confidence_threshold: float = 0.85,
    cooldown_hours: int = 24,
    max_rollbacks_per_week: int = 2,
) -> dict:
    """Enable autonomous apply for (action_type, warehouse_name, knob).

    Use ``"*"`` for warehouse_name to set the catch-all default for the
    action type.  Use ``knob="*"`` (the default) for "every knob this action
    emits"; pass a specific knob like ``"AUTO_SUSPEND"`` or
    ``"WAREHOUSE_SIZE"`` to enable autonomy on just that knob.

    NOTE: for ALTER_WAREHOUSE actions, the SNOWTUNER_ROLE in your Snowflake
    account also needs MODIFY on the target warehouse.  An ACCOUNTADMIN
    must run something like:

      GRANT MODIFY, OPERATE ON WAREHOUSE <name> TO ROLE SNOWTUNER_ROLE;

    in Snowsight.  Without that GRANT, autonomous apply will fail with a
    privilege error.
    """
    cfg = _put(
        f"/autonomous/config/{action_type}/{warehouse_name}/{knob}",
        {
            "enabled": True,
            "confidence_threshold": confidence_threshold,
            "cooldown_hours": cooldown_hours,
            "max_rollbacks_per_week": max_rollbacks_per_week,
        },
    )
    if action_type == "ALTER_WAREHOUSE" and warehouse_name != "*":
        cfg["snowflake_grant_required"] = (
            f"GRANT MODIFY, OPERATE ON WAREHOUSE {warehouse_name} "
            f"TO ROLE SNOWTUNER_ROLE;  -- run in Snowsight as ACCOUNTADMIN"
        )
    return cfg


@mcp.tool()
def disable_autonomous(action_type: str, warehouse_name: str, knob: str = "*") -> dict:
    """Disable autonomous apply for (action_type, warehouse_name, knob) without
    deleting the config row (so the threshold/cooldown settings stick around
    if you re-enable later)."""
    return _put(
        f"/autonomous/config/{action_type}/{warehouse_name}/{knob}",
        {"enabled": False},
    )


@mcp.tool()
def reset_autonomous_circuit(
    action_type: str, warehouse_name: str, knob: str = "*",
) -> dict:
    """Close a tripped circuit breaker.  Run after investigating why
    autonomous apply was rolled back enough times to trip the breaker."""
    return _post(
        f"/autonomous/config/{action_type}/{warehouse_name}/{knob}/reset-circuit"
    )


@mcp.tool()
def list_autonomous_applications(
    warehouse: str | None = None, limit: int = 20,
) -> list[dict]:
    """List recent autonomous applications (audit log).  Each row records
    what was applied, when, the rollback SQL, and current state
    (APPLIED / ROLLED_BACK / FAILED)."""
    return _get("/autonomous/applications", warehouse=warehouse, limit=limit)


@mcp.tool()
def rollback_autonomous_application(application_id: int) -> dict:
    """Roll back a previously-autonomously-applied change.  Executes the
    recorded rollback SQL against Snowflake and updates the audit log.
    Requires Snowflake credentials to be configured on the snowtuner host."""
    return _post(f"/autonomous/applications/{application_id}/rollback")


# ── Experiments ───────────────────────────────────────────────────

@mcp.tool()
def list_experiment_recipes() -> list[dict]:
    """List the preset experiment recipes available.

    A recipe is a one-click template for proposing an A/B-style replay
    experiment against a warehouse.  Use ``propose_experiment`` to actually
    create one.
    """
    return _get("/experiments/recipes")


@mcp.tool()
def list_experiments(
    status: str | None = None,
    target_warehouse: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List experiments (running, completed, proposed, etc.).

    Filter by status (PROPOSED / ACCEPTED / RUNNING / COMPLETED / ABORTED /
    FAILED / REJECTED) or by the warehouse being targeted.
    """
    return _get(
        "/experiments", status=status,
        target_warehouse=target_warehouse, limit=limit,
    )


@mcp.tool()
def get_experiment(experiment_id: int) -> dict:
    """Fetch a single experiment — its proposed spec, arms, eligibility
    issues, lifecycle timestamps, and (if it has completed) the report
    with best-arm identification and savings projection."""
    return _get(f"/experiments/{experiment_id}")


@mcp.tool()
def list_experiment_runs(
    experiment_id: int, arm_name: str | None = None,
) -> list[dict]:
    """Per-(arm, query, rep) observation rows for an experiment.  Use this
    to investigate which queries succeeded/failed and look at raw timing
    data when the report's headline numbers seem off."""
    return _get(f"/experiments/{experiment_id}/runs", arm_name=arm_name)


@mcp.tool()
def propose_experiment(recipe_name: str, target_warehouse: str) -> dict:
    """Propose an experiment using one of the preset recipes.  Returns the
    full proposed-experiment spec including arms, hypothesis, and cost
    estimate range.  The experiment starts in PROPOSED status — a human
    must accept it (and run it) to actually replay queries against
    Snowflake."""
    return _post("/experiments/propose", json={
        "recipe_name": recipe_name,
        "target_warehouse": target_warehouse,
    })


@mcp.tool()
def accept_experiment(experiment_id: int) -> dict:
    """Mark a PROPOSED experiment as ACCEPTED — but does NOT start the
    engine.  Use ``run_experiment`` afterwards (or the snowtuner CLI/UI)
    to actually execute it.  Only one experiment can be accepted or running
    at a time."""
    return _post(f"/experiments/{experiment_id}/accept")


@mcp.tool()
def reject_experiment(experiment_id: int) -> dict:
    """Mark a PROPOSED experiment as REJECTED (terminal)."""
    return _post(f"/experiments/{experiment_id}/reject")


@mcp.tool()
def run_experiment(experiment_id: int) -> dict:
    """Start the engine for an ACCEPTED experiment.  Returns immediately;
    poll ``get_experiment`` for status transitions through RUNNING → COMPLETED.
    Requires Snowflake experiments credentials configured on the snowtuner
    host."""
    return _post(f"/experiments/{experiment_id}/run")


@mcp.tool()
def abort_experiment(experiment_id: int, reason: str) -> dict:
    """Mark an ACCEPTED or RUNNING experiment as ABORTED.  ``reason`` is
    required so the audit trail is useful.  Note: v0.2 doesn't yet
    cooperatively cancel a running engine thread; the engine notices
    status changes between phases."""
    return _post(f"/experiments/{experiment_id}/abort", json={"reason": reason})


@mcp.tool()
def propose_benchmark_experiment(
    hypothesis: str,
    arms: list[dict],
    workload_warehouse: str | None = None,
    query_group_id: int | None = None,
    control_arm_name: str | None = None,
    sample_size: int = 30,
    reps_per_arm: int = 3,
) -> dict:
    """Propose a benchmark experiment with user-built arms.

    Each arm is a dict like
    ``{"name": "medium", "size": "MEDIUM", "generation": "2",
       "qas_state": "off"}``.  Pass either a ``workload_warehouse`` to
    auto-sample queries from OR a ``query_group_id`` to use a saved group.
    ``control_arm_name`` is optional; without it the report shows a
    Pareto frontier with no designated baseline.
    """
    body: dict = {
        "hypothesis": hypothesis,
        "arms": arms,
        "control_arm_name": control_arm_name,
        "sample_size": sample_size,
        "reps_per_arm": reps_per_arm,
    }
    if workload_warehouse:
        body["workload_warehouse"] = workload_warehouse
    if query_group_id is not None:
        body["query_group_id"] = query_group_id
    return _post("/experiments/propose-benchmark", json=body)


@mcp.tool()
def remove_sampled_query_from_experiment(
    experiment_id: int, query_id: str,
) -> dict:
    """Remove a single query from a PROPOSED experiment's frozen workload.

    Useful when previewing the proposed workload and one query is known to
    be unrepresentative or unsafe to replay.  Re-estimates cost from the
    remaining queries.  Refuses if status != PROPOSED or removing would
    leave the workload empty.
    """
    return _delete(f"/experiments/{experiment_id}/sampled-queries/{query_id}")


@mcp.tool()
def backfill_experiment_metrics(experiment_id: int) -> dict:
    """Recover metrics on a COMPLETED experiment whose live fetch was empty.

    Pulls elapsed_ms / bytes_scanned from
    ``SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY`` (~45-minute lag) for every
    SUCCESS run with a replay_query_id but no elapsed_ms.  UPDATEs the run
    rows in place, then re-aggregates and writes the new report.

    Use when you see ``"control arm produced no successful runs"`` in a
    completed experiment's report despite seeing queries actually run.
    """
    return _post(f"/experiments/{experiment_id}/backfill-metrics")


# ── Query groups + queries ───────────────────────────────────────

@mcp.tool()
def list_query_groups(limit: int = 100) -> list[dict]:
    """List all saved query groups.  Each entry includes name, kind
    (static/dynamic), and current member count."""
    return _get("/query-groups", limit=limit)


@mcp.tool()
def create_query_group(
    name: str,
    kind: str,
    description: str | None = None,
    warehouse_name: str | None = None,
    user_name: str | None = None,
    role_name: str | None = None,
    query_type: str | None = None,
    execution_status: str | None = None,
    query_parameterized_hash: str | None = None,
    start_time_from: str | None = None,
    start_time_to: str | None = None,
    min_elapsed_ms: int | None = None,
    max_elapsed_ms: int | None = None,
    has_remote_spill: bool | None = None,
    has_local_spill: bool | None = None,
    has_queueing: bool | None = None,
    search: str | None = None,
    min_joins: int | None = None,
    max_joins: int | None = None,
    min_tables: int | None = None,
    max_tables: int | None = None,
    min_ctes: int | None = None,
    max_ctes: int | None = None,
    referenced_tables_include: str | None = None,
    referenced_tables_exclude: str | None = None,
    where_columns_include: str | None = None,
    where_columns_exclude: str | None = None,
) -> dict:
    """Create a saved query group.

    ``kind`` must be ``"static"`` (snapshot membership at creation time —
    immutable) or ``"dynamic"`` (re-evaluate filter on every read).  The
    rest of the args are the same filters ``search_queries`` accepts;
    multi-value categorical fields take comma-separated strings.  Numeric
    range filters (``min_*``/``max_*``) come from the Phase 1 structural
    counts.  Phase 2 semantic filters (``referenced_tables_include``,
    ``where_columns_include``, etc.) take comma-separated names.

    Returns the created group with its assigned id and member_count.
    """
    body: dict = {"name": name, "kind": kind, "description": description}
    # Pass through any non-None filter argument; the server normalizes
    # comma-strings into list[str].
    for k, v in {
        "warehouse_name": warehouse_name, "user_name": user_name,
        "role_name": role_name, "query_type": query_type,
        "execution_status": execution_status,
        "query_parameterized_hash": query_parameterized_hash,
        "start_time_from": start_time_from, "start_time_to": start_time_to,
        "min_elapsed_ms": min_elapsed_ms, "max_elapsed_ms": max_elapsed_ms,
        "has_remote_spill": has_remote_spill,
        "has_local_spill": has_local_spill,
        "has_queueing": has_queueing, "search": search,
        "min_joins": min_joins, "max_joins": max_joins,
        "min_tables": min_tables, "max_tables": max_tables,
        "min_ctes": min_ctes, "max_ctes": max_ctes,
        "referenced_tables_include": referenced_tables_include,
        "referenced_tables_exclude": referenced_tables_exclude,
        "where_columns_include": where_columns_include,
        "where_columns_exclude": where_columns_exclude,
    }.items():
        if v is not None:
            body[k] = v
    return _post("/query-groups", json=body)


@mcp.tool()
def get_query_group(group_id: int) -> dict:
    """Fetch one query group's full record: filter_spec, snapshot_query_ids
    (static groups only), and the member count."""
    return _get(f"/query-groups/{group_id}")


@mcp.tool()
def get_query_group_members(group_id: int, limit: int = 200) -> dict:
    """Fetch the current members of a query group.

    For static groups, returns the frozen snapshot.  For dynamic groups,
    re-evaluates the filter_spec against raw.query_history at call time.
    """
    return _get(f"/query-groups/{group_id}/members", limit=limit)


@mcp.tool()
def delete_query_group(group_id: int) -> dict:
    """Delete a saved query group.  Doesn't touch any experiments that
    referenced it (their workload was frozen at propose time)."""
    return _delete(f"/query-groups/{group_id}")


@mcp.tool()
def search_queries(
    warehouse: str | None = None,
    user: str | None = None,
    role: str | None = None,
    query_type: str | None = None,
    status: str | None = None,
    min_elapsed_ms: int | None = None,
    max_elapsed_ms: int | None = None,
    has_remote_spill: bool | None = None,
    search: str | None = None,
    referenced_tables_include: str | None = None,
    where_columns_include: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Search ingested queries with the same filters the UI exposes.

    Multi-value fields (warehouse, user, role, query_type, status) accept
    comma-separated strings.  ``referenced_tables_include`` and
    ``where_columns_include`` are Phase 2 semantic filters — comma-separated
    table/column names that the query must touch.

    Returns a paginated list response: ``{rows, total, limit, offset}``.
    """
    return _get(
        "/queries",
        warehouse=warehouse, user=user, role=role,
        query_type=query_type, status=status,
        min_elapsed_ms=min_elapsed_ms, max_elapsed_ms=max_elapsed_ms,
        has_remote_spill=has_remote_spill, search=search,
        referenced_tables_include=referenced_tables_include,
        where_columns_include=where_columns_include,
        limit=limit, offset=offset,
    )


@mcp.tool()
def get_query_detail(query_id: str) -> dict:
    """Fetch full detail for one query: text, structural counts (joins,
    tables, CTEs, etc.), and Phase 2 semantic data (referenced_tables,
    where_columns)."""
    return _get(f"/queries/{query_id}")


@mcp.tool()
def get_query_facets() -> dict:
    """Get distinct values for the query-explorer filter dropdowns:
    warehouses, users, roles, query_types, execution_statuses, plus the
    top-N most-used referenced_tables and where_columns."""
    return _get("/queries/facets")


# ── Orchestration ────────────────────────────────────────────────

@mcp.tool()
def run_orchestrator(skip_sync: bool = True) -> dict:
    """Run the full optimization pipeline: feature transforms + every
    registered recommender.  When ``skip_sync=False``, also runs the
    Snowflake → DuckDB sync first (slower; usually unnecessary for an
    ad-hoc re-run because sync happens hourly via the background scheduler)."""
    return _post("/orchestrator/run", json={"skip_sync": skip_sync})


@mcp.tool()
def run_sync() -> dict:
    """Run ONLY the Snowflake → DuckDB sync (no features, no recommenders).

    Pulls deltas from ACCOUNT_USAGE views into ``raw.*``, respecting each
    source's watermark.  Cheap on subsequent runs (only new rows since
    the high-water mark).  Use this when you want fresh raw data without
    re-running the full optimizer pipeline.
    """
    return _post("/sync/run")


@mcp.tool()
def run_backfill(days: int, source: str | None = None) -> dict:
    """Re-pull a wider historical window without destroying app.* state.

    Mechanism: DELETE the sync watermarks for the targeted incremental
    sources, then sync with ``days`` lookback.  Preserves recommendations,
    experiments + reports, autonomous configs + audit trail, saved query
    groups, and derived features.

    Use this when:
      * 14-day initial lookback wasn't long enough for your analysis
      * You want to refetch a window because rows were redacted or changed
      * You added a new source mid-deployment and want history for it
    """
    return _post(f"/sync/backfill?days={days}" + (f"&source={source}" if source else ""))


@mcp.tool()
def run_features() -> dict:
    """Run ONLY the feature transforms (no sync, no recommenders).

    Recomputes derived tables in ``features.*`` from whatever's currently
    in ``raw.*``.  Cheap if nothing changed (incremental transforms skip
    already-processed rows).  Use this after a sync if you want fresh
    structural / semantic data without triggering recommender output.
    """
    return _post("/features/run")


@mcp.tool()
def list_events(
    actor: str | None = None,
    action: str | None = None,
    action_prefix: str | None = None,
    subject: str | None = None,
    outcome: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Paginated, filterable feed from app.events.

    The events table is the cross-cutting timeline of state-changing
    actions across snowtuner: operator clicks, AutomationLoop ticks per
    stage, sync outcomes per source, autonomous applies.  Use this to
    answer "what happened between X and Y?" without joining the domain
    tables.

    Common patterns:
      * list_events(action_prefix='experiment.', limit=20) — recent
        experiment lifecycle activity
      * list_events(actor='autonomous') — every autonomous apply
      * list_events(action='sync.source.failure') — sync failures only
      * list_events(subject='ETL_WH') — everything touching ETL_WH
    """
    return _get(
        "/events",
        actor=actor, action=action, action_prefix=action_prefix,
        subject=subject, outcome=outcome,
        since=since, until=until, limit=limit, offset=offset,
    )


@mcp.tool()
def get_automation_status() -> dict:
    """Snapshot the AutomationLoop's state.

    Returns whether the loop is enabled (SNOWTUNER_AUTOMATION_INTERVAL>0),
    the configured interval, when the next tick fires, and a fully-
    decomposed report of the last tick (per-stage outcomes, durations,
    errors).  Use this to verify automation is configured correctly and
    to debug ticks that failed silently in the background.
    """
    return _get("/automation/status")


@mcp.tool()
def run_automation_now() -> dict:
    """Trigger one tick of the AutomationLoop synchronously.

    Runs the full sync→features→recommenders→autonomous pipeline now,
    rather than waiting for the next scheduled interval.  Returns the
    tick report when complete.

    Useful for kicking off a fresh cycle after enabling autonomous mode
    or installing a new recommender — you don't have to wait an hour to
    see whether it works.  Refuses (returns a skipped report) if another
    tick is already running.
    """
    return _post("/automation/run-now")


@mcp.tool()
def get_schema_drift() -> dict:
    """Detect schema drift between snowtuner's expected source columns and
    what Snowflake's ACCOUNT_USAGE views actually expose.

    Warn-only — never auto-evolves.  Use when sync starts failing with
    ``invalid identifier`` errors, or to check whether a Snowflake release
    added columns we should mirror.
    """
    return _get("/schema/drift")


@mcp.tool()
def get_credentials_status() -> dict:
    """Public-safe view of the resolved Snowflake credentials.  Includes
    account / user / role / source backend, but never the secrets themselves.
    Use to debug 'why isn't snowtuner connecting'."""
    return _get("/credentials/status")


def main() -> None:
    """Entrypoint: start the MCP server on stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
