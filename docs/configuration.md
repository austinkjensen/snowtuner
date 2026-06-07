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
