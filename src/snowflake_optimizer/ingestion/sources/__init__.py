from snowflake_optimizer.ingestion.sources.query_history import QueryHistorySource
from snowflake_optimizer.ingestion.sources.warehouse_metering import (
    WarehouseMeteringSource,
)
from snowflake_optimizer.ingestion.sources.warehouse_events import (
    WarehouseEventsSource,
)
from snowflake_optimizer.ingestion.sources.warehouses import WarehousesSource

DEFAULT_SOURCES = [
    QueryHistorySource(),
    WarehouseMeteringSource(),
    WarehouseEventsSource(),
    WarehousesSource(),
]

__all__ = [
    "QueryHistorySource",
    "WarehouseMeteringSource",
    "WarehouseEventsSource",
    "WarehousesSource",
    "DEFAULT_SOURCES",
]
