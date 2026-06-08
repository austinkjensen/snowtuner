# snowtuner

**Locally-hosted, open-source Snowflake cost & performance advisor.**

Connects to your Snowflake account (read-mostly, dedicated service user), ingests usage data into a local DuckDB, and produces concrete recommendations: change `AUTO_SUSPEND` on `ETL_WH` to 60s, downsize `OVERKILL_WH` from `LARGE` to `MEDIUM`, etc. Each recommendation comes with the SQL to run, a rollback statement, a rationale, and a confidence score. You review them in a terminal or web UI, accept the ones you trust, and reject the rest.

For warehouses where you trust the algorithm, you can flip a per-warehouse switch to **autonomous mode**: snowtuner applies new recommendations on its own, with a cooldown between changes and a circuit breaker that pauses autonomy after too many rollbacks. Every autonomous change records a rollback statement so you can revert with one click.

> **Status:** v0.1 shipped (recommenders + autonomous-apply). v0.2 in flight: replay-experiments framework and Queries explorer are landing now. Single-account, advisory-by-default, autonomous-mode opt-in. Not yet recommended for unattended production use. See the [roadmap](#roadmap) below.

---

## Why this exists

There are good commercial Snowflake optimizers that do parts of what snowtuner does plus more. They're closed-source and require sending your usage metadata to a third-party service.

snowtuner exists to:

1. **Be the only credible OSS option** for "make my Snowflake bill smaller" and "make my queries faster," for accounts that don't want to send their query metadata anywhere.
2. **Run entirely on your infrastructure.** No outbound calls except to your Snowflake account.
3. **Make autonomous-apply a free-tier feature, not a paid one.** The narrative "advisory then autonomous" is the product, not the paywall.

## What it does today

| Capability | Status |
|---|---|
| Ingests `QUERY_HISTORY`, `WAREHOUSE_METERING_HISTORY`, `WAREHOUSE_EVENTS_HISTORY`, and `SHOW WAREHOUSES` into local DuckDB | v0.1 |
| **AUTO_SUSPEND tuning** via cost-minimizing survival analysis on reactivation gaps | v0.1 |
| **Warehouse right-sizing** (`WAREHOUSE_SIZE` only) via transparent rules + alternative spill-aware model | v0.1 |
| Per-(action_type, warehouse, knob) **autonomous-apply** with cooldown and circuit breaker | v0.1 |
| One-click **rollback** of autonomous applications | v0.1 |
| HTTP API (FastAPI) + React web UI + Admin MCP server (Claude Desktop, 42 tools) | v0.1+ |
| Service-user setup with RSA key-pair auth + `bootstrap-sql` generator | v0.1 |
| **Queries explorer**: filter / drill into ingested query history; family rollup view | v0.2 |
| **Replay experiments framework**: in-vitro A/B testing of warehouse configs with paired t-tests, Bonferroni correction, confidence intervals | v0.2 |
| **Gen2 / QAS / size sweeps** as preset experiment recipes (`gen1_to_gen2`, `size_sweep_pm1`, `qas_on_off`, `factorial_gen_x_size`) | v0.2 |
| **CloudFormation deploy** to AWS (single instance, SSM port-forward, no public URL) | v0.2 |
| Multi-cluster tuning (`MIN_CLUSTER_COUNT`, `MAX_CLUSTER_COUNT`, `SCALING_POLICY`) | roadmap |
| Saved query groups (static + dynamic) for monitoring and experiment inputs | roadmap |
| User-built experiment recipes; benchmark-style experiments | roadmap |
| Snowflake-compatible proxy + caching layer; multi-platform (Snowflake + Databricks) | roadmap |
| Non-destructive query rewrite *suggestions* | roadmap |
| Multi-account fleet management, SLO-backed savings, managed hosting | paid tier |

## Quickstart (~10 minutes from install to first recommendation)

```bash
# 1. Install
git clone https://github.com/austinkjensen/snowtuner
cd snowtuner
uv venv && source .venv/bin/activate
uv pip install -e '.[snowflake]'

# 2. Configure credentials (interactive; generates an RSA keypair)
snowtuner init   # prompts for account, defaults to SNOWTUNER_SVC service user

# 3. Print the bootstrap SQL and run it in Snowsight as ACCOUNTADMIN
snowtuner bootstrap-sql > bootstrap.sql
# Open Snowsight, paste bootstrap.sql, run.

# 4. Verify the connection
snowtuner verify

# 5. Pull data and run the recommenders
snowtuner sync --lookback-days 14
snowtuner run

# 6. Review from the terminal
snowtuner list                 # list recommendations
snowtuner status               # ingestion + warehouse summary
snowtuner check-schema         # detect Snowflake-side column drift (warn-only)

# 7. Launch the web UI (two terminals)
snowtuner api --host 127.0.0.1 --port 8770       # terminal A: backend
cd web && npm install && npm run dev             # terminal B: Vite at :5173
# Open http://127.0.0.1:5173. Vite proxies /api/* to the backend.
```

### Going automatic

Once it's working, replace cron / manual `snowtuner run` with the built-in **AutomationLoop**:

```bash
SNOWTUNER_AUTOMATION_INTERVAL=3600 snowtuner api   # hourly full pipeline
```

The loop runs the full `sync -> features -> recommenders -> autonomous` chain on every tick. Watch its state from the freshness pill in the nav bar, or `GET /automation/status` / `snowtuner-mcp run_automation_now`. Failures are fail-fast: a sync error aborts the rest of that tick and retries next interval. While an experiment is `RUNNING`, the autonomous stage defers automatically. See [docs/configuration.md](docs/configuration.md) for every knob.

### Wider history

The first sync caps at 14 days back. If you want more, **don't** `snowtuner reset`. That nukes recommendations, experiments, query groups, and autonomous configs. Instead:

```bash
snowtuner backfill --days 90        # reset watermarks, refetch 90 days
# app.recommendations, app.experiments, app.autonomous_*, app.query_groups all preserved
```

### Optional: experiments framework (v0.2)

The replay-experiments framework needs a separate Snowflake service user with `CREATE WAREHOUSE` privilege. Generate its bootstrap and run as `ACCOUNTADMIN`:

```bash
snowtuner bootstrap-sql --enable-experiments > experiments-bootstrap.sql
```

Then grant `SELECT` on whatever databases / tables you want experiments to replay queries against. See the comment block in the generated SQL.

### Demo mode (see it work on your own account)

If you want to watch snowtuner light up end-to-end before you commit time to figuring out what it'd say about your real workload:

```bash
snowtuner demo seed       # ~30 min wall, asks y/n on cost first
# (wait ~45 min for Snowflake ACCOUNT_USAGE to catch up)
snowtuner sync && snowtuner run
snowtuner demo teardown   # drops the 6 demo warehouses
```

Demo mode provisions 6 throwaway warehouses prefixed `SNOWTUNER_DEMO_*`, runs cooked workloads (TPC-H sample data) shaped to trip specific recommender rules, and tears them down on completion. Cost is ~0.85 credits (~$2.55 at standard $3/credit) per run; the command prints the estimate and waits for confirmation. Two extra grants required - `snowtuner bootstrap-sql` prints them under the "OPTIONAL: enable `snowtuner demo seed`" block.

Demo data is intentionally cooked - the workloads are engineered to trigger known recommendations. Real-account findings come from `snowtuner sync` against your actual history.

## Quick deploy to AWS

When you want snowtuner running somewhere other than your laptop:

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home?region=us-west-2#/stacks/quickcreate?templateURL=https://raw.githubusercontent.com/austinkjensen/snowtuner/main/deploy/snowtuner.cf.yaml&stackName=snowtuner)

Before clicking the button, push your Snowflake creds into Secrets Manager (one CLI command; multi-line PEM doesn't fit in the CloudFormation console form):

```bash
SECRET_ARN=$(
  jq -n \
    --arg account   "xy12345.us-west-2"   \
    --arg user      "SNOWTUNER_SVC"       \
    --arg warehouse "SNOWTUNER_WH"        \
    --arg role      "SNOWTUNER_ROLE"      \
    --rawfile pem   "$HOME/.snowtuner/snowtuner_rsa_key.p8" \
    '{account: $account, user: $user, warehouse: $warehouse, role: $role, private_key_pem: $pem}' \
  | aws secretsmanager create-secret \
      --name snowtuner/snowflake \
      --secret-string file:///dev/stdin \
      --region us-west-2 \
      --query ARN --output text
)
echo "$SECRET_ARN"
```

Paste `$SECRET_ARN` into the stack form. Wait ~10 minutes for CREATE_COMPLETE. The stack outputs an `aws ssm start-session` command: copy it, run it on your laptop, hit `http://localhost:8770`. No public URL, no certificate management, no second vendor.

Cost: ~$30/mo (t3.medium + 30GB gp3 + 1 secret). Full runbook + troubleshooting in [docs/aws-deploy.md](docs/aws-deploy.md). If you outgrow the SSM-port-forward access pattern, the "Upgrade paths" section there walks through Tailscale, Cloudflare Tunnel, and ALB+ACM as additive options.

## Going autonomous (one warehouse at a time)

```bash
# 1. Pick a warehouse you trust the algorithm on, set the threshold for autopilot
snowtuner autonomous enable ALTER_WAREHOUSE ETL_WH --threshold 0.85

# (paste the printed GRANT into Snowsight as ACCOUNTADMIN, ~10 seconds)

# 2. Re-run with autonomous on (safe to schedule via cron)
snowtuner run --auto

# 3. Audit + rollback
snowtuner autonomous applications
snowtuner autonomous rollback <id>     # if needed
```

The first time autonomous mode is asked to apply on a warehouse, the SQL it would run is printed and re-checked against the configured confidence threshold and cooldown. If anything looks off in the audit log, one rollback command undoes it.

## Talking to it from Claude

snowtuner ships with an Admin MCP server. After running `snowtuner api` (the HTTP service), point Claude Desktop at the local MCP:

```json
// ~/Library/Application Support/Claude/claude_desktop_config.json
{
  "mcpServers": {
    "snowtuner": {
      "command": "/path/to/.venv/bin/snowtuner",
      "args": ["mcp"],
      "env": { "SNOWTUNER_API_URL": "http://127.0.0.1:8770" }
    }
  }
}
```

Then ask Claude things like *"What recommendations are open?"*, *"What's the audit log say about POSIT_TEAM?"*, *"Roll back application #2."*

Tools exposed (42 total):

- **Status / discovery**: `get_status`, `list_warehouses`, `list_recommenders`, `get_credentials_status`, `get_schema_drift`
- **Recommendations**: `list_recommendations`, `get_recommendation`, `accept_recommendation`, `reject_recommendation`
- **Autonomous mode**: `list_autonomous_config`, `enable_autonomous`, `disable_autonomous`, `reset_autonomous_circuit`, `list_autonomous_applications`, `rollback_autonomous_application`
- **Experiments**: `list_experiment_recipes`, `list_experiments`, `get_experiment`, `list_experiment_runs`, `propose_experiment`, `propose_benchmark_experiment`, `accept_experiment`, `reject_experiment`, `run_experiment`, `abort_experiment`, `remove_sampled_query_from_experiment`, `backfill_experiment_metrics`
- **Query groups + queries**: `list_query_groups`, `get_query_group`, `get_query_group_members`, `delete_query_group`, `search_queries`, `get_query_detail`, `get_query_facets`
- **Orchestration**: `run_orchestrator`, `run_sync`, `run_backfill`, `get_automation_status`, `run_automation_now`

## How recommenders decide

Both built-in recommenders are **principled statistics, not learned ML models**. They compute distributions on your own data and pick decisions that minimize a cost function. No training data, no labels, no third-party hosted model.

- **`auto_suspend_survival_tuner`** finds the AUTO_SUSPEND value that minimizes the expected cost per cycle: `min(T, AS) + C*1{T > AS}`, where `T` is the observed reactivation gap and `C` is the cold-start cost (sized by warehouse class). Equivalent to setting AS where the hazard rate hits `1/C`.
- **`rule_based_right_sizer`** applies four ordered rules: any remote spill -> +1 size, >= 20% local spill -> +1 size, average queue >= 5s with sample size >= 30 -> +1 size, p99 <= 1s on a quiet warehouse with >= 100 queries -> -1 size.

Both implementations are short single-file modules under `src/snowtuner/recommenders/builtins/`. See [docs/architecture.md](docs/architecture.md) for the design and [docs/recommenders.md](docs/recommenders.md) for how to add your own.

## Roadmap

**v0.2 (in flight):** the replay-experiments framework and the Queries explorer are shipped. The next slices in this line:

- **Saved query groups**: static (snapshot) and dynamic (filter-defined, live) groups, feeding experiments and ad-hoc analysis.
- **Benchmark-style experiments**: "compare N configurations against this workload" (no implicit production-warehouse control), distinct from the tuning-experiment flow.
- **From-scratch recipe builder**: arm-by-arm UI for user-defined experiment templates.
- **Multi-cluster tuning** (`MIN_CLUSTER_COUNT`, `MAX_CLUSTER_COUNT`, `SCALING_POLICY`).

**v0.3+ (strategic direction):** the longer-term play is a **Snowflake-compatible proxy + caching layer**, multi-platform (Snowflake + Databricks), with BYOC deployment for regulated industries. The proxy unlocks in-vivo experiments on live traffic (with the experiments framework providing the statistical inference) and structurally larger savings via query routing / caching, rather than warehouse-config tuning alone.

What stays explicitly out of scope:

- **Automatic query rewriting** (the correctness surface is enormous; we'll surface suggestions, not auto-apply).
- **Cross-platform arbitrage** (Databricks federation etc.) at the OSS tier. That's paid-tier territory once we get there.

## Architecture

Three logical layers, each independently testable:

```
Snowflake -- (sync) --> raw.*  -- (transform) --> features.*  -- (recommenders) --> app.recommendations
                                                                                         |
                                                              advisory  <----------------+
                                                              autonomous --> Snowflake (ALTER WAREHOUSE)
```

The **AutomationLoop** runs that whole chain on a configurable interval (`SNOWTUNER_AUTOMATION_INTERVAL`). Manual triggers (`snowtuner run`, `POST /orchestrator/run`, `snowtuner-mcp run_orchestrator`) all hit the same code path.

See [docs/architecture.md](docs/architecture.md) for the full module breakdown and a Mermaid diagram, [docs/schema.md](docs/schema.md) for the DuckDB ERD, and [docs/configuration.md](docs/configuration.md) for every env var + CLI flag.

## Security model

- snowtuner runs on **your** infrastructure. No outbound calls except to your Snowflake account.
- Auth uses a **dedicated `SNOWTUNER_SVC` service user** with `TYPE=SERVICE` and **RSA key-pair auth**. Private key lives at `~/.snowtuner/snowtuner_rsa_key.p8` (mode 0600).
- Privileges granted to `SNOWTUNER_ROLE` for advisory mode are intentionally narrow:
  - `IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE`: read `ACCOUNT_USAGE`
  - `MONITOR USAGE ON ACCOUNT`: `SHOW WAREHOUSES` + account-wide observability
  - `USAGE / OPERATE / MONITOR ON SNOWTUNER_WH`: run snowtuner's own metadata queries
  - `DATABASE ROLE SNOWFLAKE.GOVERNANCE_VIEWER`: unredacted `query_text` in `QUERY_HISTORY` (without this, Snowflake redacts text for queries run by roles you haven't been granted, making them invisible to the explorer and unreplayable in experiments). Comment this out in `bootstrap-sql` output for stricter scoping; grant `MONITOR ON WAREHOUSE <name>` per warehouse instead.
- **Autonomous apply** requires an additional `GRANT MODIFY, OPERATE ON WAREHOUSE <name>` per warehouse, granted by an account admin one warehouse at a time.
- **Experiments framework** requires a separate `SNOWTUNER_EXP_SVC` user (created by `snowtuner bootstrap-sql --enable-experiments`) with `CREATE WAREHOUSE` privilege, so the engine can provision side-by-side test warehouses. The experiments user has no default warehouse: every operation is explicit.
- Credentials are stored in your OS keychain by default (`keyring`) with a plaintext-TOML fallback at `~/.snowtuner/creds.toml` (mode 0600) for headless environments.
- **Optional broader visibility:** `bootstrap-sql` emits a commented block to grant `MONITOR ON ALL WAREHOUSES` to the role. Without it, the Gen2/QAS detection recommenders silently skip warehouses the role isn't explicitly granted on. Default is opt-in (minimum-privilege); uncomment if you want fleet-wide recommendations.

## License

Apache-2.0. See [LICENSE](LICENSE).

## Contributing

This is currently in design-partner mode. Open an issue describing your Snowflake setup and what you wish snowtuner would tell you about it; we'll prioritize accordingly. PRs welcome for the documented [roadmap](#roadmap) items.
