# Configuration reference

Every environment variable and config file snowtuner reads, in one place.

## Environment variables

### Credentials

snowtuner resolves credentials from the environment first, then the OS keyring, then a plaintext-TOML file at `$SNOWTUNER_DATA_DIR/creds.toml` (mode 0600). `snowtuner init` writes to the keyring; set the environment variables directly for headless or containerized deployments. Every variable carries the `SNOWTUNER_SNOWFLAKE_` prefix so it stays clear of the Snowflake connector's own `SNOWFLAKE_*` variables.

| Variable | Notes |
|---|---|
| `SNOWTUNER_SNOWFLAKE_ACCOUNT` | Account identifier, e.g. `xyz12345.us-east-1`. Required. |
| `SNOWTUNER_SNOWFLAKE_USER` | Service user. `bootstrap-sql` creates `SNOWTUNER_SVC`. Required. |
| `SNOWTUNER_SNOWFLAKE_AUTHENTICATOR` | `key_pair` or `password`. Defaults to `password`; `keypair`, `key-pair`, and `rsa` are accepted spellings of `key_pair`. |
| `SNOWTUNER_SNOWFLAKE_PRIVATE_KEY_PATH` | RSA private key for key-pair auth. `snowtuner init` writes `$SNOWTUNER_DATA_DIR/snowtuner_rsa_key.p8` (mode 0600). |
| `SNOWTUNER_SNOWFLAKE_PASSWORD` | Password, read only when `AUTHENTICATOR=password`. |
| `SNOWTUNER_SNOWFLAKE_ROLE` | Advisory-mode role. `bootstrap-sql` creates `SNOWTUNER_ROLE`. |
| `SNOWTUNER_SNOWFLAKE_WAREHOUSE` | Warehouse for snowtuner's own metadata queries. `bootstrap-sql` creates `SNOWTUNER_WH`. |

`ACCOUNT` and `USER` are the only required fields. If either is missing the resolver skips the environment entirely and falls through to the keyring, then the file. The remaining fields have no built-in defaults, so an environment-only setup has to supply the auth material itself.

### API auth

| Variable | Default | Notes |
|---|---|---|
| `SNOWTUNER_AUTH_MODE` | `none` | `none` (loopback-only, no token) or `token` (bearer-token required on every request). `none` mode refuses to bind to a non-loopback host as a safety check. |
| `SNOWTUNER_API_TOKEN` | (auto-generated to `~/.snowtuner/api_token`, mode 0600) | Operator-supplied bearer token. Inspect via `snowtuner auth show`; rotate via `snowtuner auth rotate`. |

The MCP server uses the same token automatically. The web UI stores the token in `localStorage` after you paste it once via the Settings page.

A handful of paths bypass auth even in `token` mode: `/health`, `/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc`. Useful for load-balancer probes and the OpenAPI viewer.

### AutomationLoop

The background runner that fires the full pipeline (`sync → features → recommenders → autonomous`) on an interval.

| Variable | Default | Notes |
|---|---|---|
| `SNOWTUNER_AUTOMATION_INTERVAL` | `0` (disabled) | Seconds between ticks. `3600` (hourly) is the recommended production setting - matches Snowflake `ACCOUNT_USAGE`'s ~45-minute refresh cadence. |
| `SNOWTUNER_AUTOMATION_ON_START` | `false` | If `true`, block API startup until the first tick completes. Used by ephemeral container deployments that want guaranteed-fresh state before accepting traffic. |

A tick that fails on the sync stage aborts the rest of the pipeline ("fail-fast") and retries next interval. While an experiment is in `RUNNING` state, the autonomous stage defers automatically to avoid corrupting in-flight measurements. Inspect tick history via `GET /automation/status` or the freshness pill in the nav bar.

### Recommender consideration windows

How far back each recommender reads when aggregating history. Read-side only: these do not change what `snowtuner sync` ingests or retains - a window larger than the ingested history just degrades to "everything we have". Inspect the resolved values anytime with `snowtuner recommenders`.

| Variable | Default | Notes |
|---|---|---|
| `SNOWTUNER_WINDOW_DAYS` | (unset) | Global override: every recommender uses this many days. `0` means unbounded (all ingested history). |
| `SNOWTUNER_WINDOW_DAYS__<RECOMMENDER>` | (unset) | Per-recommender override; beats the global. Name is the recommender's registry name uppercased, e.g. `SNOWTUNER_WINDOW_DAYS__RULE_BASED_RIGHT_SIZER=30`. |

Built-in defaults when nothing is set:

| Recommender | Default window |
|---|---|
| `rule_based_right_sizer` | 14 days |
| `multi_cluster_reducer` | 14 days |
| `qas_candidate_finder` | 14 days |
| `gen2_candidate_finder` | 14 days |
| `auto_suspend_survival_tuner` | 30 days |

Notes on semantics:

- Within a window every observation weighs the same; there is no recency decay (yet). Shrinking the window is the lever for "react faster to workload changes"; growing it is the lever for "smooth over noisy weeks".
- The auto-suspend tuner defaults to 30 days (not 14) because low-traffic warehouses need more runway to accumulate the 10 idle gaps its model requires. Its pre-window behavior - unbounded history, with proposals drifting as data accumulated - is restorable with `SNOWTUNER_WINDOW_DAYS__AUTO_SUSPEND_SURVIVAL_TUNER=0`.
- Credits-per-week figures are extrapolated as `total x 7/window`; under an unbounded window they normalize by the observed span of the data instead.
- Recommendation rationales state the window they were computed over, and readiness-gate output includes `window_days` + `window_source`, so a surprising recommendation is always traceable to its window.
- **Reserved:** `SNOWTUNER_WINDOW_DAYS__<RECOMMENDER>__<WAREHOUSE>` is the planned shape for per-warehouse windows. Setting one today logs a warning and is ignored - per-(warehouse, recommender) windows need per-warehouse query passes and are a future release.

### API server

| Variable | Default | Notes |
|---|---|---|
| `SNOWTUNER_API_URL` | `http://127.0.0.1:8770` | Where the MCP server expects to find the HTTP API. Set this in `claude_desktop_config.json` when wiring up MCP. |

### Storage and runtime

| Variable | Default | Notes |
|---|---|---|
| `SNOWTUNER_DATA_DIR` | `~/.snowtuner` | Base directory for every path in the "Files snowtuner reads/writes" table below. Point it at a mounted volume for containerized deployments. |
| `SNOWTUNER_STATIC_DIR` | (unset) | Path to the built SPA (`web/dist`). When set, the API serves the web UI at `/`. The AWS deploy sets this. For local development it stays unset and the Vite dev server serves the UI. |
| `SNOWTUNER_DUCKDB_MEMORY_LIMIT` | `3GB` | Caps DuckDB's working memory. Raise it on larger hosts (e.g. `12GB` on an `m6i.xlarge`); lower it when snowtuner shares a box with memory-hungry neighbors. |
| `SNOWTUNER_QUERY_HISTORY_CHUNK_DAYS` | `1` | Day-window size for the chunked `QUERY_HISTORY` pull. Smaller chunks bound peak memory during sync on dense accounts. |

## CLI flags worth knowing

These aren't env vars but are referenced enough to belong in the reference:

| Command | Flag | Purpose |
|---|---|---|
| `snowtuner sync` | `--lookback-days` | First-time lookback window (default 14). On subsequent syncs the stored watermark takes over. |
| `snowtuner backfill` | `--days N` | Reset watermarks and re-pull N days of history. Preserves all `app.*` state. Use this for "I want more history"; don't reach for `reset`. |
| `snowtuner backfill` | `--source <name>` | Only backfill one source. |
| `snowtuner reset` | `--yes` | Skip the confirmation prompt. |
| `snowtuner reset` | `--include-user-config` | Also wipe `app.query_groups` and `app.autonomous_config` (default: preserved across reset). |
| `snowtuner api` | `--host`, `--port` | Bind address. `--host` other than `127.0.0.1` requires `SNOWTUNER_AUTH_MODE=token`. |

## Demo mode

`snowtuner demo` provisions six throwaway warehouses (prefixed `SNOWTUNER_DEMO_*`), runs cooked TPC-H workloads engineered to trigger each recommender, then tears the warehouses down. It is the quickest way to see end-to-end output on a real account before pointing snowtuner at production history.

| Command | Purpose |
|---|---|
| `snowtuner demo seed` | Provision the warehouses and run the workloads. Prompts for confirmation on the estimated cost first. |
| `snowtuner demo status` | Show the most recent run's per-workload progress. |
| `snowtuner demo verify` | Query ACCOUNT_USAGE and report, per warehouse, whether the intended signal (spill, queueing, suspend cycles) actually landed. |
| `snowtuner demo teardown` | Drop every `SNOWTUNER_DEMO_*` warehouse. |

A run costs roughly 3.5 credits (about $10 at standard $3/credit pricing) and takes 45 to 70 minutes of wall time. It needs two grants beyond advisory mode, which `snowtuner bootstrap-sql` prints in a commented block:

```sql
GRANT CREATE WAREHOUSE ON ACCOUNT TO ROLE SNOWTUNER_ROLE;
GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA TO ROLE SNOWTUNER_ROLE;
```

## Files snowtuner reads/writes

All paths are relative to `$SNOWTUNER_DATA_DIR` (default `~/.snowtuner`).

| Path | Mode | Contents |
|---|---|---|
| `~/.snowtuner/snowtuner.duckdb` | 0600 | The local OLAP store. Wipe via `snowtuner reset`. |
| `~/.snowtuner/snowtuner.duckdb.wal` | 0600 | DuckDB write-ahead log. Cleaned up by `reset`. |
| `~/.snowtuner/creds.toml` | 0600 | Plaintext credential fallback. Only written if you opt out of the keyring backend. |
| `~/.snowtuner/snowtuner_rsa_key.p8` | 0600 | Service-user RSA private key. |
| `~/.snowtuner/api_token` | 0600 | Auto-generated API bearer token (only present once `SNOWTUNER_AUTH_MODE=token` has been used). |
| `~/.snowtuner/audit-archive/autonomous-applications-*.json` | 0644 | Archived audit trail snapshots, written automatically before every `snowtuner reset`. |
