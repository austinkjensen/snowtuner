"""AutomationLoop — the background runner that keeps snowtuner self-driving.

What it does
------------
On a configurable interval, runs the FULL orchestrator pipeline:

    sync → features → recommenders → autonomous_apply

Each stage is gated by the orchestrator's existing checks (autonomous fires
only if any per-knob config is enabled; recommender fits skipped if the
training gate isn't satisfied; sync skipped if no client provided).

Why a single loop, not one per stage
------------------------------------
The stages have a strict data dependency chain — recommenders need fresh
features, autonomous needs fresh recommendations.  Running them at independent
cadences creates ordering edge cases (autonomous looks at recs from N hours
ago).  One loop, one tick, end-to-end gives operators a single mental model:
"if the loop is enabled and healthy, my installation is current."

Configuration
-------------
* ``SNOWTUNER_AUTOMATION_INTERVAL`` — seconds between ticks.  0 (default)
  disables the loop entirely.  Recommended: 3600 (hourly) in production,
  to match Snowflake ACCOUNT_USAGE's ~45-minute refresh cadence.
* ``SNOWTUNER_AUTOMATION_ON_START`` — if "true", block API startup until
  the first tick completes (cold-start "guaranteed fresh on boot").

Safety invariants
-----------------
* **No overlapping ticks.**  A threading.Lock prevents a long sync (cold
  start, backfill) from being followed by another tick before it finishes.
  An overlap would just skip; next interval tries fresh.
* **Fail-fast on sync error.**  If any source returned an error this tick,
  abort the rest of the pipeline.  Features and recommenders don't run on
  potentially-corrupt state.  Retry next tick.
* **Separate Snowflake connection.**  The loop owns its own SnowflakeClient
  so it doesn't contend with an in-flight experiment using the engine's
  connection.  Closed and reopened each tick to avoid stale-connection
  failure modes on long-running deployments.
* **Autonomous defers under RUNNING experiment.**  Handled inside
  ``AutonomousRunner.run()`` — see the experiment-running guard there.
  This loop trusts that guard rather than duplicating it.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class StageOutcome:
    """One stage of a single tick."""
    name: str                          # 'sync' | 'features' | 'recommenders' | 'autonomous'
    started_at: datetime
    duration_seconds: float
    outcome: str                        # 'success' | 'failed' | 'skipped'
    error: str | None = None
    details: dict = field(default_factory=dict)


@dataclass
class TickReport:
    """Aggregate state from one pipeline tick."""
    started_at: datetime
    completed_at: datetime | None = None
    stages: list[StageOutcome] = field(default_factory=list)
    overall: str = "running"            # 'running' | 'success' | 'failed' | 'skipped'
    skip_reason: str | None = None


@dataclass
class LoopStatus:
    """Public-safe snapshot of the loop's current state."""
    enabled: bool
    interval_seconds: int
    last_tick: TickReport | None = None
    currently_running: bool = False
    next_run_at: datetime | None = None


class AutomationLoop:
    """The background pipeline runner.  Singleton per API process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()           # held while a tick is running
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_tick: TickReport | None = None
        self._next_run_at: datetime | None = None
        self._interval_seconds = _interval_from_env()

    @property
    def enabled(self) -> bool:
        return self._interval_seconds > 0

    @property
    def interval_seconds(self) -> int:
        return self._interval_seconds

    def start(self) -> None:
        """Spawn the loop thread.  No-op if interval is 0 or already running."""
        if not self.enabled:
            logger.info(
                "AutomationLoop disabled (SNOWTUNER_AUTOMATION_INTERVAL=0); "
                "use `snowtuner run` or `POST /orchestrator/run` for manual triggers"
            )
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="snowtuner-automation", daemon=True,
        )
        self._thread.start()
        logger.info(
            "AutomationLoop started; interval=%ds", self._interval_seconds,
        )

    def stop(self) -> None:
        """Signal the loop to stop.  Blocks briefly waiting for current tick."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def status(self) -> LoopStatus:
        return LoopStatus(
            enabled=self.enabled,
            interval_seconds=self._interval_seconds,
            last_tick=self._last_tick,
            currently_running=self._lock.locked(),
            next_run_at=self._next_run_at,
        )

    def run_one_tick(self) -> TickReport:
        """Run one pipeline tick synchronously.  Used by run-on-start and
        for ad-hoc invocation; the loop calls this too.

        Returns the populated TickReport.  If the lock is held by another
        tick already in flight, returns a skipped report instead.
        """
        if not self._lock.acquire(blocking=False):
            report = TickReport(started_at=_utc_now())
            report.overall = "skipped"
            report.skip_reason = "another tick is still running"
            report.completed_at = _utc_now()
            self._last_tick = report
            return report
        try:
            report = self._tick()
            self._last_tick = report
            return report
        finally:
            self._lock.release()

    # ── internals ──────────────────────────────────────────────────

    def _loop(self) -> None:
        # Run immediately on start so the operator sees something happen
        # without waiting an hour.  Subsequent ticks honor the interval.
        while not self._stop.is_set():
            try:
                self.run_one_tick()
            except Exception:
                # Defensive — _tick already catches everything, but the
                # loop must never die from an unhandled exception.
                logger.exception("AutomationLoop tick raised unexpectedly")
            self._next_run_at = _utc_now_plus(self._interval_seconds)
            # ``Event.wait`` returns True if stop was signaled, False on timeout.
            if self._stop.wait(timeout=self._interval_seconds):
                break

    def _tick(self) -> TickReport:
        from snowtuner.events import log_event
        from snowtuner.storage.db import get_connection

        report = TickReport(started_at=_utc_now())
        # tick.start is logged with outcome='started' so the matching
        # tick.complete event closes the pair.  Useful for "how long did
        # tick X take?" queries against app.events alone.
        try:
            log_event(
                get_connection(),
                actor="automation",
                action="automation.tick.start",
                outcome="started",
            )
        except Exception:
            # Logging must never block the pipeline.  Defensive — the
            # log_event helper itself is best-effort, but if get_connection()
            # fails (e.g. DB locked) we just press on.
            pass
        try:
            self._run_pipeline(report)
            if any(s.outcome == "failed" for s in report.stages):
                report.overall = "failed"
            else:
                report.overall = "success"
        except Exception as e:
            logger.exception("automation tick failed at top level: %s", e)
            report.overall = "failed"
            report.skip_reason = f"top-level exception: {type(e).__name__}: {e}"
        finally:
            report.completed_at = _utc_now()
            try:
                log_event(
                    get_connection(),
                    actor="automation",
                    action="automation.tick.complete",
                    outcome=report.overall,
                    payload={
                        "duration_seconds": (
                            (report.completed_at - report.started_at).total_seconds()
                            if report.completed_at else None
                        ),
                        "stages": [
                            {
                                "name": s.name,
                                "outcome": s.outcome,
                                "duration_seconds": s.duration_seconds,
                                "error": s.error,
                            }
                            for s in report.stages
                        ],
                        "skip_reason": report.skip_reason,
                    },
                    error=report.skip_reason if report.overall == "failed" else None,
                )
            except Exception:
                pass
        return report

    def _run_pipeline(self, report: TickReport) -> None:
        # Lazy imports — keep this module importable even if the rest of
        # snowtuner isn't fully wired up (e.g., in a unit test).
        from snowtuner.features import DEFAULT_TRANSFORMS
        from snowtuner.features.base import FeaturePipeline
        from snowtuner.ingestion.snowflake_client import SnowflakeClient
        from snowtuner.ingestion.sources import DEFAULT_SOURCES
        from snowtuner.ingestion.sync import sync_all
        from snowtuner.orchestrator import Orchestrator
        from snowtuner.recommenders.registry import default_registry
        from snowtuner.storage.db import get_connection

        # Build a fresh Snowflake client each tick (and close it at the end).
        # On long-running deployments this avoids stale-connection failures
        # that occur if a single client sits idle for hours.
        try:
            client = SnowflakeClient.from_resolver()
        except RuntimeError as e:
            # No creds — automation is a no-op.  Mark the tick skipped so
            # the operator can tell from /automation/status that they need
            # to run `snowtuner init`.
            report.stages.append(StageOutcome(
                name="sync",
                started_at=_utc_now(),
                duration_seconds=0.0,
                outcome="skipped",
                error=str(e),
                details={"reason": "no Snowflake credentials configured"},
            ))
            report.skip_reason = "no Snowflake credentials"
            return

        try:
            conn = get_connection()

            # ── Stage 1: sync ────────────────────────────────────────
            t0 = time.time()
            started = _utc_now()
            try:
                results, errors = sync_all(
                    list(DEFAULT_SOURCES), client, conn,
                )
                sync_outcome = "failed" if errors else "success"
                report.stages.append(StageOutcome(
                    name="sync",
                    started_at=started,
                    duration_seconds=time.time() - t0,
                    outcome=sync_outcome,
                    error="; ".join(
                        f"{e.source_name}: {e.error}" for e in errors
                    ) if errors else None,
                    details={
                        "sources": [
                            {
                                "name": r.source_name,
                                "rows": r.rows_ingested,
                                "high_water": r.high_water.isoformat() if r.high_water else None,
                            }
                            for r in results
                        ],
                        "error_count": len(errors),
                    },
                ))
            except Exception as e:
                report.stages.append(StageOutcome(
                    name="sync",
                    started_at=started,
                    duration_seconds=time.time() - t0,
                    outcome="failed",
                    error=f"{type(e).__name__}: {e}",
                ))
                logger.exception("automation: sync stage failed")

            # Fail-fast: if sync had any failure, don't run the rest of
            # the pipeline this tick.  Features+recommenders+autonomous
            # against potentially-corrupted state is worse than a stale
            # tick.  Next interval tries fresh.
            if report.stages[-1].outcome == "failed":
                report.skip_reason = (
                    "sync errored; skipping features/recommenders/autonomous "
                    "for this tick (next tick will retry)"
                )
                return

            # ── Stages 2-4: features, recommenders, autonomous ──────
            # Delegated to the orchestrator with skip_sync=True (we just
            # did it ourselves so we could fail-fast on errors).
            orch = Orchestrator(
                conn,
                sources=list(DEFAULT_SOURCES),
                pipeline=FeaturePipeline(DEFAULT_TRANSFORMS),
                registry=default_registry(),
            )

            t0 = time.time()
            started = _utc_now()
            try:
                orch_report = orch.run(client=client, skip_sync=True)
                # Decompose into individual stage outcomes for the status view.
                report.stages.append(StageOutcome(
                    name="features",
                    started_at=started,
                    duration_seconds=sum(
                        f.duration_seconds for f in orch_report.feature_results
                    ),
                    outcome="success",
                    details={
                        "transforms_run": len(orch_report.feature_results),
                    },
                ))
                rec_errors = [
                    f"{r.name}: {r.error}"
                    for r in orch_report.recommender_results if r.error
                ]
                report.stages.append(StageOutcome(
                    name="recommenders",
                    started_at=started,
                    duration_seconds=time.time() - t0,
                    outcome="failed" if rec_errors else "success",
                    error="; ".join(rec_errors) if rec_errors else None,
                    details={
                        "recommenders_run": len(orch_report.recommender_results),
                        "total_predictions": sum(
                            r.predictions_emitted for r in orch_report.recommender_results
                        ),
                        "total_experiments_proposed": sum(
                            r.experiments_proposed for r in orch_report.recommender_results
                        ),
                    },
                ))
                # Autonomous outcome: success if it ran, skipped if it
                # didn't (the orchestrator's autonomous_skipped_reason
                # captures why).
                if orch_report.autonomous_report is not None:
                    a = orch_report.autonomous_report
                    applied = a.applied()
                    failed = a.failed()
                    report.stages.append(StageOutcome(
                        name="autonomous",
                        started_at=started,
                        duration_seconds=0.0,
                        outcome="failed" if failed else "success",
                        error=("; ".join(
                            f"{d.action_type}/{d.warehouse_name}: {d.reason}"
                            for d in failed
                        ) if failed else None),
                        details={
                            "applied": len(applied),
                            "skipped": len([
                                d for d in a.decisions if d.decision == "skipped"
                            ]),
                            "failed": len(failed),
                            "loop_skipped_reason": a.skipped_reason,
                        },
                    ))
                else:
                    report.stages.append(StageOutcome(
                        name="autonomous",
                        started_at=started,
                        duration_seconds=0.0,
                        outcome="skipped",
                        details={
                            "reason": orch_report.autonomous_skipped_reason,
                        },
                    ))
            except Exception as e:
                report.stages.append(StageOutcome(
                    name="orchestrator",
                    started_at=started,
                    duration_seconds=time.time() - t0,
                    outcome="failed",
                    error=f"{type(e).__name__}: {e}",
                ))
                logger.exception("automation: post-sync stages failed")
        finally:
            try:
                client.close()
            except Exception:
                pass


# ── Module-level singleton + helpers ─────────────────────────────


_LOOP: AutomationLoop | None = None


def get_loop() -> AutomationLoop:
    """Get the per-process AutomationLoop singleton."""
    global _LOOP
    if _LOOP is None:
        _LOOP = AutomationLoop()
    return _LOOP


def _interval_from_env() -> int:
    raw = os.environ.get("SNOWTUNER_AUTOMATION_INTERVAL", "0")
    try:
        v = int(raw)
    except ValueError:
        logger.warning(
            "invalid SNOWTUNER_AUTOMATION_INTERVAL=%r; disabling automation",
            raw,
        )
        return 0
    return max(0, v)


def _run_on_start() -> bool:
    return os.environ.get("SNOWTUNER_AUTOMATION_ON_START", "").lower() in (
        "1", "true", "yes",
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_now_plus(seconds: int) -> datetime:
    from datetime import timedelta
    return _utc_now() + timedelta(seconds=seconds)
