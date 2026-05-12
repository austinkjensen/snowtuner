"""Streamlit UI — thin client over the snowtuner HTTP API.

The UI does not touch DuckDB directly.  Everything goes through the FastAPI
service at SFO_API_URL (default http://127.0.0.1:8770).  This avoids the
single-writer lock constraint of DuckDB and keeps the API as the one
integration surface — the UI is just one client among (eventually) many.
"""
from __future__ import annotations

import os

import httpx
import pandas as pd
import streamlit as st

from snowtuner import format as fmt


API_URL = os.environ.get("SNOWTUNER_API_URL", "http://127.0.0.1:8770").rstrip("/")


st.set_page_config(
    page_title="snowtuner",
    page_icon="❄️",
    layout="wide",
)


# ---- API client ----
@st.cache_resource
def _client() -> httpx.Client:
    return httpx.Client(base_url=API_URL, timeout=30.0)


def api_get(path: str, **params):
    r = _client().get(path, params=params)
    r.raise_for_status()
    return r.json()


def api_post(path: str, json: dict | None = None):
    r = _client().post(path, json=json or {})
    r.raise_for_status()
    return r.json()


def api_put(path: str, json: dict | None = None):
    r = _client().put(path, json=json or {})
    r.raise_for_status()
    return r.json()


def api_delete(path: str):
    r = _client().delete(path)
    r.raise_for_status()
    return r.json()


# ---- Connection check ----
try:
    _client().get("/health", timeout=3.0).raise_for_status()
except Exception as e:
    st.error(
        f"Can't reach the snowtuner API at **{API_URL}**.\n\n"
        f"Start it with `snowtuner api` in another terminal, or set "
        f"`SNOWTUNER_API_URL` to point at a running instance.\n\n"
        f"Error: `{e}`"
    )
    st.stop()


# ---- Sidebar ----
st.sidebar.title("❄️  snowtuner")
st.sidebar.caption(f"Connected to `{API_URL}`")

with st.sidebar.expander("Data", expanded=False):
    st.caption(
        "Run a sync (requires Snowflake creds) or seed synthetic data to try the UI."
    )
    days = st.number_input("Seed days", min_value=7, max_value=60, value=21)
    if st.button("Seed synthetic data", use_container_width=True):
        counts = api_post("/seed", {"days": int(days)})
        st.success("Seeded: " + ", ".join(f"{k}={v}" for k, v in counts.items()))

    if st.button("Run features + recommenders", use_container_width=True):
        report = api_post("/orchestrator/run", {"skip_sync": True})
        total_preds = sum(r["predictions_emitted"] for r in report["recommender_results"])
        st.success(f"Run complete.  {total_preds} recommendation(s) emitted.")

with st.sidebar.expander("Recommenders", expanded=False):
    recs_list = api_get("/recommenders")
    if not recs_list:
        st.caption("No recommenders discovered.")
    else:
        for r in recs_list:
            cols = st.columns([3, 1])
            cols[0].markdown(f"**{r['name']}**  \n"
                             f"<span style='color:#888;font-size:0.85em'>"
                             f"{r['action_type']} · v{r['version']}</span>",
                             unsafe_allow_html=True)
            if cols[1].button("Run", key=f"run_{r['name']}", use_container_width=True):
                result = api_post(f"/recommenders/{r['name']}/run", {"skip_sync": True})
                st.success(f"{r['name']}: {result['predictions_emitted']} proposal(s)")

status_filter = st.sidebar.selectbox(
    "Status",
    options=["PROPOSED", "ACCEPTED", "REJECTED", "APPLIED", "ROLLED_BACK", "SUPERSEDED"],
    index=0,
)


# ---- Main: tabs ----
recs_tab, auto_tab = st.tabs(["📋 Recommendations", "🤖 Autonomous"])


# ─── Recommendations tab ───────────────────────────────────────────
with recs_tab:
    st.header("Recommendations")

    recs = api_get("/recommendations", status=status_filter, limit=200)

    # Summary strip
    proposed = api_get("/recommendations", status="PROPOSED", limit=500)
    accepted = api_get("/recommendations", status="ACCEPTED", limit=500)
    rejected = api_get("/recommendations", status="REJECTED", limit=500)
    total_savings = sum(
        (r["expected_impact"].get("credits_delta_daily") or 0.0) for r in proposed
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Open proposals", len(proposed))
    c2.metric("Accepted", len(accepted))
    c3.metric("Rejected", len(rejected))
    c4.metric(
        "Est. daily credit delta", fmt.credits_delta(total_savings),
        help="Sum of expected_impact.credits_delta_daily across open proposals. "
             "Negative = credits saved.  '≈0' means rounds below 0.01.",
    )
    st.divider()

    if not recs:
        st.info(f"No recommendations with status = {status_filter}.")
    else:
        rows = []
        for r in recs:
            impact = r["expected_impact"]
            rows.append({
                "id": r["id"],
                "type": r["action_type"],
                "target": r["target_resource"] or "",
                "proposal": r["preview"].splitlines()[-1],
                "credits/day": fmt.credits_delta(impact.get("credits_delta_daily")),
                "confidence": impact.get("confidence"),
                "generated_by": r["generated_by"],
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        rec_id = st.selectbox(
            "Open a recommendation",
            options=[r["id"] for r in recs],
            format_func=lambda x: f"#{x}",
        )
        rec = next(r for r in recs if r["id"] == rec_id)

        st.subheader(f"#{rec['id']} · {rec['action_type']}")
        st.caption(
            f"Generated by `{rec['generated_by']}` · target `{rec['target_resource']}`"
        )

        d1, d2 = st.columns([2, 1])
        with d1:
            st.markdown("**Preview**")
            st.code(rec["preview"], language="text")

            st.markdown("**SQL to run**")
            st.code(rec["sql"], language="sql")

            if rec.get("rollback_sql"):
                with st.expander("Rollback SQL"):
                    st.code(rec["rollback_sql"], language="sql")

            st.markdown("**Rationale**")
            st.write(rec["rationale"])

            st.markdown("**Evidence**")
            for ev in rec["evidence"]:
                val = (
                    "" if ev.get("value") is None
                    else f"  —  **{ev.get('metric') or 'value'}**: `{ev['value']}`"
                )
                st.markdown(f"- *{ev['kind']}* · {ev['description']}{val}")

        with d2:
            st.markdown("**Expected impact**")
            impact = rec["expected_impact"]
            label, value_text = fmt.credits_savings_for_metric(
                impact.get("credits_delta_daily")
            )
            st.metric(label, value_text)
            st.metric("Confidence", f"{(impact.get('confidence') or 0):.0%}")
            if impact.get("notes"):
                st.caption(impact["notes"])

            st.divider()

            if rec["status"] == "PROPOSED":
                note = st.text_input("Note (optional)", key=f"note_{rec['id']}")
                b1, b2 = st.columns(2)
                if b1.button("✅ Accept", key=f"acc_{rec['id']}",
                             use_container_width=True):
                    api_post(f"/recommendations/{rec['id']}/accept",
                             {"note": note or None})
                    st.success("Accepted.")
                    st.rerun()
                if b2.button("❌ Reject", key=f"rej_{rec['id']}",
                             use_container_width=True):
                    api_post(f"/recommendations/{rec['id']}/reject",
                             {"note": note or None})
                    st.warning("Rejected.")
                    st.rerun()
            else:
                st.info(f"Status: **{rec['status']}**")


# ─── Autonomous tab ────────────────────────────────────────────────
with auto_tab:
    st.header("Autonomous mode")
    st.caption(
        "Per (action type, warehouse) opt-in for autonomous apply.  Use `*` for "
        "the warehouse to set the catch-all default for an action type."
    )

    # Existing config
    cfg_rows = api_get("/autonomous/config")
    if not cfg_rows:
        st.info(
            "No autonomous configuration yet.  Add a row below to enable "
            "autonomous apply for a specific (action type, warehouse)."
        )
    else:
        st.subheader("Active configuration")
        for cfg in cfg_rows:
            with st.container(border=True):
                cols = st.columns([3, 2, 2, 2, 2])
                cols[0].markdown(
                    f"**{cfg['action_type']}** on "
                    f"`{cfg['warehouse_name']}`"
                    + ("  *(default)*" if cfg["warehouse_name"] == "*" else "")
                )
                cols[1].metric(
                    "Status",
                    "🟢 ON" if cfg["enabled"] else "off",
                    label_visibility="collapsed",
                )
                cols[2].metric("Threshold", f"{cfg['confidence_threshold']:.2f}",
                               label_visibility="collapsed")
                cols[3].metric("Cooldown",
                               f"{cfg['cooldown_hours']}h",
                               label_visibility="collapsed")
                circuit = cfg.get("circuit_open_until")
                cols[4].markdown(
                    f"⚠️ **Circuit open**" if circuit else "✓ Circuit closed"
                )

                action_cols = st.columns(4)
                if cfg["enabled"]:
                    if action_cols[0].button("Disable",
                                              key=f"dis_{cfg['action_type']}_{cfg['warehouse_name']}"):
                        api_put(
                            f"/autonomous/config/{cfg['action_type']}/{cfg['warehouse_name']}",
                            {"enabled": False},
                        )
                        st.rerun()
                else:
                    if action_cols[0].button("Enable",
                                              key=f"en_{cfg['action_type']}_{cfg['warehouse_name']}"):
                        api_put(
                            f"/autonomous/config/{cfg['action_type']}/{cfg['warehouse_name']}",
                            {"enabled": True},
                        )
                        st.rerun()
                if circuit:
                    if action_cols[1].button("Reset circuit",
                                              key=f"rc_{cfg['action_type']}_{cfg['warehouse_name']}"):
                        api_post(
                            f"/autonomous/config/{cfg['action_type']}/{cfg['warehouse_name']}/reset-circuit"
                        )
                        st.rerun()
                if action_cols[3].button("Delete",
                                          key=f"del_{cfg['action_type']}_{cfg['warehouse_name']}"):
                    api_delete(
                        f"/autonomous/config/{cfg['action_type']}/{cfg['warehouse_name']}"
                    )
                    st.rerun()

    # Add a new config row
    with st.expander("Add / update config row", expanded=not cfg_rows):
        with st.form("autonomous_upsert_form"):
            f_action_type = st.text_input(
                "Action type", value="ALTER_WAREHOUSE",
                help="The action type the rule covers (e.g. ALTER_WAREHOUSE).",
            )
            f_warehouse = st.text_input(
                "Warehouse name", value="",
                help="The warehouse to enable autonomous apply on, or `*` for the catch-all default.",
            )
            f_enabled = st.checkbox("Enabled", value=True)
            f_threshold = st.slider(
                "Confidence threshold", min_value=0.5, max_value=1.0,
                value=0.85, step=0.05,
            )
            f_cooldown = st.number_input(
                "Cooldown (hours)", min_value=0, max_value=168, value=24, step=1,
            )
            f_max_rb = st.number_input(
                "Max rollbacks per week (circuit breaker)",
                min_value=1, max_value=20, value=2, step=1,
            )
            submitted = st.form_submit_button("Save")
            if submitted:
                if not f_warehouse:
                    st.error("Warehouse name is required (use `*` for catch-all).")
                else:
                    api_put(
                        f"/autonomous/config/{f_action_type}/{f_warehouse}",
                        {
                            "enabled": f_enabled,
                            "confidence_threshold": f_threshold,
                            "cooldown_hours": int(f_cooldown),
                            "max_rollbacks_per_week": int(f_max_rb),
                        },
                    )
                    st.success("Saved.")
                    st.rerun()

    # Applications log
    st.divider()
    st.subheader("Applications log")
    apps = api_get("/autonomous/applications", limit=20)
    if not apps:
        st.caption("No autonomous applications recorded yet.")
    else:
        for app in apps:
            with st.container(border=True):
                cols = st.columns([1, 3, 2, 2])
                cols[0].markdown(f"**#{app['id']}**")
                cols[1].markdown(
                    f"`{app['action_type']}` on **{app['warehouse_name'] or '—'}**"
                )
                state_color = {
                    "APPLIED": "🟢", "ROLLED_BACK": "🟡", "FAILED": "🔴",
                }.get(app["state"], "⚪")
                cols[2].markdown(f"{state_color} {app['state']}")
                cols[3].caption(app["applied_at"])
                with st.expander("Details"):
                    st.code(app["applied_sql"], language="sql")
                    if app.get("rollback_sql"):
                        st.markdown("**Rollback available**")
                        st.code(app["rollback_sql"], language="sql")
                    if app.get("error"):
                        st.error(app["error"])
                if app["state"] == "APPLIED" and app.get("rollback_sql"):
                    if st.button("Roll back", key=f"rollback_{app['id']}"):
                        try:
                            api_post(f"/autonomous/applications/{app['id']}/rollback")
                            st.success(f"Rolled back #{app['id']}")
                            st.rerun()
                        except httpx.HTTPStatusError as e:
                            st.error(f"Rollback failed: {e.response.text}")
