"""Top-level orchestrator: sync → features → per-recommender fit/predict → persist.

The orchestrator is the only place that talks to Sources, FeaturePipeline, and
Recommenders collectively.  Everything below it is independently testable.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import duckdb

from snowtuner.autonomous import AutonomousRunner, AutonomousRunReport
from snowtuner.autonomous.config import AutonomousConfigStore
from snowtuner.experiments import ExperimentStore
from snowtuner.features.base import FeaturePipeline, TransformResult
from snowtuner.ingestion.base import Source, SnowflakeClient, SyncResult
from snowtuner.ingestion.sync import SyncError, sync_all
from snowtuner.recommendations.store import RecommendationStore
from snowtuner.recommenders.base import Recommender
from snowtuner.recommenders.registry import RecommenderRegistry
from snowtuner.recommenders.training_state import TrainingStateStore


@dataclass
class RecommenderRunReport:
    name: str
    is_ready: bool
    readiness_reason: str
    fit_completed: bool
    predictions_emitted: int = 0
    experiments_proposed: int = 0
    error: str | None = None


@dataclass
class RunReport:
    sync_results: list[SyncResult] = field(default_factory=list)
    sync_errors: list[SyncError] = field(default_factory=list)
    feature_results: list[TransformResult] = field(default_factory=list)
    recommender_results: list[RecommenderRunReport] = field(default_factory=list)
    autonomous_report: AutonomousRunReport | None = None
    autonomous_skipped_reason: str | None = None


class Orchestrator:
    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        sources: Iterable[Source] | None = None,
        pipeline: FeaturePipeline | None = None,
        registry: RecommenderRegistry | None = None,
    ):
        self.conn = conn
        self.sources = list(sources) if sources is not None else None
        self.pipeline = pipeline
        self.registry = registry
        self.rec_store = RecommendationStore(conn)
        self.experiment_store = ExperimentStore(conn)
        self.training_store = TrainingStateStore(conn)

    def run(
        self,
        *,
        client: SnowflakeClient | None = None,
        skip_sync: bool = False,
        initial_lookback_days: int | None = None,
    ) -> RunReport:
        report = RunReport()

        if not skip_sync and client is not None and self.sources:
            report.sync_results, report.sync_errors = sync_all(
                self.sources, client, self.conn,
                initial_lookback_days=initial_lookback_days,
            )

        if self.pipeline is not None:
            report.feature_results = self.pipeline.run(self.conn)

        if self.registry is not None:
            for recommender in self.registry.all():
                report.recommender_results.append(self._run_recommender(recommender))

        # Autonomous-apply pass — only runs if (a) at least one config row is
        # enabled and (b) we have a credentialed client to talk to Snowflake.
        # Either condition missing is normal and not an error.
        config_store = AutonomousConfigStore(self.conn)
        any_enabled = any(c.enabled for c in config_store.list())
        if not any_enabled:
            report.autonomous_skipped_reason = "no enabled autonomous config rows"
        elif client is None:
            report.autonomous_skipped_reason = (
                "autonomous configured but no Snowflake client provided "
                "(pass --auto/--no-auto explicitly)"
            )
        else:
            runner = AutonomousRunner(self.conn, client)
            report.autonomous_report = runner.run()

        return report

    def _run_recommender(self, rec: Recommender) -> RecommenderRunReport:
        result = RecommenderRunReport(
            name=rec.name,
            is_ready=False,
            readiness_reason="",
            fit_completed=False,
        )
        try:
            readiness = rec.training_gate.evaluate(self.conn)
            result.is_ready = readiness.is_ready
            result.readiness_reason = readiness.reason

            # Always fit while training; once ready, re-fit periodically anyway.
            model_state = rec.fit(self.conn)
            result.fit_completed = True
            self.training_store.upsert(
                rec.name,
                is_ready=readiness.is_ready,
                readiness_report=readiness.model_dump(mode="json"),
                model_state=model_state,
                fit_now=True,
            )

            if not readiness.is_ready:
                return result

            # Supersede any stale proposals from this recommender before re-emitting.
            self.rec_store.supersede_all_from(rec.generated_by, rec.action_type.value)

            predictions = rec.predict(self.conn, model_state)
            for pred in predictions:
                rec_id = self.rec_store.insert(pred)
                self.rec_store.supersede_overlapping(
                    target_resource=pred.action.target_resource() or "",
                    action_type=pred.action.type.value,
                    except_id=rec_id,
                )
            result.predictions_emitted = len(predictions)

            # Optional: propose experiments alongside (or instead of)
            # advisory recommendations.  Default implementation returns [],
            # so recommenders that don't opt in see zero overhead.
            proposed = rec.propose_experiments(self.conn, model_state)
            for prop in proposed:
                self.experiment_store.insert(prop)
            result.experiments_proposed = len(proposed)

            self.training_store.upsert(rec.name, predict_now=True)
        except Exception as e:
            result.error = repr(e)
        return result
