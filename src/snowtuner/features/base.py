"""FeatureTransform: one composable step in the feature-enrichment DAG.

Each transform declares the DuckDB tables/views it reads and writes.
A FeaturePipeline topologically sorts transforms by their I/O contract
and runs them in order.  Developers plug in their own transforms by
implementing this interface and registering them in the pipeline.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass

import duckdb


class FeatureTransform(ABC):
    name: str
    inputs: set[str]   # fully-qualified DuckDB tables this reads
    outputs: set[str]  # fully-qualified DuckDB tables this writes/replaces

    @abstractmethod
    def run(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Execute this transform against the given connection."""


@dataclass
class TransformResult:
    name: str
    duration_seconds: float
    rows_written: int | None = None


class FeaturePipeline:
    """Topologically orders transforms by their input/output contracts."""

    def __init__(self, transforms: Iterable[FeatureTransform]):
        self.transforms = list(transforms)
        self._validate_no_duplicate_outputs()

    def _validate_no_duplicate_outputs(self) -> None:
        seen: dict[str, str] = {}
        for t in self.transforms:
            for out in t.outputs:
                if out in seen:
                    raise ValueError(
                        f"Transforms {seen[out]!r} and {t.name!r} both write to {out!r}"
                    )
                seen[out] = t.name

    def ordered(self) -> list[FeatureTransform]:
        """Kahn's algorithm: a transform may run once all its upstream writers have run."""
        producers: dict[str, str] = {}  # table -> transform name
        for t in self.transforms:
            for o in t.outputs:
                producers[o] = t.name

        by_name = {t.name: t for t in self.transforms}
        pending = set(by_name)
        ordered: list[FeatureTransform] = []

        while pending:
            ready = [
                n for n in pending
                if not any(
                    inp in producers and producers[inp] != n and producers[inp] in pending
                    for inp in by_name[n].inputs
                )
            ]
            if not ready:
                raise ValueError(f"Feature pipeline has a cycle involving {pending!r}")
            # Stable order by declaration sequence.
            ready.sort(key=lambda n: next(i for i, t in enumerate(self.transforms) if t.name == n))
            for n in ready:
                ordered.append(by_name[n])
                pending.remove(n)
        return ordered

    def run(self, conn: duckdb.DuckDBPyConnection) -> list[TransformResult]:
        import time
        results: list[TransformResult] = []
        for t in self.ordered():
            t0 = time.time()
            t.run(conn)
            results.append(TransformResult(name=t.name, duration_seconds=time.time() - t0))
        return results
