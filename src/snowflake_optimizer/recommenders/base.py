"""Recommender protocol + training gate.

A Recommender is a strategy: given feature tables, emit Recommendations of
a particular action type.  Multiple recommenders may target the same action
type (later: ensemble/voting); the orchestrator dedupes by target_resource.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import duckdb
from pydantic import BaseModel

from snowflake_optimizer.actions.base import ActionType
from snowflake_optimizer.recommendations.model import Recommendation


class ReadinessReport(BaseModel):
    is_ready: bool
    reason: str
    signals: dict[str, Any] = {}


class TrainingGate(ABC):
    """Decides whether a recommender has seen enough data to start predicting."""

    @abstractmethod
    def evaluate(self, conn: duckdb.DuckDBPyConnection) -> ReadinessReport:
        ...


class AlwaysReadyGate(TrainingGate):
    """Useful for recommenders that need no training period."""
    def evaluate(self, conn: duckdb.DuckDBPyConnection) -> ReadinessReport:
        return ReadinessReport(is_ready=True, reason="no training required")


class Recommender(ABC):
    """Base class for all recommenders."""

    name: str
    version: str = "0.1.0"
    action_type: ActionType
    required_feature_tables: set[str] = set()
    training_gate: TrainingGate

    @abstractmethod
    def fit(self, conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        """Update model state.  Returns a JSON-serializable state dict that the
        orchestrator will persist to app.training_state."""

    @abstractmethod
    def predict(
        self,
        conn: duckdb.DuckDBPyConnection,
        model_state: dict[str, Any] | None,
    ) -> list[Recommendation]:
        """Produce recommendations given the current feature tables + model state."""

    @property
    def generated_by(self) -> str:
        return f"{self.name}@{self.version}"
