"""Allergen hard constraint (PLAN.md §11): a product carrying an allergen a
pantry restricts must never be allocated there -- in the LP, the greedy
baseline, and the independent evaluator. Token matching is case-insensitive
(a safety comparison must not depend on data-entry casing)."""

from __future__ import annotations

from foodshock.db import init_db, insert, rows
from foodshock.engine import build_plans, evaluate_plan
from test_timing import DAILY, _conn, _d, _lines, _metrics


def _allergen_db(*, product_allergens: str = "wheat",
                 pantry_restriction: str = "wheat"):
    """Two pantries (P-SAFE restricted), wheat product plus a clean product.
    Both produce-category so category demand alone cannot separate them."""
    conn = _conn()
    init_db(conn)
    insert(conn, "suppliers", {"supplier_id": "SUP-A", "name": "Supplier A"})
    insert(conn, "warehouses", {"warehouse_id": "WH-OAK", "name": "Oakland DC"})
    for pid, name, allergens in (("PROD-WHEAT", "Wheat Thing", product_allergens),
                                 ("PROD-CLEAN", "Clean Thing", "")):
        insert(conn, "products", {"product_id": pid, "name": name, "category": "produce",
                                  "upc": None, "unit_cost_per_lb": 0.5,
                                  "temperature_zone": "ambient", "shelf_life_days": 30,
                                  "allergens": allergens})
    for pid, name, restrict in (("P-ANY", "Anything Pantry", ""),
                                ("P-SAFE", "Restricted Pantry", pantry_restriction)):
        insert(conn, "pantries", {"pantry_id": pid, "name": name, "city": "Oakland",
                                  "state": "CA", "lat": 0.0, "lon": 0.0,
                                  "has_refrigeration": 1, "has_freezer": 1,
                                  "storage_capacity_lb": 100000.0, "service_floor": 0.6,
                                  "allergen_restrictions": restrict})
        insert(conn, "pantry_demand", {"pantry_id": pid, "category": "produce",
                                       "daily_demand_lb": DAILY})
    # Plenty of wheat product; a little clean product.
    for lot_id, prod, qty in (("L-W", "PROD-WHEAT", 50_000.0), ("L-C", "PROD-CLEAN", 300.0)):
        insert(conn, "inventory_lots", {
            "lot_id": lot_id, "product_id": prod, "supplier_id": "SUP-A",
            "supplier_lot_code": None, "quantity_lb": qty,
            "received_at": _d(-1) + "T12:00:00+00:00",
            "expires_at": _d(30) + "T23:00:00+00:00",
            "warehouse_id": "WH-OAK", "status": "available"})
    conn.commit()
    return conn


def _check_no_restricted_allocs(conn) -> None:
    baseline_id, rec_id = build_plans(conn)
    for plan_id in (baseline_id, rec_id):
        bad = [ln for ln in _lines(conn, plan_id)
               if ln["action"] == "allocate" and ln["to_id"] == "P-SAFE"
               and ln["product_id"] == "PROD-WHEAT"]
        assert bad == [], f"restricted product allocated to P-SAFE in {plan_id}"
        assert _metrics(conn, plan_id)["hard_constraint_violations"] == 0
    # The unrestricted pantry is fully served from abundant wheat stock; the
    # restricted pantry is served only as far as the clean product reaches.
    rec_allocs = [ln for ln in _lines(conn, rec_id) if ln["action"] == "allocate"]
    safe_served = sum(ln["quantity_lb"] for ln in rec_allocs if ln["to_id"] == "P-SAFE")
    assert 0 < safe_served <= 300.0 + 0.6


def test_lp_and_greedy_respect_allergen_restriction():
    _check_no_restricted_allocs(_allergen_db())


def test_allergen_tokens_case_insensitive():
    """'Wheat' on the product must still trip a 'wheat' restriction."""
    _check_no_restricted_allocs(_allergen_db(product_allergens="Wheat, Soy",
                                             pantry_restriction="WHEAT"))


def test_evaluator_flags_restricted_allocation():
    conn = _allergen_db()
    _, rec_id = build_plans(conn)
    assert _metrics(conn, rec_id)["hard_constraint_violations"] == 0
    insert(conn, "plan_lines", {"plan_id": rec_id, "action": "allocate",
                                "product_id": "PROD-WHEAT", "to_id": "P-SAFE",
                                "day": 0, "quantity_lb": 10.0})
    conn.commit()
    assert evaluate_plan(conn, rec_id).hard_constraint_violations >= 1


def test_datagen_carries_allergen_seed():
    """Demo scenario really exercises the constraint: pasta is wheat-tagged and
    P-MIL restricts wheat, so no plan may ship pasta there."""
    from foodshock.datagen import generate
    conn = _conn()
    generate(conn)
    assert rows(conn, "SELECT allergens FROM products WHERE product_id='PROD-PASTA'")[0]["allergens"] == "wheat"
    assert rows(conn, "SELECT allergen_restrictions FROM pantries WHERE pantry_id='P-MIL'")[0]["allergen_restrictions"] == "wheat"
    baseline_id, rec_id = build_plans(conn)
    for plan_id in (baseline_id, rec_id):
        bad = [ln for ln in _lines(conn, plan_id)
               if ln["action"] == "allocate" and ln["to_id"] == "P-MIL"
               and ln["product_id"] == "PROD-PASTA"]
        assert bad == []
        assert _metrics(conn, plan_id)["hard_constraint_violations"] == 0
