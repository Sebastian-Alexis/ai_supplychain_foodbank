"""Lineage-graph regressions (PLAN.md §13 view 4, §19 risk: the graph must
never fabricate lineage).

1. Membership is event-scoped: a target the selected event examined and
   REJECTED (not_matched) never appears in its graph -- even when another
   active event implicates the same target (no leak via global coloring).
2. Node color stays globally effective: evidence from another active recall
   must never render weaker in the selected event's graph.
3. A PO-only exposure still routes to the distribution layer:
   supplier -> PO <- product, PO -> receiving warehouse -> pantry.
4. Record attributes CONVERGE: a shared product node is never a hub between
   suppliers, so supplier A has no path into supplier B's records.
"""

from __future__ import annotations

import networkx as nx

from foodshock.db import insert, now_iso, rows
from foodshock.viz import _COLUMN, graph_figure, lineage_graph, map_arcs, map_deck, map_points
from test_timing import _d, _mini_db


def _event(conn, event_id: str) -> None:
    insert(conn, "recall_events", {"event_id": event_id, "authority": "FDA",
                                   "status": "active", "ingested_at": now_iso(),
                                   "raw_text": f"synthetic notice {event_id}"})


def _match(conn, event_id: str, kind: str, target: str, state: str) -> None:
    insert(conn, "matches", {"event_id": event_id, "target_type": kind,
                             "target_id": target, "state": state, "tier": 1,
                             "score": 1.0, "evidence_json": "{}"})
    conn.commit()


def test_membership_scoped_to_event_with_shared_target():
    conn = _mini_db(lots=(("L-A", 900.0, 6), ("L-B", 400.0, 6)))
    _event(conn, "EV-A")
    _event(conn, "EV-B")
    _match(conn, "EV-A", "lot", "L-A", "confirmed")
    _match(conn, "EV-B", "lot", "L-A", "not_matched")  # B examined and rejected L-A
    _match(conn, "EV-B", "lot", "L-B", "possible")

    ga = lineage_graph(conn, "EV-A")
    assert ("lot", "L-A") in ga
    assert ("lot", "L-B") not in ga  # other event's evidence stays out

    gb = lineage_graph(conn, "EV-B")
    assert ("lot", "L-A") not in gb  # not_matched: no leak via EV-A's global color
    assert ("lot", "L-B") in gb


def test_node_color_stays_globally_effective():
    conn = _mini_db(lots=(("L-X", 500.0, 6),))
    _event(conn, "EV-A")
    _event(conn, "EV-B")
    _match(conn, "EV-A", "lot", "L-X", "possible")
    _match(conn, "EV-B", "lot", "L-X", "confirmed")

    ga = lineage_graph(conn, "EV-A")
    assert ga.nodes[("lot", "L-X")]["state"] == "confirmed"  # never renders weaker


def test_po_exposure_routes_to_warehouse_and_pantry():
    conn = _mini_db(po=(500.0, 2))
    insert(conn, "distribution_plans", {"dist_id": "D-1", "pantry_id": "P-1",
                                        "warehouse_id": "WH-OAK", "product_id": "PROD-X",
                                        "quantity_lb": 100.0, "scheduled_date": _d(1)})
    conn.commit()
    _event(conn, "EV-A")
    _match(conn, "EV-A", "po", "PO-T1", "probable")

    g = lineage_graph(conn, "EV-A")
    assert g.has_edge(("supplier", "SUP-A"), ("po", "PO-T1"))
    assert g.has_edge(("product", "PROD-X"), ("po", "PO-T1"))
    assert g.has_edge(("po", "PO-T1"), ("warehouse", "WH-OAK"))
    assert g.has_edge(("warehouse", "WH-OAK"), ("pantry", "P-1"))
    # Layered layout stays left-to-right: every edge advances a column.
    assert all(_COLUMN[u[0]] < _COLUMN[v[0]] for u, v in g.edges)
    graph_figure(g)  # renders without KeyError on any node kind


def test_no_path_between_suppliers_through_shared_product():
    conn = _mini_db(po=(500.0, 2), lots=(("L-A", 900.0, 6),))  # both PROD-X via SUP-A
    insert(conn, "suppliers", {"supplier_id": "SUP-B", "name": "Supplier B"})
    insert(conn, "inventory_lots", {
        "lot_id": "L-B", "product_id": "PROD-X", "supplier_id": "SUP-B",
        "supplier_lot_code": None, "quantity_lb": 300.0,
        "received_at": _d(-1) + "T12:00:00+00:00",
        "expires_at": _d(6) + "T23:00:00+00:00",
        "warehouse_id": "WH-OAK", "status": "available"})
    conn.commit()
    _event(conn, "EV-A")
    _match(conn, "EV-A", "lot", "L-A", "confirmed")
    _match(conn, "EV-A", "lot", "L-B", "possible")
    _match(conn, "EV-A", "po", "PO-T1", "probable")

    g = lineage_graph(conn, "EV-A")
    # Supplier A's chain must not reach supplier B's lot, nor vice versa,
    # even though every record shares product PROD-X.
    assert not nx.has_path(g, ("supplier", "SUP-A"), ("lot", "L-B"))
    assert not nx.has_path(g, ("supplier", "SUP-B"), ("lot", "L-A"))
    assert not nx.has_path(g, ("supplier", "SUP-B"), ("po", "PO-T1"))
    # The true attributions stay intact.
    assert nx.has_path(g, ("supplier", "SUP-A"), ("lot", "L-A"))
    assert nx.has_path(g, ("supplier", "SUP-B"), ("lot", "L-B"))


def test_same_product_plan_from_uninvolved_warehouse_stays_out():
    """PROD-X planned from both the implicated warehouse (WH-OAK holds the
    matched lot) and a clean one (WH-2): the clean warehouse must appear in
    neither the lineage graph nor the map's distribution arcs -- a
    same-product plan elsewhere is not downstream of the exposure."""
    conn = _mini_db(lots=(("L-A", 900.0, 6),))
    insert(conn, "warehouses", {"warehouse_id": "WH-2", "name": "Clean Annex",
                                "lat": 37.0, "lon": -122.0})
    for dist_id, wh in (("D-OAK", "WH-OAK"), ("D-CLEAN", "WH-2")):
        insert(conn, "distribution_plans", {"dist_id": dist_id, "pantry_id": "P-1",
                                            "warehouse_id": wh, "product_id": "PROD-X",
                                            "quantity_lb": 100.0, "scheduled_date": _d(1)})
    conn.commit()
    _event(conn, "EV-A")
    _match(conn, "EV-A", "lot", "L-A", "confirmed")

    g = lineage_graph(conn, "EV-A")
    assert g.has_edge(("warehouse", "WH-OAK"), ("pantry", "P-1"))
    assert ("warehouse", "WH-2") not in g
    # Pantry aggregate counts only the implicated route's pounds.
    assert "100 lb planned" in g.nodes[("pantry", "P-1")]["detail"]

    arcs = map_arcs(conn)
    dist_arcs = arcs[arcs["label"].str.startswith("planned")]
    assert len(dist_arcs) == 1  # WH-OAK -> P-1 only; no arc from the clean annex
    oak = rows(conn, "SELECT lat, lon FROM warehouses WHERE warehouse_id='WH-OAK'")[0]
    assert float(dist_arcs.iloc[0]["from_lat"]) == oak["lat"]
    assert float(dist_arcs.iloc[0]["from_lon"]) == oak["lon"]


def test_map_deck_spec_offline_and_online():
    """The deck the app ships (§13 view 5) must carry non-empty point/arc
    layers with the accessors deck.gl binds, a finite view state, and NO
    basemap by default -- the demo replays with zero network (§14). Online
    mode opts into Carto tiles."""
    import json as _json
    import math

    conn = _mini_db(lots=(("L-A", 900.0, 6),))
    conn.execute("UPDATE suppliers SET lat=36.7, lon=-121.6 WHERE supplier_id='SUP-A'")
    insert(conn, "distribution_plans", {"dist_id": "D-1", "pantry_id": "P-1",
                                        "warehouse_id": "WH-OAK", "product_id": "PROD-X",
                                        "quantity_lb": 100.0, "scheduled_date": _d(1)})
    conn.commit()
    _event(conn, "EV-A")
    _match(conn, "EV-A", "lot", "L-A", "confirmed")

    pts, arcs = map_points(conn), map_arcs(conn)
    spec = _json.loads(map_deck(pts, arcs).to_json())

    by_type = {l["@@type"]: l for l in spec["layers"]}
    assert set(by_type) == {"ScatterplotLayer", "ArcLayer"}
    assert len(by_type["ScatterplotLayer"]["data"]) == len(pts) >= 3
    assert len(by_type["ArcLayer"]["data"]) == len(arcs) >= 2  # lot flow + dist route
    p0 = by_type["ScatterplotLayer"]["data"][0]
    assert {"lat", "lon", "color", "radius", "label"} <= set(p0)
    assert by_type["ScatterplotLayer"]["getFillColor"] == "@@=color"
    assert by_type["ScatterplotLayer"]["getPosition"] == "@@=[lon, lat]"
    a0 = by_type["ArcLayer"]["data"][0]
    assert {"from_lat", "from_lon", "to_lat", "to_lon", "color", "width", "label"} <= set(a0)
    assert by_type["ArcLayer"]["getSourcePosition"] == "@@=[from_lon, from_lat]"
    assert by_type["ArcLayer"]["getWidth"] == "@@=width"
    view = spec["initialViewState"]
    assert math.isfinite(view["latitude"]) and math.isfinite(view["longitude"])
    # zero-network default: the shipped spec carries NO basemap keys at all
    assert "mapProvider" not in spec and "mapStyle" not in spec

    online = _json.loads(map_deck(pts, arcs, online=True).to_json())
    # pydeck resolves ('carto', 'light') to the Positron style URL on serialize
    assert online["mapProvider"] == "carto" and "cartocdn" in online["mapStyle"]
