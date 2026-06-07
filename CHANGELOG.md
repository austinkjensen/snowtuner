# Changelog

All notable changes to snowtuner will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Initial release

### Added

- **Ingestion** for `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY`,
  `WAREHOUSE_METERING_HISTORY`, `WAREHOUSE_EVENTS_HISTORY`, and
  `SHOW WAREHOUSES` into a local DuckDB. Per-source error isolation,
  watermark-driven incremental sync, configurable initial-lookback cap.
- **Auto-suspend recommender** (`auto_suspend_survival_tuner`): cost-minimizing
  survival analysis on per-warehouse reactivation gaps. Picks the
  `AUTO_SUSPEND` value that minimizes expected per-cycle cost
  `E[min(T, AS) + C·1{T > AS}]` where C is sized by warehouse class.
- **Right-sizing recommender** (`rule_based_right_sizer`): four transparent
  rules covering remote spill, local spill ratio, queue overload, and
  overprovisioning. Targets `WAREHOUSE_SIZE` only in v0.1.
- **Alternative spill-aware right-sizer** (`spill_aware_right_sizer`):
  importable but not registered by default. Empirical memory-required
  distribution from observed spill bytes; snaps to smallest size covering p95.
- **Autonomous mode**: per-`(action_type, warehouse)` opt-in. Confidence
  threshold, cooldown window, weekly rollback budget that trips a circuit
  breaker. One-click rollback per applied change. Fully separate from
  recommenders so you can apply manually for some, automate others.
- **HTTP API** (FastAPI) at `snowtuner api`. Endpoints for recommendations,
  recommenders, autonomous config + applications, status, and warehouses.
- **Streamlit UI** (`snowtuner ui`) with two tabs: Recommendations and
  Autonomous mode. Talks to the API, never DuckDB directly - sidesteps the
  single-writer constraint.
- **Admin MCP server** (`snowtuner mcp`) for Claude Desktop. 13 tools wrapping
  the API. Lets you ask Claude *"what recommendations are open?"* / *"roll
  back application #3"* / *"enable autonomous on ETL_WH"*.
- **Service-user setup**: `snowtuner init` generates an RSA keypair, stores
  the private key at mode 0600, and prompts for account / user / role /
  warehouse. `snowtuner bootstrap-sql` prints the ACCOUNTADMIN script that
  creates `SNOWTUNER_SVC` (service user with `TYPE=SERVICE`),
  `SNOWTUNER_ROLE`, `SNOWTUNER_WH` (XSMALL, auto-suspend 60s), and the
  minimal advisory-mode grants. `--autonomous-warehouse <name>` emits the
  one-line per-warehouse `MODIFY` grant.
- **Tiered credential resolver**: env vars → OS keyring → 0600 plaintext file.
- **Synthetic data generator** (`snowtuner seed`) producing six fictional
  warehouses with distinct patterns for both recommenders.

### Architectural

- Plugin/entry-points discovery removed from public surface; the
  `Recommender` protocol + `RecommenderRegistry` remain internal-only.
- All polymorphic action types persist as JSON on
  `app.recommendations.action_payload` and dispatch via `actions/registry.py`.
- DuckDB schema split across three logical schemas: `raw.*`, `features.*`,
  `app.*`. Forward-only schema migrations in
  `storage/schema.py:_pre_create_migrations`.
- Apache 2.0 license.

### Fixed during pre-release shakedown

- API segfaulted (SIGSEGV / exit 139) under concurrent requests because the
  DuckDB Python connection isn't thread-safe and uvicorn runs sync handlers
  in a thread pool.  Fixed by minting per-thread cursors off a shared master
  connection (the pattern DuckDB documents).  Also kept `--loop asyncio` as
  the default for the api command (uvloop on Python 3.14 was a separate red
  herring).

### Known limitations

- `raw.warehouses` is not patched in-place when autonomous mode applies a
  change. A re-`run` between syncs may emit a duplicate proposal until the
  next `snowtuner sync` refreshes the snapshot.
- Multi-cluster warehouse tuning, `CREATE WAREHOUSE`, query routing, and
  result caching are all v0.2.
- No unit-test suite shipped with v0.1 - validation is by integration smoke
  tests against synthetic data + dogfooding on a personal Snowflake account.
- Right-sizing on Snowflake accounts with no spill / no queueing produces no
  recommendations (intentional - see the README "How recommenders decide"
  section). This is correct for healthy small accounts but produces no demo
  output without the synthetic seed.

[0.1.0]: https://github.com/austinkjensen/snowtuner/releases/tag/v0.1.0
