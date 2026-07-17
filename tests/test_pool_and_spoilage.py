"""Safety-boundary and spoilage-exactness regressions.

1. A PO with ANY unresolved connecting match state (possible/unknown included)
   must never enter the conservative pool -- even before propagate() runs
   (PLAN.md §11 supply boundary; defense in depth in db.expected_pos).
2. Operator clearance is for UNCONFIRMED evidence only: a confirmed match can
   never be cleared back into the pool (PLAN.md §10 unconditional exclusion).
3. Spoilage is per-bucket: allocations served from long-lived or purchased
   supply must not erase a dead bucket's spoilage, and stock outliving the
   horizon is not spoilage (LP flow equality + evaluator FEFO consumption).
"""

from __future__ import annotations

import pytest

from foodshock.db import available_lots, expected_pos, insert, now_iso, rows
from foodshock.engine import (BOX_LB, build_plans, project_supply, propagate,
                              review_match)
from test_timing import DAILY, TOL, _d, _lines, _metrics, _mini_db


def _add_match(conn, state: str, *, kind: str = "po", target: str = "PO-T1") -> int:
    if not rows(conn, "SELECT 1 FROM recall_events WHERE event_id='EV-T'"):
        insert(conn, "recall_events", {"event_id": "EV-T", "authority": "FDA",
                                       "status": "active", "ingested_at": now_iso(),
                                       "raw_text": "synthetic test notice"})
    insert(conn, "matches", {"event_id": "EV-T", "target_type": kind,
                             "target_id": target, "state": state, "tier": 4,
                             "score": 0.45, "evidence_json": "{}"})
    conn.commit()
    return rows(conn, "SELECT match_id FROM matches ORDER BY match_id DESC LIMIT 1")[0]["match_id"]


# ------------------------------------------------------ PO pool boundary

def test_possible_po_never_in_conservative_pool():
    """The leak: a 'possible' PO stays status='open' until propagate runs;
    the conservative pool must exclude it on match state alone."""
    conn = _mini_db(po=(500.0, 2))
    _add_match(conn, "possible")
    assert expected_pos(conn, "conservative") == []
    assert [p["po_id"] for p in expected_pos(conn, "optimistic")] == ["PO-T1"]

    _, rec_id = build_plans(conn)  # no lots, no offers: PO was the only supply
    assert [ln for ln in _lines(conn, rec_id) if ln["action"] == "allocate"] == []
    m = _metrics(conn, rec_id)
    assert m["hard_constraint_violations"] == 0
    assert m["unmet_demand_lb"] >= 7 * DAILY - TOL

    proj = project_supply(conn, "conservative")
    assert float(proj[proj["category"] == "produce"]["inbound_lb"].sum()) == 0.0


def test_propagate_flags_possible_po_at_risk():
    conn = _mini_db(po=(500.0, 2))
    _add_match(conn, "possible")
    result = propagate(conn, "EV-T")
    assert result["pos_at_risk"] == 1
    assert rows(conn, "SELECT status FROM purchase_orders")[0]["status"] == "at_risk"
    # at_risk still arrives under the optimistic assumption (PLAN.md §10)...
    assert [p["po_id"] for p in expected_pos(conn, "optimistic")] == ["PO-T1"]
    assert expected_pos(conn, "conservative") == []


def test_confirmed_po_excluded_from_every_scenario():
    """Effective 'confirmed' = recalled product inbound: no scenario toggle
    may count it (mirrors the lot invariant)."""
    conn = _mini_db(po=(500.0, 2))
    _add_match(conn, "confirmed")
    assert expected_pos(conn, "optimistic") == []
    assert expected_pos(conn, "conservative") == []
    propagate(conn, "EV-T")
    assert expected_pos(conn, "optimistic") == []


def test_cleared_po_reenters_pool():
    """Operator clearance is the ONLY path back into the pool."""
    conn = _mini_db(po=(500.0, 2))
    match_id = _add_match(conn, "possible")
    propagate(conn, "EV-T")
    assert expected_pos(conn, "conservative") == []
    review_match(conn, match_id, "cleared", actor="op-test")
    assert [p["po_id"] for p in expected_pos(conn, "conservative")] == ["PO-T1"]
    assert rows(conn, "SELECT status FROM purchase_orders")[0]["status"] == "open"


# ------------------------------------------- confirmed-clearance guard

def test_confirmed_po_clearance_rejected():
    """§10: no operator toggle re-includes confirmed recalled stock -- the
    'cleared' action is rejected outright and the match stays unreviewed."""
    conn = _mini_db(po=(500.0, 2))
    match_id = _add_match(conn, "confirmed")
    propagate(conn, "EV-T")
    with pytest.raises(ValueError, match="confirmed"):
        review_match(conn, match_id, "cleared", actor="op-test")
    m = rows(conn, "SELECT reviewed, review_action FROM matches WHERE match_id=?",
             (match_id,))[0]
    assert m["reviewed"] == 0 and m["review_action"] is None
    assert expected_pos(conn, "optimistic") == []
    assert expected_pos(conn, "conservative") == []


def test_confirmed_lot_clearance_rejected_quarantine_allowed():
    conn = _mini_db(lots=(("L-REC", 500.0, 6),))
    match_id = _add_match(conn, "confirmed", kind="lot", target="L-REC")
    propagate(conn, "EV-T")
    assert rows(conn, "SELECT status FROM inventory_lots")[0]["status"] == "quarantine_proposed"
    with pytest.raises(ValueError, match="confirmed"):
        review_match(conn, match_id, "cleared", actor="op-test")
    assert rows(conn, "SELECT status FROM inventory_lots")[0]["status"] == "quarantine_proposed"
    assert available_lots(conn, "optimistic") == []
    assert available_lots(conn, "conservative") == []
    review_match(conn, match_id, "quarantined", actor="op-test")  # confirm path still works
    assert rows(conn, "SELECT status FROM inventory_lots")[0]["status"] == "quarantined"
    assert available_lots(conn, "optimistic") == []


def test_sibling_clearance_blocked_while_target_confirmed():
    """Clearing a possible match is rejected while ANOTHER match keeps the
    same target effectively confirmed -- otherwise the lot status would flip
    to 'cleared' under a live confirmed match."""
    conn = _mini_db(lots=(("L-REC", 500.0, 6),))
    _add_match(conn, "confirmed", kind="lot", target="L-REC")
    sibling = _add_match(conn, "possible", kind="lot", target="L-REC")
    with pytest.raises(ValueError, match="confirmed"):
        review_match(conn, sibling, "cleared", actor="op-test")
    assert available_lots(conn, "optimistic") == []


# ------------------------------------------------------ spoilage exactness

def test_spoilage_counts_dying_bucket_despite_replacement_supply():
    """500 lb dies after day 1; a lead-2 offer covers days 2-6. Days 0-1 drain
    200 lb of the dying lot; the other 300 lb IS spoilage even though total
    allocations (700 lb) exceed the dying quantity. The old aggregate
    accounting reported 0 here."""
    conn = _mini_db(lots=(("L-DIE", 500.0, 1),),
                    offers=(("OFF-L2", 10_000.0, 1.0, 2),))
    _, rec_id = build_plans(conn)
    assert rows(conn, "SELECT method FROM plans WHERE plan_id=?", (rec_id,))[0]["method"] == "lp-cbc"
    m = _metrics(conn, rec_id)
    assert m["hard_constraint_violations"] == 0
    assert m["unmet_demand_lb"] <= TOL
    assert abs(m["spoilage_lb"] - 300.0) <= TOL


def test_stock_outliving_horizon_is_not_spoilage():
    """900 lb usable through day 7 (one past the last planned day 6): the
    200 lb unallocated after full service is NOT spoilage."""
    conn = _mini_db(lots=(("L-LIVE", 900.0, 7),))
    _, rec_id = build_plans(conn)
    m = _metrics(conn, rec_id)
    assert m["hard_constraint_violations"] == 0
    assert m["unmet_demand_lb"] <= TOL
    assert m["spoilage_lb"] == 0.0


# ------------------------------------------------- disruption metric scope

def test_boxes_disrupted_counts_nonproduce_unmet_demand():
    """A protein-only shortfall still disrupts food boxes."""
    conn = _mini_db()
    conn.execute("UPDATE products SET category='protein'")
    conn.execute("UPDATE pantry_demand SET category='protein'")
    conn.commit()

    _, rec_id = build_plans(conn)
    metrics = _metrics(conn, rec_id)

    assert metrics["unmet_demand_lb"] == 7 * DAILY
    assert metrics["boxes_disrupted"] == int(
        round(metrics["unmet_demand_lb"] / BOX_LB)
    )


# -------------------------------------------------- incident attribution

def _second_event(conn, event_id: str) -> None:
    insert(conn, "recall_events", {"event_id": event_id, "authority": "FDA",
                                   "status": "active", "ingested_at": now_iso(),
                                   "raw_text": f"synthetic notice {event_id}"})
    conn.commit()


def test_propagate_metrics_are_event_scoped():
    """propagate's SIDE EFFECTS are global (safety state sees every recall),
    but its returned metrics are the incident's own: a second incident never
    claims the first one's pounds or orders, and a target matched twice by
    one event (confirmed + possible) is counted once, as confirmed."""
    conn = _mini_db(po=(500.0, 2), lots=(("L-A", 900.0, 6), ("L-B", 400.0, 6)))
    _add_match(conn, "confirmed", kind="lot", target="L-A")   # EV-T
    _add_match(conn, "possible", kind="lot", target="L-A")    # EV-T duplicate evidence
    _add_match(conn, "possible")                              # EV-T: PO-T1
    _second_event(conn, "EV-2")
    insert(conn, "matches", {"event_id": "EV-2", "target_type": "lot",
                             "target_id": "L-B", "state": "possible", "tier": 4,
                             "score": 0.4, "evidence_json": "{}"})
    conn.commit()

    m1 = propagate(conn, "EV-T")
    assert m1["quarantine_proposed_lb"] == 900.0   # L-A once, not 900 + 900
    assert m1["review_lb"] == 0.0                  # L-B belongs to EV-2
    assert m1["pos_at_risk"] == 1

    m2 = propagate(conn, "EV-2")
    assert m2["quarantine_proposed_lb"] == 0.0     # EV-T's confirmed lot not claimed
    assert m2["review_lb"] == 400.0
    assert m2["pos_at_risk"] == 0

    # Side effects stayed global across both runs.
    assert rows(conn, "SELECT status FROM inventory_lots WHERE lot_id='L-A'")[0]["status"] == "quarantine_proposed"
    assert rows(conn, "SELECT status FROM purchase_orders")[0]["status"] == "at_risk"


# ---------------------------------------- warehouse-scoped feasibility

def test_distribution_feasibility_is_scoped_by_warehouse():
    """Stock at San Jose cannot make an Oakland distribution line feasible.

    The old product-only pool combined both warehouses and incorrectly let the
    first line consume inventory located at the other site.
    """
    conn = _mini_db(lots=(("L-OAK", 100.0, 6),))
    insert(conn, "warehouses", {"warehouse_id": "WH-SJ", "name": "San Jose Annex",
                                "lat": 37.3382, "lon": -121.8863})
    insert(conn, "inventory_lots", {
        "lot_id": "L-SJ", "product_id": "PROD-X", "supplier_id": "SUP-A",
        "supplier_lot_code": None, "quantity_lb": 100.0,
        "received_at": _d(-1) + "T12:00:00+00:00",
        "expires_at": _d(6) + "T23:00:00+00:00",
        "warehouse_id": "WH-SJ", "status": "available"})
    insert(conn, "distribution_plans", {
        "dist_id": "D-OAK", "pantry_id": "P-1", "warehouse_id": "WH-OAK",
        "product_id": "PROD-X", "quantity_lb": 150.0,
        "scheduled_date": _d(0), "status": "planned"})
    insert(conn, "distribution_plans", {
        "dist_id": "D-SJ", "pantry_id": "P-1", "warehouse_id": "WH-SJ",
        "product_id": "PROD-X", "quantity_lb": 100.0,
        "scheduled_date": _d(1), "status": "planned"})
    _second_event(conn, "EV-FEAS")

    result = propagate(conn, "EV-FEAS")

    assert result["infeasible_lines"] == 1
    states = {r["dist_id"]: r["status"] for r in rows(
        conn, "SELECT dist_id, status FROM distribution_plans")}
    assert states == {"D-OAK": "infeasible", "D-SJ": "planned"}


def test_distribution_feasibility_excludes_stock_expired_before_service():
    conn = _mini_db(lots=(("L-EXPIRES", 200.0, 0),))
    insert(conn, "distribution_plans", {
        "dist_id": "D-AFTER-EXPIRY", "pantry_id": "P-1", "warehouse_id": "WH-OAK",
        "product_id": "PROD-X", "quantity_lb": 100.0,
        "scheduled_date": _d(1), "status": "planned"})
    _second_event(conn, "EV-EXPIRY")

    result = propagate(conn, "EV-EXPIRY")

    assert result["infeasible_lines"] == 1
    assert rows(conn, "SELECT status FROM distribution_plans")[0]["status"] == "infeasible"


def test_distribution_feasibility_respects_safe_po_eta():
    conn = _mini_db(po=(100.0, 1))
    insert(conn, "distribution_plans", {
        "dist_id": "D-EARLY", "pantry_id": "P-1", "warehouse_id": "WH-OAK",
        "product_id": "PROD-X", "quantity_lb": 100.0,
        "scheduled_date": _d(0), "status": "planned"})
    insert(conn, "distribution_plans", {
        "dist_id": "D-ONTIME", "pantry_id": "P-1", "warehouse_id": "WH-OAK",
        "product_id": "PROD-X", "quantity_lb": 100.0,
        "scheduled_date": _d(1), "status": "planned"})
    _second_event(conn, "EV-PO-TIMING")

    result = propagate(conn, "EV-PO-TIMING")

    assert result["infeasible_lines"] == 1
    states = {r["dist_id"]: r["status"] for r in rows(
        conn, "SELECT dist_id, status FROM distribution_plans")}
    assert states == {"D-EARLY": "infeasible", "D-ONTIME": "planned"}
