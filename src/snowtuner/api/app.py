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

import threading
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query

from snowtuner.api.schemas import (
    AbortExperimentRequest,
    AutonomousApplicationOut,
    AutonomousConfigOut,
    AutonomousConfigUpsert,
    BenchmarkArmSpec,
    CredentialStatusOut,
    CredentialVerifyOut,
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
from snowtuner.experiments.cost_estimate import QueryStats
from snowtuner.experiments.eligibility import AccountInfo
from snowtuner.experiments.recipes import PRESET_RECIPES
from snowtuner.ingestion.snowflake_client import SnowflakeClient
from snowtuner.features import DEFAULT_TRANSFORMS
from snowtuner.features.base import FeaturePipeline
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


# ---- Dependencies (per-request so the test harness can override) ----

def _get_store() -> RecommendationStore:
    return RecommendationStore(get_connection())


def _get_registry() -> RecommenderRegistry:
    return default_registry()


def create_app() -> FastAPI:
    app = FastAPI(
        title="snowtuner",
        description="Locally-hosted Snowflake cost & performance advisor.",
        version="0.1.0",
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
        orch = Orchestrator(
            get_connection(),
            pipeline=FeaturePipeline(DEFAULT_TRANSFORMS),
            registry=reg,
        )
        report = orch.run(skip_sync=req.skip_sync)
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
        orch = Orchestrator(
            get_connection(),
            pipeline=FeaturePipeline(DEFAULT_TRANSFORMS),
            registry=solo,
        )
        report = orch.run(skip_sync=req.skip_sync)
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
                queries_in_window=int(r[4] or 0),
                suspend_resume_events=int(r[5] or 0),
            )
            for r in rows
        ]

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
        proposed = recipe(
            warehouse,
            _account_info(),
            sample_query_stats=_sample_query_stats(req.target_warehouse),
        )
        if proposed is None:
            raise HTTPException(
                422,
                f"recipe {req.recipe_name!r} is not eligible for "
                f"warehouse {req.target_warehouse!r}",
            )
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
        # Validate the workload warehouse exists in our local raw.warehouses.
        workload_wh = _load_warehouse_config(req.workload_warehouse)
        if workload_wh is None:
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
            CostEstimate,
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
        # arms don't inherit from it).
        account = _account_info()
        for arm in built_arms:
            arm.eligibility_issues = check_arm_eligibility(arm, workload_wh, account)

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

        # Cost estimate.  Each arm's credit rate comes from its own size
        # (or XSMALL fallback if unset).
        sample_stats = _sample_query_stats(req.workload_warehouse, limit=req.sample_size)
        arm_rates = {
            a.name: credit_rate(a.delta.size or "XSMALL") for a in runnable_arms
        }
        if sample_stats:
            cost_estimate = estimate_experiment_cost(
                sample_query_stats=sample_stats,
                arm_credit_rates_per_hour=arm_rates,
                reps_per_arm=req.reps_per_arm,
            )
        else:
            cost_estimate = CostEstimate(
                low_credits=0.0, high_credits=0.0,
                rationale="cost not estimated (no historical query stats for this workload)",
            )

        warning_issues = [
            i for a in runnable_arms for i in a.eligibility_issues
            if i.severity == "warning"
        ]

        proposed = ProposedExperiment(
            kind=ExperimentKind.BENCHMARK,
            recipe_name="user_built_benchmark",
            target_warehouse=None,
            workload_warehouse=req.workload_warehouse.upper(),
            control_arm_name=req.control_arm_name,
            hypothesis=req.hypothesis,
            arms=runnable_arms,
            sample_size=req.sample_size,
            reps_per_arm=req.reps_per_arm,
            cost_estimate=cost_estimate,
            eligibility_issues=warning_issues,
            proposed_by="user",
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
        query_type: str | None = Query(None, description="Comma-separated query types"),
        status: str | None = Query(None, description="Comma-separated execution statuses"),
        parameterized_hash: str | None = Query(None, description="Filter to one family"),
        start_from: datetime | None = Query(None, description="start_time >= this"),
        start_to: datetime | None = Query(None, description="start_time <= this"),
        min_elapsed_ms: int | None = Query(None, ge=0),
        max_elapsed_ms: int | None = Query(None, ge=0),
        has_remote_spill: bool | None = Query(None),
        has_local_spill: bool | None = Query(None),
        has_queueing: bool | None = Query(None),
        search: str | None = Query(None, description="Substring search over query text (case-insensitive)"),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> QueryListResponse:
        where, params = _build_query_filter(
            warehouse=warehouse, user=user, query_type=query_type, status=status,
            parameterized_hash=parameterized_hash,
            start_from=start_from, start_to=start_to,
            min_elapsed_ms=min_elapsed_ms, max_elapsed_ms=max_elapsed_ms,
            has_remote_spill=has_remote_spill, has_local_spill=has_local_spill,
            has_queueing=has_queueing, search=search,
        )
        where_sql = f"WHERE {where}" if where else ""
        conn = get_connection()
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM raw.query_history {where_sql}", params,
        ).fetchone()[0])
        rows = conn.execute(
            f"""
            SELECT query_id, query_text, query_type, execution_status,
                   user_name, role_name, warehouse_name, warehouse_size,
                   start_time, total_elapsed_ms, bytes_scanned,
                   bytes_spilled_to_local, bytes_spilled_to_remote,
                   queued_overload_ms, query_parameterized_hash
            FROM raw.query_history
            {where_sql}
            ORDER BY start_time DESC NULLS LAST
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
            ) for r in rows],
            total=total, limit=limit, offset=offset,
        )

    @app.get("/queries/facets", response_model=QueryFilterFacets)
    def query_facets(
        lookback_days: int = Query(30, ge=1, le=365),
    ) -> QueryFilterFacets:
        """Distinct filter values from the last N days of query history.

        Scoped to a window so we don't surface long-departed users / decommissioned
        warehouses in the filter chips.
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

        return QueryFilterFacets(
            warehouses=_distinct("warehouse_name"),
            users=_distinct("user_name"),
            query_types=_distinct("query_type"),
            execution_statuses=_distinct("execution_status"),
        )

    @app.get("/queries/{query_id}", response_model=QueryDetail)
    def get_query(query_id: str) -> QueryDetail:
        row = get_connection().execute(
            """
            SELECT query_id, query_text, query_type, execution_status,
                   user_name, role_name, warehouse_name, warehouse_size,
                   database_name, schema_name, start_time, end_time,
                   total_elapsed_ms, compilation_ms, execution_ms,
                   queued_overload_ms, queued_provisioning_ms,
                   bytes_scanned, bytes_spilled_to_local, bytes_spilled_to_remote,
                   query_parameterized_hash
            FROM raw.query_history
            WHERE query_id = ?
            """,
            [query_id],
        ).fetchone()
        if not row:
            raise HTTPException(404, f"query {query_id!r} not found")
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
        where = f"({where} AND query_parameterized_hash IS NOT NULL)" if where \
            else "query_parameterized_hash IS NOT NULL"

        rows = get_connection().execute(
            f"""
            WITH filtered AS (
                SELECT * FROM raw.query_history WHERE {where}
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
                f.query_parameterized_hash,
                reps.query_id,
                reps.query_text,
                COUNT(*) AS occurrence_count,
                AVG(f.total_elapsed_ms) AS mean_elapsed_ms,
                quantile_cont(f.total_elapsed_ms, 0.95) AS p95_elapsed_ms,
                SUM(f.total_elapsed_ms) AS total_elapsed_ms,
                SUM(f.bytes_scanned) AS total_bytes_scanned,
                SUM(CASE WHEN f.bytes_spilled_to_remote > 0 THEN 1 ELSE 0 END) AS n_spill_remote,
                SUM(CASE WHEN f.execution_status <> 'SUCCESS' THEN 1 ELSE 0 END) AS n_failed,
                MIN(f.start_time) AS first_seen,
                MAX(f.start_time) AS last_seen,
                COUNT(DISTINCT f.warehouse_name) AS distinct_warehouses,
                COUNT(DISTINCT f.user_name) AS distinct_users
            FROM filtered f
            JOIN reps USING (query_parameterized_hash)
            GROUP BY f.query_parameterized_hash, reps.query_id, reps.query_text
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


def _sample_query_stats(warehouse_name: str, limit: int = 50) -> list[QueryStats]:
    """Pull recent query stats for cost-estimate budgeting.

    The full sampler runs at engine start (richer logic, filters); this
    quick lookup is just for the recipe's cost estimator to size the
    experiment's credit budget.
    """
    rows = get_connection().execute(
        """
        SELECT query_id, total_elapsed_ms, total_elapsed_ms, bytes_scanned
        FROM raw.query_history
        WHERE upper(warehouse_name) = upper(?)
          AND execution_status = 'SUCCESS'
          AND query_type = 'SELECT'
          AND query_parameterized_hash IS NOT NULL
        ORDER BY start_time DESC
        LIMIT ?
        """,
        [warehouse_name, limit],
    ).fetchall()
    return [
        QueryStats(
            query_id=r[0],
            p50_elapsed_ms=float(r[1] or 0),
            mean_elapsed_ms=float(r[2] or 0),
            bytes_scanned=int(r[3]) if r[3] is not None else None,
        )
        for r in rows
    ]


def _build_query_filter(
    *,
    warehouse: str | None,
    user: str | None,
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
) -> tuple[str, list[Any]]:
    """Build a WHERE-clause body (without 'WHERE') + bind params from filter args.

    Multi-value filters accept comma-separated strings ("WH_A,WH_B") and bind
    each value separately so DuckDB can use indexes.  Returns the empty string
    if no filters are active.
    """
    clauses: list[str] = []
    params: list[Any] = []

    def _in_clause(col: str, raw: str | None) -> None:
        if not raw:
            return
        values = [v.strip() for v in raw.split(",") if v.strip()]
        if not values:
            return
        placeholders = ", ".join(["?"] * len(values))
        clauses.append(f"{col} IN ({placeholders})")
        params.extend(values)

    _in_clause("warehouse_name", warehouse)
    _in_clause("user_name", user)
    _in_clause("query_type", query_type)
    _in_clause("execution_status", status)

    if parameterized_hash:
        clauses.append("query_parameterized_hash = ?")
        params.append(parameterized_hash)
    if start_from:
        clauses.append("start_time >= ?")
        params.append(start_from)
    if start_to:
        clauses.append("start_time <= ?")
        params.append(start_to)
    if min_elapsed_ms is not None:
        clauses.append("total_elapsed_ms >= ?")
        params.append(min_elapsed_ms)
    if max_elapsed_ms is not None:
        clauses.append("total_elapsed_ms <= ?")
        params.append(max_elapsed_ms)
    if has_remote_spill is True:
        clauses.append("bytes_spilled_to_remote > 0")
    elif has_remote_spill is False:
        clauses.append("(bytes_spilled_to_remote IS NULL OR bytes_spilled_to_remote = 0)")
    if has_local_spill is True:
        clauses.append("bytes_spilled_to_local > 0")
    elif has_local_spill is False:
        clauses.append("(bytes_spilled_to_local IS NULL OR bytes_spilled_to_local = 0)")
    if has_queueing is True:
        clauses.append("queued_overload_ms > 0")
    elif has_queueing is False:
        clauses.append("(queued_overload_ms IS NULL OR queued_overload_ms = 0)")
    if search:
        clauses.append("lower(query_text) LIKE ?")
        params.append(f"%{search.lower()}%")

    return " AND ".join(clauses), params


def _truncate(text: str | None, max_len: int) -> str:
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


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
