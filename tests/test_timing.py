"""Timing + pool invariants for the recovery planner (PLAN.md §11, §15).

Load-bearing guarantees under test:
1. No unit of supply -- purchased or inbound -- serves demand before its
   arrival day (lead time / expected delivery).
2. Safe inbound POs (not flagged at-risk) ARE part of the conservative pool,
   so the planner neither ignores them nor buys what they already cover.
3. Fixed inputs reproduce identical plan metrics (PLAN.md §15).
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import timedelta

import pytest

from foodshock.datagen import BUDGET_USD, TODAY, generate
from foodshock.db import (DATA_DIR, available_lots, expected_pos, init_db,
                          insert, now_iso, rows)
from foodshock.engine import (HORIZON_DAYS, _parse_date, build_plans,
                              evaluate_plan, project_supply, propagate,
                              resolve_event)
from foodshock.extraction import extract_notice

TOL = 0.6  # absorbs the 0.1 lb rounding applied when plan lines are written
DAILY = 100.0


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _d(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


def _mini_db(*, po: tuple[float, int] | None = None,
             offers: tuple[tuple[str, float, float, int], ...] = (),
             lots: tuple[tuple[str, float, int], ...] = ()) -> sqlite3.Connection:
    """One pantry, one ambient produce product.

    po:     (quantity_lb, eta_day_offset) safe inbound order
    offers: (offer_id, available_lb, cost_per_lb, lead_days)
    lots:   (lot_id, quantity_lb, expiry_day_offset) on-hand stock
    """
    conn = _conn()
    init_db(conn)
    insert(conn, "suppliers", {"supplier_id": "SUP-A", "name": "Supplier A"})
    insert(conn, "warehouses", {"warehouse_id": "WH-OAK", "name": "Oakland DC",
                                "lat": 37.8044, "lon": -122.2712})
    insert(conn, "products", {"product_id": "PROD-X", "name": "Test Produce",
                              "category": "produce", "upc": None, "unit_cost_per_lb": 0.5,
                              "temperature_zone": "ambient", "shelf_life_days": 30})
    insert(conn, "pantries", {"pantry_id": "P-1", "name": "Pantry One", "city": "Oakland",
                              "state": "CA", "lat": 0.0, "lon": 0.0, "has_refrigeration": 1,
                              "has_freezer": 1, "storage_capacity_lb": 100000.0,
                              "service_floor": 0.6})
    insert(conn, "pantry_demand", {"pantry_id": "P-1", "category": "produce",
                                   "daily_demand_lb": DAILY})
    if po is not None:
        qty, eta = po
        insert(conn, "purchase_orders", {"po_id": "PO-T1", "supplier_id": "SUP-A",
                                         "product_id": "PROD-X", "quantity_lb": qty,
                                         "unit_cost_per_lb": 0.5,
                                         "ordered_at": _d(-2) + "T12:00:00+00:00",
                                         "expected_delivery": _d(eta), "status": "open",
                                         "warehouse_id": "WH-OAK"})
    for off_id, avail, cost, lead in offers:
        insert(conn, "replacement_offers", {"offer_id": off_id, "supplier_id": "SUP-A",
                                            "product_id": "PROD-X", "available_lb": avail,
                                            "unit_cost_per_lb": cost, "lead_time_days": lead,
                                            "receiving_warehouse_id": "WH-OAK"})
    for lot_id, qty, exp in lots:
        insert(conn, "inventory_lots", {
            "lot_id": lot_id, "product_id": "PROD-X", "supplier_id": "SUP-A",
            "supplier_lot_code": None, "quantity_lb": qty,
            "received_at": _d(-1) + "T12:00:00+00:00",
            "expires_at": _d(exp) + "T23:00:00+00:00",
            "warehouse_id": "WH-OAK", "status": "available"})
    conn.commit()
    return conn


def _lines(conn, plan_id: str) -> list[dict]:
    return rows(conn, "SELECT * FROM plan_lines WHERE plan_id=?", (plan_id,))


def _metrics(conn, plan_id: str) -> dict:
    return json.loads(rows(conn, "SELECT metrics_json FROM plans WHERE plan_id=?",
                           (plan_id,))[0]["metrics_json"])


def _assert_no_service_before_arrival(conn, plan_id: str) -> None:
    """THE invariant: cumulative allocation of a product through day d never
    exceeds supply arrived by day d (pool at day 0, safe POs at ETA,
    purchases at their lead-time day)."""
    arrivals: dict[tuple[str, int], float] = {}
    for lot in available_lots(conn, "conservative"):
        key = (lot["product_id"], 0)
        arrivals[key] = arrivals.get(key, 0.0) + lot["quantity_lb"]
    for po in expected_pos(conn, "conservative"):
        d = (_parse_date(po["expected_delivery"]) - TODAY).days
        if 0 <= d < HORIZON_DAYS:
            key = (po["product_id"], d)
            arrivals[key] = arrivals.get(key, 0.0) + po["quantity_lb"]
    alloc: dict[tuple[str, int], float] = {}
    for ln in _lines(conn, plan_id):
        assert ln["day"] is not None, f"plan line missing day: {dict(ln)}"
        key = (ln["product_id"], ln["day"])
        if ln["action"] == "purchase":
            arrivals[key] = arrivals.get(key, 0.0) + ln["quantity_lb"]
        elif ln["action"] == "allocate":
            alloc[key] = alloc.get(key, 0.0) + ln["quantity_lb"]
    for i in {i for (i, _) in alloc}:
        cum_a = cum_s = 0.0
        for d in range(HORIZON_DAYS):
            cum_a += alloc.get((i, d), 0.0)
            cum_s += arrivals.get((i, d), 0.0)
            assert cum_a <= cum_s + TOL, (
                f"{i}: {cum_a:.1f} lb served through day {d}, "
                f"but only {cum_s:.1f} lb has arrived")


def test_purchase_cannot_serve_before_eta():
    """A lead-5 offer must not cover day 0-4 demand: >= 5 days stay unmet."""
    conn = _mini_db(offers=(("OFF-L5", 10_000.0, 1.0, 5),))
    _, rec_id = build_plans(conn)
    assert rows(conn, "SELECT method FROM plans WHERE plan_id=?", (rec_id,))[0]["method"] == "lp-cbc"

    allocs = [ln for ln in _lines(conn, rec_id) if ln["action"] == "allocate"]
    assert allocs, "expected the plan to allocate purchased produce"
    assert all(ln["day"] >= 5 for ln in allocs)
    assert sum(ln["quantity_lb"] for ln in allocs) <= 2 * DAILY + TOL

    m = _metrics(conn, rec_id)
    assert m["hard_constraint_violations"] == 0
    assert m["unmet_demand_lb"] >= 5 * DAILY - TOL
    _assert_no_service_before_arrival(conn, rec_id)


def test_safe_po_counts_as_supply_and_prevents_overbuy():
    """Safe inbound POs are pool supply from their ETA (PLAN.md §11), and the
    LP must not buy produce the PO already covers."""
    conn = _mini_db(po=(700.0, 2), offers=(("OFF-L1", 10_000.0, 1.0, 1),))
    baseline_id, rec_id = build_plans(conn)

    base_allocs = [ln for ln in _lines(conn, baseline_id) if ln["action"] == "allocate"]
    assert sum(ln["quantity_lb"] for ln in base_allocs) >= 5 * DAILY - TOL
    assert all(ln["day"] >= 2 for ln in base_allocs)

    bought = sum(ln["quantity_lb"] for ln in _lines(conn, rec_id) if ln["action"] == "purchase")
    assert bought <= DAILY + TOL, "LP bought produce the safe PO already covers"
    m = _metrics(conn, rec_id)
    assert m["unmet_demand_lb"] == pytest.approx(DAILY, abs=1.0)  # only day 0 unserveable
    assert m["hard_constraint_violations"] == 0
    _assert_no_service_before_arrival(conn, rec_id)


def _run_full_scenario():
    conn = _conn()
    generate(conn)
    raw = (DATA_DIR / "notice_ecoli_onions.txt").read_text()
    extraction, _method, _dropped = extract_notice(raw, allow_llm=False)
    event_id = "FDA-DEMO-2026-001"
    insert(conn, "recall_events", {"event_id": event_id, "authority": "FDA",
                                   "ingested_at": now_iso(), "raw_text": raw,
                                   "extraction_json": extraction.model_dump_json()})
    conn.commit()
    resolve_event(conn, event_id)
    propagate(conn, event_id)
    baseline_id, rec_id = build_plans(conn)
    return conn, baseline_id, rec_id


def test_full_scenario_timing_and_reproducibility():
    conn, baseline_id, rec_id = _run_full_scenario()
    for plan_id in (baseline_id, rec_id):
        _assert_no_service_before_arrival(conn, plan_id)
        assert _metrics(conn, plan_id)["hard_constraint_violations"] == 0

    offers = {o["offer_id"]: o for o in rows(conn, "SELECT * FROM replacement_offers")}
    purchases = [ln for ln in _lines(conn, rec_id) if ln["action"] == "purchase"]
    assert purchases, "recall scenario should trigger replacement purchases"
    for ln in purchases:
        assert ln["day"] == offers[ln["from_id"]]["lead_time_days"]

    conn2, b2, r2 = _run_full_scenario()  # PLAN.md §15: reproducible outputs
    assert _metrics(conn2, r2) == _metrics(conn, rec_id)
    assert _metrics(conn2, b2) == _metrics(conn, baseline_id)


def test_evaluator_rejects_tampered_purchase_day():
    """evaluate_plan derives arrival from the offer's lead time; a purchase
    line edited to claim day=0 must be flagged, not believed."""
    conn = _mini_db(offers=(("OFF-L5", 10_000.0, 1.0, 5),))
    _, rec_id = build_plans(conn)
    assert _metrics(conn, rec_id)["hard_constraint_violations"] == 0
    conn.execute("UPDATE plan_lines SET day=0 WHERE plan_id=? AND action='purchase'", (rec_id,))
    conn.commit()
    assert evaluate_plan(conn, rec_id).hard_constraint_violations >= 1


def test_evaluator_rejects_overbought_offer():
    """Purchases beyond the offer's available_lb must be flagged."""
    conn = _mini_db(offers=(("OFF-L1", 150.0, 1.0, 1),))
    _, rec_id = build_plans(conn)
    assert _metrics(conn, rec_id)["hard_constraint_violations"] == 0
    conn.execute("UPDATE plan_lines SET quantity_lb = quantity_lb + 500 "
                 "WHERE plan_id=? AND action='purchase'", (rec_id,))
    conn.commit()
    assert evaluate_plan(conn, rec_id).hard_constraint_violations >= 1


def test_evaluator_rejects_out_of_horizon_allocation_day():
    """An allocation day outside 0..HORIZON_DAYS-1 dodges no check: it is
    flagged AND earns no served credit."""
    conn = _mini_db(offers=(("OFF-L1", 10_000.0, 1.0, 1),))
    _, rec_id = build_plans(conn)
    m0 = _metrics(conn, rec_id)
    assert m0["hard_constraint_violations"] == 0
    line_id = rows(conn, "SELECT line_id FROM plan_lines WHERE plan_id=? AND action='allocate' "
                         "ORDER BY line_id LIMIT 1", (rec_id,))[0]["line_id"]
    conn.execute("UPDATE plan_lines SET day=? WHERE line_id=?", (HORIZON_DAYS, line_id))
    conn.commit()
    m = evaluate_plan(conn, rec_id)
    assert m.hard_constraint_violations >= 1
    assert m.served_lb < m0["served_lb"], "phantom out-of-horizon service was counted"
    conn.execute("UPDATE plan_lines SET day=-1 WHERE line_id=?", (line_id,))
    conn.commit()
    assert evaluate_plan(conn, rec_id).hard_constraint_violations >= 1


def test_projection_overlay_rejects_tampered_offer_fields():
    """A projected recovery benefit requires a purchase line that agrees with
    its cited offer. Unknown offers or forged day, price, or destination add
    no projected supply.
    """
    conn = _mini_db(offers=(("OFF-L1", 10_000.0, 1.0, 1),))
    _, rec_id = build_plans(conn)
    valid = project_supply(conn, "conservative", plan_id=rec_id)
    produce = valid[valid["category"] == "produce"].set_index("day")
    assert produce.loc[0, "inbound_lb"] == 0.0
    assert produce.loc[1, "inbound_lb"] > 0.0

    for clause, params in (
        ("day=0", ()),
        ("day=1, unit_cost_per_lb=0", ()),
        ("unit_cost_per_lb=1, to_id='WH-GHOST'", ()),
        ("to_id='WH-OAK', from_id='OFF-GHOST'", ()),
    ):
        conn.execute(f"UPDATE plan_lines SET {clause} "
                     "WHERE plan_id=? AND action='purchase'", (*params, rec_id))
        conn.commit()
        tampered = project_supply(conn, "conservative", plan_id=rec_id)
        assert float(tampered[tampered["category"] == "produce"]["inbound_lb"].sum()) == 0.0


def test_evaluator_budget_uses_offer_price():
    """Zeroing the persisted price cannot hide procurement cost: cost derives
    from the cited offer, so an inflated buy still trips the budget check."""
    conn = _mini_db(offers=(("OFF-EXP", 20_000.0, 30.0, 1),))
    _, rec_id = build_plans(conn)
    m0 = _metrics(conn, rec_id)
    assert m0["hard_constraint_violations"] == 0
    assert m0["procurement_cost"] <= BUDGET_USD + 1.0
    conn.execute("UPDATE plan_lines SET unit_cost_per_lb=0, quantity_lb=quantity_lb+500 "
                 "WHERE plan_id=? AND action='purchase'", (rec_id,))
    conn.commit()
    m = evaluate_plan(conn, rec_id)
    assert m.procurement_cost == pytest.approx(m0["procurement_cost"] + 500 * 30.0, rel=1e-6)
    assert m.hard_constraint_violations >= 2  # price mismatch + over budget


def test_evaluator_rejects_corrupt_quantities():
    """Negative or non-finite quantities are flagged and excluded -- a negative
    line must not free supply for a sibling or shrink served accounting."""
    conn = _mini_db(offers=(("OFF-L1", 10_000.0, 1.0, 1),))
    _, rec_id = build_plans(conn)
    m0 = _metrics(conn, rec_id)
    assert m0["hard_constraint_violations"] == 0
    first = rows(conn, "SELECT line_id, quantity_lb FROM plan_lines "
                       "WHERE plan_id=? AND action='allocate' ORDER BY line_id LIMIT 1",
                 (rec_id,))[0]
    # The schema CHECK blocks honest paths from writing corrupt rows...
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE plan_lines SET quantity_lb=-100 WHERE line_id=?", (first["line_id"],))
    # ...so simulate pre-existing corruption; the evaluator must still catch it.
    conn.execute("PRAGMA ignore_check_constraints=ON")
    conn.execute("UPDATE plan_lines SET quantity_lb=-100 WHERE line_id=?", (first["line_id"],))
    conn.commit()
    m = evaluate_plan(conn, rec_id)
    assert m.hard_constraint_violations >= 1
    assert m.served_lb == pytest.approx(m0["served_lb"] - first["quantity_lb"], abs=TOL), \
        "corrupt line must be excluded, not subtracted"

    # Non-finite purchase quantity: flagged, no cost, no supply credit.
    conn.execute("UPDATE plan_lines SET quantity_lb=9e999 WHERE plan_id=? AND action='purchase'",
                 (rec_id,))
    conn.commit()
    m2 = evaluate_plan(conn, rec_id)
    assert m2.hard_constraint_violations >= 1
    assert math.isfinite(m2.procurement_cost) and m2.procurement_cost == 0.0


def test_projection_overlay_guards_quantities():
    """Corrupt or over-offer purchase quantities cannot poison the projection:
    invalid lines add nothing; credit is capped at the offer's availability."""
    conn = _mini_db(offers=(("OFF-L1", 800.0, 1.0, 1),))
    _, rec_id = build_plans(conn)
    conn.execute("UPDATE plan_lines SET quantity_lb=quantity_lb+5000 "
                 "WHERE plan_id=? AND action='purchase'", (rec_id,))
    conn.commit()
    proj = project_supply(conn, "conservative", plan_id=rec_id)
    produce = proj[proj["category"] == "produce"]
    assert float(produce["inbound_lb"].sum()) <= 800.0 + TOL, \
        "projection credited more than the offer's availability"

    conn.execute("UPDATE plan_lines SET quantity_lb=9e999 "
                 "WHERE plan_id=? AND action='purchase'", (rec_id,))
    conn.commit()
    proj2 = project_supply(conn, "conservative", plan_id=rec_id)
    assert float(proj2[proj2["category"] == "produce"]["inbound_lb"].sum()) == 0.0
    for col in ("start_lb", "inbound_lb", "end_lb"):
        assert proj2[col].apply(math.isfinite).all(), f"non-finite {col} in projection"
