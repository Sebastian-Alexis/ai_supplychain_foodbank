"""FoodShock Streamlit app (PLAN.md §13): the five operator views.

1. Exposure queue    -- match states + evidence + clearance actions
2. Impact dashboard  -- match-state inventory, POs at risk, 7-day projection
3. Recovery plan     -- recommended vs do-nothing, approval, drafted comms
4. Supply-chain graph-- derived lineage (event -> ... -> pantry)
5. Geographic map    -- sites and flows; operational evidence only

DB resolution: $FOODSHOCK_DB if set (shared file, single-operator demo);
otherwise each browser session works on its own temp COPY of data/foodshock.db
so concurrent viewers (Streamlit Community Cloud) can replay/reset/approve
without clobbering each other. All demo data is synthetic (PLAN.md §3).

Run:  streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from html import escape
import tempfile
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from foodshock.agent import RecallResponseAgent
from foodshock.datagen import generate
from foodshock.db import DEFAULT_DB, get_conn, rows
from foodshock.engine import (HORIZON_DAYS, approve_plan, build_plans,
                              days_of_supply, project_supply, propagate,
                              review_match)
from foodshock.extraction import ExtractionUnavailable
from foodshock.schemas import SCENARIO_LABELS, SCENARIOS
from foodshock.viz import (STATE_COLORS, graph_figure, latest_event_id,
                           latest_plan_ids, lineage_graph, map_arcs, map_deck,
                           map_points)

st.set_page_config(page_title="FoodShock | Recall response", layout="wide")

ALLOW_LLM = os.environ.get("FOODSHOCK_LIVE_LLM", "") == "1"
NOTICE_PATH = Path(__file__).parent / "data" / "notice_ecoli_onions.txt"
DEMO_EVENT = "FDA-DEMO-2026-001"
OPERATOR = "operator (streamlit)"

SAFETY_CAPTION = ("Confirmed recalled or quarantined lots and canceled POs are excluded "
                  "from every scenario and every plan; no toggle re-includes them (PLAN.md §10).")
STATE_ORDER = ["confirmed", "probable", "possible", "unknown", "not_matched"]

def _inject_theme() -> None:
    st.markdown(
        """
        <style>
        :root {
          --fs-ink: #132524;
          --fs-pine: #143b37;
          --fs-teal: #1d6b62;
          --fs-coral: #b94735;
          --fs-cream: #f4f0e6;
          --fs-paper: #fffdf8;
          --fs-line: #d9d2c2;
        }
        [data-testid="stAppViewContainer"] {
          background: var(--fs-cream);
          color: var(--fs-ink);
        }
        [data-testid="stHeader"] { background: rgba(244, 240, 230, 0.92); }
        [data-testid="stSidebar"] {
          background: var(--fs-pine);
          border-right: 1px solid #28514d;
        }
        [data-testid="stSidebar"] h1,
        [data-testid="stSidebar"] h2,
        [data-testid="stSidebar"] h3,
        [data-testid="stSidebar"] p,
        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
          color: #f7f2e8;
        }
        [data-testid="stSidebar"] hr { border-color: #3f625e; }
        .block-container { max-width: 1380px; padding-top: 2rem; }
        h1, h2, h3 {
          color: var(--fs-ink);
          letter-spacing: -0.025em;
        }
        .fs-brand {
          display: flex;
          align-items: center;
          gap: 0.75rem;
          margin: 0.25rem 0 0.75rem;
        }
        .fs-monogram {
          display: grid;
          place-items: center;
          width: 2.25rem;
          height: 2.25rem;
          border: 1px solid #8eb8b1;
          color: #fffdf8;
          font-weight: 800;
          letter-spacing: -0.08em;
        }
        .fs-brand-name {
          color: #fffdf8;
          font-size: 1.35rem;
          font-weight: 800;
          letter-spacing: -0.03em;
        }
        .fs-hero {
          background: var(--fs-ink);
          border-left: 6px solid var(--fs-coral);
          color: #fffdf8;
          padding: 1.45rem 1.65rem 1.35rem;
          margin: 0 0 1.15rem;
        }
        .fs-eyebrow {
          color: #91c9c1;
          font-size: 0.72rem;
          font-weight: 800;
          letter-spacing: 0.14em;
          text-transform: uppercase;
        }
        .fs-hero h1 {
          color: #fffdf8;
          font-size: clamp(2rem, 4vw, 3.2rem);
          line-height: 0.98;
          margin: 0.4rem 0 0.55rem;
        }
        .fs-hero p {
          color: #d9e7e4;
          font-size: 1.02rem;
          line-height: 1.5;
          margin: 0;
          max-width: 58rem;
        }
        .fs-rail {
          display: flex;
          flex-wrap: wrap;
          gap: 0.5rem 1.35rem;
          border-top: 1px solid #34524f;
          margin-top: 1rem;
          padding-top: 0.75rem;
          color: #b9cbc8;
          font-size: 0.78rem;
          letter-spacing: 0.03em;
        }
        .fs-rail strong { color: #fffdf8; }
        [data-testid="stMetric"] {
          background: var(--fs-paper);
          border: 1px solid var(--fs-line);
          border-top: 3px solid var(--fs-teal);
          padding: 0.8rem 0.95rem;
        }
        [data-testid="stMetricValue"] { color: var(--fs-ink); font-weight: 800; }
        .stButton > button[kind="primary"] {
          background: var(--fs-coral);
          border-color: var(--fs-coral);
          color: white;
          font-weight: 750;
        }
        .stButton > button { border-radius: 2px; font-weight: 700; }
        [data-testid="stSidebar"] .stButton > button {
          background: #fffdf8;
          border-color: #fffdf8;
          color: var(--fs-ink);
        }
        [data-testid="stSidebar"] .stButton > button p {
          color: var(--fs-ink);
        }
        [data-baseweb="tab-list"] {
          gap: 0.35rem;
          border-bottom: 1px solid var(--fs-line);
        }
        [data-baseweb="tab"] {
          background: transparent;
          border-radius: 0;
          padding-left: 0.75rem;
          padding-right: 0.75rem;
        }
        [data-baseweb="tab"][aria-selected="true"] {
          border-bottom: 3px solid var(--fs-coral);
        }
        [data-testid="stDataFrame"],
        [data-testid="stPlotlyChart"] {
          background: var(--fs-paper);
          border: 1px solid var(--fs-line);
        }
        code { color: #185d56; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _hero(eyebrow: str, title: str, subtitle: str) -> None:
    st.markdown(
        f"""
        <section class="fs-hero">
          <div class="fs-eyebrow">{escape(eyebrow)}</div>
          <h1>{escape(title)}</h1>
          <p>{escape(subtitle)}</p>
          <div class="fs-rail">
            <span><strong>CONTROL</strong> Human approval required</span>
            <span><strong>WINDOW</strong> Seven-day supply projection</span>
            <span><strong>DATA</strong> Synthetic operations · no client PII</span>
          </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------ session DB

def _session_db_path() -> Path:
    """$FOODSHOCK_DB -> shared file; else a per-session temp copy of the repo DB."""
    env = os.environ.get("FOODSHOCK_DB")
    if env:
        return Path(env)
    if "db_path" not in st.session_state:
        fd, tmp = tempfile.mkstemp(prefix="foodshock-", suffix=".db")
        os.close(fd)
        if DEFAULT_DB.exists():
            shutil.copy(DEFAULT_DB, tmp)
        st.session_state.db_path = tmp
        st.session_state.db_mode = ("session copy of data/foodshock.db"
                                    if DEFAULT_DB.exists() else "session-local (seeded here)")
    return Path(st.session_state.db_path)


def _scenario_ready(conn: sqlite3.Connection) -> bool:
    ok = rows(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='suppliers'")
    return bool(ok) and rows(conn, "SELECT COUNT(*) c FROM suppliers")[0]["c"] > 0


def _replay_incident(conn: sqlite3.Connection) -> None:
    """Reset scenario and run the canned recall through the agent (§14 arc)."""
    generate(conn)
    agent = RecallResponseAgent(conn, allow_llm=ALLOW_LLM)
    agent.run(NOTICE_PATH.read_text(), event_id=DEMO_EVENT,
              source_url="https://example.invalid/recall")


def _recompute(conn: sqlite3.Connection, event_id: str) -> None:
    """After any clearance action: refresh feasibility and supersede the plan
    pair so no view presents a plan built on a stale pool (PLAN.md §11)."""
    propagate(conn, event_id)
    build_plans(conn)


# ------------------------------------------------------------------ helpers

def _fmt_dos(v: float | None) -> str:
    return f"{HORIZON_DAYS}+" if v is None else f"{v:g}"


def _badge(state: str) -> str:
    color = STATE_COLORS.get(state, "#5d6d7e")
    return (f'<span style="background:{color};color:white;padding:1px 8px;white-space:nowrap;'
            f'border-radius:8px;font-size:0.85em">{state}</span>')


def _metrics(plan_row: dict) -> dict:
    return json.loads(plan_row["metrics_json"]) if plan_row.get("metrics_json") else {}


def _latest_run_id(conn) -> str | None:
    r = rows(conn, "SELECT run_id FROM agent_transcript ORDER BY id DESC LIMIT 1")
    return r[0]["run_id"] if r else None


def _transcript_lines(conn, run_id: str) -> list[str]:
    out = []
    for t in rows(conn, "SELECT * FROM agent_transcript WHERE run_id=? ORDER BY seq", (run_id,)):
        c = json.loads(t["content_json"])
        if t["kind"] == "tool_call":
            out.append(f"{t['seq']:>3} {t['phase']:<11} -> {t['name']}({json.dumps(c['args'])})")
        elif t["kind"] == "tool_result":
            n = c["result"].get("rows") if isinstance(c["result"], dict) else None
            out.append(f"{t['seq']:>3} {t['phase']:<11} <- {t['name']}"
                       + (f": {n} row(s)" if n is not None else ""))
        elif t["kind"] == "gap":
            out.append(f"{t['seq']:>3} {t['phase']:<11} ?? {c['question']}")
        else:
            out.append(f"{t['seq']:>3} {t['phase']:<11} :: [{c['method']}] {c['text']}")
    return out


def _gaps(conn, run_id: str) -> list[str]:
    return [json.loads(t["content_json"])["question"] for t in rows(
        conn, "SELECT * FROM agent_transcript WHERE run_id=? AND kind='gap' ORDER BY seq",
        (run_id,))]


# ------------------------------------------------------------------ views

def view_exposure(conn, event_id: str) -> None:
    ev = rows(conn, "SELECT * FROM recall_events WHERE event_id=?", (event_id,))[0]
    ext = json.loads(ev["extraction_json"]) if ev["extraction_json"] else {}

    left, right = st.columns([1, 1])
    with left:
        st.subheader("Incident facts")
        st.markdown(
            f"**{ev['event_id']}** · authority {ev['authority']} · status {ev['status']} · "
            f"extraction {ev['extraction_method'] or '-'} "
            f"(confidence {ev['extraction_confidence'] or 0:g})")
        facts = {
            "Products": ", ".join(ext.get("products", [])) or "—",
            "Suppliers": ", ".join(ext.get("supplier_names", [])) or "—",
            "Facilities": ", ".join(ext.get("facility_names", [])) or "—",
            "Lot codes": ", ".join(ext.get("lot_codes", [])) or "—",
            "UPCs": ", ".join(ext.get("upcs", [])) or "—",
            "Production window": (f"{ext.get('production_date_start') or '?'} to "
                                  f"{ext.get('production_date_end') or '?'}"),
            "Regions": ", ".join(ext.get("distribution_regions", [])) or "—",
            "Pathogen": ext.get("pathogen") or "—",
            "Action required": ext.get("action_required") or "—",
        }
        st.table(pd.DataFrame({"fact": facts.keys(), "extracted value": facts.values()}))
        if ev["human_confirmed"]:
            st.success("Notice extraction human-confirmed.")
        elif st.button("Confirm extraction against source notice"):
            conn.execute("UPDATE recall_events SET human_confirmed=1 WHERE event_id=?", (event_id,))
            conn.commit()
            st.rerun()
    with right:
        st.subheader("Source excerpts (verbatim provenance)")
        exc = ext.get("excerpts", {})
        if exc:
            st.dataframe(pd.DataFrame({"field": exc.keys(), "supporting quote": exc.values()}),
                         hide_index=True, use_container_width=True)
        with st.expander("Raw notice text"):
            st.text(ev["raw_text"])

    st.divider()
    st.subheader("Exposure queue")
    st.caption("Clearance below is a food-safety review action and the ONLY path back into "
               "the allocatable pool. Confirmed matches have no clearance action here; "
               "disposition of confirmed recalled product follows the recall process.")

    matches = rows(conn, """
        SELECT m.*, l.quantity_lb lot_lb, l.status lot_status, l.expires_at,
               lp.name lot_product, po.quantity_lb po_lb, po.status po_status,
               po.expected_delivery, pp.name po_product
        FROM matches m
        LEFT JOIN inventory_lots l ON m.target_type='lot' AND m.target_id=l.lot_id
        LEFT JOIN products lp ON l.product_id=lp.product_id
        LEFT JOIN purchase_orders po ON m.target_type='po' AND m.target_id=po.po_id
        LEFT JOIN products pp ON po.product_id=pp.product_id
        WHERE m.event_id=? ORDER BY m.match_id""", (event_id,))
    show_nm = st.toggle("show not_matched records", value=False)
    shown = [m for m in matches if show_nm or m["state"] != "not_matched"]
    shown.sort(key=lambda m: (STATE_ORDER.index(m["state"]), -(m["score"] or 0)))
    if not shown:
        st.info("No match rows for this incident.")
    for m in shown:
        if m["target_type"] == "lot":
            head = (f"{m['target_id']} · {m['lot_product']} · {m['lot_lb']:g} lb · "
                    f"lot status {m['lot_status']} · expires {m['expires_at'][:10]}")
        else:
            head = (f"{m['target_id']} · {m['po_product']} · {m['po_lb']:g} lb · "
                    f"PO status {m['po_status']} · ETA {m['expected_delivery'][:10]}")
        cols = st.columns([1.1, 4.1, 1.5, 1.4, 1.4])
        cols[0].markdown(_badge(m["state"]), unsafe_allow_html=True)
        with cols[1].expander(head):
            ev_j = json.loads(m["evidence_json"])
            st.markdown("\n".join(f"- {r}" for r in ev_j.get("reasons", [])) or "_no reasons_")
            if ev_j.get("matched_fields"):
                st.markdown("**Matched fields:** " + ", ".join(
                    f"`{k}` = {v}" for k, v in ev_j["matched_fields"].items()))
            for f, q in ev_j.get("notice_excerpts", {}).items():
                st.caption(f"notice [{f}]: “{q}”")
        cols[2].markdown(f"tier {m['tier'] or '—'} · score {m['score'] or 0:.2f}")
        reviewable = (not m["reviewed"]) and m["state"] in ("probable", "possible", "unknown")
        if m["reviewed"]:
            cols[3].markdown(f"reviewed: **{m['review_action']}**")
        elif reviewable:
            if cols[3].button("Clear", key=f"clr{m['match_id']}",
                              help="Return to pool: evidence reviewed, not affected"):
                review_match(conn, m["match_id"], "cleared", OPERATOR)
                _recompute(conn, event_id)
                st.rerun()
            if cols[4].button("Quarantine", key=f"qtn{m['match_id']}",
                              help="Escalate to confirmed-equivalent exclusion"):
                review_match(conn, m["match_id"], "quarantined", OPERATOR)
                _recompute(conn, event_id)
                st.rerun()
        elif m["state"] == "confirmed":
            cols[3].caption("recall process governs")

    run_id = _latest_run_id(conn)
    if run_id:
        gaps = _gaps(conn, run_id)
        if gaps:
            open_n = rows(conn, "SELECT COUNT(*) c FROM matches WHERE event_id=? AND reviewed=0 "
                                "AND state IN ('probable','possible','unknown')", (event_id,))[0]["c"]
            st.subheader(f"Agent-flagged review questions at ingest ({len(gaps)})")
            st.caption(f"Immutable transcript log from the agent run; current review state "
                       f"lives in the queue above ({open_n} match(es) still unreviewed).")
            for g in gaps:
                st.markdown(f"- {g}")


def view_impact(conn, event_id: str) -> None:
    q = rows(conn, "SELECT COALESCE(SUM(quantity_lb),0) lb, COUNT(*) n FROM inventory_lots "
                   "WHERE status IN ('quarantine_proposed','quarantined')")[0]
    inf = rows(conn, "SELECT COUNT(*) n, COALESCE(SUM(quantity_lb),0) lb "
                     "FROM distribution_plans WHERE status='infeasible'")[0]
    at_risk = rows(conn, "SELECT COUNT(*) n FROM purchase_orders WHERE status='at_risk'")[0]["n"]
    review_lb = rows(conn, """
        SELECT COALESCE(SUM(l.quantity_lb),0) lb FROM inventory_lots l WHERE l.status='available'
        AND EXISTS (SELECT 1 FROM matches m WHERE m.target_type='lot' AND m.target_id=l.lot_id
                    AND m.reviewed=0 AND m.state IN ('probable','possible','unknown'))""")[0]["lb"]

    dos = {s: days_of_supply(project_supply(conn, s)) for s in SCENARIOS}
    c = st.columns(5)
    c[0].metric("Quarantine proposed/held", f"{q['lb']:g} lb", f"{q['n']} lot(s)", delta_color="off")
    c[1].metric("Awaiting human review", f"{review_lb:g} lb")
    c[2].metric("Inbound POs at risk", at_risk)
    c[3].metric("Infeasible distribution lines", inf["n"], f"{inf['lb']:g} lb planned", delta_color="off")
    c[4].metric("Produce days of supply (conservative)", _fmt_dos(dos["conservative"].get("produce")),
                f"optimistic {_fmt_dos(dos['optimistic'].get('produce'))}", delta_color="off")
    st.caption(SAFETY_CAPTION)

    st.divider()
    left, right = st.columns([1, 1])

    with left:
        st.subheader("Inventory by effective match state")
        inv = rows(conn, """
            SELECT l.lot_id, l.quantity_lb, l.status,
                   (SELECT m.state FROM matches m WHERE m.target_type='lot' AND m.target_id=l.lot_id
                     AND m.reviewed=0 ORDER BY CASE m.state WHEN 'confirmed' THEN 4 WHEN 'probable' THEN 3
                     WHEN 'possible' THEN 2 WHEN 'unknown' THEN 1 ELSE 0 END DESC LIMIT 1) mstate
            FROM inventory_lots l""")
        def _bucket(r):
            if r["status"] in ("quarantine_proposed", "quarantined"):
                return "quarantined/proposed"
            if r["status"] == "cleared":
                return "cleared (reviewed)"
            return r["mstate"] if r["mstate"] and r["mstate"] != "not_matched" else "unaffected"
        df = pd.DataFrame([{"bucket": _bucket(r), "lb": r["quantity_lb"]} for r in inv])
        agg = df.groupby("bucket", as_index=False)["lb"].sum()
        order = ["quarantined/proposed", "probable", "possible", "unknown",
                 "cleared (reviewed)", "unaffected"]
        colors = {"quarantined/proposed": STATE_COLORS.get("confirmed", "#922b21"),
                  "probable": STATE_COLORS.get("probable", "#b9770e"),
                  "possible": STATE_COLORS.get("possible", "#b7950b"),
                  "unknown": STATE_COLORS.get("unknown", "#5d6d7e"),
                  "cleared (reviewed)": STATE_COLORS.get("cleared", "#1e8449"),
                  "unaffected": "#85929e"}
        fig = px.bar(agg, x="bucket", y="lb", color="bucket",
                     category_orders={"bucket": order}, color_discrete_map=colors)
        fig.update_layout(showlegend=False, height=320, margin=dict(t=10, b=10),
                          yaxis_title="pounds on hand", xaxis_title=None)
        st.plotly_chart(fig, use_container_width=True)

    with right:
        st.subheader("Days of supply by category")
        cats = sorted(dos["conservative"])
        st.dataframe(pd.DataFrame({
            "category": cats,
            "optimistic": [_fmt_dos(dos["optimistic"].get(c)) for c in cats],
            "conservative": [_fmt_dos(dos["conservative"].get(c)) for c in cats],
        }), hide_index=True, use_container_width=True)
        for s in SCENARIOS:
            st.caption(SCENARIO_LABELS[s])

    st.divider()
    st.subheader("7-day projection")
    scen = st.radio("Scenario assumption", SCENARIOS, horizontal=True,
                    format_func=lambda s: SCENARIO_LABELS[s])
    proj = project_supply(conn, scen)
    fig = px.line(proj, x="day", y="end_lb", color="category", markers=True)
    so = proj[proj["stockout"]]
    if not so.empty:
        fig.add_scatter(x=so["day"], y=so["end_lb"], mode="markers", name="below zero",
                        marker=dict(color="#922b21", size=11, symbol="x"))
    fig.add_hline(y=0, line_dash="dot", line_color="#922b21")
    fig.update_layout(height=380, margin=dict(t=10, b=10),
                      yaxis_title="projected end-of-day lb", xaxis_title="day (0 = today)")
    st.plotly_chart(fig, use_container_width=True)
    with st.expander("Projection table (start / inbound / demand / expired / end)"):
        st.dataframe(proj, hide_index=True, use_container_width=True)

    inf_rows = rows(conn, """
        SELECT d.dist_id, p.name pantry, w.name warehouse, pr.name product,
               d.quantity_lb, d.scheduled_date, d.status
        FROM distribution_plans d JOIN pantries p USING (pantry_id)
        JOIN warehouses w USING (warehouse_id) JOIN products pr USING (product_id)
        WHERE d.status='infeasible' ORDER BY d.scheduled_date""")
    if inf_rows:
        st.subheader(f"Infeasible planned distributions ({len(inf_rows)})")
        st.dataframe(pd.DataFrame(inf_rows), hide_index=True, use_container_width=True)


def _pantry_service(conn, plan_id: str) -> pd.DataFrame:
    """Per-pantry fulfillment, engine-consistent: per-day service capped at
    daily demand (evaluate_plan's no-backlog definition)."""
    demand7 = {(r["pantry_id"], r["category"]): r["d"] for r in rows(
        conn, "SELECT pantry_id, category, SUM(daily_demand_lb)*? d "
              "FROM pantry_demand GROUP BY pantry_id, category", (HORIZON_DAYS,))}
    daily = {k: v / HORIZON_DAYS for k, v in demand7.items()}
    served: dict[tuple[str, str, int], float] = {}
    for ln in rows(conn, """
            SELECT pl.to_id pid, pr.category c, pl.day d, pl.quantity_lb q
            FROM plan_lines pl JOIN products pr USING (product_id)
            WHERE pl.plan_id=? AND pl.action='allocate'""", (plan_id,)):
        key = (ln["pid"], ln["c"], ln["d"] or 0)
        served[key] = served.get(key, 0.0) + ln["q"]
    pantries = rows(conn, "SELECT * FROM pantries ORDER BY pantry_id")
    recs = []
    for p in pantries:
        d_total = s_total = 0.0
        for (pid, c), dd in daily.items():
            if pid != p["pantry_id"]:
                continue
            for d in range(HORIZON_DAYS):
                s_total += min(served.get((pid, c, d), 0.0), dd)
                d_total += dd
        ful = (s_total / d_total) if d_total else 1.0
        recs.append({"pantry": p["name"], "7-day demand lb": round(d_total, 1),
                     "served lb": round(s_total, 1), "fulfillment": round(ful, 3),
                     "service floor": p["service_floor"],
                     "below floor": ful < p["service_floor"]})
    return pd.DataFrame(recs)


def view_plan(conn, event_id: str) -> None:
    base_id, rec_id = latest_plan_ids(conn)
    if not (base_id and rec_id):
        st.info("No plans yet — replay the recall incident from the sidebar.")
        return
    base = rows(conn, "SELECT * FROM plans WHERE plan_id=?", (base_id,))[0]
    rec = rows(conn, "SELECT * FROM plans WHERE plan_id=?", (rec_id,))[0]
    bm, rm = _metrics(base), _metrics(rec)

    st.caption(f"Comparing latest pair: {base_id} (do-nothing) vs {rec_id} "
               f"({rec['method']}). Earlier drafts are superseded and retained in the audit trail.")
    dos_b = days_of_supply(project_supply(conn, "conservative"))
    dos_a = days_of_supply(project_supply(conn, "conservative", plan_id=rec_id))

    c = st.columns(4)
    c[0].metric("Served", f"{rm.get('served_lb', 0):,.10g} lb",
                f"{rm.get('served_lb', 0) - bm.get('served_lb', 0):+,.10g} vs baseline")
    c[1].metric("Unmet demand", f"{rm.get('unmet_demand_lb', 0):,.10g} lb",
                f"{rm.get('unmet_demand_lb', 0) - bm.get('unmet_demand_lb', 0):+,.10g}",
                delta_color="inverse")
    c[2].metric("Worst pantry fulfillment", f"{rm.get('worst_pantry_fulfillment', 0):.0%}",
                f"{rm.get('worst_pantry_fulfillment', 0) - bm.get('worst_pantry_fulfillment', 0):+.0%}")
    c[3].metric("Procurement cost", f"${rm.get('procurement_cost', 0):,.0f}",
                f"${rm.get('procurement_cost', 0) - bm.get('procurement_cost', 0):+,.0f}",
                delta_color="off")
    c = st.columns(4)
    c[0].metric("Spoilage", f"{rm.get('spoilage_lb', 0):,.10g} lb",
                f"{rm.get('spoilage_lb', 0) - bm.get('spoilage_lb', 0):+,.10g}", delta_color="inverse")
    c[1].metric("Boxes disrupted", rm.get("boxes_disrupted", 0),
                f"{rm.get('boxes_disrupted', 0) - bm.get('boxes_disrupted', 0):+d}",
                delta_color="inverse")
    c[2].metric("Hard-constraint violations", rm.get("hard_constraint_violations", 0),
                help="Deterministic re-evaluation of the stored plan lines (PLAN.md §15)")
    c[3].metric("Produce days of supply (conservative)",
                _fmt_dos(dos_a.get("produce")), f"from {_fmt_dos(dos_b.get('produce'))} without plan",
                delta_color="off")

    st.divider()
    st.subheader("Pantry service under the recommended plan")
    svc = _pantry_service(conn, rec_id)
    sty = svc.style.format({"7-day demand lb": "{:,.10g}", "served lb": "{:,.10g}",
                            "fulfillment": "{:.1%}", "service floor": "{:.0%}"}).map(
        lambda v: "background-color:#f5b7b1" if v is True else "", subset=["below floor"])
    st.dataframe(sty, hide_index=True, use_container_width=True)

    st.subheader("Plan lines")
    tabs = st.tabs(["Purchases", "Transfers", "Allocations"])
    with tabs[0]:
        pur = rows(conn, """
            SELECT pl.quantity_lb, pr.name product, s.name supplier, o.lead_time_days,
                   pl.unit_cost_per_lb, pl.quantity_lb*pl.unit_cost_per_lb cost, pl.to_id warehouse
            FROM plan_lines pl JOIN replacement_offers o ON pl.from_id=o.offer_id
            JOIN suppliers s ON o.supplier_id=s.supplier_id
            JOIN products pr ON pl.product_id=pr.product_id
            WHERE pl.plan_id=? AND pl.action='purchase' ORDER BY cost DESC""", (rec_id,))
        if pur:
            st.dataframe(pd.DataFrame(pur), hide_index=True, use_container_width=True)
        else:
            st.caption("none")
    with tabs[1]:
        tr = rows(conn, """
            SELECT pl.day, pl.quantity_lb, pr.name product, pl.from_id "from", pl.to_id "to"
            FROM plan_lines pl JOIN products pr USING (product_id)
            WHERE pl.plan_id=? AND pl.action='transfer' ORDER BY pl.day""", (rec_id,))
        if tr:
            st.dataframe(pd.DataFrame(tr), hide_index=True, use_container_width=True)
        else:
            st.caption("none")
    with tabs[2]:
        al = rows(conn, """
            SELECT p.name pantry, pr.name product, SUM(pl.quantity_lb) lb
            FROM plan_lines pl JOIN products pr USING (product_id)
            JOIN pantries p ON pl.to_id=p.pantry_id
            WHERE pl.plan_id=? AND pl.action='allocate'
            GROUP BY p.name, pr.name ORDER BY p.name, lb DESC""", (rec_id,))
        if al:
            piv = pd.DataFrame(al).pivot_table(index="pantry", columns="product",
                                               values="lb", fill_value=0.0)
            st.dataframe(piv, use_container_width=True)
        else:
            st.caption("none")

    st.divider()
    if rec["status"] == "approved":
        st.success(f"Plan {rec_id} approved by {rec['approved_by']} at {rec['approved_at']}.")
    elif rec["status"] == "rejected":
        st.warning(f"Plan {rec_id} was rejected. Replay or adjust reviews to regenerate.")
    else:
        st.markdown("**This plan is a DRAFT.** Nothing executes without operator approval; "
                    "approval is not food-safety clearance of any lot (PLAN.md §11).")
        a, r, _ = st.columns([1, 1, 4])
        if a.button("Approve recommended plan", type="primary"):
            try:
                approve_plan(conn, rec_id, OPERATOR, allow_llm=ALLOW_LLM)
            except ValueError as exc:
                st.error(f"Approval blocked: {exc}")
            else:
                st.rerun()
        if r.button("Reject"):
            conn.execute("UPDATE plans SET status='rejected' WHERE plan_id=?", (rec_id,))
            conn.commit()
            st.rerun()

    comms = rows(conn, "SELECT * FROM comms WHERE plan_id=? ORDER BY comm_id", (rec_id,))
    if comms:
        st.subheader("Drafted communications")
        st.caption("Drafts only — reviewed and sent by staff, never auto-sent. "
                   "Facts come from the database; method labels the writing layer.")
        for cm in comms:
            with st.expander(f"[{cm['audience']}] {cm['subject']}  ·  method: {cm['method']}"):
                st.text(cm["body"])

    with st.expander("Audit log (latest 25)"):
        st.dataframe(pd.DataFrame(rows(
            conn, "SELECT at, actor, action, detail_json FROM audit_log ORDER BY id DESC LIMIT 25")),
            hide_index=True, use_container_width=True)


def view_graph(conn, event_id: str) -> None:
    st.caption("Lineage derived from relational joins for THIS incident's evidence "
               "(PLAN.md §8): recall → suppliers/facilities/products → specific lot or PO "
               "→ warehouse → pantry. Colors are global effective states; convergence on "
               "records never fabricates supplier-to-supplier paths.")
    G = lineage_graph(conn, event_id)
    if not G.number_of_nodes():
        st.info("No implicated lineage for this incident.")
        return
    st.plotly_chart(graph_figure(G), use_container_width=True)
    legend = " · ".join(f"<span style='color:{c}'>&#9632;</span> {s}"
                        for s, c in STATE_COLORS.items())
    st.markdown(f"Match-state colors: {legend}", unsafe_allow_html=True)


def view_map(conn, event_id: str) -> None:
    st.caption("Sites and flows carry OPERATIONAL evidence roles only — implicated "
               "supplier/facility, at-risk flows, replacement sources. Proximity to "
               "reported human infections never marks inventory (PLAN.md §13).")
    pts = map_points(conn)
    _, rec_id = latest_plan_ids(conn)
    arcs = map_arcs(conn, plan_id=rec_id)
    if pts.empty:
        st.info("No mappable sites.")
        return
    online = st.toggle("Online basemap (Carto tiles — requires network)", value=False,
                       help="Off = offline-safe blank canvas (PLAN.md §14: zero-network demo)")
    st.pydeck_chart(map_deck(pts, arcs, online=online))
    st.markdown("Sites: dark red = implicated · green = replacement source / cleared · "
                "teal = warehouse · purple = pantry. Arcs: supplier→warehouse matched-lot "
                "flows (state color), warehouse→pantry implicated planned distributions "
                "(red = infeasible lines), green = replacement purchases on the "
                "recommended plan.")


# ------------------------------------------------------------------ shell

def main() -> None:
    _inject_theme()
    db_path = _session_db_path()
    conn = get_conn(db_path)

    st.sidebar.markdown(
        """
        <div class="fs-brand">
          <div class="fs-monogram">FS</div>
          <div class="fs-brand-name">FoodShock</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.sidebar.caption(
        "Food-bank supply shock radar — recall response with a "
        "human-approved recovery plan."
    )
    st.sidebar.markdown(
        "**Demo boundary:** all operational data is synthetic "
        "(public-data-inspired; PLAN.md §3)."
    )

    ready = _scenario_ready(conn)
    if not ready:
        st.sidebar.info("No scenario loaded yet.")
        _hero(
            "Supply shock radar",
            "FoodShock",
            "Load the synthetic network, replay a food-safety recall, and trace "
            "the response from implicated lots to a human-approved recovery plan.",
        )
        st.markdown(
            "Scenario: **2 warehouses · 6 pantries · 16 lots · 5 inbound POs · "
            "7-day distribution plan**"
        )
        if st.button("Load scenario and replay recall", type="primary"):
            with st.spinner("Seeding scenario and running RecallResponseAgent..."):
                try:
                    _replay_incident(conn)
                except ExtractionUnavailable as exc:
                    st.error(f"Extraction unavailable: {exc}")
                    st.stop()
            st.rerun()
        st.stop()

    if st.sidebar.button("Replay recall incident",
                         help="Reset the scenario and run the canned notice through the agent"):
        with st.spinner("Resetting scenario and running RecallResponseAgent..."):
            try:
                _replay_incident(conn)
            except ExtractionUnavailable as exc:
                st.error(f"Extraction unavailable: {exc}")
                st.stop()
        st.rerun()

    event_id = latest_event_id(conn)
    events = [r["event_id"] for r in rows(conn, "SELECT event_id FROM recall_events "
                                                "ORDER BY ingested_at DESC")]
    if len(events) > 1:
        event_id = st.sidebar.selectbox("Incident", events)

    st.sidebar.divider()
    st.sidebar.caption(f"DB: `{db_path.name}` — "
                       f"{st.session_state.get('db_mode', 'shared via $FOODSHOCK_DB')}")
    st.sidebar.caption("LLM narration: " + ("live allowed" if ALLOW_LLM
                       else "cache/template only (zero network)"))

    if not event_id:
        _hero(
            "Scenario ready",
            "FoodShock",
            "The synthetic network is loaded. Replay the recall incident to begin "
            "the operator workflow.",
        )
        st.info("Scenario loaded; no recall incident yet. Use 'Replay recall incident'.")
        st.stop()

    _hero(
        f"Active incident · {event_id}",
        "Recall response command center",
        "Trace the implicated lot to each pantry, quantify seven-day supply risk, "
        "and review a feasible recovery plan before anything executes.",
    )
    run_id = _latest_run_id(conn)
    if run_id:
        expl = rows(conn, "SELECT content_json FROM agent_transcript WHERE run_id=? "
                          "AND kind='narration' AND name='explain' ORDER BY seq DESC LIMIT 1",
                    (run_id,))
        if expl:
            c = json.loads(expl[0]["content_json"])
            st.markdown(f"**Agent ({c['method']}):** {c['text']}")
        ba_row = rows(conn, "SELECT content_json FROM agent_transcript WHERE run_id=? "
                            "AND kind='tool_result' AND name='before_after' "
                            "ORDER BY seq DESC LIMIT 1", (run_id,))
        if ba_row:
            rt = json.loads(ba_row[0]["content_json"])["result"].get("runtime_s")
            if rt is not None:
                st.caption(f"Response-planning time: {rt:g} s measured for this run, "
                           "notice to draft plan. Comparator: 2.0 staff-hours from a "
                           "hypothetical task model (20m triage + 60m trace + 25m replan "
                           "+ 15m comms); not operator-validated.")
        lines = _transcript_lines(conn, run_id)
        with st.expander(f"Agent transcript — {run_id} ({len(lines)} steps: "
                         "observe → investigate → explain → approve)"):
            st.code("\n".join(lines), language=None)

    tabs = st.tabs(["1 · Exposure queue", "2 · Impact dashboard", "3 · Recovery plan",
                    "4 · Supply-chain graph", "5 · Map"])
    with tabs[0]:
        view_exposure(conn, event_id)
    with tabs[1]:
        view_impact(conn, event_id)
    with tabs[2]:
        view_plan(conn, event_id)
    with tabs[3]:
        view_graph(conn, event_id)
    with tabs[4]:
        view_map(conn, event_id)


main()
