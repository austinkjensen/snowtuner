"""`snowtuner` CLI — drive the optimizer from a terminal."""
from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from snowtuner.credentials import (
    AuthMethod,
    CredentialBackend,
    CredentialResolver,
    SnowflakeCredentials,
    generate_keypair,
    public_blob_from_private,
)
from snowtuner import format
from snowtuner.autonomous import (
    AutonomousApplicationStore,
    AutonomousConfigStore,
    AutonomousRunner,
    CATCH_ALL,
)
from snowtuner.experiments import ExperimentStatus
from snowtuner.features import DEFAULT_TRANSFORMS
from snowtuner.features.base import FeaturePipeline
from snowtuner.ingestion.snowflake_client import SnowflakeClient
from snowtuner.ingestion.sources import DEFAULT_SOURCES
from snowtuner.ingestion.sync import sync_all
from snowtuner.orchestrator import Orchestrator
from snowtuner.recommendations import RecommendationStatus, RecommendationStore
from snowtuner.recommenders.registry import default_registry
from snowtuner.seed import seed_demo_data
from snowtuner.storage import get_connection

console = Console()


@click.group()
def cli() -> None:
    """snowtuner — locally-hosted Snowflake cost & performance advisor."""


@cli.command()
@click.option("--days", default=21, show_default=True, help="Days of synthetic history to generate")
def seed(days: int) -> None:
    """Clear raw.* and populate with synthetic demo data."""
    conn = get_connection()
    counts = seed_demo_data(conn, days=days)
    for tbl, n in counts.items():
        console.print(f"  {tbl}: {n} rows")
    console.print("[green]Seed complete.[/green]")


@cli.command()
@click.option("--yes", is_flag=True, default=False,
              help="Skip the confirmation prompt.  Useful for scripted resets.")
def reset(yes: bool) -> None:
    """Wipe the local snowtuner.duckdb and re-initialize from scratch.

    Pre-release we don't ship schema migrations; when the schema changes,
    upgrade by running this command then [cyan]snowtuner sync[/cyan] to
    repopulate raw.* from Snowflake.

    DOES NOT TOUCH: credentials (~/.snowtuner/creds.toml), the RSA key,
    or anything on Snowflake.

    DOES WIPE: app.recommendations, app.experiments, app.autonomous_applications,
    app.training_state, app.sync_watermarks, and all of raw.* and features.*.

    If you have orphaned SNOWTUNER_EXP_* test warehouses on Snowflake from a
    crashed experiment, run [cyan]snowtuner experiments recover[/cyan] FIRST —
    after reset, snowtuner forgets their names and can't clean them up
    for you.
    """
    from snowtuner.storage.db import db_path, reset_database

    # Pre-flight: check if any experiments have un-cleaned test warehouses.
    # If we can't even open the DB cleanly (schema mismatch from a prior
    # version), skip the check and surface a generic warning.
    orphan_backlog: list = []
    try:
        from snowtuner.experiments import ExperimentStore
        store = ExperimentStore(get_connection())
        orphan_backlog = store.needing_cleanup()
    except Exception:
        orphan_backlog = []

    path = db_path()
    if not path.exists():
        console.print(f"[dim]Nothing to delete — {path} doesn't exist yet.[/dim]")
    else:
        size_mb = path.stat().st_size / (1024 * 1024)
        console.print(
            f"[bold]This will delete[/bold] [cyan]{path}[/cyan] "
            f"([dim]{size_mb:.1f} MB[/dim])"
        )

    if orphan_backlog:
        console.print()
        console.print(
            f"[yellow]Warning:[/yellow] {len(orphan_backlog)} experiment(s) have "
            f"un-cleaned test warehouses on Snowflake."
        )
        for exp in orphan_backlog:
            names = ", ".join(exp.test_warehouse_names) or "(none recorded)"
            console.print(f"  experiment #{exp.id}: {names}")
        console.print(
            "[yellow]Recommended:[/yellow] cancel this command, run "
            "[cyan]snowtuner experiments recover[/cyan] first to drop them via "
            "Snowflake, then re-run [cyan]snowtuner reset[/cyan]."
        )
        console.print()

    if not yes:
        if not click.confirm("Proceed with reset?", default=False):
            console.print("[dim]Cancelled.[/dim]")
            raise SystemExit(0)

    deleted = reset_database()
    console.print(f"[green]Wiped[/green] {deleted}")

    # Trigger fresh init so the file exists with the current schema.
    get_connection()
    console.print(f"[green]Recreated[/green] with current schema.")
    console.print(
        "Next steps: [cyan]snowtuner sync[/cyan] to repopulate raw.*, then "
        "[cyan]snowtuner run[/cyan] to regenerate recommendations."
    )


@cli.command()
@click.option("--lookback-days", default=14, show_default=True,
              help="On sources with no stored watermark yet, look back this far.")
@click.option("--source", "source_filter", default=None,
              help="Run only one source by name (e.g. 'warehouses'). Repeatable not supported.")
def sync(lookback_days: int, source_filter: str | None) -> None:
    """Ingest from Snowflake into local DuckDB.  Uses the stored service-user creds."""
    conn = get_connection()
    try:
        client = SnowflakeClient.from_resolver()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)

    sources = list(DEFAULT_SOURCES)
    if source_filter:
        sources = [s for s in sources if s.name == source_filter]
        if not sources:
            names = ", ".join(s.name for s in DEFAULT_SOURCES)
            console.print(f"[red]No source named {source_filter!r}.[/red]  "
                          f"Available: {names}")
            raise SystemExit(1)

    console.print(
        f"[bold]Syncing[/bold] {len(sources)} source(s) with lookback={lookback_days}d…"
    )
    results, errors = sync_all(
        sources, client, conn, initial_lookback_days=lookback_days,
    )
    client.close()

    tbl = Table(show_header=True, header_style="bold")
    tbl.add_column("Source")
    tbl.add_column("Rows", justify="right")
    tbl.add_column("High water")
    tbl.add_column("Duration", justify="right")
    for r in results:
        tbl.add_row(
            r.source_name, str(r.rows_ingested),
            str(r.high_water or "—"),
            f"{r.duration_seconds:.2f}s",
        )
    console.print(tbl)

    if errors:
        console.print("\n[red]Errors:[/red]")
        for e in errors:
            console.print(f"  • [bold]{e.source_name}[/bold]: {e.error}")
        raise SystemExit(2)

    console.print("[green]Sync complete.[/green]")


@cli.command()
def status() -> None:
    """Snapshot of ingested data, warehouses, recommenders, and recommendations."""
    from datetime import datetime, timezone
    conn = get_connection()

    # ── Data freshness per source ──────────────────────────────────
    sources_meta = [
        ("query_history",              "raw.query_history",              "start_time"),
        ("warehouse_metering_history", "raw.warehouse_metering_history", "start_time"),
        ("warehouse_events_history",   "raw.warehouse_events_history",   "timestamp"),
        ("warehouses",                 "raw.warehouses",                 None),
    ]
    data_tbl = Table(title="Data", show_header=True, header_style="bold",
                     title_style="bold", title_justify="left")
    data_tbl.add_column("Source")
    data_tbl.add_column("Rows", justify="right")
    data_tbl.add_column("Date range")
    data_tbl.add_column("Last synced")

    for source_name, tbl, ts_col in sources_meta:
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        if ts_col and n:
            lo, hi = conn.execute(f"SELECT MIN({ts_col}), MAX({ts_col}) FROM {tbl}").fetchone()
            date_range = f"{_fmt_dt(lo)}  →  {_fmt_dt(hi)}"
        elif ts_col:
            date_range = "—"
        else:
            date_range = "(full refresh)"
        wm_row = conn.execute(
            "SELECT last_sync_at FROM app.sync_watermarks WHERE source_name = ?",
            [source_name],
        ).fetchone()
        last_sync = _humanize_ago(wm_row[0]) if wm_row else "never"
        data_tbl.add_row(source_name, f"{n:,}", date_range, last_sync)
    console.print(data_tbl)

    # ── Per-warehouse activity ────────────────────────────────────
    wh_rows = conn.execute(
        """
        SELECT w.name, w.size, w.auto_suspend_seconds,
               (SELECT COUNT(*) FROM raw.query_history q WHERE q.warehouse_name = w.name) AS q_cnt,
               (SELECT COUNT(*) FROM raw.warehouse_events_history e
                  WHERE e.warehouse_name = w.name
                    AND e.event_name IN ('SUSPEND_WAREHOUSE','RESUME_WAREHOUSE')) AS cycle_cnt
        FROM raw.warehouses w
        ORDER BY q_cnt DESC
        """
    ).fetchall()
    if wh_rows:
        wh_tbl = Table(title="\nWarehouses", show_header=True, header_style="bold",
                       title_style="bold", title_justify="left")
        wh_tbl.add_column("Name")
        wh_tbl.add_column("Size")
        wh_tbl.add_column("Auto-suspend (s)", justify="right")
        wh_tbl.add_column("Queries", justify="right")
        wh_tbl.add_column("Susp/Res events", justify="right")
        for name, size, asusp, q_cnt, cycle_cnt in wh_rows:
            wh_tbl.add_row(
                name or "—",
                size or "—",
                "—" if asusp is None else str(asusp),
                f"{q_cnt:,}",
                f"{cycle_cnt:,}",
            )
        console.print(wh_tbl)

    # ── Recommender training state ────────────────────────────────
    registry = default_registry()
    rec_tbl = Table(title="\nRecommenders", show_header=True, header_style="bold",
                    title_style="bold", title_justify="left")
    rec_tbl.add_column("Name")
    rec_tbl.add_column("State")
    rec_tbl.add_column("Last fit")
    rec_tbl.add_column("Notes")
    for r in registry.all():
        ts_row = conn.execute(
            """
            SELECT is_ready, last_fit_at, readiness_report
            FROM app.training_state WHERE recommender_name = ?
            """,
            [r.name],
        ).fetchone()
        if ts_row is None:
            state, last_fit, notes = "untrained", "—", "run `snowtuner run` once"
        else:
            is_ready, last_fit_at, readiness_json = ts_row
            state = "[green]ready[/green]" if is_ready else "[yellow]training[/yellow]"
            last_fit = _humanize_ago(last_fit_at) if last_fit_at else "—"
            notes = ""
            if readiness_json:
                import json as _json
                try:
                    notes = (_json.loads(readiness_json) or {}).get("reason", "")
                except Exception:
                    pass
        rec_tbl.add_row(r.name, state, last_fit, notes)
    console.print(rec_tbl)

    # ── Recommendations summary ───────────────────────────────────
    counts = {row[0]: row[1] for row in conn.execute(
        "SELECT status, COUNT(*) FROM app.recommendations GROUP BY status"
    ).fetchall()}
    parts = [
        f"{s.value}: [bold]{counts.get(s.value, 0)}[/bold]"
        for s in RecommendationStatus
    ]
    console.print("\n[bold]Recommendations[/bold]   " + "   ".join(parts))


def _fmt_dt(v: object) -> str:
    if v is None:
        return "—"
    if hasattr(v, "isoformat"):
        s = v.isoformat(sep=" ", timespec="minutes")  # type: ignore[union-attr]
        return s
    return str(v)


def _humanize_ago(ts: object) -> str:
    """Format a stored timestamp as 'X ago'.

    Stored timestamps are naive UTC by convention (see storage.db.naive_utcnow).
    Strip any tz the caller passes in so both sides of the comparison are naive.
    """
    from snowtuner.storage.db import naive_utcnow
    if ts is None:
        return "—"
    if not hasattr(ts, "timestamp"):
        return str(ts)
    now = naive_utcnow()
    t = ts  # type: ignore[assignment]
    if getattr(t, "tzinfo", None) is not None:
        t = t.replace(tzinfo=None)  # type: ignore[union-attr]
    secs = int((now - t).total_seconds())  # type: ignore[operator]
    if secs < 0:
        return "in the future"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


@cli.command()
@click.option("--skip-sync/--no-skip-sync", default=True,
              help="Skip Snowflake sync (default yes — assumes raw.* already populated)")
@click.option("--auto/--no-auto", default=True,
              help="Run the autonomous-apply pass after recommenders finish.  Requires "
                   "Snowflake credentials and at least one enabled autonomous config row.")
def run(skip_sync: bool, auto: bool) -> None:
    """Run features + recommenders and persist Recommendations."""
    conn = get_connection()
    pipeline = FeaturePipeline(DEFAULT_TRANSFORMS)
    registry = default_registry()
    orch = Orchestrator(conn, pipeline=pipeline, registry=registry)

    client = None
    if auto:
        try:
            client = SnowflakeClient.from_resolver()
        except RuntimeError:
            client = None  # no creds; orchestrator will report 'skipped'

    report = orch.run(skip_sync=skip_sync, client=client)

    console.print("[bold]Feature pipeline:[/bold]")
    for f in report.feature_results:
        console.print(f"  • {f.name}  ({f.duration_seconds:.3f}s)")

    console.print("\n[bold]Recommenders:[/bold]")
    for r in report.recommender_results:
        status = "ready" if r.is_ready else "training"
        err = f"  [red]error:[/red] {r.error}" if r.error else ""
        console.print(
            f"  • {r.name}: {status} — {r.readiness_reason}  "
            f"[blue]{r.predictions_emitted} proposal(s)[/blue]{err}"
        )

    if report.autonomous_report is not None:
        applied = report.autonomous_report.applied()
        failed = report.autonomous_report.failed()
        console.print(
            f"\n[bold]Autonomous:[/bold] [green]{len(applied)} applied[/green], "
            f"[red]{len(failed)} failed[/red], "
            f"{len(report.autonomous_report.decisions) - len(applied) - len(failed)} skipped"
        )
        for d in applied:
            console.print(f"  ✓ #{d.recommendation_id}  {d.action_type} "
                          f"on {d.warehouse_name}  ({d.reason})")
        for d in failed:
            console.print(f"  [red]✗ #{d.recommendation_id}[/red]  "
                          f"{d.action_type} on {d.warehouse_name}: {d.reason}")
    elif report.autonomous_skipped_reason:
        console.print(f"\n[dim]Autonomous: skipped — "
                      f"{report.autonomous_skipped_reason}[/dim]")


@cli.command("list")
@click.option("--status", type=click.Choice([s.value for s in RecommendationStatus]),
              default=RecommendationStatus.PROPOSED.value)
@click.option("--limit", default=50, show_default=True)
def list_recs(status: str, limit: int) -> None:
    """List recommendations."""
    conn = get_connection()
    store = RecommendationStore(conn)
    recs = store.list(status=RecommendationStatus(status), limit=limit)

    if not recs:
        console.print(f"[yellow]No recommendations with status={status}[/yellow]")
        return

    tbl = Table(show_header=True, header_style="bold")
    tbl.add_column("ID", justify="right")
    tbl.add_column("Type")
    tbl.add_column("Target")
    tbl.add_column("Proposal", max_width=40)
    tbl.add_column("Impact (credits/day)", justify="right")
    tbl.add_column("Confidence", justify="right")
    for r in recs:
        tbl.add_row(
            str(r.id),
            r.action.type.value,
            r.action.target_resource() or "—",
            r.action.dry_run_preview().splitlines()[-1][:60],
            format.credits_delta(r.expected_impact.credits_delta_daily),
            f"{r.expected_impact.confidence:.2f}",
        )
    console.print(tbl)


@cli.command()
@click.argument("rec_id", type=int)
def show(rec_id: int) -> None:
    """Show full detail for one recommendation."""
    conn = get_connection()
    store = RecommendationStore(conn)
    rec = store.get(rec_id)
    if not rec:
        console.print(f"[red]No recommendation with id={rec_id}[/red]")
        raise SystemExit(1)

    console.rule(f"[bold]Recommendation #{rec.id}[/bold]")
    console.print(f"[bold]Generated by:[/bold] {rec.generated_by}")
    console.print(f"[bold]Status:[/bold] {rec.status.value}")
    console.print(f"[bold]Target:[/bold] {rec.action.target_resource()}")
    console.print()
    console.print("[bold]Preview[/bold]")
    console.print(rec.action.dry_run_preview())
    console.print()
    console.print("[bold]SQL to run[/bold]")
    console.print(f"[cyan]{rec.action.to_sql()}[/cyan]")
    if rec.rollback_sql or (hasattr(rec.action, "rollback_sql") and rec.action.rollback_sql()):  # type: ignore[attr-defined]
        rollback = rec.rollback_sql or rec.action.rollback_sql()  # type: ignore[attr-defined]
        console.print()
        console.print("[bold]Rollback[/bold]")
        console.print(f"[dim]{rollback}[/dim]")
    console.print()
    console.print("[bold]Rationale[/bold]")
    console.print(rec.rationale)
    console.print()
    console.print("[bold]Evidence[/bold]")
    for ev in rec.evidence:
        val = "" if ev.value is None else f"  value={ev.value}"
        console.print(f"  • [{ev.kind}] {ev.description}{val}")
    console.print()
    console.print("[bold]Expected impact[/bold]")
    console.print(rec.expected_impact.model_dump_json(indent=2))


@cli.command()
@click.argument("rec_id", type=int)
@click.option("--note", default=None)
def accept(rec_id: int, note: str | None) -> None:
    """Mark a recommendation ACCEPTED (advisory-only — does not execute)."""
    conn = get_connection()
    store = RecommendationStore(conn)
    if not store.get(rec_id):
        console.print(f"[red]No recommendation with id={rec_id}[/red]")
        raise SystemExit(1)
    store.set_status(rec_id, RecommendationStatus.ACCEPTED, notes=note)
    console.print(f"[green]#{rec_id} marked ACCEPTED.[/green]  "
                  f"Run `snowtuner show {rec_id}` then execute the SQL yourself.")


@cli.command()
@click.argument("rec_id", type=int)
@click.option("--note", default=None)
def reject(rec_id: int, note: str | None) -> None:
    """Mark a recommendation REJECTED."""
    conn = get_connection()
    store = RecommendationStore(conn)
    if not store.get(rec_id):
        console.print(f"[red]No recommendation with id={rec_id}[/red]")
        raise SystemExit(1)
    store.set_status(rec_id, RecommendationStatus.REJECTED, notes=note)
    console.print(f"[yellow]#{rec_id} marked REJECTED.[/yellow]")


@cli.command()
def ui() -> None:
    """Launch the Streamlit UI."""
    import subprocess
    import sys
    from pathlib import Path

    app_path = Path(__file__).parent / "ui" / "app.py"
    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", str(app_path)],
        check=False,
    )


@cli.command()
def recommenders() -> None:
    """List the built-in recommenders."""
    reg = default_registry()
    if not reg.all():
        console.print("[yellow]No recommenders registered.[/yellow]")
        return
    tbl = Table(show_header=True, header_style="bold")
    tbl.add_column("Name")
    tbl.add_column("Version")
    tbl.add_column("Action type")
    tbl.add_column("Class")
    for r in reg.all():
        tbl.add_row(
            r.name, r.version, r.action_type.value,
            f"{r.__class__.__module__}.{r.__class__.__name__}",
        )
    console.print(tbl)


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8770, show_default=True)
@click.option("--reload", is_flag=True, default=False, help="Enable autoreload (dev only)")
@click.option(
    "--loop",
    default="asyncio",
    type=click.Choice(["asyncio", "uvloop"]),
    show_default=True,
    help="Event loop backend.  uvloop has shown SIGSEGV interactions with "
         "DuckDB on Python 3.14; default 'asyncio' is the safer pick for now.",
)
def api(host: str, port: int, reload: bool, loop: str) -> None:
    """Launch the HTTP API service."""
    import uvicorn
    uvicorn.run(
        "snowtuner.api.app:create_app",
        host=host, port=port, reload=reload, factory=True, loop=loop,
    )


@cli.command()
def mcp() -> None:
    """Launch the Admin MCP server (stdio transport).

    Requires the snowtuner API to be running (`snowtuner api`).  Configure
    Claude Desktop with this in `claude_desktop_config.json`:

    \b
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
    from snowtuner.mcp.admin import main
    main()


DEFAULT_SVC_USER = "SNOWTUNER_SVC"
DEFAULT_SVC_ROLE = "SNOWTUNER_ROLE"
DEFAULT_SVC_WAREHOUSE = "SNOWTUNER_WH"
DEFAULT_EXP_USER = "SNOWTUNER_EXP_SVC"
DEFAULT_EXP_ROLE = "SNOWTUNER_EXP_ROLE"


@cli.command()
@click.option(
    "--backend",
    type=click.Choice([b.value for b in CredentialBackend if b != CredentialBackend.ENV]),
    default=None,
    help="Storage backend.  Default: keyring if available, else file.",
)
@click.option(
    "--auth",
    type=click.Choice([m.value for m in AuthMethod]),
    default=AuthMethod.KEY_PAIR.value,
    show_default=True,
    help="Authentication method.  key_pair is the recommended path for a dedicated service user.",
)
def init(backend: str | None, auth: str) -> None:
    """Interactive setup: configure Snowflake credentials.

    The recommended path (``--auth key_pair``, the default) sets up a dedicated
    ``SNOWTUNER_SVC`` service user with RSA key-pair auth.  snowtuner generates
    the keypair; you paste the bootstrap SQL (printed by ``snowtuner bootstrap-sql``)
    into Snowsight to create the user, role, and warehouse.
    """
    console.print("[bold]snowtuner credential setup[/bold]")
    resolver = CredentialResolver()
    auth_method = AuthMethod(auth)

    existing = resolver.load()
    if existing is not None:
        console.print(
            f"[yellow]Credentials already exist[/yellow] "
            f"(source: {existing.source.value}, "
            f"account={existing.credentials.account}, user={existing.credentials.user}).  "
            f"Continuing will overwrite non-env sources."
        )
        if not click.confirm("Continue?", default=False):
            raise SystemExit(0)

    account = click.prompt(
        "Snowflake account identifier (e.g. myorg-myaccount, or legacy xy12345.us-east-1)"
    )

    if auth_method == AuthMethod.KEY_PAIR:
        user = click.prompt("Service user", default=DEFAULT_SVC_USER)
        warehouse = click.prompt("Default warehouse", default=DEFAULT_SVC_WAREHOUSE)
        role = click.prompt("Default role", default=DEFAULT_SVC_ROLE)

        console.print("\n[dim]Generating a 2048-bit RSA keypair…[/dim]")
        kp = generate_keypair()
        console.print(f"Wrote private key to [cyan]{kp.private_key_path}[/cyan] (mode 0600).")

        creds = SnowflakeCredentials(
            account=account,
            user=user,
            auth_method=auth_method,
            private_key_path=str(kp.private_key_path),
            warehouse=warehouse,
            role=role,
        )
    else:
        user = click.prompt("User")
        password = None
        if auth_method == AuthMethod.PASSWORD:
            console.print("[yellow]Password auth is intended for dev/test only.  "
                          "Switch to key-pair with a service user for production.[/yellow]")
            password = click.prompt("Password", hide_input=True)
        warehouse = click.prompt("Default warehouse (optional)", default="",
                                 show_default=False) or None
        role = click.prompt("Default role (optional)", default="",
                            show_default=False) or None
        creds = SnowflakeCredentials(
            account=account,
            user=user,
            auth_method=auth_method,
            password=password,
            warehouse=warehouse,
            role=role,
        )

    backend_enum = CredentialBackend(backend) if backend else None
    used = resolver.store(creds, backend=backend_enum)
    console.print(f"[green]Stored to {used.value} backend.[/green]\n")

    if auth_method == AuthMethod.KEY_PAIR:
        console.print("[bold]Next step[/bold]: run the Snowflake bootstrap as ACCOUNTADMIN.\n"
                      "  [cyan]snowtuner bootstrap-sql[/cyan]  (prints the SQL; paste into Snowsight)\n"
                      "Then verify the connection:\n"
                      "  [cyan]snowtuner verify[/cyan]")
    else:
        console.print("Run [cyan]snowtuner verify[/cyan] to test the connection.")


@cli.command("bootstrap-sql")
@click.option("--user", default=DEFAULT_SVC_USER, show_default=True,
              help="Service user name to create.")
@click.option("--role", default=DEFAULT_SVC_ROLE, show_default=True,
              help="Role name to create.")
@click.option("--warehouse", default=DEFAULT_SVC_WAREHOUSE, show_default=True,
              help="Dedicated warehouse name to create.")
@click.option("--autonomous-warehouse", default=None,
              help="If given, print ONLY the per-warehouse MODIFY grant to enable "
                   "autonomous mode on that warehouse (no CREATE statements).")
@click.option("--enable-experiments", is_flag=True, default=False,
              help="Print ONLY the experiments-user bootstrap (CREATE SNOWTUNER_EXP_SVC "
                   "+ grants needed to create/drop test warehouses).  Run this AFTER "
                   "the base bootstrap, once you're ready to use v0.2 experiments.")
@click.option("--exp-user", default=DEFAULT_EXP_USER, show_default=True,
              help="Experiment service user name to create (with --enable-experiments).")
@click.option("--exp-role", default=DEFAULT_EXP_ROLE, show_default=True,
              help="Experiment role name to create (with --enable-experiments).")
@click.option("--public-key", default=None,
              help="Override: path to the public key PEM file.  Default: re-derive "
                   "from the stored private key.")
def bootstrap_sql(
    user: str, role: str, warehouse: str,
    autonomous_warehouse: str | None,
    enable_experiments: bool, exp_user: str, exp_role: str,
    public_key: str | None,
) -> None:
    """Print the Snowflake ACCOUNTADMIN bootstrap SQL for snowtuner.

    Without any flags: prints the base install script (create user/role/warehouse
    + advisory-mode grants).

    With ``--autonomous-warehouse``: prints only the single per-warehouse
    MODIFY grant, so autonomous mode can be enabled on that warehouse with
    minimum additional privilege.

    With ``--enable-experiments``: prints the experiments service-user bootstrap
    (SNOWTUNER_EXP_SVC + role + grants to CREATE/DROP test warehouses).  Run
    once when you're ready to use v0.2 experiments.
    """
    if autonomous_warehouse:
        console.print(
            f"-- Enable autonomous mode for warehouse {autonomous_warehouse.upper()}:\n"
            f"GRANT MODIFY, OPERATE ON WAREHOUSE {autonomous_warehouse.upper()} "
            f"TO ROLE {role};"
        )
        return

    if enable_experiments:
        # Same public key reuse logic as the base bootstrap.
        pubkey_blob = _resolve_pubkey_blob(public_key)
        sql = _render_experiments_bootstrap(
            exp_user=exp_user, exp_role=exp_role, pubkey=pubkey_blob,
        )
        click.echo(sql)
        return

    pubkey_blob = _resolve_pubkey_blob(public_key)
    sql = _render_bootstrap(user=user, role=role, warehouse=warehouse, pubkey=pubkey_blob)
    # Print to stdout (not via Rich) so the output is copy-pasteable as plain SQL.
    click.echo(sql)


def _resolve_pubkey_blob(public_key: str | None) -> str:
    """Resolve the public-key blob from a path or by re-deriving from the
    stored private key.  Shared by the base and experiments bootstraps."""
    if public_key:
        from pathlib import Path as _Path
        pem = _Path(public_key).expanduser().read_text()
        from snowtuner.credentials.keypair import _strip_pem_headers  # type: ignore
        return _strip_pem_headers(pem)
    resolver = CredentialResolver()
    result = resolver.load()
    if result is None or not result.credentials.private_key_path:
        console.print("[red]No stored private key found.[/red]  "
                      "Run [cyan]snowtuner init[/cyan] first or pass "
                      "[cyan]--public-key /path/to/public.pem[/cyan].")
        raise SystemExit(1)
    from pathlib import Path as _Path
    return public_blob_from_private(_Path(result.credentials.private_key_path))


def _render_bootstrap(*, user: str, role: str, warehouse: str, pubkey: str) -> str:
    return f"""-- snowtuner bootstrap — run as ACCOUNTADMIN in Snowsight.
-- Creates the service user, role, and a dedicated XSMALL warehouse, plus the
-- base grants snowtuner needs for advisory mode.  Autonomous mode requires an
-- additional per-warehouse GRANT MODIFY — see `snowtuner bootstrap-sql --help`.

USE ROLE ACCOUNTADMIN;

CREATE ROLE IF NOT EXISTS {role}
    COMMENT = 'Used by snowtuner (locally-hosted Snowflake cost/performance advisor)';

CREATE USER IF NOT EXISTS {user}
    TYPE = SERVICE
    DEFAULT_ROLE = {role}
    DEFAULT_WAREHOUSE = {warehouse}
    COMMENT = 'Used by snowtuner'
    RSA_PUBLIC_KEY = '{pubkey}';

GRANT ROLE {role} TO USER {user};

CREATE WAREHOUSE IF NOT EXISTS {warehouse}
    WAREHOUSE_SIZE = XSMALL
    AUTO_SUSPEND = 60
    INITIALLY_SUSPENDED = TRUE
    COMMENT = 'Used by snowtuner for its own metadata reads';

GRANT USAGE, OPERATE, MONITOR ON WAREHOUSE {warehouse} TO ROLE {role};

-- ACCOUNT_USAGE views (QUERY_HISTORY, WAREHOUSE_METERING_HISTORY, ...)
GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE {role};

-- SHOW WAREHOUSES + account-wide observability
GRANT MONITOR USAGE ON ACCOUNT TO ROLE {role};

-- Unredacted query_text in QUERY_HISTORY across the account.
-- Without this, Snowflake redacts query_text for queries that were run by
-- roles snowtuner hasn't been granted MONITOR on — which makes those queries
-- invisible to the explorer and unreplayable in experiments.  GOVERNANCE_VIEWER
-- is Snowflake's standard role for "observability across the account" and is
-- the right default for a self-hosted optimizer (the query text never leaves
-- your cloud account).
--
-- For stricter scoping: comment this out and grant MONITOR per warehouse
-- instead — e.g. `GRANT MONITOR ON WAREHOUSE ANALYTICS_WH TO ROLE {role};`
GRANT DATABASE ROLE SNOWFLAKE.GOVERNANCE_VIEWER TO ROLE {role};
"""


def _render_experiments_bootstrap(*, exp_user: str, exp_role: str, pubkey: str) -> str:
    """Bootstrap SQL for the v0.2 experiments service user.

    Separate from the base bootstrap because experiments need substantially
    more privilege (CREATE WAREHOUSE) than advisory mode.  Customers can opt
    in only when they're ready to use experiments.

    Design:
      - Dedicated role (SNOWTUNER_EXP_ROLE) so blast radius is contained.
      - Dedicated user (SNOWTUNER_EXP_SVC) so QUERY_HISTORY clearly attributes
        replay traffic.
      - GRANT CREATE WAREHOUSE ON ACCOUNT lets the engine create per-arm
        test warehouses.  The engine names them SNOWTUNER_EXP_* so a
        janitor can find and drop them after a crash.
      - The user has NO default warehouse — they must USE WAREHOUSE
        explicitly, eliminating "accidentally ran on production" risk.
      - SELECT grants on customer tables are intentionally NOT included
        here — customers grant SELECT on whatever they want replays to
        touch.  This is a feature: tight blast radius by default.
    """
    return f"""-- snowtuner experiments bootstrap — run as ACCOUNTADMIN.
-- Creates the dedicated experiments service user + role and grants the
-- minimum privileges needed to run replay experiments.
--
-- After running this, decide which tables/databases the experiments user
-- should be able to SELECT from, and grant SELECT manually:
--
--   GRANT USAGE ON DATABASE my_db TO ROLE {exp_role};
--   GRANT USAGE ON SCHEMA my_db.public TO ROLE {exp_role};
--   GRANT SELECT ON ALL TABLES IN SCHEMA my_db.public TO ROLE {exp_role};
--
-- Then configure snowtuner with the experiments credentials:
--   snowtuner config-experiments-user

USE ROLE ACCOUNTADMIN;

CREATE ROLE IF NOT EXISTS {exp_role}
    COMMENT = 'Used by snowtuner experiments framework';

CREATE USER IF NOT EXISTS {exp_user}
    TYPE = SERVICE
    DEFAULT_ROLE = {exp_role}
    -- NO DEFAULT_WAREHOUSE: experiments always USE WAREHOUSE explicitly,
    -- so the experiments user can never accidentally run on production.
    COMMENT = 'Used by snowtuner v0.2 experiments — creates ephemeral test warehouses'
    RSA_PUBLIC_KEY = '{pubkey}';

GRANT ROLE {exp_role} TO USER {exp_user};

-- Lets the engine CREATE and DROP test warehouses named SNOWTUNER_EXP_*.
-- Tightly scoped: this role has no other privileges beyond what's granted
-- here.  Customers may revoke this grant when experiments aren't in active
-- use.
GRANT CREATE WAREHOUSE ON ACCOUNT TO ROLE {exp_role};

-- Read access to ACCOUNT_USAGE.QUERY_HISTORY for the engine's per-replay
-- metrics fetch (elapsed_ms, bytes_scanned, etc.).  This is the same grant
-- the base SNOWTUNER_ROLE has, but the experiments role needs its own copy
-- so it can query its own session's replays.
GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE {exp_role};

-- SHOW WAREHOUSES + account observability so the engine can verify its
-- test warehouses exist post-CREATE.
GRANT MONITOR USAGE ON ACCOUNT TO ROLE {exp_role};

-- IMPORTANT: This bootstrap does NOT grant SELECT on any customer tables.
-- Grant SELECT manually for each database/schema/table you want experiments
-- to be able to replay queries from.  See the comment at top of this file
-- for the syntax.
"""


_SETTABLE_FIELDS = {"account", "user", "warehouse", "role"}


@cli.group()
def config() -> None:
    """View or update individual credential fields without re-running `init`."""


@config.command("show")
def config_show() -> None:
    """Print the currently-resolved credentials (password redacted)."""
    resolver = CredentialResolver()
    result = resolver.load()
    if result is None:
        console.print("[yellow]No credentials found.[/yellow]  "
                      "Run [cyan]snowtuner init[/cyan] to set them up.")
        raise SystemExit(1)
    tbl = Table(show_header=False)
    tbl.add_row("Source", result.source.value)
    for k, v in result.credentials.redacted().items():
        tbl.add_row(k, "—" if v is None else str(v))
    console.print(tbl)


@config.command("set")
@click.argument("field", type=click.Choice(sorted(_SETTABLE_FIELDS)))
@click.argument("value")
def config_set(field: str, value: str) -> None:
    """Update a single credential field (account, user, warehouse, role).

    Examples:
      snowtuner config set account ABCORG-XY12345
      snowtuner config set warehouse SNOWTUNER_WH
    """
    resolver = CredentialResolver()
    result = resolver.load()
    if result is None:
        console.print("[red]No credentials found.[/red]  "
                      "Run [cyan]snowtuner init[/cyan] first.")
        raise SystemExit(1)
    if result.source == CredentialBackend.ENV:
        env_var = f"SNOWTUNER_SNOWFLAKE_{field.upper()}"
        console.print(
            f"[red]Cannot update via CLI:[/red] {field!r} is currently set via "
            f"the [cyan]{env_var}[/cyan] environment variable.  "
            f"Unset it (or change it in your shell config) to manage with "
            f"[cyan]snowtuner config[/cyan]."
        )
        raise SystemExit(2)

    # Allow empty string to mean "clear" for optional fields.
    new_value: str | None = value if value != "" else None
    if field in {"account", "user"} and not new_value:
        console.print(f"[red]{field!r} cannot be empty.[/red]")
        raise SystemExit(2)

    updated = result.credentials.model_copy(update={field: new_value})
    resolver.store(updated, backend=result.source)
    console.print(
        f"[green]Updated {field}[/green] "
        f"(backend: {result.source.value}).  "
        f"Run [cyan]snowtuner verify[/cyan] to test."
    )


@cli.command()
def verify() -> None:
    """Resolve credentials and run `SELECT 1` against Snowflake."""
    resolver = CredentialResolver()
    result = resolver.load()
    if result is None:
        console.print("[red]No credentials found.[/red]  "
                      "Run [cyan]snowtuner init[/cyan] to set them up.")
        raise SystemExit(1)

    console.print(f"Using credentials from [bold]{result.source.value}[/bold]: "
                  f"account={result.credentials.account}, user={result.credentials.user}, "
                  f"auth={result.credentials.auth_method.value}")

    client = SnowflakeClient(result.credentials)
    try:
        rows = client.execute(
            "SELECT CURRENT_ACCOUNT(), CURRENT_USER(), CURRENT_ROLE(), "
            "CURRENT_WAREHOUSE(), CURRENT_REGION()"
        )
    except Exception as e:
        console.print(f"[red]Connection failed:[/red] {e}")
        raise SystemExit(2)
    finally:
        client.close()

    account, user, role, warehouse, region = rows[0]
    tbl = Table(show_header=False)
    tbl.add_row("Account", str(account))
    tbl.add_row("User", str(user))
    tbl.add_row("Role", str(role or "—"))
    tbl.add_row("Warehouse", str(warehouse or "—"))
    tbl.add_row("Region", str(region))
    console.print("[green]Connected.[/green]")
    console.print(tbl)


@cli.command("creds-delete")
def creds_delete() -> None:
    """Remove stored credentials from keyring + file backends (env vars untouched)."""
    resolver = CredentialResolver()
    resolver.delete()
    console.print("[green]Deleted.[/green]  "
                  "Any SNOWTUNER_SNOWFLAKE_* env vars still take effect.")


# ── Autonomous mode ───────────────────────────────────────────────────

@cli.group()
def autonomous() -> None:
    """Manage autonomous-apply config + audit log."""


@autonomous.command("list")
def autonomous_list() -> None:
    """Show all autonomous-config rows."""
    store = AutonomousConfigStore(get_connection())
    rows = store.list()
    if not rows:
        console.print("[yellow]No autonomous config rows.[/yellow]  "
                      "Use [cyan]snowtuner autonomous enable[/cyan] to opt in.")
        return
    tbl = Table(show_header=True, header_style="bold")
    tbl.add_column("Action type")
    tbl.add_column("Warehouse")
    tbl.add_column("Knob")
    tbl.add_column("Enabled")
    tbl.add_column("Threshold", justify="right")
    tbl.add_column("Cooldown (h)", justify="right")
    tbl.add_column("Max rollbacks/wk", justify="right")
    tbl.add_column("Circuit")
    for r in rows:
        circuit = "[red]open[/red]" if r.circuit_open_until else "closed"
        tbl.add_row(
            r.action_type,
            "[dim]*[/dim] (default)" if r.is_catch_all_warehouse else r.warehouse_name,
            "[dim]*[/dim] (any)" if r.is_catch_all_knob else r.knob,
            "[green]ON[/green]" if r.enabled else "off",
            f"{r.confidence_threshold:.2f}",
            str(r.cooldown_hours),
            str(r.max_rollbacks_per_week),
            circuit,
        )
    console.print(tbl)


@autonomous.command("enable")
@click.argument("action_type")
@click.argument("warehouse_name")
@click.option("--knob", default="*", show_default=True,
              help="Restrict to a specific knob (e.g. AUTO_SUSPEND, "
                   "WAREHOUSE_SIZE).  Default '*' = every knob this action emits.")
@click.option("--threshold", type=float, default=None,
              help="Confidence threshold required to apply.  Default: 0.85.")
@click.option("--cooldown-hours", type=int, default=None,
              help="Minimum hours between auto-applies on this (action, warehouse, knob).")
@click.option("--max-rollbacks-per-week", type=int, default=None,
              help="Circuit breaker: pause autonomous after N rollbacks in 7 days.")
def autonomous_enable(
    action_type: str, warehouse_name: str, knob: str,
    threshold: float | None, cooldown_hours: int | None,
    max_rollbacks_per_week: int | None,
) -> None:
    """Enable autonomous apply for (ACTION_TYPE, WAREHOUSE_NAME, KNOB).

    Use ``*`` for WAREHOUSE_NAME to set the catch-all default for the action_type.
    Use ``--knob`` to restrict to one knob (e.g. ``AUTO_SUSPEND`` only) — leaving
    ``WAREHOUSE_SIZE`` advisory on the same warehouse.

    Examples:

      snowtuner autonomous enable ALTER_WAREHOUSE ETL_WH
      snowtuner autonomous enable ALTER_WAREHOUSE ETL_WH --knob AUTO_SUSPEND
      snowtuner autonomous enable ALTER_WAREHOUSE '*' --threshold 0.90
    """
    store = AutonomousConfigStore(get_connection())
    cfg = store.upsert(
        action_type, warehouse_name, knob,
        enabled=True,
        confidence_threshold=threshold,
        cooldown_hours=cooldown_hours,
        max_rollbacks_per_week=max_rollbacks_per_week,
    )
    knob_label = "(any)" if cfg.is_catch_all_knob else cfg.knob
    console.print(
        f"[green]Enabled[/green] {cfg.action_type} on "
        f"{'(default)' if cfg.is_catch_all_warehouse else cfg.warehouse_name} / {knob_label}  "
        f"(threshold={cfg.confidence_threshold:.2f}, "
        f"cooldown={cfg.cooldown_hours}h, "
        f"max_rollbacks/wk={cfg.max_rollbacks_per_week})"
    )
    if cfg.action_type == "ALTER_WAREHOUSE":
        console.print()
        if cfg.is_catch_all_warehouse:
            console.print(
                "[yellow]One more step per warehouse.[/yellow]  For each "
                "warehouse you want autonomous to apply to, run the following "
                "SQL in your Snowflake environment while logged in as a user "
                "holding the [bold]ACCOUNTADMIN[/bold] role (substituting the "
                "warehouse name):"
            )
            console.print()
            console.print(
                f"  [cyan]GRANT MODIFY, OPERATE ON WAREHOUSE <warehouse_name> "
                f"TO ROLE {DEFAULT_SVC_ROLE};[/cyan]"
            )
        else:
            grant_sql = (
                f"GRANT MODIFY, OPERATE ON WAREHOUSE {cfg.warehouse_name} "
                f"TO ROLE {DEFAULT_SVC_ROLE};"
            )
            console.print(
                "[yellow]One more step.[/yellow]  Run the following SQL in "
                "your Snowflake environment while logged in as a user holding "
                "the [bold]ACCOUNTADMIN[/bold] role:"
            )
            console.print()
            console.print(f"  [cyan]{grant_sql}[/cyan]")
            console.print()
            copied = format.copy_to_clipboard(grant_sql)
            if copied:
                console.print("[green]✓ Copied to clipboard.[/green]  "
                              "Paste it into Snowsight.")
            console.print(
                "[dim]Until this GRANT runs, autonomous apply on this "
                "warehouse will fail with a privilege error.[/dim]"
            )


@autonomous.command("disable")
@click.argument("action_type")
@click.argument("warehouse_name")
@click.option("--knob", default="*", show_default=True,
              help="Restrict to a specific knob.  Default '*' = the catch-all row.")
def autonomous_disable(action_type: str, warehouse_name: str, knob: str) -> None:
    """Disable autonomous apply for (ACTION_TYPE, WAREHOUSE_NAME, KNOB) without removing the row."""
    store = AutonomousConfigStore(get_connection())
    store.upsert(action_type, warehouse_name, knob, enabled=False)
    console.print(f"[yellow]Disabled[/yellow] {action_type} on {warehouse_name} / {knob}.")


@autonomous.command("delete")
@click.argument("action_type")
@click.argument("warehouse_name")
@click.option("--knob", default="*", show_default=True)
def autonomous_delete(action_type: str, warehouse_name: str, knob: str) -> None:
    """Remove the config row entirely."""
    store = AutonomousConfigStore(get_connection())
    store.delete(action_type, warehouse_name, knob)
    console.print(
        f"[yellow]Deleted[/yellow] config for {action_type} / {warehouse_name} / {knob}."
    )


@autonomous.command("reset-circuit")
@click.argument("action_type")
@click.argument("warehouse_name")
@click.option("--knob", default="*", show_default=True)
def autonomous_reset_circuit(action_type: str, warehouse_name: str, knob: str) -> None:
    """Close a tripped circuit so autonomous apply resumes."""
    store = AutonomousConfigStore(get_connection())
    store.reset_circuit(action_type, warehouse_name, knob)
    console.print(
        f"[green]Circuit reset[/green] for {action_type} / {warehouse_name} / {knob}."
    )


@autonomous.command("applications")
@click.option("--warehouse", default=None, help="Filter by warehouse name.")
@click.option("--limit", default=20, show_default=True)
def autonomous_applications(warehouse: str | None, limit: int) -> None:
    """Show the autonomous-apply audit log (most recent first)."""
    store = AutonomousApplicationStore(get_connection())
    rows = store.list(warehouse_name=warehouse, limit=limit)
    if not rows:
        console.print("[yellow]No autonomous applications recorded.[/yellow]")
        return
    tbl = Table(show_header=True, header_style="bold")
    tbl.add_column("ID", justify="right")
    tbl.add_column("Rec", justify="right")
    tbl.add_column("Action")
    tbl.add_column("Warehouse")
    tbl.add_column("Applied at")
    tbl.add_column("State")
    for r in rows:
        applied_at = r.applied_at.isoformat(sep=" ", timespec="seconds") if r.applied_at else "—"
        state_color = {
            "APPLIED": "green",
            "ROLLED_BACK": "yellow",
            "FAILED": "red",
        }.get(r.state.value, "white")
        tbl.add_row(
            str(r.id), str(r.recommendation_id),
            r.action_type, r.warehouse_name or "—",
            applied_at,
            f"[{state_color}]{r.state.value}[/{state_color}]",
        )
    console.print(tbl)


@autonomous.command("rollback")
@click.argument("application_id", type=int)
@click.confirmation_option(prompt="Roll back this application against Snowflake?")
def autonomous_rollback(application_id: int) -> None:
    """Execute the recorded rollback for an autonomous application."""
    conn = get_connection()
    try:
        client = SnowflakeClient.from_resolver()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)
    runner = AutonomousRunner(conn, client)
    decision = runner.rollback(application_id)
    client.close()
    if decision.decision == "applied":
        console.print(f"[green]Rolled back[/green] application #{application_id}.")
    else:
        console.print(f"[red]Rollback failed:[/red] {decision.reason}")
        raise SystemExit(2)


# ── Experiments (v0.2) ─────────────────────────────────────────────

@cli.group()
def experiments() -> None:
    """Propose, accept, and run replay experiments."""


@experiments.command("recipes")
def experiments_recipes() -> None:
    """List preset experiment recipes."""
    from snowtuner.experiments.recipes import PRESET_RECIPES

    t = Table(title="Experiment recipes", title_style="bold")
    t.add_column("name", style="cyan")
    t.add_column("summary")
    for name, recipe in PRESET_RECIPES.items():
        doc = (recipe.__doc__ or "").strip().split("\n")[0]
        t.add_row(name, doc)
    console.print(t)


@experiments.command("propose")
@click.argument("recipe_name")
@click.argument("target_warehouse")
def experiments_propose(recipe_name: str, target_warehouse: str) -> None:
    """Propose an experiment: RECIPE_NAME against TARGET_WAREHOUSE."""
    from snowtuner.api.app import (
        _account_info, _load_warehouse_config, _sample_query_stats,
    )
    from snowtuner.experiments import ExperimentStore
    from snowtuner.experiments.recipes import PRESET_RECIPES

    if recipe_name not in PRESET_RECIPES:
        console.print(
            f"[red]Unknown recipe {recipe_name!r}.  Valid:[/red] "
            f"{sorted(PRESET_RECIPES.keys())}"
        )
        raise SystemExit(2)
    warehouse = _load_warehouse_config(target_warehouse)
    if warehouse is None:
        console.print(
            f"[red]Warehouse {target_warehouse!r} not found in raw.warehouses.[/red]  "
            f"Run [cyan]snowtuner sync[/cyan] first."
        )
        raise SystemExit(2)
    proposed = PRESET_RECIPES[recipe_name](
        warehouse,
        _account_info(),
        sample_query_stats=_sample_query_stats(target_warehouse),
    )
    if proposed is None:
        console.print(
            f"[yellow]Recipe {recipe_name!r} is not eligible for "
            f"warehouse {target_warehouse!r}.[/yellow]"
        )
        raise SystemExit(2)
    store = ExperimentStore(get_connection())
    new_id = store.insert(proposed)
    console.print(
        f"[green]Proposed experiment #{new_id}[/green] — recipe="
        f"{recipe_name}, {len(proposed.arms)} arms, "
        f"cost estimate {proposed.cost_estimate.low_credits:.2f}–"
        f"{proposed.cost_estimate.high_credits:.2f} credits."
    )


@experiments.command("list")
@click.option(
    "--status",
    type=click.Choice([s.value for s in ExperimentStatus]),
    default=None,
)
@click.option("--limit", default=50, show_default=True)
def experiments_list(status: str | None, limit: int) -> None:
    """List experiments."""
    from snowtuner.experiments import ExperimentStore
    store = ExperimentStore(get_connection())
    exps = store.list(
        status=ExperimentStatus(status) if status else None,
        limit=limit,
    )
    if not exps:
        console.print("[dim]No experiments.[/dim]")
        return
    t = Table(title=f"Experiments ({len(exps)})")
    t.add_column("id", justify="right")
    t.add_column("recipe")
    t.add_column("target")
    t.add_column("status")
    t.add_column("arms", justify="right")
    t.add_column("proposed_at")
    t.add_column("cost_est")
    for e in exps:
        ce = e.proposed.cost_estimate
        t.add_row(
            str(e.id),
            e.proposed.recipe_name,
            e.proposed.target_warehouse,
            e.status.value,
            str(len(e.proposed.arms)),
            e.proposed_at.strftime("%Y-%m-%d %H:%M"),
            f"{ce.low_credits:.2f}–{ce.high_credits:.2f}",
        )
    console.print(t)


@experiments.command("show")
@click.argument("experiment_id", type=int)
def experiments_show(experiment_id: int) -> None:
    """Show an experiment in detail (spec, arms, runs, report)."""
    from snowtuner.experiments import ExperimentStore
    store = ExperimentStore(get_connection())
    exp = store.get(experiment_id)
    if exp is None:
        console.print(f"[red]Experiment {experiment_id} not found.[/red]")
        raise SystemExit(2)

    console.print(
        f"[bold]Experiment #{exp.id}[/bold] — "
        f"recipe=[cyan]{exp.proposed.recipe_name}[/cyan] "
        f"target=[cyan]{exp.proposed.target_warehouse}[/cyan] "
        f"status=[yellow]{exp.status.value}[/yellow]"
    )
    console.print(f"\n[bold]Hypothesis:[/bold] {exp.proposed.hypothesis}")
    console.print(
        f"\n[bold]Cost estimate:[/bold] "
        f"{exp.proposed.cost_estimate.low_credits:.2f}–"
        f"{exp.proposed.cost_estimate.high_credits:.2f} credits — "
        f"{exp.proposed.cost_estimate.rationale}"
    )
    if exp.actual_cost_credits is not None:
        cap = "[red] (cap hit)[/red]" if exp.cost_cap_hit else ""
        console.print(f"[bold]Actual cost:[/bold] {exp.actual_cost_credits:.4f} credits{cap}")

    t = Table(title="Arms")
    t.add_column("name", style="cyan")
    t.add_column("delta")
    t.add_column("issues")
    for arm in exp.proposed.arms:
        delta = (
            ", ".join(f"{k}={getattr(arm.delta, k)!r}" for k in arm.delta.fields_set())
            or "control"
        )
        issues = "; ".join(f"{i.severity}:{i.message}" for i in arm.eligibility_issues) or "—"
        t.add_row(arm.name, delta, issues)
    console.print(t)

    runs = store.runs_for(experiment_id)
    if runs:
        # Compact aggregation per arm.
        from collections import defaultdict
        agg: dict[str, list[int]] = defaultdict(list)
        for r in runs:
            if r.elapsed_ms is not None:
                agg[r.arm_name].append(r.elapsed_ms)
        rt = Table(title=f"Runs ({len(runs)} total)")
        rt.add_column("arm")
        rt.add_column("n", justify="right")
        rt.add_column("median elapsed (ms)", justify="right")
        for arm, vals in agg.items():
            vals_sorted = sorted(vals)
            mid = vals_sorted[len(vals_sorted) // 2] if vals_sorted else 0
            rt.add_row(arm, str(len(vals)), str(mid))
        console.print(rt)

    if exp.report:
        console.print(f"\n[bold]Best arm:[/bold] {exp.report.best_arm_name or '—'}")
        if exp.report.best_arm_rationale:
            console.print(f"  {exp.report.best_arm_rationale}")
        if exp.report.projected_annual_savings_low_credits is not None:
            console.print(
                f"[bold]Projected annual savings:[/bold] "
                f"{exp.report.projected_annual_savings_low_credits:.0f}–"
                f"{exp.report.projected_annual_savings_high_credits:.0f} credits"
            )
        if exp.report.sample_size_warnings:
            console.print("[yellow]Sample-size warnings:[/yellow]")
            for w in exp.report.sample_size_warnings:
                console.print(f"  • {w}")


@experiments.command("accept")
@click.argument("experiment_id", type=int)
def experiments_accept(experiment_id: int) -> None:
    """Mark an experiment as ACCEPTED (does not start the engine)."""
    from snowtuner.experiments import ExperimentStatus, ExperimentStore
    store = ExperimentStore(get_connection())
    exp = store.get(experiment_id)
    if exp is None:
        console.print(f"[red]Not found.[/red]")
        raise SystemExit(2)
    if exp.status != ExperimentStatus.PROPOSED:
        console.print(f"[red]Not in PROPOSED ({exp.status.value}).[/red]")
        raise SystemExit(2)
    if store.has_running_experiment():
        console.print(
            "[red]Another experiment is already accepted or running.[/red]  "
            "Abort it first."
        )
        raise SystemExit(2)
    store.set_status(experiment_id, ExperimentStatus.ACCEPTED)
    console.print(
        f"[green]Accepted[/green] experiment #{experiment_id}.  "
        f"Run it with [cyan]snowtuner experiments run {experiment_id}[/cyan]."
    )


@experiments.command("reject")
@click.argument("experiment_id", type=int)
def experiments_reject(experiment_id: int) -> None:
    """Mark an experiment as REJECTED."""
    from snowtuner.experiments import ExperimentStatus, ExperimentStore
    store = ExperimentStore(get_connection())
    exp = store.get(experiment_id)
    if exp is None or exp.status != ExperimentStatus.PROPOSED:
        console.print("[red]Only PROPOSED experiments can be rejected.[/red]")
        raise SystemExit(2)
    store.set_status(experiment_id, ExperimentStatus.REJECTED)
    console.print(f"[green]Rejected[/green] experiment #{experiment_id}.")


@experiments.command("run")
@click.argument("experiment_id", type=int)
@click.confirmation_option(
    prompt="Run this experiment against Snowflake?  This will create test warehouses and replay queries.",
)
def experiments_run(experiment_id: int) -> None:
    """Run an ACCEPTED experiment to completion (foreground)."""
    from snowtuner.experiments import (
        ExperimentEngine, ExperimentStatus, ExperimentStore,
    )
    store = ExperimentStore(get_connection())
    exp = store.get(experiment_id)
    if exp is None:
        console.print(f"[red]Not found.[/red]")
        raise SystemExit(2)
    if exp.status != ExperimentStatus.ACCEPTED:
        console.print(f"[red]Not in ACCEPTED ({exp.status.value}).[/red]")
        raise SystemExit(2)
    try:
        client = SnowflakeClient.from_resolver()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)
    console.print(
        f"[bold]Running experiment #{experiment_id}[/bold] "
        f"({exp.proposed.recipe_name} on {exp.proposed.target_warehouse})…"
    )
    engine = ExperimentEngine(get_connection(), client)
    try:
        engine.run(experiment_id)
    finally:
        client.close()
    final = store.get(experiment_id)
    color = "green" if final.status == ExperimentStatus.COMPLETED else "yellow"  # type: ignore[union-attr]
    console.print(f"[{color}]Final status:[/{color}] {final.status.value}")  # type: ignore[union-attr]
    if final.aborted_reason:  # type: ignore[union-attr]
        console.print(f"[yellow]Reason:[/yellow] {final.aborted_reason}")  # type: ignore[union-attr]


@experiments.command("abort")
@click.argument("experiment_id", type=int)
@click.option("--reason", required=True, help="Why this experiment is being aborted.")
def experiments_abort(experiment_id: int, reason: str) -> None:
    """Mark an experiment as ABORTED."""
    from snowtuner.experiments import ExperimentStatus, ExperimentStore
    store = ExperimentStore(get_connection())
    exp = store.get(experiment_id)
    if exp is None:
        console.print(f"[red]Not found.[/red]")
        raise SystemExit(2)
    if exp.status not in (ExperimentStatus.ACCEPTED, ExperimentStatus.RUNNING):
        console.print(
            f"[red]Only ACCEPTED or RUNNING can be aborted "
            f"({exp.status.value}).[/red]"
        )
        raise SystemExit(2)
    store.set_status(experiment_id, ExperimentStatus.ABORTED, aborted_reason=reason)
    console.print(f"[green]Aborted[/green] experiment #{experiment_id}.")


@experiments.command("recover")
def experiments_recover() -> None:
    """Drop any test warehouses left orphaned by a prior crash."""
    from snowtuner.experiments import ExperimentEngine
    try:
        client = SnowflakeClient.from_resolver()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)
    engine = ExperimentEngine(get_connection(), client)
    try:
        dropped = engine.recover_orphaned_warehouses()
    finally:
        client.close()
    if dropped:
        console.print(
            f"[green]Dropped[/green] {len(dropped)} orphaned warehouse(s):"
        )
        for name in dropped:
            console.print(f"  • {name}")
    else:
        console.print("[dim]No orphaned warehouses to clean up.[/dim]")


if __name__ == "__main__":
    cli()
