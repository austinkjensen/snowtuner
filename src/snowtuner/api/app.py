"""FastAPI HTTP service for snowtuner.

This is the **integration surface**: CI jobs, the Streamlit UI, and the admin
MCP server all call these endpoints.  Recommenders themselves still run
in-process against the live DuckDB connection — the API is for *driving* the
optimizer, not for how recommenders access data.

Endpoints
---------
  GET  /health
  GET  /recommenders                     List registered recommenders
  POST /orchestrator/run                 Run features + all recommenders
  POST /recommenders/{name}/run          Run a single recommender
  GET  /recommendations                  List recommendations
  GET  /recommendations/{id}             One recommendation
  POST /recommendations/{id}/accept      Mark ACCEPTED (advisory)
  POST /recommendations/{id}/reject      Mark REJECTED
  POST /seed                             Regenerate synthetic data
  POST /features/run                     Run only the feature pipeline
  GET  /experiments/recipes              List preset recipes
  GET  /experiments                      List experiments
  GET  /experiments/{id}                 One experiment
  GET  /experiments/{id}/runs            Per-(arm,query,rep) observations
  POST /experiments/propose              Propose tuning experiment via a preset recipe
  POST /experiments/propose-benchmark    Propose benchmark experiment (absolute-config arms)
  POST /experiments/{id}/accept          Mark ACCEPTED
  POST /experiments/{id}/reject          Mark REJECTED
  POST /experiments/{id}/run             Start engine (background thread)
  POST /experiments/{id}/abort           Mark ABORTED (best-effort engine signal)
  GET  /queries                          List ingested queries (filtered, paginated)
  GET  /queries/facets                   Distinct values for filter chips
  GET  /queries/{id}                     Full detail for one query
  GET  /query-families                   Aggregated rollup by parameterized hash
"""
from __future__ import annotations

import contextlib
import threading
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query

from snowtuner.api.schemas import (
    AbortExperimentRequest,
    AutonomousApplicationOut,
    AutonomousConfigOut,
    AutonomousConfigUpsert,
    CliCommand,
    CliParam,
    CreateQueryGroupRequest,
    CredentialStatusOut,
    CredentialVerifyOut,
    AutomationStatusOut,
    DriftReportOut,
    McpToolInfo,
    StageOutcomeOut,
    TickReportOut,
    ProposeBenchmarkRequest,
    ProposeExperimentRequest,
    QueryDetail,
    QueryFamily,
    QueryFilterFacets,
    QueryListResponse,
    QueryRow,
    RecipeInfo,
    RecommendationOut,
    RecommenderInfo,
    RunRecommenderReport,
    RunRequest,
    RunResponse,
    SeedRequest,
    SourceDriftOut,
    SourceFreshnessOut,
    StatusOut,
    StatusUpdateRequest,
    WarehouseSummaryOut,
)
from snowtuner.autonomous import (
    AutonomousApplicationStore,
    AutonomousConfigStore,
    AutonomousRunner,
)
from snowtuner.experiments import (
    Experiment,
    ExperimentEngine,
    ExperimentRun,
    ExperimentStatus,
    ExperimentStore,
)
from snowtuner.experiments.config_delta import WarehouseConfig
from snowtuner.experiments.eligibility import AccountInfo
from snowtuner.experiments.recipes import PRESET_RECIPES
from snowtuner.query_groups import (
    QueryFilterSpec,
    QueryGroup,
    QueryGroupKind,
    QueryGroupStore,
)
from snowtuner.ingestion.snowflake_client import SnowflakeClient
from snowtuner.features import DEFAULT_TRANSFORMS
from snowtuner.features.base import FeaturePipeline
from snowtuner.ingestion.sources import DEFAULT_SOURCES
from snowtuner.orchestrator import Orchestrator
from snowtuner.recommendations import (
    RecommendationStatus,
    RecommendationStore,
)
from snowtuner.recommenders.registry import (
    RecommenderRegistry,
    default_registry,
)
from snowtuner.seed import seed_demo_data
from snowtuner.storage import get_connection
from snowtuner.storage.db import naive_utcnow


# ---- Dependencies (per-request so the test harness can override) ----

def _get_store() -> RecommendationStore:
    return RecommendationStore(get_connection())


def _get_registry() -> RecommenderRegistry:
    return default_registry()


def create_app() -> FastAPI:
    # The auth dependency is attached to the app via ``dependencies=[...]``
    # so every endpoint inherits the check.  Mode is controlled by the
    # SNOWTUNER_AUTH_MODE env var:
    #   * 'none'  (default for local dev): loopback-only, no token
    #   * 'token': bearer token required (env or ~/.snowtuner/api_token)
    # See snowtuner.api.auth.require_auth for the gory details.
    from snowtuner.api.auth import require_auth
    from snowtuner.api.automation import get_loop, _run_on_start

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # ── AutomationLoop ──────────────────────────────────────────
        # Spawns a background thread that runs sync→features→recommenders→
        # autonomous every SNOWTUNER_AUTOMATION_INTERVAL seconds.  Off
        # by default (interval=0).  Optionally runs one tick synchronously
        # before serving via SNOWTUNER_AUTOMATION_ON_START.
        loop = get_loop()
        if loop.enabled and _run_on_start():
            # Block startup until the first tick completes.  Used by
            # ephemeral deployments where every container boot should
            # produce fresh state before accepting traffic.
            loop.run_one_tick()
        loop.start()
        try:
            yield
        finally:
            loop.stop()

    app = FastAPI(
        title="snowtuner",
        description="Locally-hosted Snowflake cost & performance advisor.",
        version="0.1.0",
        dependencies=[Depends(require_auth)],
        lifespan=lifespan,
    )

    @app.get("/")
    def root() -> dict[str, str]:
        return {
            "name": "snowtuner",
            "version": "0.1.0",
            "docs": "/docs",
            "health": "/health",
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ── Recommender discovery ─────────────────────────────────────
    @app.get("/recommenders", response_model=list[RecommenderInfo])
    def list_recommenders(
        reg: RecommenderRegistry = Depends(_get_registry),
    ) -> list[RecommenderInfo]:
        return [
            RecommenderInfo(
                name=r.name,
                version=r.version,
                action_type=r.action_type.value,
                class_path=f"{r.__class__.__module__}.{r.__class__.__name__}",
                required_feature_tables=list(r.required_feature_tables),
            )
            for r in reg.all()
        ]

    # ── Running ───────────────────────────────────────────────────
    @app.post("/orchestrator/run", response_model=RunResponse)
    def run_all(
        req: RunRequest = RunRequest(),
        reg: RecommenderRegistry = Depends(_get_registry),
    ) -> RunResponse:
        # NOTE: a SnowflakeClient must be passed for two stages to actually
        # run — sync (the `client is not None` check in Orchestrator.run)
        # AND autonomous (silently skips with "no client provided" if None).
        # Build it from the resolver here so this endpoint has CLI-parity:
        # `snowtuner run` and `POST /orchestrator/run` now do the same thing.
        # If creds aren't configured, fall back to sync-disabled mode so
        # users without Snowflake creds (e.g. local seed-data dev) can still
        # run features+recommenders against existing raw.* data.
        try:
            client = SnowflakeClient.from_resolver()
        except RuntimeError:
            client = None
        orch = Orchestrator(
            get_connection(),
            sources=list(DEFAULT_SOURCES),
            pipeline=FeaturePipeline(DEFAULT_TRANSFORMS),
            registry=reg,
        )
        try:
            report = orch.run(client=client, skip_sync=req.skip_sync)
        finally:
            if client is not None:
                client.close()
        return RunResponse(
            feature_results=[
                {"name": f.name, "duration_seconds": f.duration_seconds}
                for f in report.feature_results
            ],
            recommender_results=[
                RunRecommenderReport(**vars(r)) for r in report.recommender_results
            ],
        )

    @app.post("/recommenders/{name}/run", response_model=RunRecommenderReport)
    def run_one(
        name: str,
        req: RunRequest = RunRequest(),
        reg: RecommenderRegistry = Depends(_get_registry),
    ) -> RunRecommenderReport:
        rec = reg.get(name)
        if rec is None:
            raise HTTPException(404, f"recommender {name!r} not found")
        # Build a single-recommender registry so the orchestrator reuses its
        # existing run path (readiness → fit → predict → persist).
        solo = RecommenderRegistry()
        solo.register(rec, name=rec.name)  # same name
        try:
            client = SnowflakeClient.from_resolver()
        except RuntimeError:
            client = None
        orch = Orchestrator(
            get_connection(),
            sources=list(DEFAULT_SOURCES),
            pipeline=FeaturePipeline(DEFAULT_TRANSFORMS),
            registry=solo,
        )
        try:
            report = orch.run(client=client, skip_sync=req.skip_sync)
        finally:
            if client is not None:
                client.close()
        if not report.recommender_results:
            raise HTTPException(500, "orchestrator returned no results")
        return RunRecommenderReport(**vars(report.recommender_results[0]))

    @app.post("/features/run")
    def run_features() -> dict[str, list[dict[str, float | str]]]:
        pipeline = FeaturePipeline(DEFAULT_TRANSFORMS)
        results = pipeline.run(get_connection())
        return {
            "feature_results": [
                {"name": r.name, "duration_seconds": r.duration_seconds}
                for r in results
            ]
        }

    @app.post("/sync/backfill")
    def run_backfill(
        days: int = Query(..., gt=0, le=365),
        source: str | None = Query(None),
    ) -> dict[str, Any]:
        """Re-pull a wider historical window without destroying app.* state.

        Mechanism: DELETE the sync watermarks for the targeted incremental
        sources, then sync with ``initial_lookback_days=days``.  Idempotent
        because raw.* tables upsert on a PK.  Preserves recommendations,
        experiments, autonomous configs + audit, query groups, features.

        Use this when you want more history than the default 14-day initial
        lookback, OR when you want to refetch a window because something
        was redacted / changed.  For schema-level rebuilds, use
        ``snowtuner reset`` instead (more destructive — see its docs).
        """
        from snowtuner.ingestion.sync import backfill as do_backfill
        from snowtuner.ingestion.sources import DEFAULT_SOURCES

        sources = list(DEFAULT_SOURCES)
        if source:
            sources = [s for s in sources if s.name == source]
            if not sources:
                raise HTTPException(
                    404,
                    f"no source named {source!r}; "
                    f"available: {[s.name for s in DEFAULT_SOURCES]}",
                )
        incremental = [s for s in sources if s.watermark_column]
        if not incremental:
            return {
                "sync_results": [],
                "note": "no incremental sources matched; "
                        "full-refresh sources don't use a lookback",
            }

        client = SnowflakeClient.from_resolver()
        results, errors = do_backfill(
            incremental, client, get_connection(), days=days,
        )
        client.close()
        return {
            "sync_results": [
                {
                    "source_name": r.source_name,
                    "rows_ingested": r.rows_ingested,
                    "duration_seconds": r.duration_seconds,
                    "high_water": r.high_water.isoformat() if r.high_water else None,
                }
                for r in results
            ],
            "errors": [
                {"source_name": e.source_name, "error": e.error} for e in errors
            ],
        }

    @app.post("/sync/run")
    def run_sync() -> dict[str, Any]:
        """Run sync only (no features, no recommenders).

        Pulls deltas from Snowflake's ACCOUNT_USAGE views into ``raw.*``,
        respecting each source's watermark.  Use this when you want
        fresh raw data without paying for the full orchestrator pipeline
        (which also runs all feature transforms + every recommender).

        Uses a dedicated SnowflakeClient so it doesn't contend with any
        in-flight experiment using the engine's connection.
        """
        from snowtuner.ingestion.sync import sync_all
        from snowtuner.ingestion.sources import DEFAULT_SOURCES
        client = SnowflakeClient.from_resolver()
        results, errors = sync_all(
            list(DEFAULT_SOURCES), client, get_connection(),
        )
        client.close()
        return {
            "sync_results": [
                {
                    "source_name": r.source_name,
                    "rows_ingested": r.rows_ingested,
                    "duration_seconds": r.duration_seconds,
                    "high_water": r.high_water.isoformat() if r.high_water else None,
                }
                for r in results
            ],
            "errors": [
                {"source_name": e.source_name, "error": e.error} for e in errors
            ],
        }

    # ── Recommendations ───────────────────────────────────────────
    @app.get("/recommendations", response_model=list[RecommendationOut])
    def list_recs(
        status: RecommendationStatus = RecommendationStatus.PROPOSED,
        action_type: str | None = None,
        limit: int = Query(100, le=500),
        store: RecommendationStore = Depends(_get_store),
    ) -> list[RecommendationOut]:
        recs = store.list(status=status, action_type=action_type, limit=limit)
        return [RecommendationOut.from_model(r) for r in recs]

    @app.get("/recommendations/{rec_id}", response_model=RecommendationOut)
    def get_rec(
        rec_id: int,
        store: RecommendationStore = Depends(_get_store),
    ) -> RecommendationOut:
        rec = store.get(rec_id)
        if rec is None:
            raise HTTPException(404, f"recommendation {rec_id} not found")
        return RecommendationOut.from_model(rec)

    @app.post("/recommendations/{rec_id}/accept", response_model=RecommendationOut)
    def accept(
        rec_id: int,
        body: StatusUpdateRequest = StatusUpdateRequest(),
        store: RecommendationStore = Depends(_get_store),
    ) -> RecommendationOut:
        if store.get(rec_id) is None:
            raise HTTPException(404, f"recommendation {rec_id} not found")
        store.set_status(rec_id, RecommendationStatus.ACCEPTED, notes=body.note)
        return RecommendationOut.from_model(store.get(rec_id))  # type: ignore[arg-type]

    @app.post("/recommendations/{rec_id}/reject", response_model=RecommendationOut)
    def reject(
        rec_id: int,
        body: StatusUpdateRequest = StatusUpdateRequest(),
        store: RecommendationStore = Depends(_get_store),
    ) -> RecommendationOut:
        if store.get(rec_id) is None:
            raise HTTPException(404, f"recommendation {rec_id} not found")
        store.set_status(rec_id, RecommendationStatus.REJECTED, notes=body.note)
        return RecommendationOut.from_model(store.get(rec_id))  # type: ignore[arg-type]

    # ── Autonomous mode ───────────────────────────────────────────
    def _config_store() -> AutonomousConfigStore:
        return AutonomousConfigStore(get_connection())

    def _apps_store() -> AutonomousApplicationStore:
        return AutonomousApplicationStore(get_connection())

    @app.get("/autonomous/config", response_model=list[AutonomousConfigOut])
    def autonomous_list() -> list[AutonomousConfigOut]:
        return [AutonomousConfigOut(**c.__dict__) for c in _config_store().list()]

    @app.put(
        "/autonomous/config/{action_type}/{warehouse_name}/{knob}",
        response_model=AutonomousConfigOut,
    )
    def autonomous_upsert(
        action_type: str, warehouse_name: str, knob: str,
        body: AutonomousConfigUpsert,
    ) -> AutonomousConfigOut:
        cfg = _config_store().upsert(
            action_type, warehouse_name, knob,
            enabled=body.enabled,
            confidence_threshold=body.confidence_threshold,
            cooldown_hours=body.cooldown_hours,
            max_rollbacks_per_week=body.max_rollbacks_per_week,
        )
        return AutonomousConfigOut(**cfg.__dict__)

    @app.delete("/autonomous/config/{action_type}/{warehouse_name}/{knob}")
    def autonomous_delete(
        action_type: str, warehouse_name: str, knob: str,
    ) -> dict[str, str]:
        _config_store().delete(action_type, warehouse_name, knob)
        return {"status": "deleted"}

    @app.post(
        "/autonomous/config/{action_type}/{warehouse_name}/{knob}/reset-circuit"
    )
    def autonomous_reset_circuit(
        action_type: str, warehouse_name: str, knob: str,
    ) -> dict[str, str]:
        _config_store().reset_circuit(action_type, warehouse_name, knob)
        return {"status": "circuit reset"}

    @app.get(
        "/autonomous/applications",
        response_model=list[AutonomousApplicationOut],
    )
    def autonomous_applications(
        warehouse: str | None = None,
        action_type: str | None = None,
        limit: int = Query(50, ge=1, le=500),
    ) -> list[AutonomousApplicationOut]:
        rows = _apps_store().list(
            warehouse_name=warehouse, action_type=action_type, limit=limit,
        )
        return [
            AutonomousApplicationOut(**{**r.__dict__, "state": r.state.value})
            for r in rows
        ]

    @app.post("/autonomous/applications/{application_id}/rollback")
    def autonomous_rollback(application_id: int) -> dict[str, str]:
        try:
            client = SnowflakeClient.from_resolver()
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        runner = AutonomousRunner(get_connection(), client)
        try:
            decision = runner.rollback(application_id)
        finally:
            client.close()
        if decision.decision != "applied":
            raise HTTPException(status_code=500, detail=decision.reason)
        return {"status": "rolled back", "application_id": str(application_id)}

    # ── Warehouses + status ───────────────────────────────────────
    @app.get("/warehouses", response_model=list[WarehouseSummaryOut])
    def list_warehouses() -> list[WarehouseSummaryOut]:
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT w.name, w.size, w.auto_suspend_seconds, w.auto_resume,
                   w.generation,
                   (SELECT COUNT(*) FROM raw.query_history q
                    WHERE q.warehouse_name = w.name) AS q_cnt,
                   (SELECT COUNT(*) FROM raw.warehouse_events_history e
                    WHERE e.warehouse_name = w.name
                      AND e.event_name IN ('SUSPEND_WAREHOUSE','RESUME_WAREHOUSE')
                   ) AS cycle_cnt
            FROM raw.warehouses w
            ORDER BY q_cnt DESC
            """
        ).fetchall()
        return [
            WarehouseSummaryOut(
                name=r[0], size=r[1],
                auto_suspend_seconds=r[2],
                auto_resume=bool(r[3]) if r[3] is not None else None,
                generation=r[4],
                queries_in_window=int(r[5] or 0),
                suspend_resume_events=int(r[6] or 0),
            )
            for r in rows
        ]

    @app.get("/automation/status", response_model=AutomationStatusOut)
    def get_automation_status() -> AutomationStatusOut:
        """Snapshot the AutomationLoop's state.

        Returns whether the loop is enabled, the configured interval, when
        the next tick fires, and a fully-decomposed report of the last tick
        (per-stage outcomes, durations, errors).  Use this to verify
        ``SNOWTUNER_AUTOMATION_INTERVAL`` is set correctly and to debug
        ticks that failed silently in the background.
        """
        from snowtuner.api.automation import get_loop
        s = get_loop().status()

        def _stage_to_out(st) -> StageOutcomeOut:
            return StageOutcomeOut(
                name=st.name,
                started_at=st.started_at,
                duration_seconds=st.duration_seconds,
                outcome=st.outcome,
                error=st.error,
                details=st.details,
            )

        last_tick_out: TickReportOut | None = None
        if s.last_tick is not None:
            last_tick_out = TickReportOut(
                started_at=s.last_tick.started_at,
                completed_at=s.last_tick.completed_at,
                stages=[_stage_to_out(st) for st in s.last_tick.stages],
                overall=s.last_tick.overall,
                skip_reason=s.last_tick.skip_reason,
            )

        return AutomationStatusOut(
            enabled=s.enabled,
            interval_seconds=s.interval_seconds,
            currently_running=s.currently_running,
            next_run_at=s.next_run_at,
            last_tick=last_tick_out,
        )

    @app.post("/automation/run-now", response_model=TickReportOut)
    def run_automation_now() -> TickReportOut:
        """Trigger one tick of the AutomationLoop synchronously.

        Same code path as the background loop fires; runs the full
        sync→features→recommenders→autonomous pipeline.  Returns the
        tick report when complete.

        Useful for: validating the loop's behavior without waiting for
        an interval; triggering a fresh cycle on demand (UI's "Run now"
        button); CI/CD verification before declaring a deploy healthy.
        Refuses with a skipped report if another tick is already running.
        """
        from snowtuner.api.automation import get_loop
        report = get_loop().run_one_tick()
        return TickReportOut(
            started_at=report.started_at,
            completed_at=report.completed_at,
            stages=[
                StageOutcomeOut(
                    name=st.name,
                    started_at=st.started_at,
                    duration_seconds=st.duration_seconds,
                    outcome=st.outcome,
                    error=st.error,
                    details=st.details,
                )
                for st in report.stages
            ],
            overall=report.overall,
            skip_reason=report.skip_reason,
        )

    @app.get("/schema/drift", response_model=DriftReportOut)
    def get_schema_drift() -> DriftReportOut:
        """Compare each source's expected Snowflake columns against the live
        view's actual columns.

        Returns a structured report; the CLI's ``snowtuner check-schema``
        renders the same data.  Warn-only — never auto-evolves the schema.
        """
        from snowtuner.ingestion.drift import check_drift
        from snowtuner.ingestion.sources import DEFAULT_SOURCES
        client = SnowflakeClient.from_resolver()
        report = check_drift(client, list(DEFAULT_SOURCES))
        return DriftReportOut(
            sources=[
                SourceDriftOut(
                    source_name=s.source_name,
                    source_view=s.source_view,
                    expected_columns=s.expected_columns,
                    actual_columns=s.actual_columns,
                    missing_from_snowflake=s.missing_from_snowflake,
                    extra_in_snowflake=s.extra_in_snowflake,
                    error=s.error,
                    is_actionable=s.is_actionable,
                )
                for s in report.sources
            ],
            any_actionable=report.any_actionable,
        )

    @app.get("/status", response_model=StatusOut)
    def get_status() -> StatusOut:
        conn = get_connection()
        sources_meta = [
            ("query_history",              "raw.query_history",              "start_time"),
            ("warehouse_metering_history", "raw.warehouse_metering_history", "start_time"),
            ("warehouse_events_history",   "raw.warehouse_events_history",   "timestamp"),
            ("warehouses",                 "raw.warehouses",                 None),
        ]
        sources: list[SourceFreshnessOut] = []
        for source_name, tbl, ts_col in sources_meta:
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            earliest = latest = None
            if ts_col and n:
                lo, hi = conn.execute(
                    f"SELECT MIN({ts_col}), MAX({ts_col}) FROM {tbl}"
                ).fetchone()
                earliest, latest = lo, hi
            wm = conn.execute(
                "SELECT last_sync_at FROM app.sync_watermarks "
                "WHERE source_name = ?",
                [source_name],
            ).fetchone()
            sources.append(SourceFreshnessOut(
                name=source_name, rows=int(n),
                earliest=earliest, latest=latest,
                last_synced_at=wm[0] if wm else None,
            ))

        # Reuse the warehouse endpoint's logic via a direct call
        warehouses = list_warehouses()

        # Recommender training states
        rs_rows = conn.execute(
            """
            SELECT recommender_name, is_ready, last_fit_at, readiness_report
            FROM app.training_state
            """
        ).fetchall()
        import json as _json
        rec_states = []
        for name, ready, last_fit, report_json in rs_rows:
            try:
                rep = _json.loads(report_json) if report_json else {}
            except Exception:
                rep = {}
            rec_states.append({
                "name": name,
                "is_ready": bool(ready),
                "last_fit_at": last_fit.isoformat() if last_fit else None,
                "reason": rep.get("reason"),
            })

        # Recommendation counts by status
        rc_rows = conn.execute(
            "SELECT status, COUNT(*) FROM app.recommendations GROUP BY status"
        ).fetchall()
        counts = {s: 0 for s in [
            "PROPOSED", "ACCEPTED", "REJECTED",
            "APPLIED", "ROLLED_BACK", "SUPERSEDED",
        ]}
        for status, n in rc_rows:
            counts[status] = int(n)

        return StatusOut(
            sources=sources,
            warehouses=warehouses,
            recommender_states=rec_states,
            recommendation_counts=counts,
        )

    # ── Credentials (read-only summary + connectivity test) ─────────
    @app.get("/credentials", response_model=CredentialStatusOut)
    def get_credentials() -> CredentialStatusOut:
        from snowtuner.credentials import CredentialResolver
        result = CredentialResolver().load()
        if result is None:
            return CredentialStatusOut(configured=False)
        c = result.credentials
        return CredentialStatusOut(
            configured=True,
            account=c.account,
            user=c.user,
            role=c.role,
            warehouse=c.warehouse,
            auth_method=c.auth_method.value,
            source=result.source.value,
            private_key_path=c.private_key_path,
        )

    @app.post("/credentials/verify", response_model=CredentialVerifyOut)
    def verify_credentials() -> CredentialVerifyOut:
        from snowtuner.credentials import CredentialResolver
        from snowtuner.ingestion.snowflake_client import SnowflakeClient

        result = CredentialResolver().load()
        if result is None:
            return CredentialVerifyOut(
                ok=False,
                error="No credentials configured.  Run `snowtuner init` to set them up.",
            )
        client = SnowflakeClient(result.credentials)
        try:
            rows = client.execute(
                "SELECT CURRENT_ACCOUNT(), CURRENT_USER(), CURRENT_ROLE(), "
                "CURRENT_WAREHOUSE(), CURRENT_REGION()"
            )
        except Exception as e:
            return CredentialVerifyOut(ok=False, error=f"{type(e).__name__}: {e}")
        finally:
            client.close()
        if not rows:
            return CredentialVerifyOut(ok=False, error="connection succeeded but no rows returned")
        account, user, role, warehouse, region = rows[0]
        return CredentialVerifyOut(
            ok=True,
            account=str(account) if account is not None else None,
            user=str(user) if user is not None else None,
            role=str(role) if role is not None else None,
            warehouse=str(warehouse) if warehouse is not None else None,
            region=str(region) if region is not None else None,
        )

    # ── Experiments (v0.2) ────────────────────────────────────────
    def _experiment_store() -> ExperimentStore:
        return ExperimentStore(get_connection())

    @app.get("/experiments/recipes", response_model=list[RecipeInfo])
    def list_recipes() -> list[RecipeInfo]:
        out: list[RecipeInfo] = []
        for name, recipe in PRESET_RECIPES.items():
            doc = (recipe.__doc__ or "").strip().split("\n")[0]
            out.append(RecipeInfo(name=name, summary=doc))
        return out

    @app.get("/experiments", response_model=list[Experiment])
    def list_experiments(
        status: ExperimentStatus | None = None,
        target_warehouse: str | None = None,
        limit: int = Query(100, le=500),
        store: ExperimentStore = Depends(_experiment_store),
    ) -> list[Experiment]:
        return store.list(
            status=status, target_warehouse=target_warehouse, limit=limit,
        )

    @app.get("/experiments/{experiment_id}", response_model=Experiment)
    def get_experiment(
        experiment_id: int,
        store: ExperimentStore = Depends(_experiment_store),
    ) -> Experiment:
        exp = store.get(experiment_id)
        if exp is None:
            raise HTTPException(404, f"experiment {experiment_id} not found")
        return exp

    @app.get("/experiments/{experiment_id}/runs", response_model=list[ExperimentRun])
    def list_experiment_runs(
        experiment_id: int,
        arm_name: str | None = None,
        store: ExperimentStore = Depends(_experiment_store),
    ) -> list[ExperimentRun]:
        if store.get(experiment_id) is None:
            raise HTTPException(404, f"experiment {experiment_id} not found")
        return store.runs_for(experiment_id, arm_name=arm_name)

    @app.post("/experiments/propose", response_model=Experiment)
    def propose_experiment(
        req: ProposeExperimentRequest,
        store: ExperimentStore = Depends(_experiment_store),
    ) -> Experiment:
        if req.recipe_name not in PRESET_RECIPES:
            raise HTTPException(
                400, f"unknown recipe {req.recipe_name!r}; "
                f"valid: {sorted(PRESET_RECIPES.keys())}",
            )
        recipe = PRESET_RECIPES[req.recipe_name]
        warehouse = _load_warehouse_config(req.target_warehouse)
        if warehouse is None:
            raise HTTPException(
                404,
                f"warehouse {req.target_warehouse!r} not found in raw.warehouses; "
                f"run a sync first",
            )

        # ── Resolve workload at propose-time (Phase 3) ──────────────
        # Either auto-sample from target_warehouse (default) or use the
        # picked saved group's members.  The resolved sampled queries seed
        # both the cost estimate and the persisted ``sampled_query_ids``
        # the engine will later replay from.
        from snowtuner.experiments.workload import resolve_workload
        group = _resolve_query_group(req.query_group_id) if req.query_group_id else None
        resolved = resolve_workload(
            get_connection(),
            workload_warehouse=req.target_warehouse,
            query_group=group,
            sample_size=_recipe_default_sample_size(),
        )
        if not resolved.sampled:
            raise HTTPException(
                422,
                (
                    f"no eligible queries found in saved group #{req.query_group_id}; "
                    f"warnings: {resolved.warnings}"
                ) if req.query_group_id else
                f"no eligible queries found on warehouse {req.target_warehouse!r} "
                f"(check that sync has run and there is recent SELECT activity)",
            )

        proposed = recipe(
            warehouse,
            _account_info(),
            sample_query_stats=[s.historical for s in resolved.sampled],
        )
        if proposed is None:
            raise HTTPException(
                422,
                f"recipe {req.recipe_name!r} is not eligible for "
                f"warehouse {req.target_warehouse!r}",
            )
        # Freeze the workload onto the proposal so the user can preview /
        # edit it before accepting, and so the engine reads exactly this
        # set when it runs.
        proposed.sampled_query_ids = [s.query_id for s in resolved.sampled]
        proposed.workload_source = resolved.source
        proposed.sample_warnings = resolved.warnings
        # The recipe used a default sample_size; align it with what the
        # resolver actually produced so downstream cost estimates and
        # warnings agree.
        proposed.sample_size = len(resolved.sampled)
        new_id = store.insert(proposed)
        return store.get(new_id)  # type: ignore[return-value]

    @app.post("/experiments/propose-benchmark", response_model=Experiment)
    def propose_benchmark_experiment(
        req: ProposeBenchmarkRequest,
        store: ExperimentStore = Depends(_experiment_store),
    ) -> Experiment:
        """Propose a benchmark-kind experiment: compare N absolute configurations
        against a workload.

        Distinct from `/experiments/propose` because:
          - No recipe — arms are user-built
          - No target warehouse to clone control from — arms are absolute
          - Workload source is explicit (workload_warehouse), not derived
        """
        # Either a workload warehouse OR a query group must be supplied.
        # If both, the group takes precedence (warehouse is used purely as
        # "eligibility context" — see below).
        if not req.query_group_id and not req.workload_warehouse:
            raise HTTPException(
                422,
                "benchmark experiments need a workload source: either "
                "workload_warehouse or query_group_id",
            )
        workload_wh = (
            _load_warehouse_config(req.workload_warehouse)
            if req.workload_warehouse else None
        )
        if req.workload_warehouse and workload_wh is None:
            raise HTTPException(
                404,
                f"workload warehouse {req.workload_warehouse!r} not found in "
                f"raw.warehouses; run a sync first",
            )
        if len(req.arms) < 2:
            raise HTTPException(
                422, "benchmark experiments need at least 2 arms to compare",
            )
        if req.control_arm_name and req.control_arm_name not in {a.name for a in req.arms}:
            raise HTTPException(
                422,
                f"control_arm_name {req.control_arm_name!r} is not one of the "
                f"submitted arms: {[a.name for a in req.arms]}",
            )

        # Build Arms from the spec.  Benchmark arms are full configs encoded
        # as deltas (every field set); merge() against an empty control will
        # pass them through verbatim at engine time.
        from snowtuner.experiments.arm import Arm
        from snowtuner.experiments.axes import Generation, QASState
        from snowtuner.experiments.config_delta import WarehouseConfigDelta
        from snowtuner.experiments.eligibility import check_arm_eligibility
        from snowtuner.experiments.model import (
            ExperimentKind,
            ProposedExperiment,
        )
        from snowtuner.experiments.cost_estimate import (
            estimate_experiment_cost,
        )
        from snowtuner.recommenders.sizes import credit_rate, normalize as normalize_size

        built_arms: list[Arm] = []
        for spec in req.arms:
            size = normalize_size(spec.size) if spec.size else None
            generation = Generation(spec.generation) if spec.generation else None
            qas_state = QASState(spec.qas_state.lower()) if spec.qas_state else None
            delta = WarehouseConfigDelta(
                size=size,
                generation=generation,
                qas_state=qas_state,
                qas_max_scale_factor=spec.qas_max_scale_factor,
            )
            arm = Arm.from_delta(delta, name=spec.name)
            built_arms.append(arm)

        # Run eligibility on each arm against the workload warehouse's config
        # (used as a stand-in "context"; not as a control source — benchmark
        # arms don't inherit from it).  If no workload_warehouse was supplied
        # (group-only mode), use an empty WarehouseConfig as the context.
        account = _account_info()
        eligibility_ctx = workload_wh or WarehouseConfig(name="__GROUP_CTX__")
        for arm in built_arms:
            arm.eligibility_issues = check_arm_eligibility(arm, eligibility_ctx, account)

        runnable_arms = [a for a in built_arms if not a.has_blocking_issues]
        if len(runnable_arms) < 2:
            blocked = [
                {"arm": a.name, "issues": [
                    {"severity": i.severity, "message": i.message}
                    for i in a.eligibility_issues
                ]}
                for a in built_arms if a.has_blocking_issues
            ]
            raise HTTPException(
                422,
                f"after eligibility, fewer than 2 arms can run.  Blocked: {blocked}",
            )

        # ── Resolve workload at propose-time (Phase 3) ──────────────
        # Either auto-sample from workload_warehouse or load the picked
        # saved group's members.  Cost estimate uses the actual selected
        # queries' stats, not a separate "preview" sample.
        from snowtuner.experiments.workload import resolve_workload
        group = _resolve_query_group(req.query_group_id) if req.query_group_id else None
        resolved = resolve_workload(
            get_connection(),
            workload_warehouse=req.workload_warehouse,
            query_group=group,
            sample_size=req.sample_size,
        )
        if not resolved.sampled:
            raise HTTPException(
                422,
                (
                    f"no eligible queries found in saved group #{req.query_group_id}; "
                    f"warnings: {resolved.warnings}"
                ) if req.query_group_id else
                f"no eligible queries found on warehouse {req.workload_warehouse!r}",
            )

        # Cost estimate.  Each arm's credit rate comes from its own size
        # (or XSMALL fallback if unset).
        sample_stats = [s.historical for s in resolved.sampled]
        arm_rates = {
            a.name: credit_rate(a.delta.size or "XSMALL") for a in runnable_arms
        }
        cost_estimate = estimate_experiment_cost(
            sample_query_stats=sample_stats,
            arm_credit_rates_per_hour=arm_rates,
            reps_per_arm=req.reps_per_arm,
        )

        warning_issues = [
            i for a in runnable_arms for i in a.eligibility_issues
            if i.severity == "warning"
        ]

        proposed = ProposedExperiment(
            kind=ExperimentKind.BENCHMARK,
            recipe_name="user_built_benchmark",
            target_warehouse=None,
            workload_warehouse=(
                req.workload_warehouse.upper() if req.workload_warehouse else None
            ),
            control_arm_name=req.control_arm_name,
            hypothesis=req.hypothesis,
            arms=runnable_arms,
            sample_size=len(resolved.sampled),
            reps_per_arm=req.reps_per_arm,
            cost_estimate=cost_estimate,
            eligibility_issues=warning_issues,
            proposed_by="user",
            sampled_query_ids=[s.query_id for s in resolved.sampled],
            workload_source=resolved.source,
            sample_warnings=resolved.warnings,
        )
        new_id = store.insert(proposed)
        return store.get(new_id)  # type: ignore[return-value]

    @app.post("/experiments/{experiment_id}/accept", response_model=Experiment)
    def accept_experiment(
        experiment_id: int,
        store: ExperimentStore = Depends(_experiment_store),
    ) -> Experiment:
        exp = store.get(experiment_id)
        if exp is None:
            raise HTTPException(404, f"experiment {experiment_id} not found")
        if exp.status != ExperimentStatus.PROPOSED:
            raise HTTPException(
                409,
                f"experiment is in status {exp.status.value}; "
                f"only PROPOSED experiments can be accepted",
            )
        if store.has_running_experiment():
            raise HTTPException(
                409,
                "another experiment is already accepted or running; "
                "abort it first",
            )
        store.set_status(experiment_id, ExperimentStatus.ACCEPTED)
        return store.get(experiment_id)  # type: ignore[return-value]

    @app.post("/experiments/{experiment_id}/reject", response_model=Experiment)
    def reject_experiment(
        experiment_id: int,
        store: ExperimentStore = Depends(_experiment_store),
    ) -> Experiment:
        exp = store.get(experiment_id)
        if exp is None:
            raise HTTPException(404, f"experiment {experiment_id} not found")
        if exp.status != ExperimentStatus.PROPOSED:
            raise HTTPException(
                409, f"only PROPOSED experiments can be rejected; "
                f"this one is {exp.status.value}",
            )
        store.set_status(experiment_id, ExperimentStatus.REJECTED)
        return store.get(experiment_id)  # type: ignore[return-value]

    @app.delete(
        "/experiments/{experiment_id}/sampled-queries/{query_id}",
        response_model=Experiment,
    )
    def remove_sampled_query(
        experiment_id: int,
        query_id: str,
        store: ExperimentStore = Depends(_experiment_store),
    ) -> Experiment:
        """Remove a single query from a PROPOSED experiment's frozen workload.

        Re-estimates cost from the remaining queries.  Refuses if:
          * the experiment isn't PROPOSED (already accepted/running)
          * the resulting list would be empty (would leave the experiment
            with no workload — better to reject the proposal entirely)
        """
        exp = store.get(experiment_id)
        if exp is None:
            raise HTTPException(404, f"experiment {experiment_id} not found")
        if exp.status != ExperimentStatus.PROPOSED:
            raise HTTPException(
                409,
                f"workload can only be edited while PROPOSED; this experiment "
                f"is {exp.status.value}",
            )
        ids = list(exp.proposed.sampled_query_ids or [])
        if query_id not in ids:
            raise HTTPException(
                404,
                f"query {query_id!r} is not in this experiment's workload",
            )
        ids.remove(query_id)
        if not ids:
            raise HTTPException(
                422,
                "removing this query would leave the experiment with no "
                "workload; reject the proposal instead",
            )

        # Re-estimate cost from the remaining queries' stats.
        rows = get_connection().execute(
            f"""
            SELECT query_id, total_elapsed_ms, bytes_scanned
            FROM raw.query_history
            WHERE query_id IN ({", ".join(["?"] * len(ids))})
            """,
            ids,
        ).fetchall()
        from snowtuner.experiments.cost_estimate import (
            QueryStats,
            estimate_experiment_cost,
        )
        from snowtuner.recommenders.sizes import credit_rate
        sample_stats = [
            QueryStats(
                query_id=r[0],
                p50_elapsed_ms=float(r[1] or 0),
                mean_elapsed_ms=float(r[1] or 0),
                bytes_scanned=int(r[2]) if r[2] is not None else None,
            )
            for r in rows
        ]
        # Arm credit rates: for TUNING use the target warehouse's size
        # (control merged with each arm's delta); for BENCHMARK each arm has
        # its own size.  Mirror the same logic each propose endpoint used.
        if exp.proposed.kind.value == "tuning":
            wh = _load_warehouse_config(exp.proposed.target_warehouse or "")
            base_size = (wh.size if wh else None) or "XSMALL"
            arm_rates = {
                a.name: credit_rate(((wh or WarehouseConfig(name="x")).merge(a.delta)).size or base_size)
                for a in exp.proposed.arms
            }
        else:
            arm_rates = {
                a.name: credit_rate(a.delta.size or "XSMALL")
                for a in exp.proposed.arms
            }
        new_cost = estimate_experiment_cost(
            sample_query_stats=sample_stats,
            arm_credit_rates_per_hour=arm_rates,
            reps_per_arm=exp.proposed.reps_per_arm,
        )

        # Persist the edited proposal.
        new_proposed = exp.proposed.model_copy(update={
            "sampled_query_ids": ids,
            "sample_size": len(ids),
            "cost_estimate": new_cost,
        })
        store.set_proposed(experiment_id, new_proposed)
        return store.get(experiment_id)  # type: ignore[return-value]

    @app.post("/experiments/{experiment_id}/backfill-metrics")
    def backfill_experiment_metrics(
        experiment_id: int,
        store: ExperimentStore = Depends(_experiment_store),
    ) -> dict[str, Any]:
        """Recover metrics on a COMPLETED experiment whose live fetch failed.

        Pulls elapsed_ms / bytes_scanned / spill stats from
        ``SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY`` for every SUCCESS run that
        has a ``replay_query_id`` but no ``elapsed_ms``.  UPDATEs the run
        rows in place, then re-aggregates and writes the new report.

        ACCOUNT_USAGE has a ~45-minute lag; if a recent experiment came back
        empty, wait an hour before backfilling or the rows won't be there.

        Returns a small summary: rows_inspected / rows_updated /
        rows_unreachable / report_regenerated.
        """
        from snowtuner.experiments.backfill import backfill_metrics
        try:
            return backfill_metrics(
                store=store,
                snowflake_client=SnowflakeClient.from_resolver(),
                experiment_id=experiment_id,
            )
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/experiments/{experiment_id}/run", response_model=Experiment)
    def run_experiment(
        experiment_id: int,
        store: ExperimentStore = Depends(_experiment_store),
    ) -> Experiment:
        """Spawn the engine in a background thread; return immediately.

        The client polls GET /experiments/{id} to watch status transitions
        through RUNNING → COMPLETED/FAILED/ABORTED.
        """
        exp = store.get(experiment_id)
        if exp is None:
            raise HTTPException(404, f"experiment {experiment_id} not found")
        if exp.status != ExperimentStatus.ACCEPTED:
            raise HTTPException(
                409,
                f"only ACCEPTED experiments can be run; "
                f"this one is {exp.status.value}",
            )

        try:
            client = SnowflakeClient.from_resolver()
        except RuntimeError as e:
            raise HTTPException(503, str(e))

        def _run() -> None:
            try:
                # Use a fresh DuckDB connection for the background thread —
                # get_connection() returns a thread-local cursor, so this
                # call inside the new thread mints a separate cursor.
                engine = ExperimentEngine(get_connection(), client)
                engine.run(experiment_id)
            finally:
                client.close()

        threading.Thread(target=_run, daemon=True, name=f"exp-{experiment_id}").start()
        # Return the experiment in its (probably-still-ACCEPTED) state; the
        # caller polls for transitions.
        return store.get(experiment_id)  # type: ignore[return-value]

    @app.post("/experiments/{experiment_id}/abort", response_model=Experiment)
    def abort_experiment(
        experiment_id: int,
        body: AbortExperimentRequest,
        store: ExperimentStore = Depends(_experiment_store),
    ) -> Experiment:
        """Mark an experiment as ABORTED.

        v0.2 doesn't yet have a cooperative-cancel signal to the running
        engine thread — the engine notices status changes between phases.
        For a hard abort during a long-running query, restart the API
        process; the next startup will clean up orphaned warehouses.
        """
        exp = store.get(experiment_id)
        if exp is None:
            raise HTTPException(404, f"experiment {experiment_id} not found")
        if exp.status not in (ExperimentStatus.ACCEPTED, ExperimentStatus.RUNNING):
            raise HTTPException(
                409,
                f"only ACCEPTED or RUNNING experiments can be aborted; "
                f"this one is {exp.status.value}",
            )
        store.set_status(
            experiment_id, ExperimentStatus.ABORTED,
            aborted_reason=body.reason,
        )
        return store.get(experiment_id)  # type: ignore[return-value]

    # ── Queries explorer ──────────────────────────────────────────
    @app.get("/queries", response_model=QueryListResponse)
    def list_queries(
        warehouse: str | None = Query(None, description="Comma-separated warehouse names"),
        user: str | None = Query(None, description="Comma-separated user names"),
        role: str | None = Query(None, description="Comma-separated role names"),
        query_type: str | None = Query(None, description="Comma-separated query types"),
        status: str | None = Query(None, description="Comma-separated execution statuses"),
        parameterized_hash: str | None = Query(None, description="Filter to one parameterized_hash"),
        start_from: datetime | None = Query(None, description="start_time >= this"),
        start_to: datetime | None = Query(None, description="start_time <= this"),
        min_elapsed_ms: int | None = Query(None, ge=0),
        max_elapsed_ms: int | None = Query(None, ge=0),
        has_remote_spill: bool | None = Query(None),
        has_local_spill: bool | None = Query(None),
        has_queueing: bool | None = Query(None),
        search: str | None = Query(None, description="Substring search over query text (case-insensitive)"),
        # Structural filters (joined from features.query_sql_features)
        min_joins: int | None = Query(None, ge=0),
        max_joins: int | None = Query(None, ge=0),
        min_tables: int | None = Query(None, ge=0),
        max_tables: int | None = Query(None, ge=0),
        min_ctes: int | None = Query(None, ge=0),
        max_ctes: int | None = Query(None, ge=0),
        min_subqueries: int | None = Query(None, ge=0),
        max_subqueries: int | None = Query(None, ge=0),
        min_where_blocks: int | None = Query(None, ge=0),
        max_where_blocks: int | None = Query(None, ge=0),
        min_where_predicates: int | None = Query(None, ge=0),
        max_where_predicates: int | None = Query(None, ge=0),
        # Semantic predicates (Phase 2) — comma-separated names.
        # Names are case-insensitive (uppercased server-side).
        referenced_tables_include: str | None = Query(
            None, description="Comma-sep table names; query must touch ALL"
        ),
        referenced_tables_exclude: str | None = Query(
            None, description="Comma-sep table names; query must touch NONE"
        ),
        where_columns_include: str | None = Query(
            None, description="Comma-sep column names; query must filter on ALL"
        ),
        where_columns_exclude: str | None = Query(
            None, description="Comma-sep column names; query must NOT filter on any"
        ),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> QueryListResponse:
        where, params = _build_query_filter(
            warehouse=warehouse, user=user, role=role,
            query_type=query_type, status=status,
            parameterized_hash=parameterized_hash,
            start_from=start_from, start_to=start_to,
            min_elapsed_ms=min_elapsed_ms, max_elapsed_ms=max_elapsed_ms,
            has_remote_spill=has_remote_spill, has_local_spill=has_local_spill,
            has_queueing=has_queueing, search=search,
            min_joins=min_joins, max_joins=max_joins,
            min_tables=min_tables, max_tables=max_tables,
            min_ctes=min_ctes, max_ctes=max_ctes,
            min_subqueries=min_subqueries, max_subqueries=max_subqueries,
            min_where_blocks=min_where_blocks, max_where_blocks=max_where_blocks,
            min_where_predicates=min_where_predicates,
            max_where_predicates=max_where_predicates,
            referenced_tables_include=referenced_tables_include,
            referenced_tables_exclude=referenced_tables_exclude,
            where_columns_include=where_columns_include,
            where_columns_exclude=where_columns_exclude,
        )
        where_sql = f"WHERE {where}" if where else ""
        conn = get_connection()
        total = int(conn.execute(
            f"""
            SELECT COUNT(*)
            FROM raw.query_history q
            LEFT JOIN features.query_sql_features f USING (query_id)
            {where_sql}
            """,
            params,
        ).fetchone()[0])
        rows = conn.execute(
            f"""
            SELECT q.query_id, q.query_text, q.query_type, q.execution_status,
                   q.user_name, q.role_name, q.warehouse_name, q.warehouse_size,
                   q.start_time, q.total_elapsed_ms, q.bytes_scanned,
                   q.bytes_spilled_to_local, q.bytes_spilled_to_remote,
                   q.queued_overload_ms, q.query_parameterized_hash,
                   f.joins_count, f.tables_referenced_count, f.ctes_count,
                   f.subqueries_count, f.where_block_count, f.where_predicate_count
            FROM raw.query_history q
            LEFT JOIN features.query_sql_features f USING (query_id)
            {where_sql}
            ORDER BY q.start_time DESC NULLS LAST
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
        return QueryListResponse(
            rows=[QueryRow(
                query_id=r[0],
                query_text_preview=_truncate(r[1], 200),
                query_type=r[2], execution_status=r[3],
                user_name=r[4], role_name=r[5],
                warehouse_name=r[6], warehouse_size=r[7],
                start_time=r[8], total_elapsed_ms=r[9],
                bytes_scanned=r[10],
                bytes_spilled_to_local=r[11], bytes_spilled_to_remote=r[12],
                queued_overload_ms=r[13],
                query_parameterized_hash=r[14],
                joins_count=r[15], tables_referenced_count=r[16],
                ctes_count=r[17], subqueries_count=r[18],
                where_block_count=r[19], where_predicate_count=r[20],
            ) for r in rows],
            total=total, limit=limit, offset=offset,
        )

    @app.get("/queries/facets", response_model=QueryFilterFacets)
    def query_facets(
        lookback_days: int = Query(30, ge=1, le=365),
        semantic_limit: int = Query(
            500, ge=1, le=5000,
            description="Cap on the number of semantic facet entries (tables, where columns).",
        ),
    ) -> QueryFilterFacets:
        """Distinct filter values from the last N days of query history.

        Scoped to a window so we don't surface long-departed users / decommissioned
        warehouses in the filter chips.

        Semantic facets (tables, where columns) are ranked by usage frequency
        within the window and capped at ``semantic_limit`` so the payload stays
        bounded on big workloads.
        """
        conn = get_connection()
        scope = f"start_time >= now() - INTERVAL {lookback_days} DAYS"

        def _distinct(col: str) -> list[str]:
            rows = conn.execute(
                f"""
                SELECT DISTINCT {col}
                FROM raw.query_history
                WHERE {scope} AND {col} IS NOT NULL
                ORDER BY {col}
                """
            ).fetchall()
            return [str(r[0]) for r in rows]

        # Semantic facets: join the side tables to raw.query_history so we
        # can scope by the same lookback window and rank by query count.
        def _semantic(side_table: str, name_col: str) -> list[str]:
            rows = conn.execute(
                f"""
                SELECT s.{name_col}, COUNT(*) AS uses
                FROM features.{side_table} s
                JOIN raw.query_history q ON q.query_id = s.query_id
                WHERE q.{scope}
                GROUP BY s.{name_col}
                ORDER BY uses DESC, s.{name_col}
                LIMIT ?
                """,
                [semantic_limit],
            ).fetchall()
            return [str(r[0]) for r in rows]

        return QueryFilterFacets(
            warehouses=_distinct("warehouse_name"),
            users=_distinct("user_name"),
            roles=_distinct("role_name"),
            query_types=_distinct("query_type"),
            execution_statuses=_distinct("execution_status"),
            referenced_tables=_semantic("query_referenced_tables", "table_ref"),
            where_columns=_semantic("query_where_columns", "column_ref"),
        )

    @app.get("/queries/{query_id}", response_model=QueryDetail)
    def get_query(query_id: str) -> QueryDetail:
        conn = get_connection()
        row = conn.execute(
            """
            SELECT q.query_id, q.query_text, q.query_type, q.execution_status,
                   q.user_name, q.role_name, q.warehouse_name, q.warehouse_size,
                   q.database_name, q.schema_name, q.start_time, q.end_time,
                   q.total_elapsed_ms, q.compilation_ms, q.execution_ms,
                   q.queued_overload_ms, q.queued_provisioning_ms,
                   q.bytes_scanned, q.bytes_spilled_to_local, q.bytes_spilled_to_remote,
                   q.query_parameterized_hash,
                   f.joins_count, f.tables_referenced_count, f.ctes_count,
                   f.subqueries_count, f.where_block_count, f.where_predicate_count,
                   f.parse_error
            FROM raw.query_history q
            LEFT JOIN features.query_sql_features f USING (query_id)
            WHERE q.query_id = ?
            """,
            [query_id],
        ).fetchone()
        if not row:
            raise HTTPException(404, f"query {query_id!r} not found")
        table_rows = conn.execute(
            "SELECT table_ref FROM features.query_referenced_tables "
            "WHERE query_id = ? ORDER BY table_ref",
            [query_id],
        ).fetchall()
        col_rows = conn.execute(
            "SELECT column_ref FROM features.query_where_columns "
            "WHERE query_id = ? ORDER BY column_ref",
            [query_id],
        ).fetchall()
        return QueryDetail(
            query_id=row[0], query_text=row[1] or "",
            query_type=row[2], execution_status=row[3],
            user_name=row[4], role_name=row[5],
            warehouse_name=row[6], warehouse_size=row[7],
            database_name=row[8], schema_name=row[9],
            start_time=row[10], end_time=row[11],
            total_elapsed_ms=row[12],
            compilation_ms=row[13], execution_ms=row[14],
            queued_overload_ms=row[15], queued_provisioning_ms=row[16],
            bytes_scanned=row[17],
            bytes_spilled_to_local=row[18], bytes_spilled_to_remote=row[19],
            query_parameterized_hash=row[20],
            joins_count=row[21], tables_referenced_count=row[22],
            ctes_count=row[23], subqueries_count=row[24],
            where_block_count=row[25], where_predicate_count=row[26],
            sql_features_parse_error=row[27],
            referenced_tables=[str(r[0]) for r in table_rows],
            where_columns=[str(r[0]) for r in col_rows],
        )

    @app.get("/query-families", response_model=list[QueryFamily])
    def list_query_families(
        warehouse: str | None = Query(None, description="Comma-separated warehouse names"),
        user: str | None = Query(None, description="Comma-separated user names"),
        query_type: str | None = Query(None, description="Comma-separated query types"),
        status: str | None = Query(None, description="Comma-separated execution statuses"),
        start_from: datetime | None = Query(None),
        start_to: datetime | None = Query(None),
        min_elapsed_ms: int | None = Query(None, ge=0),
        max_elapsed_ms: int | None = Query(None, ge=0),
        has_remote_spill: bool | None = Query(None),
        has_local_spill: bool | None = Query(None),
        search: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
    ) -> list[QueryFamily]:
        """Aggregated rollup by query_parameterized_hash.

        Default sort: total_elapsed_ms DESC (the "biggest cost contributors first"
        view — same impact ranking the experiments sampler uses internally).
        """
        where, params = _build_query_filter(
            warehouse=warehouse, user=user, query_type=query_type, status=status,
            parameterized_hash=None,
            start_from=start_from, start_to=start_to,
            min_elapsed_ms=min_elapsed_ms, max_elapsed_ms=max_elapsed_ms,
            has_remote_spill=has_remote_spill, has_local_spill=has_local_spill,
            has_queueing=None, search=search,
        )
        # Families need a non-null hash.
        where = f"({where} AND q.query_parameterized_hash IS NOT NULL)" if where \
            else "q.query_parameterized_hash IS NOT NULL"

        rows = get_connection().execute(
            f"""
            WITH filtered AS (
                SELECT q.*
                FROM raw.query_history q
                LEFT JOIN features.query_sql_features f USING (query_id)
                WHERE {where}
            ),
            ranked AS (
                SELECT
                    query_parameterized_hash,
                    query_id,
                    query_text,
                    ROW_NUMBER() OVER (
                        PARTITION BY query_parameterized_hash
                        ORDER BY start_time DESC
                    ) AS rn
                FROM filtered
            ),
            reps AS (
                SELECT query_parameterized_hash, query_id, query_text
                FROM ranked WHERE rn = 1
            )
            SELECT
                filtered.query_parameterized_hash,
                reps.query_id,
                reps.query_text,
                COUNT(*) AS occurrence_count,
                AVG(filtered.total_elapsed_ms) AS mean_elapsed_ms,
                quantile_cont(filtered.total_elapsed_ms, 0.95) AS p95_elapsed_ms,
                SUM(filtered.total_elapsed_ms) AS total_elapsed_ms,
                SUM(filtered.bytes_scanned) AS total_bytes_scanned,
                SUM(CASE WHEN filtered.bytes_spilled_to_remote > 0 THEN 1 ELSE 0 END) AS n_spill_remote,
                SUM(CASE WHEN filtered.execution_status <> 'SUCCESS' THEN 1 ELSE 0 END) AS n_failed,
                MIN(filtered.start_time) AS first_seen,
                MAX(filtered.start_time) AS last_seen,
                COUNT(DISTINCT filtered.warehouse_name) AS distinct_warehouses,
                COUNT(DISTINCT filtered.user_name) AS distinct_users
            FROM filtered
            JOIN reps USING (query_parameterized_hash)
            GROUP BY filtered.query_parameterized_hash, reps.query_id, reps.query_text
            ORDER BY total_elapsed_ms DESC NULLS LAST
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
        return [QueryFamily(
            query_parameterized_hash=r[0],
            representative_query_id=r[1],
            representative_sql=_truncate(r[2], 300),
            occurrence_count=int(r[3]),
            mean_elapsed_ms=float(r[4]) if r[4] is not None else None,
            p95_elapsed_ms=float(r[5]) if r[5] is not None else None,
            total_elapsed_ms=int(r[6]) if r[6] is not None else None,
            total_bytes_scanned=int(r[7]) if r[7] is not None else None,
            n_spill_remote=int(r[8] or 0),
            n_failed=int(r[9] or 0),
            first_seen=r[10], last_seen=r[11],
            distinct_warehouses=int(r[12] or 0),
            distinct_users=int(r[13] or 0),
        ) for r in rows]

    # ── Query groups (slice 2) ────────────────────────────────────
    def _group_store() -> QueryGroupStore:
        return QueryGroupStore(get_connection())

    @app.post("/query-groups", response_model=QueryGroup)
    def create_query_group(
        req: CreateQueryGroupRequest,
        store: QueryGroupStore = Depends(_group_store),
    ) -> QueryGroup:
        try:
            kind = QueryGroupKind(req.kind)
        except ValueError:
            raise HTTPException(
                400, f"unknown kind {req.kind!r}; must be 'static' or 'dynamic'",
            )

        spec = _filter_spec_from_create_req(req)

        # For static groups, snapshot the matching query_ids at creation time.
        # For dynamic groups, the filter is the canonical definition and members
        # are re-evaluated on every read.
        snapshot_ids: list[str] | None = None
        snapshot_at = None
        if kind == QueryGroupKind.STATIC:
            where, params = _build_filter_from_spec(spec)
            where_sql = f"WHERE {where}" if where else ""
            rows = get_connection().execute(
                f"""
                SELECT q.query_id
                FROM raw.query_history q
                LEFT JOIN features.query_sql_features f USING (query_id)
                {where_sql}
                """,
                params,
            ).fetchall()
            snapshot_ids = [r[0] for r in rows]
            snapshot_at = naive_utcnow()

        new_id = store.insert(
            name=req.name, description=req.description, kind=kind,
            filter_spec=spec, snapshot_query_ids=snapshot_ids,
            snapshot_at=snapshot_at,
        )
        # Re-fetch + decorate with member_count.
        group = store.get(new_id)
        if group is None:
            raise HTTPException(500, "insert succeeded but read-back failed")
        group.member_count = _group_member_count(group)
        return group

    @app.get("/query-groups", response_model=list[QueryGroup])
    def list_query_groups(
        kind: str | None = Query(None, description="Filter to 'static' or 'dynamic'"),
        limit: int = Query(200, ge=1, le=500),
        store: QueryGroupStore = Depends(_group_store),
    ) -> list[QueryGroup]:
        kind_enum: QueryGroupKind | None = None
        if kind is not None:
            try:
                kind_enum = QueryGroupKind(kind)
            except ValueError:
                raise HTTPException(400, f"unknown kind {kind!r}")
        groups = store.list(kind=kind_enum, limit=limit)
        for g in groups:
            g.member_count = _group_member_count(g)
        return groups

    @app.get("/query-groups/{group_id}", response_model=QueryGroup)
    def get_query_group(
        group_id: int,
        store: QueryGroupStore = Depends(_group_store),
    ) -> QueryGroup:
        group = store.get(group_id)
        if group is None:
            raise HTTPException(404, f"query group {group_id} not found")
        group.member_count = _group_member_count(group)
        return group

    @app.delete("/query-groups/{group_id}")
    def delete_query_group(
        group_id: int,
        store: QueryGroupStore = Depends(_group_store),
    ) -> dict[str, str]:
        if not store.delete(group_id):
            raise HTTPException(404, f"query group {group_id} not found")
        return {"status": "deleted", "id": str(group_id)}

    @app.get("/query-groups/{group_id}/members", response_model=QueryListResponse)
    def query_group_members(
        group_id: int,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        store: QueryGroupStore = Depends(_group_store),
    ) -> QueryListResponse:
        group = store.get(group_id)
        if group is None:
            raise HTTPException(404, f"query group {group_id} not found")

        conn = get_connection()
        select_cols = (
            "q.query_id, q.query_text, q.query_type, q.execution_status, "
            "q.user_name, q.role_name, q.warehouse_name, q.warehouse_size, "
            "q.start_time, q.total_elapsed_ms, q.bytes_scanned, "
            "q.bytes_spilled_to_local, q.bytes_spilled_to_remote, "
            "q.queued_overload_ms, q.query_parameterized_hash, "
            "f.joins_count, f.tables_referenced_count, f.ctes_count, "
            "f.subqueries_count, f.where_block_count, f.where_predicate_count"
        )
        join_clause = (
            "FROM raw.query_history q "
            "LEFT JOIN features.query_sql_features f USING (query_id)"
        )
        # Static: members = the frozen snapshot; paginate against that list.
        if group.kind == QueryGroupKind.STATIC:
            ids = group.snapshot_query_ids or []
            total = len(ids)
            slice_ids = ids[offset : offset + limit]
            if not slice_ids:
                return QueryListResponse(rows=[], total=total, limit=limit, offset=offset)
            placeholders = ", ".join(["?"] * len(slice_ids))
            rows = conn.execute(
                f"""
                SELECT {select_cols}
                {join_clause}
                WHERE q.query_id IN ({placeholders})
                ORDER BY q.start_time DESC NULLS LAST
                """,
                slice_ids,
            ).fetchall()
        else:
            # Dynamic: apply the filter spec live.
            where, params = _build_filter_from_spec(group.filter_spec)
            where_sql = f"WHERE {where}" if where else ""
            total = int(conn.execute(
                f"SELECT COUNT(*) {join_clause} {where_sql}", params,
            ).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT {select_cols}
                {join_clause}
                {where_sql}
                ORDER BY q.start_time DESC NULLS LAST
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            ).fetchall()

        return QueryListResponse(
            rows=[QueryRow(
                query_id=r[0],
                query_text_preview=_truncate(r[1], 200),
                query_type=r[2], execution_status=r[3],
                user_name=r[4], role_name=r[5],
                warehouse_name=r[6], warehouse_size=r[7],
                start_time=r[8], total_elapsed_ms=r[9],
                bytes_scanned=r[10],
                bytes_spilled_to_local=r[11], bytes_spilled_to_remote=r[12],
                queued_overload_ms=r[13],
                query_parameterized_hash=r[14],
                joins_count=r[15], tables_referenced_count=r[16],
                ctes_count=r[17], subqueries_count=r[18],
                where_block_count=r[19], where_predicate_count=r[20],
            ) for r in rows],
            total=total, limit=limit, offset=offset,
        )

    # ── Self-documentation (Docs tab) ─────────────────────────────
    @app.get("/cli-help", response_model=CliCommand)
    def cli_help() -> CliCommand:
        """Introspect the snowtuner CLI and return a structured tree of commands.

        Used by the web UI's Docs tab to render an auto-generated CLI reference.
        The tree mirrors what `snowtuner --help` shows in the terminal, but
        recursive — each group's subcommands are included inline.
        """
        from snowtuner.cli import cli as snowtuner_cli
        return _introspect_click_command(snowtuner_cli, "snowtuner", ["snowtuner"])

    @app.get("/mcp-tools", response_model=list[McpToolInfo])
    def mcp_tools_list() -> list[McpToolInfo]:
        """List MCP tools registered on the admin server with descriptions and
        JSON-schema parameter specs.  Used by the web UI's Docs tab."""
        from snowtuner.mcp.admin import mcp as admin_mcp
        tools = admin_mcp._tool_manager.list_tools()  # noqa: SLF001
        out: list[McpToolInfo] = []
        for t in tools:
            params = getattr(t, "parameters", None)
            if params is None:
                # Some FastMCP versions stash the schema as ``inputSchema``.
                params = getattr(t, "inputSchema", None)
            out.append(McpToolInfo(
                name=t.name,
                description=(getattr(t, "description", "") or "").strip(),
                parameters=params,
            ))
        return out

    # ── Dev helpers ───────────────────────────────────────────────
    @app.post("/seed")
    def seed(req: SeedRequest = SeedRequest()) -> dict[str, int]:
        return seed_demo_data(get_connection(), days=req.days, seed=req.seed)

    return app


# ── Experiments: shared helpers ─────────────────────────────────────

def _load_warehouse_config(warehouse_name: str) -> WarehouseConfig | None:
    """Load a control warehouse's config from raw.warehouses.

    Returns None if the warehouse isn't synced.  Mirrors the engine's
    ``_load_control_config`` so propose-time and run-time see the same view.
    """
    row = get_connection().execute(
        """
        SELECT name, size, auto_suspend_seconds, auto_resume
        FROM raw.warehouses
        WHERE upper(name) = upper(?)
        """,
        [warehouse_name],
    ).fetchone()
    if not row:
        return None
    return WarehouseConfig(
        name=row[0], size=row[1],
        auto_suspend_seconds=row[2],
        auto_resume=bool(row[3]) if row[3] is not None else None,
        generation=None, qas_state=None,
    )


# ``_sample_query_stats`` was removed in Phase 3 — the workload resolver
# now produces both the replay list and the cost-estimate stats from a single
# canonical pass.  Recipe code paths take ``sample_query_stats`` directly
# from ``[s.historical for s in resolved.sampled]``.


def _resolve_query_group(group_id: int) -> "QueryGroup":
    """Load a saved query group by id; 404 if missing.

    Used by the propose endpoints to seed the workload resolver when the
    user picks a saved group instead of warehouse auto-sampling.
    """
    from snowtuner.query_groups import QueryGroupStore
    g = QueryGroupStore(get_connection()).get(group_id)
    if g is None:
        raise HTTPException(404, f"query group {group_id} not found")
    return g


def _recipe_default_sample_size() -> int:
    """The sample size to ask the workload resolver for when proposing a
    recipe-based (tuning) experiment.

    Recipes don't currently accept a sample_size knob; they default to 30.
    We mirror that default at the resolver level so the warning surfaces if
    the pool can't produce that many eligible queries.
    """
    from snowtuner.experiments.recipes import _DEFAULT_SAMPLE_SIZE
    return _DEFAULT_SAMPLE_SIZE


def _build_query_filter(
    *,
    warehouse: str | None,
    user: str | None,
    role: str | None = None,
    query_type: str | None,
    status: str | None,
    parameterized_hash: str | None,
    start_from: datetime | None,
    start_to: datetime | None,
    min_elapsed_ms: int | None,
    max_elapsed_ms: int | None,
    has_remote_spill: bool | None,
    has_local_spill: bool | None,
    has_queueing: bool | None,
    search: str | None,
    min_joins: int | None = None,
    max_joins: int | None = None,
    min_tables: int | None = None,
    max_tables: int | None = None,
    min_ctes: int | None = None,
    max_ctes: int | None = None,
    min_subqueries: int | None = None,
    max_subqueries: int | None = None,
    min_where_blocks: int | None = None,
    max_where_blocks: int | None = None,
    min_where_predicates: int | None = None,
    max_where_predicates: int | None = None,
    referenced_tables_include: str | None = None,
    referenced_tables_exclude: str | None = None,
    where_columns_include: str | None = None,
    where_columns_exclude: str | None = None,
) -> tuple[str, list[Any]]:
    """Build a WHERE-clause body + bind params from URL-style filter args.

    Multi-value filters accept comma-separated strings ("WH_A,WH_B").
    Converts to a ``QueryFilterSpec`` and delegates to
    ``_build_filter_from_spec`` so both URL filtering and group-spec
    filtering go through one codepath.
    """
    def _split(raw: str | None) -> list[str] | None:
        if not raw:
            return None
        values = [v.strip() for v in raw.split(",") if v.strip()]
        return values or None

    spec = QueryFilterSpec(
        warehouse_name=_split(warehouse),
        user_name=_split(user),
        role_name=_split(role),
        query_type=_split(query_type),
        execution_status=_split(status),
        query_parameterized_hash=[parameterized_hash] if parameterized_hash else None,
        start_time_from=start_from,
        start_time_to=start_to,
        min_elapsed_ms=min_elapsed_ms,
        max_elapsed_ms=max_elapsed_ms,
        has_remote_spill=has_remote_spill,
        has_local_spill=has_local_spill,
        has_queueing=has_queueing,
        search=search,
        min_joins=min_joins, max_joins=max_joins,
        min_tables=min_tables, max_tables=max_tables,
        min_ctes=min_ctes, max_ctes=max_ctes,
        min_subqueries=min_subqueries, max_subqueries=max_subqueries,
        min_where_blocks=min_where_blocks, max_where_blocks=max_where_blocks,
        min_where_predicates=min_where_predicates,
        max_where_predicates=max_where_predicates,
        referenced_tables_include=_split(referenced_tables_include),
        referenced_tables_exclude=_split(referenced_tables_exclude),
        where_columns_include=_split(where_columns_include),
        where_columns_exclude=_split(where_columns_exclude),
    )
    return _build_filter_from_spec(spec)


def _truncate(text: str | None, max_len: int) -> str:
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _filter_spec_from_create_req(req: CreateQueryGroupRequest) -> QueryFilterSpec:
    """Normalize ``CreateQueryGroupRequest``'s lax field types into a
    canonical ``QueryFilterSpec``.

    The request accepts ``list[str] | str | None`` for IN-filters to match
    the URL-filter convention on ``/queries`` (comma-separated string).  We
    split strings into lists here so the spec model has a single shape.
    """
    def _to_list(v) -> list[str] | None:
        if v is None:
            return None
        if isinstance(v, list):
            return [s.strip() for s in v if s and s.strip()] or None
        if isinstance(v, str):
            parts = [p.strip() for p in v.split(",") if p.strip()]
            return parts or None
        return None

    return QueryFilterSpec(
        warehouse_name=_to_list(req.warehouse_name),
        user_name=_to_list(req.user_name),
        role_name=_to_list(req.role_name),
        query_type=_to_list(req.query_type),
        execution_status=_to_list(req.execution_status),
        query_parameterized_hash=_to_list(req.query_parameterized_hash),
        start_time_from=req.start_time_from,
        start_time_to=req.start_time_to,
        min_elapsed_ms=req.min_elapsed_ms,
        max_elapsed_ms=req.max_elapsed_ms,
        has_remote_spill=req.has_remote_spill,
        has_local_spill=req.has_local_spill,
        has_queueing=req.has_queueing,
        search=req.search,
        min_joins=req.min_joins, max_joins=req.max_joins,
        min_tables=req.min_tables, max_tables=req.max_tables,
        min_ctes=req.min_ctes, max_ctes=req.max_ctes,
        min_subqueries=req.min_subqueries, max_subqueries=req.max_subqueries,
        min_where_blocks=req.min_where_blocks, max_where_blocks=req.max_where_blocks,
        min_where_predicates=req.min_where_predicates,
        max_where_predicates=req.max_where_predicates,
        referenced_tables_include=_to_list(req.referenced_tables_include),
        referenced_tables_exclude=_to_list(req.referenced_tables_exclude),
        where_columns_include=_to_list(req.where_columns_include),
        where_columns_exclude=_to_list(req.where_columns_exclude),
    )


# ``_build_filter_from_spec`` moved to ``snowtuner.query_groups.sql`` so
# non-API callers (notably the experiments workload resolver) can share it
# without importing FastAPI.  Re-exported here for backwards compatibility
# within this module.
from snowtuner.query_groups.sql import build_filter_from_spec as _build_filter_from_spec  # noqa: E402


def _group_member_count(group: QueryGroup) -> int:
    """Compute the current member count for a group.

    Static: just the snapshot length.  Dynamic: live ``COUNT(*)`` against the
    filter spec.  Used by the API endpoint to decorate ``QueryGroup`` responses;
    not stored on the row because for dynamic groups it'd be stale immediately.
    """
    if group.kind == QueryGroupKind.STATIC:
        return len(group.snapshot_query_ids or [])
    where, params = _build_filter_from_spec(group.filter_spec)
    where_sql = f"WHERE {where}" if where else ""
    row = get_connection().execute(
        f"""
        SELECT COUNT(*)
        FROM raw.query_history q
        LEFT JOIN features.query_sql_features f USING (query_id)
        {where_sql}
        """,
        params,
    ).fetchone()
    return int(row[0]) if row else 0


def _account_info() -> AccountInfo:
    """Resolve the AccountInfo used by recipe eligibility checks.

    v0.2 first cut: most-permissive defaults so every recipe can propose;
    the per-arm eligibility check is the actual gate.  A future revision
    will cache region/edition from a Snowflake ``CURRENT_REGION()`` query
    on first sync.
    """
    return AccountInfo(
        region="AWS_US_WEST_2",
        edition="ENTERPRISE",
        gen2_supported_in_region=True,
        qas_available=True,
    )


def _introspect_click_command(
    cmd: Any, name: str, path: list[str],
) -> CliCommand:
    """Walk a Click ``Command`` / ``Group`` and turn it into a serializable
    ``CliCommand`` tree.  Used by ``GET /cli-help`` so the web UI can render
    auto-generated CLI docs without re-parsing terminal output.
    """
    import click

    is_group = isinstance(cmd, click.Group)
    params: list[CliParam] = []
    for p in cmd.params:
        if isinstance(p, click.Option):
            kind = "option"
            choices: list[str] | None = None
            if isinstance(p.type, click.Choice):
                choices = list(p.type.choices)
            default = None
            if p.default is not None and p.default is not False:
                default = str(p.default)
            type_name = (
                "CHOICE" if choices else getattr(p.type, "name", str(p.type)).upper()
            )
            params.append(CliParam(
                name=(p.opts[0] if p.opts else p.name) or "",
                kind=kind,
                type=type_name,
                help=p.help or "",
                required=bool(p.required),
                is_flag=bool(getattr(p, "is_flag", False)),
                default=default,
                choices=choices,
                multiple=bool(getattr(p, "multiple", False)),
            ))
        elif isinstance(p, click.Argument):
            type_name = getattr(p.type, "name", str(p.type)).upper()
            params.append(CliParam(
                name=p.name or "",
                kind="argument",
                type=type_name,
                help="",
                required=bool(p.required),
                is_flag=False,
                default=None,
                choices=None,
                multiple=bool(getattr(p, "nargs", 1) != 1),
            ))

    subcommands: list[CliCommand] = []
    if is_group:
        for subname in sorted(cmd.commands.keys()):
            subcmd = cmd.commands[subname]
            subcommands.append(
                _introspect_click_command(subcmd, subname, path + [subname]),
            )

    return CliCommand(
        name=name,
        path=path,
        help=(cmd.help or cmd.__doc__ or "").strip(),
        short_help=(cmd.short_help or "").strip(),
        is_group=is_group,
        params=params,
        subcommands=subcommands,
    )
