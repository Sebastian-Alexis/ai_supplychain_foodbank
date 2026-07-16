"""Expiration hard constraint (PLAN.md §11): stock is usable only through its
expiration day -- in the LP, the greedy baseline, the independent evaluator,
and the judge-facing projection. A lot that dies mid-horizon must neither
serve later demand nor inflate later days-of-supply."""

from __future__ import annotations

from foodshock.db import insert, rows
from foodshock.engine import (build_plans, days_of_supply, evaluate_plan,
                              project_supply)
from test_timing import DAILY, TOL, _lines, _metrics, _mini_db


def test_lp_cannot_serve_after_expiry():
    """500 lb dies after day 1; no other supply: only days 0-1 are servable."""
    conn = _mini_db(lots=(("L-EXP", 500.0, 1),))
    _, rec_id = build_plans(conn)
    assert rows(conn, "SELECT method FROM plans WHERE plan_id=?", (rec_id,))[0]["method"] == "lp-cbc"
    allocs = [ln for ln in _lines(conn, rec_id) if ln["action"] == "allocate"]
    assert allocs, "expected day 0-1 allocations from the dying lot"
    assert all(ln["day"] <= 1 for ln in allocs)
    assert sum(ln["quantity_lb"] for ln in allocs) <= 2 * DAILY + TOL
    m = _metrics(conn, rec_id)
    assert m["hard_constraint_violations"] == 0
    assert m["unmet_demand_lb"] >= 5 * DAILY - TOL


def test_baseline_greedy_respects_expiry():
    conn = _mini_db(lots=(("L-EXP", 500.0, 1),))
    baseline_id, _ = build_plans(conn)
    allocs = [ln for ln in _lines(conn, baseline_id) if ln["action"] == "allocate"]
    assert allocs and all(ln["day"] <= 1 for ln in allocs)


def test_evaluator_rejects_post_expiry_allocation():
    """A hand-edited line serving day 5 from stock dead since day 2 must be
    flagged by the evaluator's independent FEFO simulation."""
    conn = _mini_db(lots=(("L-EXP", 500.0, 1),))
    _, rec_id = build_plans(conn)
    assert _metrics(conn, rec_id)["hard_constraint_violations"] == 0
    insert(conn, "plan_lines", {"plan_id": rec_id, "action": "allocate",
                                "product_id": "PROD-X", "to_id": "P-1",
                                "day": 5, "quantity_lb": 50.0})
    conn.commit()
    assert evaluate_plan(conn, rec_id).hard_constraint_violations >= 1


def test_projection_drops_expired_stock():
    """500 lb dying after day 1 at 100/day: 200 consumed, 300 expires at the
    start of day 2. DoS must be 2.0 -- not the 5.0 a naive running total
    would report. On-hand stock is start_lb, never day-0 'inbound'."""
    conn = _mini_db(lots=(("L-EXP", 500.0, 1),))
    proj = project_supply(conn, "conservative")
    produce = proj[proj["category"] == "produce"].set_index("day")
    assert produce.loc[0, "start_lb"] == 500.0
    assert produce.loc[0, "inbound_lb"] == 0.0
    assert produce.loc[2, "expired_lb"] == 300.0
    assert produce.loc[2, "end_lb"] == -DAILY
    assert bool(produce.loc[2, "stockout"])
    assert days_of_supply(proj)["produce"] == 2.0


def test_already_expired_lot_excluded():
    """Stock past its expiration date is not supply for anything."""
    conn = _mini_db(lots=(("L-DEAD", 400.0, -1),))
    proj = project_supply(conn, "conservative")
    produce = proj[proj["category"] == "produce"].set_index("day")
    assert produce.loc[0, "start_lb"] == 0.0
    baseline_id, rec_id = build_plans(conn)
    assert not [ln for ln in _lines(conn, baseline_id) if ln["action"] == "allocate"]
    assert _metrics(conn, rec_id)["hard_constraint_violations"] == 0


def test_lp_buys_replacement_when_stock_expires():
    """Dying lot + lead-2 offer: the LP covers days 2+ with a purchase and the
    combined schedule passes the evaluator."""
    conn = _mini_db(lots=(("L-EXP", 500.0, 1),),
                    offers=(("OFF-L2", 10_000.0, 1.0, 2),))
    _, rec_id = build_plans(conn)
    m = _metrics(conn, rec_id)
    assert m["hard_constraint_violations"] == 0
    assert m["unmet_demand_lb"] <= TOL
    buys = [ln for ln in _lines(conn, rec_id) if ln["action"] == "purchase"]
    assert buys and sum(ln["quantity_lb"] for ln in buys) >= 5 * DAILY - TOL
