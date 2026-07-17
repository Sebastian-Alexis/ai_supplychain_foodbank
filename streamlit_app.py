"""FoodShock Streamlit app (PLAN.md §13): the five operator views.

1. Exposure queue    -- match states + evidence + clearance actions
2. Impact dashboard  -- match-state inventory, POs at risk, 7-day projection
3. Recovery plan     -- recommended vs do-nothing, approval, drafted comms
4. Supply-chain graph-- derived lineage (event -> ... -> pantry)
5. Geographic map    -- sites and flows; operational evidence only

DB resolution: $FOODSHOCK_DB if set (shared file, single-operator demo);
otherwise each browser session works on its own temp COPY of data/foodshock.db
so concurrent viewers (Streamlit Community Cloud) can replay/reset/approve
without clobbering each other. Operational demo data is synthetic (PLAN.md §3);
incident records may come from the official FDA or USDA APIs.

Run:  streamlit run streamlit_app.py
"""

from __future__ import annotations
from collections.abc import Callable

import json
import os
import shutil
import sqlite3
import tempfile
import time
from html import escape
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from foodshock.agent import AgentRunResult, RecallResponseAgent
from foodshock.datagen import generate
from foodshock.demo_incidents import (DEFAULT_INCIDENT_KEY, INCIDENTS,
                                      DemoIncident, incident_for_event,
                                      prepare_demo_incident)
from foodshock.db import DEFAULT_DB, get_conn, rows
from foodshock.engine import (BOX_LB, HORIZON_DAYS, approve_plan, build_plans,
                              days_of_supply, incident_focus, project_supply,
                              propagate, review_match)
from foodshock.extraction import ExtractionUnavailable
from foodshock.incident_sources import (
    FSIS_WARNING, OPENFDA_WARNING, IncidentSourceError, LiveIncident,
    fetch_live_incidents,
)
from foodshock.schemas import SCENARIO_LABELS, SCENARIOS
from foodshock.viz import (STATE_COLORS, graph_figure, latest_event_id,
                           latest_plan_ids, lineage_graph, map_arcs, map_deck,
                           map_points)

st.set_page_config(page_title="FoodShock | Agentic response framework", layout="wide")

ALLOW_LLM = os.environ.get("FOODSHOCK_LIVE_LLM", "") == "1"
OPERATOR = "operator (streamlit)"

SAFETY_CAPTION = ("Confirmed recalled or quarantined lots and canceled POs are excluded "
                  "from every scenario and every plan; no toggle re-includes them (PLAN.md §10).")
STATE_ORDER = ["confirmed", "probable", "possible", "unknown", "not_matched"]

IncidentChoice = DemoIncident | LiveIncident
SOURCE_LABELS = {
    "demo": "Curated incident demos",
    "openfda": "Live · FDA openFDA",
    "fsis": "Live · USDA FSIS",
}


@st.cache_data(ttl=600, show_spinner=False)
def _load_live_incidents(provider: str) -> tuple[list[LiveIncident], str | None]:
    """Cache each authority independently, including temporary failure results."""
    try:
        incidents = fetch_live_incidents(provider)  # type: ignore[arg-type]
    except IncidentSourceError as exc:
        return [], str(exc)
    return incidents, None

def _set_app_page(page: str) -> None:
    st.session_state.app_page = page


REPLAY_STAGES: dict[tuple[str, str, str], tuple[int, str]] = {
    ("observe", "tool_result", "ingest_notice"):
        (12, "Notice ingested into the incident record"),
    ("investigate", "tool_result", "extract_notice"):
        (28, "Entities validated against source excerpts"),
    ("investigate", "tool_result", "resolve_entity"):
        (46, "Lots and purchase orders resolved by evidence tier"),
    ("investigate", "tool_result", "propagate"):
        (62, "Exposure propagated through warehouse commitments"),
    ("explain", "tool_result", "project_supply"):
        (74, "Seven-day supply scenarios projected"),
    ("explain", "tool_result", "optimize_recovery"):
        (88, "Constrained recovery plan optimized"),
    ("approve", "narration", "request_approval"):
        (100, "Operator approval package ready"),
}

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
        [data-testid="stButton"] button { border-radius: 2px; font-weight: 700; }
        [data-testid="stButton"] button[kind="primary"] {
          background: var(--fs-coral);
          border-color: var(--fs-coral);
          color: #fffdf8;
          font-weight: 750;
        }
        [data-testid="stSidebar"] [data-testid="stButton"] button {
          width: 100%;
        }
        [data-testid="stSidebar"] [data-testid="stButton"] button[kind="primary"] {
          background: var(--fs-coral) !important;
          border: 1px solid var(--fs-coral) !important;
          color: #fffdf8 !important;
        }
        [data-testid="stSidebar"] [data-testid="stButton"] button[kind="secondary"] {
          background: rgba(255, 253, 248, 0.06) !important;
          border: 1px solid #54736f !important;
          color: #fffdf8 !important;
        }
        [data-testid="stSidebar"] [data-testid="stButton"] button p,
        [data-testid="stSidebar"] [data-testid="stButton"] button span {
          color: #fffdf8 !important;
        }
        [data-testid="stSidebar"] [data-testid="stButton"] button[kind="primary"]:hover,
        [data-testid="stSidebar"] [data-testid="stButton"] button[kind="primary"]:focus-visible {
          background: #8f3427 !important;
          border-color: #8f3427 !important;
        }
        [data-testid="stSidebar"] [data-testid="stButton"] button[kind="secondary"]:hover,
        [data-testid="stSidebar"] [data-testid="stButton"] button[kind="secondary"]:focus-visible {
          background: rgba(255, 253, 248, 0.14) !important;
          border-color: #91c9c1 !important;
        }
        [data-testid="stSidebar"] [data-testid="stButton"] button:focus-visible {
          outline: 3px solid #d5bd7b;
          outline-offset: 2px;
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
        .fs-project-kicker,
        .fs-side-section {
          color: #91c9c1;
          font-size: 0.68rem;
          font-weight: 800;
          letter-spacing: 0.14em;
          text-transform: uppercase;
        }
        .fs-project-copy {
          color: #d9e7e4;
          font-size: 0.86rem;
          line-height: 1.45;
          margin: 0.35rem 0 0.8rem;
        }
        .fs-side-pills {
          display: flex;
          flex-wrap: wrap;
          gap: 0.35rem;
          margin-bottom: 0.8rem;
        }
        .fs-side-pills span {
          background: #214d48;
          border: 1px solid #3f6c67;
          color: #dcebe8;
          font-size: 0.68rem;
          padding: 0.2rem 0.42rem;
        }
        .fs-incident-card {
          background: rgba(255, 253, 248, 0.055);
          border: 1px solid #3f625e;
          border-left: 3px solid #d5bd7b;
          color: #f7f2e8;
          margin: 0.45rem 0 0.7rem;
          padding: 0.7rem 0.75rem;
        }
        .fs-incident-card strong {
          display: block;
          color: #fffdf8;
          font-size: 0.9rem;
          margin-bottom: 0.2rem;
        }
        .fs-incident-card span {
          color: #b9cbc8;
          font-size: 0.76rem;
          line-height: 1.35;
        }
        .fs-tech-hero {
          position: relative;
          overflow: hidden;
          background: var(--fs-ink);
          border-left: 6px solid var(--fs-coral);
          color: #fffdf8;
          padding: 2.2rem 2.35rem;
          margin-bottom: 1.2rem;
        }
        .fs-tech-hero::after {
          content: "";
          position: absolute;
          width: 24rem;
          height: 24rem;
          right: -8rem;
          top: -11rem;
          border: 1px solid #34524f;
          border-radius: 50%;
          box-shadow: 0 0 0 2.8rem rgba(52, 82, 79, 0.22),
                      0 0 0 5.6rem rgba(52, 82, 79, 0.12);
        }
        .fs-tech-eyebrow {
          color: #91c9c1;
          font-size: 0.72rem;
          font-weight: 800;
          letter-spacing: 0.14em;
          text-transform: uppercase;
        }
        .fs-tech-hero h1 {
          position: relative;
          z-index: 1;
          color: #fffdf8;
          font-size: clamp(2.4rem, 5vw, 4.5rem);
          line-height: 0.95;
          max-width: 55rem;
          margin: 0.5rem 0 0.8rem;
        }
        .fs-tech-hero p {
          position: relative;
          z-index: 1;
          color: #d9e7e4;
          font-size: 1.08rem;
          line-height: 1.5;
          max-width: 52rem;
          margin: 0;
        }
        .fs-flow {
          display: grid;
          grid-template-columns: repeat(7, minmax(0, 1fr));
          align-items: stretch;
          gap: 0.45rem;
          margin: 0.8rem 0 1.5rem;
        }
        .fs-flow-node {
          background: var(--fs-paper);
          border: 1px solid var(--fs-line);
          border-top: 3px solid var(--fs-teal);
          min-height: 7.3rem;
          padding: 0.75rem;
        }
        .fs-flow-node strong {
          display: block;
          color: var(--fs-ink);
          font-size: 0.84rem;
          margin-bottom: 0.4rem;
        }
        .fs-flow-node span {
          color: #536360;
          font-size: 0.72rem;
          line-height: 1.35;
        }
        .fs-flow-node.fs-human { border-top-color: var(--fs-coral); }
        .fs-tech-grid {
          display: grid;
          grid-template-columns: repeat(3, minmax(0, 1fr));
          gap: 0.8rem;
          margin: 0.75rem 0 1.5rem;
        }
        .fs-tech-card {
          background: var(--fs-paper);
          border: 1px solid var(--fs-line);
          padding: 1rem 1.05rem;
        }
        .fs-tech-card .fs-card-index {
          color: var(--fs-coral);
          font-size: 0.7rem;
          font-weight: 850;
          letter-spacing: 0.12em;
        }
        .fs-tech-card strong {
          display: block;
          color: var(--fs-ink);
          font-size: 1rem;
          margin: 0.25rem 0 0.35rem;
        }
        .fs-tech-card span {
          color: #536360;
          font-size: 0.82rem;
          line-height: 1.45;
        }
        .fs-lanes {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 0.8rem;
          margin: 0.8rem 0 1.5rem;
        }
        .fs-lane {
          min-height: 10.5rem;
          padding: 1.15rem;
          border: 1px solid var(--fs-line);
        }
        .fs-lane.language { background: #e8f1ee; border-top: 4px solid var(--fs-teal); }
        .fs-lane.control { background: #f6e8e2; border-top: 4px solid var(--fs-coral); }
        .fs-lane h3 { margin: 0 0 0.45rem; }
        .fs-lane p { color: #425451; font-size: 0.88rem; line-height: 1.5; }
        .fs-safety-strip {
          display: grid;
          grid-template-columns: 1.1fr 2.9fr;
          background: var(--fs-ink);
          color: #d9e7e4;
          margin: 0.8rem 0 1.5rem;
          padding: 1.1rem 1.25rem;
        }
        .fs-safety-strip strong {
          color: #fffdf8;
          font-size: 1.05rem;
        }
        .fs-safety-strip span {
          border-left: 1px solid #3f625e;
          font-size: 0.86rem;
          line-height: 1.45;
          padding-left: 1rem;
        }
        @media (max-width: 900px) {
          .fs-flow { grid-template-columns: 1fr 1fr; }
          .fs-tech-grid, .fs-lanes { grid-template-columns: 1fr; }
          .fs-safety-strip { grid-template-columns: 1fr; gap: 0.6rem; }
          .fs-safety-strip span { border-left: 0; padding-left: 0; }
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


def _replay_incident(
    conn: sqlite3.Connection,
    incident: IncidentChoice,
    *,
    on_event: Callable[[dict], None] | None = None,
) -> AgentRunResult:
    """Reset synthetic operations, then process one curated or official incident."""
    generate(conn)
    agent = RecallResponseAgent(conn, allow_llm=ALLOW_LLM, on_event=on_event)
    if isinstance(incident, DemoIncident):
        prepare_demo_incident(conn, incident)
        return agent.run(
            incident.notice_path.read_text(),
            event_id=incident.event_id,
            source_url=incident.source_url,
        )
    return agent.run(
        incident.raw_text,
        event_id=incident.event_id,
        source_url=incident.source_url,
        published_at=incident.published_at,
        provided_extraction=incident.extraction,
        provided_extraction_method=f"{incident.provider}-api",
    )


def _has_exposure(conn: sqlite3.Connection, event_id: str) -> bool:
    found = rows(
        conn,
        "SELECT COUNT(*) c FROM matches WHERE event_id=? AND state!='not_matched' "
        "AND NOT (reviewed=1 AND review_action='cleared')",
        (event_id,),
    )
    return bool(found and found[0]["c"])


def _recompute(conn: sqlite3.Connection, event_id: str) -> None:
    """After any clearance action: refresh feasibility and supersede the plan
    pair so no view presents a plan built on a stale pool (PLAN.md §11)."""
    propagate(conn, event_id)
    build_plans(conn, recovery_enabled=_has_exposure(conn, event_id))


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
    source_api = (ev["extraction_method"] or "").endswith("-api")
    if source_api:
        warning = OPENFDA_WARNING if ev["authority"] == "FDA" else FSIS_WARNING
        st.warning(
            f"**Official incident record; synthetic operations.** {warning} "
            "The API supplied the incident only—not the inventory, purchase orders, "
            "matches, or recovery market shown here."
        )
        published = ev["published_at"] or "not stated"
        st.markdown(
            f"**Published:** {published} · "
            f"[Open official source record]({ev['source_url']})"
        )

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
            "Hazard": ext.get("pathogen") or "—",
            "Action required": ext.get("action_required") or "—",
        }
        st.table(pd.DataFrame({"fact": facts.keys(), "extracted value": facts.values()}))
        if ev["human_confirmed"]:
            st.success("Notice extraction human-confirmed.")
        elif st.button("Confirm extraction against source record"):
            conn.execute("UPDATE recall_events SET human_confirmed=1 WHERE event_id=?", (event_id,))
            conn.commit()
            st.rerun()
    with right:
        st.subheader("Source excerpts (verbatim provenance)")
        exc = ext.get("excerpts", {})
        if exc:
            st.dataframe(pd.DataFrame({"field": exc.keys(), "supporting quote": exc.values()}),
                         hide_index=True, use_container_width=True)
        with st.expander("Normalized source snapshot (API fields)"):
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
    focus = incident_focus(conn, event_id)
    focus_label = focus["category"].replace("_", " ").title()

    dos = {s: days_of_supply(project_supply(conn, s)) for s in SCENARIOS}
    c = st.columns(5)
    c[0].metric("Quarantine proposed/held", f"{q['lb']:g} lb", f"{q['n']} lot(s)", delta_color="off")
    c[1].metric("Awaiting human review", f"{review_lb:g} lb")
    c[2].metric("Inbound POs at risk", at_risk)
    c[3].metric("Infeasible distribution lines", inf["n"], f"{inf['lb']:g} lb planned", delta_color="off")
    c[4].metric(f"{focus_label} days of supply (conservative)",
                _fmt_dos(dos["conservative"].get(focus["category"])),
                f"optimistic {_fmt_dos(dos['optimistic'].get(focus['category']))}",
                delta_color="off")
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
    focus = incident_focus(conn, event_id)
    focus_label = focus["category"].replace("_", " ").title()
    has_exposure = _has_exposure(conn, event_id)
    if not has_exposure:
        st.info(
            "No evidence-linked inventory lot or inbound order was found in the "
            "synthetic operational network. No recall-triggered recovery action is "
            "recommended; the assessment below intentionally mirrors the pre-existing "
            "network baseline."
        )

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
    c = st.columns(5)
    c[0].metric("Spoilage", f"{rm.get('spoilage_lb', 0):,.10g} lb",
                f"{rm.get('spoilage_lb', 0) - bm.get('spoilage_lb', 0):+,.10g}",
                delta_color="inverse")
    c[1].metric("Food boxes disrupted", rm.get("boxes_disrupted", 0),
                f"{rm.get('boxes_disrupted', 0) - bm.get('boxes_disrupted', 0):+d}",
                delta_color="inverse",
                help=f"All unmet demand converted at the stated {BOX_LB:g} lb/box assumption")
    c[2].metric("Response focus", focus_label,
                f"{len(focus['products'])} evidence-linked catalog product(s)",
                delta_color="off")
    c[3].metric("Hard-constraint violations", rm.get("hard_constraint_violations", 0),
                help="Deterministic re-evaluation of the stored plan lines (PLAN.md §15)")
    c[4].metric(f"{focus_label} days of supply (conservative)",
                _fmt_dos(dos_a.get(focus["category"])),
                f"from {_fmt_dos(dos_b.get(focus['category']))} without plan",
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
    if not has_exposure:
        st.success(
            "No recall-triggered action package requires approval. Review the official "
            "source and the zero-exposure evidence; the generic network baseline is "
            "shown for context only."
        )
    elif rec["status"] == "approved":
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


def _run_animated_replay(
    conn: sqlite3.Connection,
    incident: IncidentChoice,
) -> AgentRunResult:
    """Render the real agent event stream with a deliberate presentation pace."""
    seen: set[tuple[str, str, str]] = set()
    with st.sidebar.status(
        f"Running {incident.title.lower()}…",
        expanded=True,
    ) as run_status:
        progress = st.progress(0, text="Preparing a fresh synthetic network")

        def show_event(event: dict) -> None:
            key = (event["phase"], event["kind"], event["name"])
            stage = REPLAY_STAGES.get(key)
            if stage is None or key in seen:
                return
            seen.add(key)
            value, label = stage
            progress.progress(value, text=label)
            run_status.write(f"{len(seen):02d} · {label}")
            time.sleep(0.22)

        result = _replay_incident(conn, incident, on_event=show_event)
        progress.progress(100, text="Assessment ready for operator review")
        run_status.update(
            label=f"{incident.title} ready",
            state="complete",
            expanded=True,
        )
        time.sleep(0.45)
        return result


def _technical_page(conn: sqlite3.Connection) -> None:
    """Judge-facing explanation of the framework, backed by the current run."""
    ready = _scenario_ready(conn)
    event_id = latest_event_id(conn) if ready else None
    active_incident = incident_for_event(event_id)
    active_runtime_incident = st.session_state.get("active_runtime_incident")
    active_choice = (
        active_runtime_incident
        if active_runtime_incident is not None
        and active_runtime_incident.event_id == event_id
        else active_incident
    )
    run_id = _latest_run_id(conn) if ready else None

    transcript_events = 0
    tool_calls = 0
    open_gaps = 0
    rec_id = None
    plan_status = "not run"
    plan_method: str | None = None
    hard_violations: int | str = "—"
    if run_id:
        transcript_events = rows(
            conn,
            "SELECT COUNT(*) c FROM agent_transcript WHERE run_id=?",
            (run_id,),
        )[0]["c"]
        tool_calls = rows(
            conn,
            "SELECT COUNT(*) c FROM agent_transcript WHERE run_id=? AND kind='tool_call'",
            (run_id,),
        )[0]["c"]
        open_gaps = rows(
            conn,
            "SELECT COUNT(*) c FROM agent_transcript WHERE run_id=? AND kind='gap'",
            (run_id,),
        )[0]["c"]
        _, rec_id = latest_plan_ids(conn)
        if rec_id:
            plan = rows(conn, "SELECT * FROM plans WHERE plan_id=?", (rec_id,))[0]
            plan_status = plan["status"]
            plan_method = plan["method"]
            hard_violations = _metrics(plan).get("hard_constraint_violations", "—")

    st.markdown(
        """
        <section class="fs-tech-hero">
          <div class="fs-tech-eyebrow">Technical architecture · bounded agency</div>
          <h1>Language understands. Code decides. Humans authorize.</h1>
          <p>
            FoodShock is a reusable incident-response framework: a code-orchestrated
            agent turns an official API record or unstructured safety notice into
            evidence-linked operational state, invokes deterministic planning tools,
            and stops at a human review or approval gate.
          </p>
        </section>
        """,
        unsafe_allow_html=True,
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Transcript events", transcript_events,
              help="Every narration, tool call, result, and review gap is persisted.")
    m2.metric("Tool invocations", tool_calls,
              help="Calls made by the current bounded agent run.")
    m3.metric("Open review gaps", open_gaps,
              help="Ambiguous evidence routed to a person rather than silently resolved.")
    m4.metric("Hard violations", hard_violations,
              help="Recomputed by deterministic plan evaluation.")
    if run_id:
        label = active_choice.title if active_choice else event_id
        if plan_method == "no-exposure":
            st.caption(
                f"Live proof from {label} · `{run_id}` · zero-exposure assessment "
                f"`{rec_id}` is **{plan_status}**."
            )
        else:
            st.caption(
                f"Live proof from {label} · `{run_id}` · recommended plan "
                f"`{rec_id}` is **{plan_status}**."
            )
    else:
        st.caption("Run an incident from the sidebar to populate these proof points.")

    st.subheader("One explicit incident-response loop")
    st.markdown(
        """
        <div class="fs-flow">
          <div class="fs-flow-node"><strong>01 · Observe</strong><span>Fetch or replay an incident and retain its normalized source snapshot.</span></div>
          <div class="fs-flow-node"><strong>02 · Extract</strong><span>Map structured API fields or validate notice entities with verbatim excerpts.</span></div>
          <div class="fs-flow-node"><strong>03 · Resolve</strong><span>Rank lot and PO matches across four evidence tiers.</span></div>
          <div class="fs-flow-node"><strong>04 · Propagate</strong><span>Trace exposure into inventory and planned distributions.</span></div>
          <div class="fs-flow-node"><strong>05 · Project</strong><span>Compute seven-day supply under labeled assumptions.</span></div>
          <div class="fs-flow-node"><strong>06 · Assess</strong><span>Optimize recovery only when exposure exists; otherwise preserve the baseline.</span></div>
          <div class="fs-flow-node fs-human"><strong>07 · Review</strong><span>Stop for a person before any safety or plan-side effect.</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.subheader("What makes the framework agentic")
    st.markdown(
        """
        <div class="fs-tech-grid">
          <div class="fs-tech-card"><div class="fs-card-index">CONTROL LOOP</div><strong>Bounded orchestration</strong><span>RecallResponseAgent owns a visible observe → investigate → explain → approve lifecycle instead of producing one opaque answer.</span></div>
          <div class="fs-tech-card"><div class="fs-card-index">TOOL GROUNDING</div><strong>State-changing tools</strong><span>Extraction, entity resolution, propagation, projection, optimization, and audit are explicit calls with persisted inputs and results.</span></div>
          <div class="fs-tech-card"><div class="fs-card-index">MEMORY</div><strong>Operational state</strong><span>SQLite is the system of record. The graph is derived from joins, so a visualization can never become a competing truth store.</span></div>
          <div class="fs-tech-card"><div class="fs-card-index">UNCERTAINTY</div><strong>Escalation over guessing</strong><span>Probable and possible matches create review gaps; confidence prioritizes work but never declares inventory safe.</span></div>
          <div class="fs-tech-card"><div class="fs-card-index">PLANNING</div><strong>Constrained action</strong><span>A time-indexed LP reasons over arrivals, expiration, storage, temperature, allergens, budget, and pantry equity.</span></div>
          <div class="fs-tech-card"><div class="fs-card-index">OBSERVABILITY</div><strong>Replayable evidence</strong><span>The same event stream animating the demo is persisted as the technical transcript judges can inspect.</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.subheader("A deliberate intelligence boundary")
    st.markdown(
        """
        <div class="fs-lanes">
          <section class="fs-lane language">
            <h3>Language layer</h3>
            <p><strong>Best at ambiguity:</strong> extract entities from narrative notices, explain tradeoffs, and draft operator communications. Structured outputs must survive schema and verbatim-provenance checks.</p>
            <p>Official API records use deterministic field mapping instead of an LLM. Curated replays retain cached extraction and templates as an offline fallback without pretending to be a live model evaluation.</p>
          </section>
          <section class="fs-lane control">
            <h3>Deterministic control layer</h3>
            <p><strong>Best at guarantees:</strong> matching tiers, status transitions, supply pools, unit arithmetic, seven-day projection, optimization, hard constraints, approval validation, and audit.</p>
            <p>The model never marks food safe, allocates recalled stock, approves a plan, or sends a communication.</p>
          </section>
        </div>
        <div class="fs-safety-strip">
          <strong>Safety invariant</strong>
          <span>Confirmed recalled or quarantined lots are removed before planning variables are created. Probable and possible records stay outside the conservative pool until a human explicitly clears them.</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.subheader("Framework tool contract")
    st.dataframe(
        pd.DataFrame([
            {"tool": "extract_notice", "contract": "notice → validated entities + source excerpts",
             "guard": "unsupported values are cleared"},
            {"tool": "resolve_entity", "contract": "catalog records → evidence tier + match state",
             "guard": "ambiguous states route to review"},
            {"tool": "propagate", "contract": "matches → quarantine, PO risk, infeasible commitments",
             "guard": "global active-recall state is idempotent"},
            {"tool": "project_supply", "contract": "safe pool + demand → seven-day projection",
             "guard": "confirmed stock can never re-enter"},
            {"tool": "optimize_recovery", "contract": "constraints + offers → baseline and plan",
             "guard": "budget, timing, storage, temperature, allergens"},
            {"tool": "approve_plan", "contract": "latest safe draft → approval + draft comms",
             "guard": "revalidates currency and feasibility"},
        ]),
        hide_index=True,
        use_container_width=True,
    )

    left, right = st.columns([1.05, 1.3])
    with left:
        st.subheader("Implementation map")
        st.table(pd.DataFrame([
            {"module": "incident_sources.py", "responsibility": "openFDA + FSIS source boundary"},
            {"module": "agent.py", "responsibility": "orchestration + event transcript"},
            {"module": "extraction.py", "responsibility": "schema + provenance boundary"},
            {"module": "engine.py", "responsibility": "resolution, propagation, projection, LP"},
            {"module": "db.py", "responsibility": "state, safety pools, audit"},
            {"module": "viz.py", "responsibility": "derived lineage and map evidence"},
        ]))
    with right:
        st.subheader("Why this generalizes")
        st.markdown(
            """
            The framework does not encode “onion recall” as its workflow. It accepts
            a real authority record or curated notice; the same agent, tools, state
            machine, constraints, transcript, and human-control contract run unchanged.
            Official incidents are compared honestly with synthetic operations, so zero
            exposure is an expected result—not a reason to invent matching lots.

            - **openFDA:** current ongoing food-enforcement events.
            - **USDA FSIS:** recalls and public-health alerts, best-effort and independently cached.
            - **Curated replay:** incident-aligned synthetic data for repeatable recovery demonstrations.
            """
        )

    st.subheader("Inspect the current agent run")
    if run_id:
        with st.expander(
            f"{run_id} · {transcript_events} persisted events",
            expanded=True,
        ):
            st.code("\n".join(_transcript_lines(conn, run_id)), language=None)
    else:
        st.info("No run is loaded. Choose an incident and run the simulation from the sidebar.")

    st.caption(
        "Trust boundary: official incidents may be live; operations remain synthetic, "
        "with no client PII and no autonomous execution. API data still requires issuing-"
        "authority verification, and model extraction accuracy is not claimed without a "
        "provenance-complete evaluation run."
    )


# ------------------------------------------------------------------ shell

def main() -> None:
    _inject_theme()
    db_path = _session_db_path()
    conn = get_conn(db_path)
    ready = _scenario_ready(conn)
    event_id = latest_event_id(conn) if ready else None
    active_incident = incident_for_event(event_id)
    active_runtime_incident = st.session_state.get("active_runtime_incident")
    if (
        active_runtime_incident is not None
        and active_runtime_incident.event_id != event_id
    ):
        active_runtime_incident = None
    active_choice: IncidentChoice | None = active_runtime_incident or active_incident

    st.sidebar.markdown(
        """
        <div class="fs-brand">
          <div class="fs-monogram">FS</div>
          <div class="fs-brand-name">FoodShock</div>
        </div>
        <div class="fs-project-kicker">Agentic operations framework</div>
        <div class="fs-project-copy">
          Converts fragmented safety notices into traceable exposure,
          constrained recovery options, and a human-gated action plan.
        </div>
        <div class="fs-side-pills">
          <span>Official feeds</span><span>Offline fallback</span><span>Human approved</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if "app_page" not in st.session_state:
        st.session_state.app_page = "demo"
    page = st.session_state.app_page

    st.sidebar.markdown('<div class="fs-side-section">Explore</div>',
                        unsafe_allow_html=True)
    nav_demo, nav_tech = st.sidebar.columns(2)
    nav_demo.button(
        "Live demo",
        type="primary" if page == "demo" else "secondary",
        key="nav_demo",
        on_click=_set_app_page,
        args=("demo",),
    )
    nav_tech.button(
        "Technical",
        type="primary" if page == "technical" else "secondary",
        key="nav_technical",
        on_click=_set_app_page,
        args=("technical",),
    )

    st.sidebar.divider()
    st.sidebar.markdown('<div class="fs-side-section">Choose an incident</div>',
                        unsafe_allow_html=True)
    source_mode = st.sidebar.selectbox(
        "Incident source",
        list(SOURCE_LABELS),
        format_func=lambda key: SOURCE_LABELS[key],
        key="incident_source_mode",
    )

    selected_incident: IncidentChoice | None = None
    selected_key: str | None = None
    if source_mode == "demo":
        if "selected_incident_key" not in st.session_state:
            st.session_state.selected_incident_key = (
                active_incident.key if active_incident else DEFAULT_INCIDENT_KEY
            )
        selected_key = st.sidebar.selectbox(
            "Curated scenario",
            list(INCIDENTS),
            format_func=lambda key: INCIDENTS[key].selector_label,
            key="selected_incident_key",
        )
        selected_incident = INCIDENTS[selected_key]
        st.sidebar.markdown(
            f"""
            <div class="fs-incident-card">
              <strong>{escape(selected_incident.title)}</strong>
              <span>{escape(selected_incident.hazard)} · {escape(selected_incident.product)}</span><br>
              <span>{escape(selected_incident.response_angle)}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        refresh_col, cache_col = st.sidebar.columns([1, 1.5])
        refresh = refresh_col.button("Refresh feed", key=f"refresh_{source_mode}")
        cache_col.caption("10-minute API cache")
        if refresh:
            _load_live_incidents.clear()
        live_incidents, source_error = _load_live_incidents(source_mode)
        if source_error:
            st.sidebar.error(source_error)
            st.sidebar.caption(
                "This authority feed failed independently. Choose openFDA or a "
                "curated demo; the rest of FoodShock remains available."
            )
        if live_incidents:
            by_key = {incident.key: incident for incident in live_incidents}
            live_widget_key = f"selected_{source_mode}_incident_key"
            if st.session_state.get(live_widget_key) not in by_key:
                st.session_state[live_widget_key] = live_incidents[0].key
            selected_key = st.sidebar.selectbox(
                "Official incident record",
                list(by_key),
                format_func=lambda key: by_key[key].selector_label,
                key=live_widget_key,
            )
            selected_incident = by_key[selected_key]
            st.sidebar.markdown(
                f"""
                <div class="fs-incident-card">
                  <strong>{escape(selected_incident.title)}</strong>
                  <span>{escape(selected_incident.authority)} ·
                    {escape(selected_incident.classification)} ·
                    {escape(selected_incident.status)}</span><br>
                  <span>{escape(selected_incident.product_summary)}</span><br>
                  <span>{escape(selected_incident.reason_summary)}</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.sidebar.warning(selected_incident.trust_warning)
            st.sidebar.caption(
                f"Retrieved {selected_incident.retrieved_at}. The incident is real; "
                "the food-bank inventory and purchase orders remain synthetic. "
                "Zero exposure is a valid result."
            )
            st.sidebar.markdown(
                f"[Open official source record]({selected_incident.source_url})"
            )
        else:
            st.sidebar.info("No incidents are currently available from this feed.")

    run_label = (
        "Run curated simulation" if source_mode == "demo"
        else "Analyze official incident"
    )
    if st.sidebar.button(
        run_label,
        type="primary",
        help=(
            "Reset this session's synthetic operations, ingest the selected incident, "
            "and animate the bounded agent workflow"
        ),
        key="run_incident",
        disabled=selected_incident is None,
    ):
        try:
            result = _run_animated_replay(conn, selected_incident)
        except ExtractionUnavailable as exc:
            st.sidebar.error(f"Extraction unavailable: {exc}")
            st.stop()
        st.session_state.replay_count = st.session_state.get("replay_count", 0) + 1
        st.session_state.last_replay_key = selected_key
        st.session_state.last_run_id = result.run_id
        st.session_state.last_run_has_exposure = result.has_exposure
        st.session_state.active_runtime_incident = selected_incident
        st.rerun()

    replay_count = st.session_state.get("replay_count", 0)
    last_replay_key = st.session_state.get("last_replay_key")
    if selected_incident and replay_count and last_replay_key == selected_key:
        outcome = (
            "Exposure assessment ready."
            if st.session_state.get("last_run_has_exposure")
            else "No operational exposure found; zero-match assessment ready."
        )
        st.sidebar.success(
            f"Run {replay_count} complete. {outcome}"
        )
    elif (
        selected_incident
        and active_choice
        and active_choice.key != selected_key
    ):
        st.sidebar.info(
            f"Current run: {active_choice.title}. Run the selection above to switch incidents."
        )

    st.sidebar.markdown('<div class="fs-side-section">Display</div>',
                        unsafe_allow_html=True)
    st.sidebar.toggle(
        "Expand agent trace",
        value=False,
        key="expand_trace",
        help="Open the persisted tool-call transcript in the live demo",
    )

    with st.sidebar.expander("Project and demo disclosures"):
        st.markdown(
            "- **Incident source:** official API record or curated replay\n"
            "- **Operations:** synthetic inventory, POs, demand, and recovery market\n"
            "- **Source boundary:** APIs never manufacture operational matches\n"
            "- **Language layer:** structured API mapping or cached notice extraction\n"
            "- **Control:** deterministic matching, projection, and optimization\n"
            "- **Execution:** no food-safety or plan action without human review"
        )
    with st.sidebar.expander("Runtime details"):
        st.caption(
            f"DB: `{db_path.name}` — "
            f"{st.session_state.get('db_mode', 'shared via $FOODSHOCK_DB')}"
        )
        st.caption(
            "Language layer: "
            + ("live model allowed" if ALLOW_LLM else "offline cache/template")
        )
        st.caption("Authority APIs use deterministic field mapping with provenance checks.")

    if page == "technical":
        _technical_page(conn)
        return

    if not ready:
        _hero(
            "Interactive framework demo",
            "Choose an incident. Watch the agent build the response.",
            "The sidebar seeds a session-isolated synthetic network and animates "
            "the real tool-event stream from notice ingestion to operator approval.",
        )
        st.info("Choose an incident in the sidebar, then run the simulation.")
        return

    if not event_id:
        _hero(
            "Scenario ready",
            "No active incident",
            "Choose an incident in the sidebar to start the operator workflow.",
        )
        return

    active_incident = incident_for_event(event_id)
    active_runtime_incident = st.session_state.get("active_runtime_incident")
    if (
        active_runtime_incident is not None
        and active_runtime_incident.event_id == event_id
    ):
        active_choice = active_runtime_incident
    else:
        active_choice = active_incident
    eyebrow = f"Active incident · {event_id}"
    if isinstance(active_choice, DemoIncident):
        subtitle = active_choice.response_angle
        eyebrow += f" · {active_choice.hazard}"
    elif isinstance(active_choice, LiveIncident):
        subtitle = (
            f"{active_choice.reason_summary} Official incident evidence is matched "
            "against a clearly labeled synthetic food-bank network."
        )
        eyebrow += f" · {active_choice.classification}"
    else:
        subtitle = (
            "Trace exposure, quantify seven-day supply risk, and review the "
            "human-gated assessment."
        )
    _hero(
        eyebrow,
        "Recall response command center",
        subtitle,
    )
    if isinstance(active_choice, LiveIncident):
        st.warning(
            f"**Live authority incident · synthetic operations.** "
            f"{active_choice.trust_warning}"
        )
        st.caption(
            f"Retrieved {active_choice.retrieved_at} from "
            f"[{active_choice.source_label}]({active_choice.source_url}). "
            "The source did not supply or confirm any displayed inventory or PO record."
        )

    run_id = _latest_run_id(conn)
    if run_id:
        expl = rows(
            conn,
            "SELECT content_json FROM agent_transcript WHERE run_id=? "
            "AND kind='narration' AND name='explain' ORDER BY seq DESC LIMIT 1",
            (run_id,),
        )
        if expl:
            content = json.loads(expl[0]["content_json"])
            st.markdown(f"**Agent ({content['method']}):** {content['text']}")
        ba_row = rows(
            conn,
            "SELECT content_json FROM agent_transcript WHERE run_id=? "
            "AND kind='tool_result' AND name='before_after' "
            "ORDER BY seq DESC LIMIT 1",
            (run_id,),
        )
        if ba_row:
            runtime_s = json.loads(ba_row[0]["content_json"])["result"].get("runtime_s")
            if runtime_s is not None:
                st.caption(
                    f"Response-planning time: {runtime_s:g} s measured for this run, "
                    "notice to draft plan. Comparator: 2.0 staff-hours from a "
                    "hypothetical task model (20m triage + 60m trace + 25m replan "
                    "+ 15m comms); not operator-validated."
                )
        transcript = _transcript_lines(conn, run_id)
        with st.expander(
            f"Agent transcript — {run_id} ({len(transcript)} steps: "
            "observe → investigate → explain → approve)",
            expanded=st.session_state.get("expand_trace", False),
        ):
            st.code("\n".join(transcript), language=None)

    tabs = st.tabs([
        "1 · Exposure queue",
        "2 · Impact dashboard",
        "3 · Recovery plan",
        "4 · Supply-chain graph",
        "5 · Map",
    ])
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
