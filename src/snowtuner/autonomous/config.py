"""Autonomous-mode configuration: per (action_type, warehouse, knob) opt-in.

The config table is the user's source of truth for "should the tool actually
apply this kind of change without my review?"  v0.1 ships everything OFF by
default — autonomous is strictly opt-in.

The literal string ``"*"`` represents catch-all in both the ``warehouse_name``
and ``knob`` columns (DuckDB primary keys disallow NULL, so we can't use NULL
as the discriminator).

Knob is what makes per-action-type granularity possible: ``ALTER_WAREHOUSE``
recommendations carry a ``knob`` like ``AUTO_SUSPEND`` or ``WAREHOUSE_SIZE``.
The runner gates each knob independently — a warehouse can be autonomous for
``AUTO_SUSPEND`` while keeping ``WAREHOUSE_SIZE`` advisory.

Resolution precedence (most-specific wins) for a single ``knob``:

  1. ``(action_type, warehouse_name, knob)``  — exact
  2. ``(action_type, warehouse_name, '*')``   — every-knob row for this warehouse
  3. ``(action_type, '*', knob)``             — knob row for every warehouse
  4. ``(action_type, '*', '*')``              — global default for this action

Returns ``None`` if no row matches, which the runner treats as "not enabled."
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import duckdb

from snowtuner.storage.db import naive_utcnow

CATCH_ALL = "*"


@dataclass
class AutonomousConfig:
    action_type: str
    warehouse_name: str  # CATCH_ALL ('*') or an actual warehouse name (uppercased)
    knob: str            # CATCH_ALL ('*') or a specific knob (e.g. 'AUTO_SUSPEND')
    enabled: bool
    confidence_threshold: float
    cooldown_hours: int
    max_rollbacks_per_week: int
    circuit_open_until: datetime | None
    updated_at: datetime | None

    @property
    def is_catch_all_warehouse(self) -> bool:
        return self.warehouse_name == CATCH_ALL

    @property
    def is_catch_all_knob(self) -> bool:
        return self.knob == CATCH_ALL


_COLUMNS = [
    "action_type", "warehouse_name", "knob", "enabled", "confidence_threshold",
    "cooldown_hours", "max_rollbacks_per_week", "circuit_open_until", "updated_at",
]


class AutonomousConfigStore:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def list(self) -> list[AutonomousConfig]:
        rows = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM app.autonomous_config "
            f"ORDER BY action_type, warehouse_name, knob"
        ).fetchall()
        return [self._row_to_config(r) for r in rows if r is not None]  # type: ignore[misc]

    def get(
        self, action_type: str, warehouse_name: str, knob: str = CATCH_ALL,
    ) -> AutonomousConfig | None:
        warehouse_key = self._normalize_wh(warehouse_name)
        knob_key = self._normalize_knob(knob)
        row = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM app.autonomous_config "
            f"WHERE action_type = ? AND warehouse_name = ? AND knob = ?",
            [action_type, warehouse_key, knob_key],
        ).fetchone()
        return self._row_to_config(row) if row else None

    def upsert(
        self,
        action_type: str,
        warehouse_name: str,
        knob: str = CATCH_ALL,
        *,
        enabled: bool | None = None,
        confidence_threshold: float | None = None,
        cooldown_hours: int | None = None,
        max_rollbacks_per_week: int | None = None,
    ) -> AutonomousConfig:
        warehouse_key = self._normalize_wh(warehouse_name)
        knob_key = self._normalize_knob(knob)
        existing = self.get(action_type, warehouse_key, knob_key)
        new_enabled = enabled if enabled is not None else (existing.enabled if existing else False)
        new_threshold = (
            confidence_threshold if confidence_threshold is not None
            else (existing.confidence_threshold if existing else 0.85)
        )
        new_cooldown = (
            cooldown_hours if cooldown_hours is not None
            else (existing.cooldown_hours if existing else 24)
        )
        new_max_rollbacks = (
            max_rollbacks_per_week if max_rollbacks_per_week is not None
            else (existing.max_rollbacks_per_week if existing else 2)
        )
        now = naive_utcnow()
        self.conn.execute(
            """
            INSERT INTO app.autonomous_config
              (action_type, warehouse_name, knob, enabled, confidence_threshold,
               cooldown_hours, max_rollbacks_per_week, circuit_open_until, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT (action_type, warehouse_name, knob) DO UPDATE SET
              enabled = excluded.enabled,
              confidence_threshold = excluded.confidence_threshold,
              cooldown_hours = excluded.cooldown_hours,
              max_rollbacks_per_week = excluded.max_rollbacks_per_week,
              updated_at = excluded.updated_at
            """,
            [
                action_type, warehouse_key, knob_key, new_enabled,
                new_threshold, new_cooldown, new_max_rollbacks, now,
            ],
        )
        return self.get(action_type, warehouse_key, knob_key)  # type: ignore[return-value]

    def delete(
        self, action_type: str, warehouse_name: str, knob: str = CATCH_ALL,
    ) -> None:
        self.conn.execute(
            "DELETE FROM app.autonomous_config "
            "WHERE action_type = ? AND warehouse_name = ? AND knob = ?",
            [action_type, self._normalize_wh(warehouse_name), self._normalize_knob(knob)],
        )

    def trip_circuit(
        self, action_type: str, warehouse_name: str, until: datetime,
        knob: str = CATCH_ALL,
    ) -> None:
        """Open the circuit (suppress autonomous apply) until *until*."""
        self.conn.execute(
            """
            UPDATE app.autonomous_config
            SET circuit_open_until = ?, updated_at = ?
            WHERE action_type = ? AND warehouse_name = ? AND knob = ?
            """,
            [
                until, naive_utcnow(), action_type,
                self._normalize_wh(warehouse_name), self._normalize_knob(knob),
            ],
        )

    def reset_circuit(
        self, action_type: str, warehouse_name: str, knob: str = CATCH_ALL,
    ) -> None:
        self.conn.execute(
            """
            UPDATE app.autonomous_config
            SET circuit_open_until = NULL, updated_at = ?
            WHERE action_type = ? AND warehouse_name = ? AND knob = ?
            """,
            [
                naive_utcnow(), action_type,
                self._normalize_wh(warehouse_name), self._normalize_knob(knob),
            ],
        )

    def resolve(
        self, action_type: str, warehouse_name: str | None, knob: str = CATCH_ALL,
    ) -> AutonomousConfig | None:
        """Find the effective config for this (action, warehouse, knob).

        Resolution order (most-specific wins):
          1. (action_type, warehouse_name, knob)
          2. (action_type, warehouse_name, '*')
          3. (action_type, '*', knob)
          4. (action_type, '*', '*')

        Returns None if no row matches.
        """
        wh_norm = self._normalize_wh(warehouse_name)
        knob_norm = self._normalize_knob(knob)

        # 1. exact (warehouse, knob)
        if wh_norm != CATCH_ALL and knob_norm != CATCH_ALL:
            r = self.get(action_type, wh_norm, knob_norm)
            if r is not None:
                return r
        # 2. warehouse-level catch-all knob
        if wh_norm != CATCH_ALL:
            r = self.get(action_type, wh_norm, CATCH_ALL)
            if r is not None:
                return r
        # 3. global catch-all warehouse, exact knob
        if knob_norm != CATCH_ALL:
            r = self.get(action_type, CATCH_ALL, knob_norm)
            if r is not None:
                return r
        # 4. global default
        return self.get(action_type, CATCH_ALL, CATCH_ALL)

    # ---- helpers ----
    @staticmethod
    def _normalize_wh(warehouse_name: str | None) -> str:
        if warehouse_name is None or warehouse_name == CATCH_ALL:
            return CATCH_ALL
        return warehouse_name.upper()

    @staticmethod
    def _normalize_knob(knob: str | None) -> str:
        if knob is None or knob == CATCH_ALL:
            return CATCH_ALL
        return knob.upper()

    @staticmethod
    def _row_to_config(row: Iterable | None) -> AutonomousConfig | None:
        if row is None:
            return None
        (action_type, warehouse_name, knob, enabled, threshold, cooldown,
         max_rollbacks, circuit_open_until, updated_at) = row
        return AutonomousConfig(
            action_type=action_type,
            warehouse_name=warehouse_name,
            knob=knob,
            enabled=bool(enabled),
            confidence_threshold=float(threshold),
            cooldown_hours=int(cooldown),
            max_rollbacks_per_week=int(max_rollbacks),
            circuit_open_until=circuit_open_until,
            updated_at=updated_at,
        )
