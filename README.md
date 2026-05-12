# snowtuner

**Locally-hosted, open-source Snowflake cost & performance advisor.**

Connects to your Snowflake account (read-mostly, dedicated service user), ingests usage data into a local DuckDB, and produces concrete recommendations: change `AUTO_SUSPEND` on `ETL_WH` to 60s, downsize `OVERKILL_WH` from `LARGE` to `MEDIUM`, etc. Each recommendation comes with the SQL to run, a rollback statement, a rationale, and a confidence score. You review them in a terminal or web UI, accept the ones you trust, and reject the rest.

For warehouses where you trust the algorithm, you can flip a per-warehouse switch to **autonomous mode** — snowtuner applies new recommendations on its own, with a cooldown between changes and a circuit breaker that pauses autonomy after too many rollbacks. Every autonomous change records a rollback statement so you can revert with one click.

> **Status:** v0.1. Single-account, advisory-by-default, autonomous-mode opt-in. Not yet recommended for unattended production use. See the [v0.2 roadmap](#v02-roadmap) below.

---

## Why this exists

There are good commercial Snowflake optimizers — [espresso.ai](https://espresso.ai), [keebo.ai](https://keebo.ai), [greybeam.ai](https://greybeam.ai), and others — that do parts of what snowtuner does plus more. They're closed-source and require sending your usage metadata to a third-party service.

snowtuner exists to:

1. **Be the only credible OSS option** for "make my Snowflake bill smaller" and "make my queries faster," for accounts that don't want to send their query metadata anywhere.
2. **Run entirely on your infrastructure.** No outbound calls except to your Snowflake account.
3. **Make autonomous-apply a free-tier feature, not a paid one.** The narrative "advisory → autonomous" is the product, not the paywall.

## What it does today (v0.1)

| Capability | Status |
|---|---|
| Ingests `QUERY_HISTORY`, `WAREHOUSE_METERING_HISTORY`, `WAREHOUSE_EVENTS_HISTORY`, and `SHOW WAREHOUSES` into local DuckDB | ✅ |
| **AUTO_SUSPEND tuning** via cost-minimizing survival analysis on reactivation gaps | ✅ |
| **Warehouse right-sizing** (`WAREHOUSE_SIZE` only) via transparent rules + alternative spill-aware model | ✅ |
| Per-(action_type, warehouse) **autonomous-apply** with cooldown and circuit breaker | ✅ |
| One-click **rollback** of autonomous applications | ✅ |
| HTTP API (FastAPI), Streamlit UI, Admin MCP server (Claude Desktop) | ✅ |
| Service-user setup with RSA key-pair auth + `bootstrap-sql` generator | ✅ |
| Multi-cluster tuning (`MIN_CLUSTER_COUNT`, `MAX_CLUSTER_COUNT`, `SCALING_POLICY`) | v0.2 |
| `CREATE WAREHOUSE` + automatic query routing | v0.2 |
| Query result caching (DuckDB) + analyst-facing MCP for cost-aware text-to-SQL | v0.2 |
| Non-destructive query rewrite *suggestions* | v0.2+ |
| Multi-account fleet management, SLO-backed savings, managed hosting | paid tier |

## Quickstart (~5 minutes from install to first recommendation)

```bash
# 1. Install
git clone https://github.com/<your-org>/snowtuner
cd snowtuner
uv venv && source .venv/bin/activate
uv pip install -e '.[snowflake]'

# 2. Configure credentials (interactive — generates an RSA keypair)
snowtuner init   # prompts for account, defaults to SNOWTUNER_SVC service user

# 3. Print the bootstrap SQL and run it in Snowsight as ACCOUNTADMIN
snowtuner bootstrap-sql > bootstrap.sql
# Open Snowsight, paste bootstrap.sql, run.

# 4. Verify the connection
snowtuner verify

# 5. Pull data and run the recommenders
snowtuner sync --lookback-days 14
snowtuner run

# 6. Review
snowtuner list                 # terminal
snowtuner status               # ingestion + warehouse summary
snowtuner ui                   # Streamlit at http://127.0.0.1:8501
```

## Going autonomous (one warehouse at a time)

```bash
# 1. Pick a warehouse you trust the algorithm on, set the threshold for autopilot
snowtuner autonomous enable ALTER_WAREHOUSE ETL_WH --threshold 0.85

# (paste the printed GRANT into Snowsight as ACCOUNTADMIN — ~10 seconds)

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

Tools exposed (13 total): `get_status`, `list_warehouses`, `list_recommendations`, `get_recommendation`, `accept_recommendation`, `reject_recommendation`, `list_recommenders`, `list_autonomous_config`, `enable_autonomous`, `disable_autonomous`, `reset_autonomous_circuit`, `list_autonomous_applications`, `rollback_autonomous_application`.

## How recommenders decide

Both built-in recommenders are **principled statistics, not learned ML models**. They compute distributions on your own data and pick decisions that minimize a cost function — no training data, no labels, no third-party hosted model.

- **`auto_suspend_survival_tuner`** finds the AUTO_SUSPEND value that minimizes the expected cost per cycle: `min(T, AS) + C·1{T > AS}`, where `T` is the observed reactivation gap and `C` is the cold-start cost (sized by warehouse class). Equivalent to setting AS where the hazard rate hits `1/C`.
- **`rule_based_right_sizer`** applies four ordered rules: any remote spill → +1 size, ≥20% local spill → +1 size, average queue ≥ 5s with sample size ≥ 30 → +1 size, p99 ≤ 1s on a quiet warehouse with ≥ 100 queries → −1 size.

Both implementations are short single-file modules under `src/snowtuner/recommenders/builtins/` — see [docs/architecture.md](docs/architecture.md) for the design and [docs/recommenders.md](docs/recommenders.md) for how to add your own.

## Comparison

| | snowtuner (v0.1) | espresso.ai | keebo.ai | greybeam.ai |
|---|---|---|---|---|
| Open source | Apache 2.0 | — | — | — |
| Self-hosted | always | — | — | — |
| Single-account "make my Snowflake cheaper" | ✅ | ✅ | ✅ | ✅ |
| Auto-suspend tuning | ✅ | ✅ | ✅ | — |
| Right-sizing | ✅ (size only) | ✅ | ✅ | — |
| Autonomous apply | ✅ (free, opt-in) | ✅ | ✅ | — |
| Multi-cluster + scaling-policy tuning | v0.2 | ✅ | ✅ | — |
| Routing rules | v0.2 | ✅ | ✅ | — |
| Query result caching | v0.2 | — | — | ✅ |
| Cost-aware text-to-SQL via MCP | v0.2 | — | — | — |
| Multi-account, SLO guarantees, managed hosting | paid | ✅ | ✅ | ✅ |

## v0.2 roadmap

The marquee feature for v0.2 is **the analyst MCP server with query result caching** — the cost-aware text-to-SQL story. When a business user asks Claude (or any LLM agent) a question that translates to SQL, snowtuner sits between Claude and Snowflake:

- Caches frequent query results in a separate local DuckDB (5–10 min default TTL, per-rule overrides).
- Identifies cache candidates automatically (`features.query_families` already feeds this).
- Routes queries to the right warehouse based on user, role, or query family.
- Uses each business user's own Snowflake credentials for the actual query — RLS / masking policies pass through cleanly.

Other v0.2 work:

- **Multi-cluster tuning** (`MIN_CLUSTER_COUNT`, `MAX_CLUSTER_COUNT`, `SCALING_POLICY`).
- **`CREATE WAREHOUSE`** + automatic routing rule generation (coupled — a new warehouse without routes is dead weight).
- **Non-destructive rewrite suggestions** ("this predicate could move," "this `SELECT *` could be narrower"). Strictly advisory.
- **Per-warehouse `MODIFY` GRANT bootstrap** automated via an admin-mode MCP tool that runs the GRANT against an admin-credentialed session.

What stays explicitly out of scope:

- **Automatic query rewriting** (the correctness surface is enormous; we'll surface suggestions, not auto-apply).
- **Cross-platform arbitrage** (Databricks federation etc.) — paid-tier territory.

## Architecture

Three logical layers, each independently testable:

```
Snowflake ── (sync) ──> raw.*  ── (transform) ──> features.*  ── (recommenders) ──> app.recommendations
                                                                                         │
                                                              advisory  <───────────────┤
                                                              autonomous ──> Snowflake (ALTER WAREHOUSE)
```

See [docs/architecture.md](docs/architecture.md) for the full module breakdown and a Mermaid diagram, and [docs/schema.md](docs/schema.md) for the DuckDB ERD.

## Security model

- snowtuner runs on **your** infrastructure. No outbound calls except to your Snowflake account.
- Auth uses a **dedicated `SNOWTUNER_SVC` service user** with `TYPE=SERVICE` and **RSA key-pair auth**. Private key lives at `~/.snowtuner/snowtuner_rsa_key.p8` (mode 0600).
- Privileges granted to `SNOWTUNER_ROLE` for advisory mode are intentionally narrow: `IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE` (read `ACCOUNT_USAGE`), `MONITOR USAGE ON ACCOUNT`, and `USAGE/OPERATE/MONITOR ON SNOWTUNER_WH`.
- **Autonomous apply** requires an additional `GRANT MODIFY, OPERATE ON WAREHOUSE <name>` per warehouse, granted by an account admin one warehouse at a time.
- Credentials are stored in your OS keychain by default (`keyring`) with a plaintext-TOML fallback at `~/.snowtuner/creds.toml` (mode 0600) for headless environments.

## License

Apache-2.0. See [LICENSE](LICENSE).

## Contributing

This is currently in design-partner mode. Open an issue describing your Snowflake setup and what you wish snowtuner would tell you about it; we'll prioritize accordingly. PRs welcome for the documented [v0.2 roadmap](#v02-roadmap) items.
