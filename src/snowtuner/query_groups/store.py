"""Persistence for QueryGroups (DuckDB-backed).

The store handles the row CRUD; member resolution (running the filter spec
against ``raw.query_history``) lives in the API endpoint where it can share
the WHERE-clause builder with the regular ``/queries`` listing.

Pattern mirrors ``RecommendationStore`` and ``ExperimentStore``.
"""
from __future__ import annotations

import json

import duckdb

from snowtuner.query_groups.model import (
    QueryFilterSpec,
    QueryGroup,
    QueryGroupKind,
)


_COLUMNS = [
    "id", "name", "description", "kind", "filter_spec",
    "snapshot_query_ids", "snapshot_at", "created_at", "created_by",
]


class QueryGroupStore:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    # ── writes ───────────────────────────────────────────────────────

    def insert(
        self,
        *,
        name: str,
        description: str | None,
        kind: QueryGroupKind,
        filter_spec: QueryFilterSpec,
        snapshot_query_ids: list[str] | None = None,
        snapshot_at = None,
        created_by: str = "user",
    ) -> int:
        """Persist a new QueryGroup and return its assigned id."""
        row = self.conn.execute(
            """
            INSERT INTO app.query_groups
              (name, description, kind, filter_spec, snapshot_query_ids,
               snapshot_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            [
                name,
                description,
                kind.value,
                filter_spec.model_dump_json(),
                json.dumps(snapshot_query_ids) if snapshot_query_ids is not None else None,
                snapshot_at,
                created_by,
            ],
        ).fetchone()
        return int(row[0])

    def delete(self, group_id: int) -> bool:
        """Delete a group by id.  Returns True if a row was deleted."""
        # DuckDB doesn't return rowcount the same way as other DBs; do an
        # explicit existence check first so we can return a useful bool.
        exists = self.conn.execute(
            "SELECT 1 FROM app.query_groups WHERE id = ?", [group_id],
        ).fetchone()
        if not exists:
            return False
        self.conn.execute("DELETE FROM app.query_groups WHERE id = ?", [group_id])
        return True

    # ── reads ────────────────────────────────────────────────────────

    def get(self, group_id: int) -> QueryGroup | None:
        cols = ", ".join(_COLUMNS)
        row = self.conn.execute(
            f"SELECT {cols} FROM app.query_groups WHERE id = ?",
            [group_id],
        ).fetchone()
        if not row:
            return None
        return self._hydrate(dict(zip(_COLUMNS, row)))

    def list(
        self,
        *,
        kind: QueryGroupKind | None = None,
        limit: int = 200,
    ) -> list[QueryGroup]:
        cols = ", ".join(_COLUMNS)
        where = []
        params: list = []
        if kind is not None:
            where.append("kind = ?")
            params.append(kind.value)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT {cols} FROM app.query_groups
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._hydrate(dict(zip(_COLUMNS, r))) for r in rows]

    # ── internals ────────────────────────────────────────────────────

    def _hydrate(self, row: dict) -> QueryGroup:
        spec = QueryFilterSpec.model_validate_json(row["filter_spec"])
        snapshot_ids = (
            json.loads(row["snapshot_query_ids"])
            if row["snapshot_query_ids"]
            else None
        )
        return QueryGroup(
            id=int(row["id"]),
            name=row["name"],
            description=row["description"],
            kind=QueryGroupKind(row["kind"]),
            filter_spec=spec,
            snapshot_query_ids=snapshot_ids,
            snapshot_at=row["snapshot_at"],
            created_at=row["created_at"],
            created_by=row["created_by"],
        )
