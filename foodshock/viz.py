"""Presentation-layer derivations (PLAN.md §13 views 4-5).

Everything here is a pure READ of the SQLite system of record: the lineage
graph is derived from the relational tables via joins (§8) and NetworkX holds
only display structure (§7 committed stack). Map rows are plain DataFrames
for pydeck. No function mutates the database.
"""

from __future__ import annotations

import sqlite3

import networkx as nx
import pandas as pd
import plotly.graph_objects as go
import pydeck as pdk

from .db import effective_states, rows

STATE_COLORS = {
    "confirmed": "#c0392b", "probable": "#e67e22", "possible": "#b7950b",
    "unknown": "#7f8c8d", "cleared": "#1e8449",
}
KIND_COLORS = {"event": "#7b241c", "supplier": "#5d6d7e", "facility": "#85929e",
               "product": "#2e86c1", "warehouse": "#117864", "pantry": "#6c3483"}
_COLUMN = {"event": 0, "supplier": 1, "facility": 2, "product": 3,
           "lot": 4, "po": 4, "warehouse": 5, "pantry": 6}
_COLUMN_TITLES = ["recall", "suppliers", "facilities", "products",
                  "lots / inbound POs", "warehouses", "pantries"]

_RGB = {  # STATE_COLORS/KIND_COLORS as [r, g, b] for pydeck
    "confirmed": [192, 57, 43], "probable": [230, 126, 34], "possible": [183, 149, 11],
    "unknown": [127, 140, 141], "cleared": [30, 132, 73],
}


def latest_event_id(conn: sqlite3.Connection) -> str | None:
    r = rows(conn, "SELECT event_id FROM recall_events "
                   "ORDER BY ingested_at DESC, event_id DESC LIMIT 1")
    return r[0]["event_id"] if r else None


def latest_plan_ids(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """(latest baseline, latest recommended), each None until plans exist."""
    out = []
    for kind in ("baseline", "recommended"):
        r = rows(conn, "SELECT plan_id FROM plans WHERE kind=? "
                       "ORDER BY created_at DESC, rowid DESC LIMIT 1", (kind,))
        out.append(r[0]["plan_id"] if r else None)
    return out[0], out[1]


def display_states(conn: sqlite3.Connection, target_type: str) -> dict[str, str]:
    """Worst display state per target for graph/map coloring -- GLOBAL across
    events (a lot confirmed by any active recall must never render clear).

    Reuses db.effective_states (the safety code path) for the worst ACTIVE
    evidence -- review overrides it there (quarantined -> confirmed, cleared
    evidence dropped). A target is labeled 'cleared' (green) only when NO
    active evidence remains and at least one review cleared it. 'not_matched'
    never shows.
    """
    out = dict(effective_states(conn, target_type))
    for m in rows(conn, "SELECT DISTINCT target_id FROM matches "
                        "WHERE target_type=? AND reviewed=1 AND review_action='cleared'",
                  (target_type,)):
        out.setdefault(m["target_id"], "cleared")
    return out


def _event_targets(conn: sqlite3.Connection, event_id: str, target_type: str) -> set[str]:
    """Target ids evidenced by `event_id`: any match row EXCEPT 'not_matched'
    (a look-alike this event examined and rejected must not inherit another
    event's color via global display_states). Cleared rows stay: state holds
    the original evidence; review_action records the clearance."""
    return {r["target_id"] for r in rows(
        conn, "SELECT DISTINCT target_id FROM matches "
              "WHERE event_id=? AND target_type=? AND state <> 'not_matched'",
        (event_id, target_type))}


def _implicated_dist_rows(conn: sqlite3.Connection,
                          pairs: set[tuple[str, str]]) -> list[dict]:
    """distribution_plans aggregated per (pantry, warehouse), restricted to
    implicated (product_id, warehouse_id) pairs. A plan drawing the same
    product from an UNINVOLVED warehouse is not downstream of the exposure
    and must not render as a route (PLAN.md §19: no fabricated lineage)."""
    if not pairs:
        return []
    agg: dict[tuple[str, str], dict] = {}
    for r in rows(conn, """SELECT pantry_id, warehouse_id, product_id,
                                  SUM(quantity_lb) lb, SUM(status = 'infeasible') bad
                           FROM distribution_plans
                           GROUP BY pantry_id, warehouse_id, product_id"""):
        if (r["product_id"], r["warehouse_id"]) not in pairs:
            continue
        a = agg.setdefault((r["pantry_id"], r["warehouse_id"]),
                           {"pantry_id": r["pantry_id"], "warehouse_id": r["warehouse_id"],
                            "lb": 0.0, "bad": 0})
        a["lb"] += r["lb"]
        a["bad"] += r["bad"]
    return [agg[k] for k in sorted(agg)]


# ------------------------------------------------------------ lineage graph

def lineage_graph(conn: sqlite3.Connection, event_id: str) -> nx.DiGraph:
    """Implicated-chain lineage derived via joins (PLAN.md §8), left to right:
    event -> suppliers/facilities and products CONVERGE on the specific lot or
    PO record -> warehouse -> pantry.

    Membership is scoped to `event_id` (only entities with a match row for
    THIS event appear; 'not_matched' look-alikes stay out), while node COLOR
    stays globally effective via display_states: a lot another active recall
    confirms must not render clear here.

    Record attributes converge (supplier/facility -> record <- product); the
    shared product node is never a hub BETWEEN suppliers, so no path implies
    supplier A's chain reaches supplier B's lot or PO -- lineage ambiguity the
    graph must not fabricate (PLAN.md §19). Warehouse -> pantry edges are the
    distribution_plans routes (warehouse_id FK) restricted to implicated
    (product, warehouse) pairs -- see _implicated_dist_rows.
    """
    G = nx.DiGraph()
    ev_lots = _event_targets(conn, event_id, "lot")
    ev_pos = _event_targets(conn, event_id, "po")
    lot_states = {k: v for k, v in display_states(conn, "lot").items() if k in ev_lots}
    po_states = {k: v for k, v in display_states(conn, "po").items() if k in ev_pos}
    ev = rows(conn, "SELECT * FROM recall_events WHERE event_id=?", (event_id,))[0]
    G.add_node(("event", event_id), kind="event", label=event_id, state=None,
               detail=f"{ev['authority']} · status {ev['status']}")

    lots = {l["lot_id"]: l for l in rows(conn, """
        SELECT l.*, p.name product_name, p.category, s.name supplier_name, f.name facility_name
        FROM inventory_lots l
        JOIN products p USING (product_id)
        JOIN suppliers s ON s.supplier_id = l.supplier_id
        LEFT JOIN facilities f ON f.facility_id = l.facility_id""")}
    pos = {p["po_id"]: p for p in rows(conn, """
        SELECT po.*, p.name product_name, p.category, s.name supplier_name
        FROM purchase_orders po
        JOIN products p USING (product_id)
        JOIN suppliers s ON s.supplier_id = po.supplier_id""")}
    warehouses = {w["warehouse_id"]: w for w in rows(conn, "SELECT * FROM warehouses")}
    pantries = {p["pantry_id"]: p for p in rows(conn, "SELECT * FROM pantries")}

    implicated_pw: set[tuple[str, str]] = set()  # (product_id, warehouse_id)

    def _supplier(sid: str, name: str):
        n = ("supplier", sid)
        G.add_node(n, kind="supplier", label=name, state=None, detail="")
        G.add_edge(("event", event_id), n)
        return n

    def _product(pid: str, name: str, category: str):
        n = ("product", pid)
        G.add_node(n, kind="product", label=name, state=None, detail=category)
        return n

    for lot_id, st in sorted(lot_states.items()):
        l = lots[lot_id]
        implicated_pw.add((l["product_id"], l["warehouse_id"]))
        sup = _supplier(l["supplier_id"], l["supplier_name"])
        prod = _product(l["product_id"], l["product_name"], l["category"])
        node = ("lot", lot_id)
        G.add_node(node, kind="lot", label=lot_id, state=st,
                   detail=f"{l['quantity_lb']:.0f} lb · status {l['status']} · "
                          f"lot code {l['supplier_lot_code'] or '(none)'}")
        if l["facility_id"]:  # sourcing lineage: through the packing facility
            fac = ("facility", l["facility_id"])
            G.add_node(fac, kind="facility", label=l["facility_name"], state=None, detail="")
            G.add_edge(sup, fac)
            G.add_edge(fac, node)
        else:
            G.add_edge(sup, node)
        G.add_edge(prod, node)  # product attribute converges on the record
        wh = ("warehouse", l["warehouse_id"])
        G.add_node(wh, kind="warehouse", label=warehouses[l["warehouse_id"]]["name"],
                   state=None, detail="")
        G.add_edge(node, wh)

    for po_id, st in sorted(po_states.items()):
        p = pos[po_id]
        implicated_pw.add((p["product_id"], p["warehouse_id"]))
        sup = _supplier(p["supplier_id"], p["supplier_name"])
        prod = _product(p["product_id"], p["product_name"], p["category"])
        node = ("po", po_id)
        G.add_node(node, kind="po", label=po_id, state=st,
                   detail=f"{p['quantity_lb']:.0f} lb inbound · ETA {p['expected_delivery']} · "
                          f"status {p['status']}")
        G.add_edge(sup, node)   # POs carry no facility linkage
        G.add_edge(prod, node)  # converging attribute, never a supplier hub
        wh = ("warehouse", p["warehouse_id"])
        G.add_node(wh, kind="warehouse", label=warehouses[p["warehouse_id"]]["name"],
                   state=None, detail="")
        G.add_edge(node, wh)  # destination: the PO's receiving warehouse

    # Pantries with planned lines drawing an implicated product FROM a
    # warehouse where implicated stock actually lands -- the warehouse_id FK
    # on each distribution row makes warehouse -> pantry a real route, and the
    # (product, warehouse) pair restriction keeps a same-product plan at an
    # uninvolved warehouse from fabricating a downstream branch.
    per_pantry: dict[str, dict] = {}
    dist_rows = _implicated_dist_rows(conn, implicated_pw)
    for d in dist_rows:
        agg = per_pantry.setdefault(d["pantry_id"], {"lb": 0.0, "bad": 0, "warehouses": set()})
        agg["lb"] += d["lb"]
        agg["bad"] += d["bad"]
        agg["warehouses"].add(d["warehouse_id"])

    for pantry_id, agg in sorted(per_pantry.items()):
        pan = ("pantry", pantry_id)
        infeasible = agg["bad"] > 0
        detail = f"{agg['lb']:.0f} lb planned (implicated products, 7 days)"
        if infeasible:
            detail += f" · {agg['bad']} line(s) infeasible"
        G.add_node(pan, kind="pantry", label=pantries[pantry_id]["name"], state=None,
                   detail=detail, infeasible=infeasible)
        for wh_id in sorted(agg["warehouses"]):
            wh = ("warehouse", wh_id)
            if wh not in G:
                G.add_node(wh, kind="warehouse", label=warehouses[wh_id]["name"],
                           state=None, detail="")
            G.add_edge(wh, pan)
    return G


def graph_figure(G: nx.DiGraph) -> go.Figure:
    """Layered left-to-right layout (deterministic: sorted within columns)."""
    cols: dict[int, list] = {}
    for n, d in G.nodes(data=True):
        cols.setdefault(_COLUMN[d["kind"]], []).append(n)
    pos: dict = {}
    for x, nodes in cols.items():
        nodes.sort(key=lambda n: (G.nodes[n]["kind"], G.nodes[n]["label"]))
        for i, n in enumerate(nodes):
            pos[n] = (x, (i + 1) / (len(nodes) + 1))

    edge_x, edge_y = [], []
    for a, b in G.edges():
        edge_x += [pos[a][0], pos[b][0], None]
        edge_y += [pos[a][1], pos[b][1], None]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=edge_x, y=edge_y, mode="lines",
                             line=dict(color="#b0b7bc", width=1.2),
                             hoverinfo="skip", showlegend=False))

    xs, ys, colors, symbols, texts, hovers, sizes = [], [], [], [], [], [], []
    for n, d in G.nodes(data=True):
        x, y = pos[n]
        xs.append(x)
        ys.append(y)
        state = d.get("state")
        if state:
            colors.append(STATE_COLORS[state])
        elif d["kind"] == "pantry" and d.get("infeasible"):
            colors.append(STATE_COLORS["confirmed"])
        else:
            colors.append(KIND_COLORS[d["kind"]])
        symbols.append({"event": "diamond", "po": "square"}.get(d["kind"], "circle"))
        sizes.append(26 if d["kind"] == "event" else 20)
        texts.append(d["label"])
        hover = f"<b>{d['label']}</b><br>{d['kind']}"
        if state:
            hover += f" · match: <b>{state}</b>"
        if d.get("detail"):
            hover += f"<br>{d['detail']}"
        hovers.append(hover)
    fig.add_trace(go.Scatter(
        x=xs, y=ys, mode="markers+text", text=texts, textposition="bottom center",
        textfont=dict(size=11), hovertext=hovers, hoverinfo="text",
        marker=dict(color=colors, size=sizes, symbol=symbols,
                    line=dict(color="#2c3e50", width=1)),
        showlegend=False))

    for x, title in enumerate(_COLUMN_TITLES):
        if x in cols:
            fig.add_annotation(x=x, y=1.09, text=f"<b>{title}</b>", showarrow=False,
                               font=dict(size=12, color="#566573"))
    fig.update_layout(
        xaxis=dict(visible=False, range=[-0.5, 6.5]),
        yaxis=dict(visible=False, range=[-0.08, 1.16]),
        margin=dict(l=10, r=10, t=10, b=10),
        height=max(430, 46 * max(len(v) for v in cols.values())),
        plot_bgcolor="white", paper_bgcolor="white")
    return fig


# ------------------------------------------------------------------ map

def map_points(conn: sqlite3.Connection) -> pd.DataFrame:
    """One row per mappable site: suppliers, facilities, warehouses, pantries.

    Roles are operational-evidence roles (implicated / replacement source /
    infeasible distributions) -- never proximity to human cases (§13 rule).
    """
    lot_states = display_states(conn, "lot")
    po_states = display_states(conn, "po")
    lots = rows(conn, "SELECT lot_id, supplier_id, facility_id, warehouse_id FROM inventory_lots")
    pos = rows(conn, "SELECT po_id, supplier_id FROM purchase_orders")

    def _live(st: str | None) -> bool:
        return st is not None and st != "cleared"

    implicated_sup = ({l["supplier_id"] for l in lots if _live(lot_states.get(l["lot_id"]))}
                      | {p["supplier_id"] for p in pos if _live(po_states.get(p["po_id"]))})
    implicated_fac = {l["facility_id"] for l in lots
                      if l["facility_id"] and _live(lot_states.get(l["lot_id"]))}
    offer_sup = {o["supplier_id"] for o in rows(conn, "SELECT supplier_id FROM replacement_offers")}
    bad_pantry = {r["pantry_id"] for r in rows(
        conn, "SELECT DISTINCT pantry_id FROM distribution_plans WHERE status='infeasible'")}

    recs = []
    for s in rows(conn, "SELECT * FROM suppliers"):
        if s["lat"] is None:
            continue
        if s["supplier_id"] in implicated_sup:
            role, color = "implicated supplier", _RGB["confirmed"]
        elif s["supplier_id"] in offer_sup:
            role, color = "replacement source", _RGB["cleared"]
        else:
            role, color = "supplier", [93, 109, 126]
        recs.append({"id": s["supplier_id"], "name": s["name"], "kind": "supplier",
                     "role": role, "lat": s["lat"], "lon": s["lon"],
                     "color": color, "radius": 2200})
    for f in rows(conn, "SELECT * FROM facilities"):
        if f["lat"] is None:
            continue
        implicated = f["facility_id"] in implicated_fac
        recs.append({"id": f["facility_id"], "name": f["name"], "kind": "facility",
                     "role": "implicated facility" if implicated else "facility",
                     "lat": f["lat"], "lon": f["lon"],
                     "color": _RGB["confirmed"] if implicated else [133, 146, 158],
                     "radius": 1800})
    for w in rows(conn, "SELECT * FROM warehouses"):
        recs.append({"id": w["warehouse_id"], "name": w["name"], "kind": "warehouse",
                     "role": "warehouse", "lat": w["lat"], "lon": w["lon"],
                     "color": [17, 120, 100], "radius": 2000})
    for p in rows(conn, "SELECT * FROM pantries"):
        bad = p["pantry_id"] in bad_pantry
        recs.append({"id": p["pantry_id"], "name": p["name"], "kind": "pantry",
                     "role": "pantry (infeasible lines)" if bad else "pantry",
                     "lat": p["lat"], "lon": p["lon"],
                     "color": _RGB["confirmed"] if bad else [108, 52, 131],
                     "radius": 1400})
    return pd.DataFrame(recs)


def map_arcs(conn: sqlite3.Connection, plan_id: str | None = None) -> pd.DataFrame:
    """Arcs: matched lot flows supplier->warehouse (state color), implicated
    distributions warehouse->pantry, and -- when `plan_id` is a recovery plan
    -- replacement purchases offer-supplier->warehouse (green)."""
    lot_states = display_states(conn, "lot")
    po_states = display_states(conn, "po")
    sups = {s["supplier_id"]: s for s in rows(conn, "SELECT * FROM suppliers")}
    whs = {w["warehouse_id"]: w for w in rows(conn, "SELECT * FROM warehouses")}
    pans = {p["pantry_id"]: p for p in rows(conn, "SELECT * FROM pantries")}
    lots = {l["lot_id"]: l for l in rows(conn, "SELECT * FROM inventory_lots")}
    pos = {p["po_id"]: p for p in rows(conn, "SELECT * FROM purchase_orders")}

    recs = []
    implicated_pw: set[tuple[str, str]] = set()  # (product_id, warehouse_id)
    for po_id in po_states:  # inbound exposure lands at the PO's warehouse
        implicated_pw.add((pos[po_id]["product_id"], pos[po_id]["warehouse_id"]))
    for lot_id, st in sorted(lot_states.items()):
        l = lots[lot_id]
        implicated_pw.add((l["product_id"], l["warehouse_id"]))
        s, w = sups[l["supplier_id"]], whs[l["warehouse_id"]]
        if s["lat"] is None:
            continue
        recs.append({"from_lat": s["lat"], "from_lon": s["lon"],
                     "to_lat": w["lat"], "to_lon": w["lon"],
                     "color": _RGB[st], "width": max(2.0, min(6.0, l["quantity_lb"] / 250)),
                     "label": f"{lot_id}: {l['quantity_lb']:.0f} lb {st} · "
                              f"{s['name']} → {w['name']}"})

    dist_rows = _implicated_dist_rows(conn, implicated_pw)
    for d in dist_rows:
        p, w = pans[d["pantry_id"]], whs[d["warehouse_id"]]
        bad = d["bad"] > 0
        recs.append({"from_lat": w["lat"], "from_lon": w["lon"],
                     "to_lat": p["lat"], "to_lon": p["lon"],
                     "color": _RGB["confirmed"] if bad else [93, 109, 126],
                     "width": 2.0,
                     "label": f"planned {d['lb']:.0f} lb (implicated products) "
                              f"{w['name']} → {p['name']}"
                              + (f" · {d['bad']} line(s) infeasible" if bad else "")})

    if plan_id:
        offers = {o["offer_id"]: o for o in rows(conn, "SELECT * FROM replacement_offers")}
        for ln in rows(conn, "SELECT * FROM plan_lines WHERE plan_id=? AND action='purchase'",
                       (plan_id,)):
            o = offers.get(ln["from_id"])
            if not o or ln["to_id"] not in whs:
                continue
            s, w = sups[o["supplier_id"]], whs[ln["to_id"]]
            recs.append({"from_lat": s["lat"], "from_lon": s["lon"],
                         "to_lat": w["lat"], "to_lon": w["lon"],
                         "color": _RGB["cleared"],
                         "width": max(2.0, min(6.0, ln["quantity_lb"] / 250)),
                         "label": f"replacement purchase {ln['quantity_lb']:.0f} lb "
                                  f"{ln['product_id']} · {s['name']} → {w['name']}"})
    return pd.DataFrame(recs)


def map_deck(points: pd.DataFrame, arcs: pd.DataFrame, *, online: bool = False) -> pdk.Deck:
    """Deck for the map view (PLAN.md §13 view 5) from map_points/map_arcs
    frames. `online=False` (default) omits the basemap entirely -- the demo
    arc must replay with zero network (§14); `online=True` uses Carto light
    tiles. Caller guards empty `points` (no sites = nothing to frame).
    """
    pts = points.assign(label=points["name"] + " — " + points["role"])
    view = pdk.ViewState(latitude=float(pts["lat"].mean()),
                         longitude=float(pts["lon"].mean()), zoom=6.2, pitch=30)
    layers = [
        pdk.Layer("ScatterplotLayer", data=pts, get_position="[lon, lat]",
                  get_fill_color="color", get_radius="radius", pickable=True, opacity=0.85),
    ]
    if not arcs.empty:
        layers.append(pdk.Layer("ArcLayer", data=arcs,
                                get_source_position="[from_lon, from_lat]",
                                get_target_position="[to_lon, to_lat]",
                                get_source_color="color", get_target_color="color",
                                get_width="width", pickable=True))
    kw = (dict(map_provider="carto", map_style="light") if online
          else dict(map_provider=None, map_style=None))
    return pdk.Deck(layers=layers, initial_view_state=view,
                    tooltip={"html": "{label}",
                             "style": {"backgroundColor": "#1b2631", "color": "white"}},
                    **kw)
