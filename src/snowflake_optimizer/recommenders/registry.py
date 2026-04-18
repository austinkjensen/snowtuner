"""Explicit, config-driven registration of recommenders."""
from __future__ import annotations

from snowflake_optimizer.recommenders.base import Recommender


class RecommenderRegistry:
    def __init__(self) -> None:
        self._recommenders: dict[str, Recommender] = {}

    def register(self, recommender: Recommender) -> None:
        if recommender.name in self._recommenders:
            raise ValueError(f"Recommender {recommender.name!r} already registered")
        self._recommenders[recommender.name] = recommender

    def unregister(self, name: str) -> None:
        self._recommenders.pop(name, None)

    def get(self, name: str) -> Recommender | None:
        return self._recommenders.get(name)

    def all(self) -> list[Recommender]:
        return list(self._recommenders.values())


def default_registry() -> RecommenderRegistry:
    """Build a registry populated with the built-in recommenders."""
    from snowflake_optimizer.recommenders.builtins.auto_suspend_tuner import AutoSuspendTuner

    reg = RecommenderRegistry()
    reg.register(AutoSuspendTuner())
    return reg
