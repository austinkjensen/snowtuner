# Configuration reference

Every environment variable and config file snowtuner reads, in one place.

## Environment variables

### Credentials

These mirror the Snowflake connector's standard names. `snowtuner init` writes them to your OS keyring by default; you only need to set them as env vars in headless / containerized environments.

| Variable | Default | Notes |
|---|---|---|
| `SNOWFLAKE_ACCOUNT` | - | Account identifier (e.g. `xyz12345.us-east-1`). |
| `SNOWFLAKE_USER` | `SNOWTUNER_SVC` | Service user created by `snowtuner bootstrap-sql`. |
| `SNOWFLAKE_ROLE` | `SNOWTUNER_ROLE` | Role granted ACCOUNT_USAGE access. |
| `SNOWFLAKE_WAREHOUSE` | `SNOWTUNER_WH` | Warehouse used for snowtuner's own metadata queries. |
| `SNOWFLAKE_PRIVATE_KEY_PATH` | `~/.snowtuner/snowtuner_rsa_key.p8` | RSA key for key-pair auth (mode 0600). |
| `SNOWTUNER_EXP_USER` | `SNOWTUNER_EXP_SVC` | Separate user for the experiments engine (provisions test warehouses). |
| `SNOWTUNER_EXP_PRIVATE_KEY_PATH` | `~/.snowtuner/snowtuner_exp_rsa_key.p8` | Experiments-user RSA key. |

The credential resolver tries env vars first, then the OS keyring, then a plaintext-TOML fallback at `~/.snowtuner/creds.toml` (mode 0600). See `snowtuner.credentials.resolver`.

### API auth

| Variable | Default | Notes |
|---|---|---|
| `SNOWTUNER_AUTH_MODE` | `none` | `none` (loopback-only, no token) or `token` (bearer-token required on every request). `none` mode refuses to bind to a non-loopback host as a safety check. |
| `SNOWTUNER_API_TOKEN` | (auto-generated to `~/.snowtuner/api_token`, mode 0600) | Operator-supplied bearer token. Inspect via `snowtuner auth show`; rotate via `snowtuner auth rotate`. |

The MCP server uses the same token automatically. The web UI stores the token in `localStorage` after you paste it once via the Settings page.

A handful of paths bypass auth even in `token` mode: `/health`, `/openapi.json`, `/docs`, `/redoc`. Useful for load-balancer probes and the OpenAPI viewer.

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

## Files snowtuner reads/writes

| Path | Mode | Contents |
|---|---|---|
| `~/.snowtuner/snowtuner.duckdb` | 0600 | The local OLAP store. Wipe via `snowtuner reset`. |
| `~/.snowtuner/snowtuner.duckdb.wal` | 0600 | DuckDB write-ahead log. Cleaned up by `reset`. |
| `~/.snowtuner/creds.toml` | 0600 | Plaintext credential fallback. Only written if you opt out of the keyring backend. |
| `~/.snowtuner/snowtuner_rsa_key.p8` | 0600 | Service-user RSA private key. |
| `~/.snowtuner/snowtuner_exp_rsa_key.p8` | 0600 | Experiments-user RSA private key (only present if you ran `bootstrap-sql --enable-experiments`). |
| `~/.snowtuner/api_token` | 0600 | Auto-generated API bearer token (only present once `SNOWTUNER_AUTH_MODE=token` has been used). |
| `~/.snowtuner/audit-archive/autonomous-applications-*.json` | 0644 | Archived audit trail snapshots, written automatically before every `snowtuner reset`. |
