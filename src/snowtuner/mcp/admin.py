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


def _client() -> httpx.Client:
    return httpx.Client(base_url=_api_url(), timeout=30.0)


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


# ── Experiments (v0.2) ────────────────────────────────────────────

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


def main() -> None:
    """Entrypoint: start the MCP server on stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
