"""Top-level orchestrator: sync → features → per-recommender fit/predict → persist.

The orchestrator is the only place that talks to Sources, FeaturePipeline, and
Recommenders collectively.  Everything below it is independently testable.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import duckdb

from snowflake_optimizer.features.base import FeaturePipeline, TransformResult
from snowflake_optimizer.ingestion.base import Source, SnowflakeClient, SyncResult
from snowflake_optimizer.ingestion.sync import sync_all
from snowflake_optimizer.recommendations.store import RecommendationStore
from snowflake_optimizer.recommenders.base import Recommender
from snowflake_optimizer.recommenders.registry import RecommenderRegistry
from snowflake_optimizer.recommenders.training_state import TrainingStateStore


@dataclass
class RecommenderRunReport:
    name: str
    is_ready: bool
    readiness_reason: str
    fit_completed: bool
    predictions_emitted: int = 0
    error: str | None = None


@dataclass
class RunReport:
    sync_results: list[SyncResult] = field(default_factory=list)
    feature_results: list[TransformResult] = field(default_factory=list)
    recommender_results: list[RecommenderRunReport] = field(default_factory=list)


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
        self.training_store = TrainingStateStore(conn)

    def run(
        self,
        *,
        client: SnowflakeClient | None = None,
        skip_sync: bool = False,
    ) -> RunReport:
        report = RunReport()

        if not skip_sync and client is not None and self.sources:
            report.sync_results = sync_all(self.sources, client, self.conn)

        if self.pipeline is not None:
            report.feature_results = self.pipeline.run(self.conn)

        if self.registry is not None:
            for recommender in self.registry.all():
                report.recommender_results.append(self._run_recommender(recommender))

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
            self.training_store.upsert(rec.name, predict_now=True)
        except Exception as e:
            result.error = repr(e)
        return result
