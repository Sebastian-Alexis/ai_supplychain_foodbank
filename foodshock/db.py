"""SQLite layer: schema, connection helpers, shared queries, and the
safety-invariant exclusion helpers used by projection and optimization.

THE invariant (PLAN.md §10-§11) lives in `available_lots` / `expected_pos`:
confirmed recalled or quarantined stock is excluded under EVERY scenario;
scenario toggles cover only unconfirmed (probable/possible) matches and
at-risk POs. A clearance review action is the only way back into the pool.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DEFAULT_DB = DATA_DIR / "foodshock.db"

# Precedence for effective match state. 'unknown' MUST rank above zero so a
# lot whose only evidence is insufficient records is still recorded and
# excluded from the conservative pool (PLAN.md §9 match states, §11 pool).
# 'not_matched' intentionally ranks 0: it never excludes anything.
STATE_RANK = {"confirmed": 4, "probable": 3, "possible": 2, "unknown": 1}

DDL = """
PRAGMA foreign_keys=ON;

DROP TABLE IF EXISTS agent_transcript;
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS comms;
DROP TABLE IF EXISTS plan_lines;
DROP TABLE IF EXISTS plans;
DROP TABLE IF EXISTS matches;
DROP TABLE IF EXISTS recall_events;
DROP TABLE IF EXISTS replacement_offers;
DROP TABLE IF EXISTS substitutions;
DROP TABLE IF EXISTS distribution_plans;
DROP TABLE IF EXISTS purchase_orders;
DROP TABLE IF EXISTS inventory_lots;
DROP TABLE IF EXISTS pantry_demand;
DROP TABLE IF EXISTS pantries;
DROP TABLE IF EXISTS warehouses;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS facilities;
DROP TABLE IF EXISTS supplier_aliases;
DROP TABLE IF EXISTS suppliers;

CREATE TABLE suppliers (
    supplier_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT, state TEXT,
    lat REAL, lon REAL
);
CREATE TABLE supplier_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    alias TEXT NOT NULL
);
CREATE TABLE facilities (
    facility_id TEXT PRIMARY KEY,
    supplier_id TEXT REFERENCES suppliers(supplier_id),
    name TEXT NOT NULL,
    city TEXT, state TEXT,
    lat REAL, lon REAL
);
CREATE TABLE products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('produce','protein','grain','dairy','shelf_stable')),
    allergens TEXT NOT NULL DEFAULT '',              -- comma-separated tokens, e.g. 'wheat'
    upc TEXT,
    unit_cost_per_lb REAL NOT NULL,
    temperature_zone TEXT NOT NULL CHECK (temperature_zone IN ('ambient','refrigerated','frozen')),
    shelf_life_days INTEGER NOT NULL
);
CREATE TABLE warehouses (
    warehouse_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT, state TEXT,
    lat REAL, lon REAL
);
CREATE TABLE pantries (
    pantry_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT, state TEXT,
    lat REAL, lon REAL,
    has_refrigeration INTEGER NOT NULL DEFAULT 1,
    has_freezer INTEGER NOT NULL DEFAULT 1,
    storage_capacity_lb REAL NOT NULL,
    service_floor REAL NOT NULL DEFAULT 0.6,
    allergen_restrictions TEXT NOT NULL DEFAULT ''   -- comma-separated tokens the pantry cannot accept
);
CREATE TABLE pantry_demand (
    pantry_id TEXT NOT NULL REFERENCES pantries(pantry_id),
    category TEXT NOT NULL,
    daily_demand_lb REAL NOT NULL,
    PRIMARY KEY (pantry_id, category)
);
CREATE TABLE inventory_lots (
    lot_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(product_id),
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    facility_id TEXT REFERENCES facilities(facility_id),
    supplier_lot_code TEXT,
    quantity_lb REAL NOT NULL,
    received_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    warehouse_id TEXT NOT NULL REFERENCES warehouses(warehouse_id),
    status TEXT NOT NULL DEFAULT 'available'
        CHECK (status IN ('available','quarantine_proposed','quarantined','cleared'))
);
CREATE TABLE purchase_orders (
    po_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    product_id TEXT NOT NULL REFERENCES products(product_id),
    quantity_lb REAL NOT NULL,
    unit_cost_per_lb REAL NOT NULL,
    ordered_at TEXT NOT NULL,
    expected_delivery TEXT NOT NULL,
    warehouse_id TEXT NOT NULL REFERENCES warehouses(warehouse_id),  -- destination
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','at_risk','canceled','received'))
);
CREATE TABLE distribution_plans (
    dist_id TEXT PRIMARY KEY,
    pantry_id TEXT NOT NULL REFERENCES pantries(pantry_id),
    warehouse_id TEXT NOT NULL REFERENCES warehouses(warehouse_id),
    product_id TEXT NOT NULL REFERENCES products(product_id),
    quantity_lb REAL NOT NULL,
    scheduled_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned','infeasible','substituted'))
);
CREATE TABLE substitutions (
    sub_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL REFERENCES products(product_id),
    substitute_product_id TEXT NOT NULL REFERENCES products(product_id),
    culinary_score REAL NOT NULL,
    note TEXT
);
CREATE TABLE replacement_offers (
    offer_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    product_id TEXT NOT NULL REFERENCES products(product_id),
    available_lb REAL NOT NULL,
    unit_cost_per_lb REAL NOT NULL,
    lead_time_days INTEGER NOT NULL,
    receiving_warehouse_id TEXT NOT NULL REFERENCES warehouses(warehouse_id)
);
CREATE TABLE recall_events (
    event_id TEXT PRIMARY KEY,
    authority TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    published_at TEXT,
    ingested_at TEXT NOT NULL,
    source_url TEXT,
    raw_text TEXT NOT NULL,
    extraction_json TEXT,
    extraction_confidence REAL,
    extraction_method TEXT,
    human_confirmed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE matches (
    match_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES recall_events(event_id),
    target_type TEXT NOT NULL CHECK (target_type IN ('lot','po','donation')),
    target_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('confirmed','probable','possible','not_matched','unknown')),
    tier INTEGER,
    score REAL,
    evidence_json TEXT NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0,
    review_action TEXT CHECK (review_action IN ('cleared','quarantined')),
    reviewed_at TEXT
);
CREATE TABLE plans (
    plan_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('baseline','recommended')),
    method TEXT NOT NULL,
    objective_json TEXT,
    metrics_json TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','rejected')),
    approved_at TEXT,
    approved_by TEXT
);
CREATE TABLE plan_lines (
    line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    action TEXT NOT NULL CHECK (action IN ('purchase','allocate','transfer','substitute')),
    product_id TEXT NOT NULL REFERENCES products(product_id),
    from_id TEXT,           -- purchase: offer_id | transfer: source warehouse | allocate: serving warehouse
    to_id TEXT,             -- purchase: receiving warehouse | transfer: destination warehouse | allocate: pantry
    day INTEGER,            -- offset from TODAY: arrival (purchase) / movement (transfer) / service (allocate)
    quantity_lb REAL NOT NULL CHECK (quantity_lb > 0),
    unit_cost_per_lb REAL,
    note TEXT
);
CREATE TABLE comms (
    comm_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    audience TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'template'
        CHECK (method IN ('template','cached-llm','live-llm')),
    created_at TEXT NOT NULL
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT
);
CREATE TABLE agent_transcript (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('observe','investigate','explain','approve')),
    kind TEXT NOT NULL CHECK (kind IN ('tool_call','tool_result','gap','narration')),
    name TEXT,
    content_json TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn(db_path: str | os.PathLike | None = None) -> sqlite3.Connection:
    path = Path(db_path or os.environ.get("FOODSHOCK_DB", DEFAULT_DB))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.commit()


def insert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    cols = ", ".join(row)
    ph = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})", list(row.values()))


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def audit(conn: sqlite3.Connection, actor: str, action: str, detail: dict | None = None) -> None:
    insert(conn, "audit_log", {
        "at": now_iso(), "actor": actor, "action": action,
        "detail_json": json.dumps(detail or {}),
    })


# ---------------------------------------------------------------- queries

def query_inventory(conn, *, supplier: str | None = None, product: str | None = None,
                    lot_code: str | None = None, category: str | None = None,
                    statuses: tuple[str, ...] | None = None) -> list[dict]:
    """Inventory joined with product+supplier; text filters match names and aliases."""
    sql = """
    SELECT l.*, p.name AS product_name, p.category, p.temperature_zone,
           p.unit_cost_per_lb, s.name AS supplier_name
    FROM inventory_lots l
    JOIN products p ON p.product_id = l.product_id
    JOIN suppliers s ON s.supplier_id = l.supplier_id
    WHERE 1=1
    """
    params: list = []
    if supplier:
        sql += """ AND (LOWER(s.name) LIKE ? OR l.supplier_id IN (
                     SELECT supplier_id FROM supplier_aliases WHERE LOWER(alias) LIKE ?))"""
        like = f"%{supplier.lower()}%"
        params += [like, like]
    if product:
        sql += " AND LOWER(p.name) LIKE ?"
        params.append(f"%{product.lower()}%")
    if lot_code:
        sql += " AND l.supplier_lot_code = ?"
        params.append(lot_code)
    if category:
        sql += " AND p.category = ?"
        params.append(category)
    if statuses:
        sql += f" AND l.status IN ({','.join('?' for _ in statuses)})"
        params += list(statuses)
    return rows(conn, sql, tuple(params))


def query_purchase_orders(conn, *, supplier: str | None = None, product: str | None = None,
                          statuses: tuple[str, ...] | None = None) -> list[dict]:
    sql = """
    SELECT po.*, p.name AS product_name, p.category, s.name AS supplier_name
    FROM purchase_orders po
    JOIN products p ON p.product_id = po.product_id
    JOIN suppliers s ON s.supplier_id = po.supplier_id
    WHERE 1=1
    """
    params: list = []
    if supplier:
        sql += """ AND (LOWER(s.name) LIKE ? OR po.supplier_id IN (
                     SELECT supplier_id FROM supplier_aliases WHERE LOWER(alias) LIKE ?))"""
        like = f"%{supplier.lower()}%"
        params += [like, like]
    if product:
        sql += " AND LOWER(p.name) LIKE ?"
        params.append(f"%{product.lower()}%")
    if statuses:
        sql += f" AND po.status IN ({','.join('?' for _ in statuses)})"
        params += list(statuses)
    return rows(conn, sql, tuple(params))


# --------------------------------------------- safety-invariant helpers

def effective_states(conn, target_type: str = "lot",
                     event_id: str | None = None) -> dict[str, str]:
    """Worst effective match state per target (lot/po/donation).

    Review overrides evidence: review_action='cleared' discards the match,
    'quarantined' escalates to confirmed-equivalent.

    Default scope is GLOBAL (all events) -- the safety pool helpers MUST see
    every active recall's evidence. Pass `event_id` to attribute states to
    one incident's own evidence (propagate's incident metrics).
    """
    out: dict[str, str] = {}
    sql = "SELECT target_id, state, reviewed, review_action FROM matches WHERE target_type=?"
    params: tuple = (target_type,)
    if event_id is not None:
        sql += " AND event_id=?"
        params += (event_id,)
    for m in rows(conn, sql, params):
        if m["reviewed"]:
            if m["review_action"] == "cleared":
                continue
            if m["review_action"] == "quarantined":
                state = "confirmed"
            else:
                state = m["state"]
        else:
            state = m["state"]
        if STATE_RANK.get(state, 0) > STATE_RANK.get(out.get(m["target_id"], ""), 0):
            out[m["target_id"]] = state
    return out


def available_lots(conn, scenario: str) -> list[dict]:
    """Lots usable for projection/planning under `scenario`.

    INVARIANT: status in ('quarantine_proposed','quarantined') or effective
    match state 'confirmed' => excluded under EVERY scenario. 'conservative'
    additionally excludes unresolved 'probable'/'possible'.
    """
    if scenario not in ("optimistic", "conservative"):
        raise ValueError(f"unknown scenario: {scenario}")
    states = effective_states(conn, "lot")
    out = []
    for lot in query_inventory(conn, statuses=("available", "cleared")):
        eff = states.get(lot["lot_id"])
        if eff == "confirmed":
            continue  # never included, no toggle can re-include (PLAN.md §10)
        if scenario == "conservative" and eff in ("probable", "possible", "unknown"):
            continue
        out.append(lot)
    return out


def expected_pos(conn, scenario: str) -> list[dict]:
    """Inbound POs counted under `scenario`. 'canceled'/'received' never count;
    'at_risk' counts only under 'optimistic'.

    Defense in depth mirroring available_lots: an effective 'confirmed' match
    excludes a PO under EVERY scenario, and 'conservative' also excludes
    unresolved probable/possible/unknown matches -- even before propagate()
    has flagged the PO at_risk.
    """
    if scenario not in ("optimistic", "conservative"):
        raise ValueError(f"unknown scenario: {scenario}")
    statuses = ("open", "at_risk") if scenario == "optimistic" else ("open",)
    states = effective_states(conn, "po")
    out = []
    for po in query_purchase_orders(conn, statuses=statuses):
        eff = states.get(po["po_id"])
        if eff == "confirmed":
            continue  # recalled product inbound: no toggle re-includes it
        if scenario == "conservative" and eff in ("probable", "possible", "unknown"):
            continue
        out.append(po)
    return out
