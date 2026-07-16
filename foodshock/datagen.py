"""Synthetic demo scenario (PLAN.md §14): one regional food bank, two
warehouses, six pantries, ~16 lots, 5 inbound POs, 7-day distribution plan.

All records are clearly synthetic ("public-data-inspired" per PLAN.md §3).
Dates are fixed relative to TODAY so every run is reproducible.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .db import init_db, insert

TODAY = date(2026, 7, 16)
BUDGET_USD = 15_000.0


def _d(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


def _ts(offset: int, hour: int = 12) -> str:
    return f"{_d(offset)}T{hour:02d}:00:00+00:00"


def generate(conn: sqlite3.Connection) -> None:
    """Reset the DB and load the seeded scenario."""
    init_db(conn)

    suppliers = [
        # id, name, city, state, lat, lon
        ("SUP-GVP", "Golden Valley Produce LLC", "Salinas", "CA", 36.6777, -121.6555),
        ("SUP-RVF", "Rio Verde Farms", "Bakersfield", "CA", 35.3733, -119.0187),
        ("SUP-DF", "Delta Fresh Wholesale", "Stockton", "CA", 37.9577, -121.2908),
        ("SUP-PFF", "Pacific Frozen Foods", "Fremont", "CA", 37.5485, -121.9886),
        ("SUP-IVO", "Imperial Valley Onion Co", "El Centro", "CA", 32.7920, -115.5631),
        ("SUP-MWG", "Midwest Grains Co-op", "Sacramento", "CA", 38.5816, -121.4944),
        ("SUP-BDD", "Bay Dairy Distributors", "San Leandro", "CA", 37.7249, -122.1561),
    ]
    for sid, name, city, state, lat, lon in suppliers:
        insert(conn, "suppliers", {"supplier_id": sid, "name": name, "city": city,
                                   "state": state, "lat": lat, "lon": lon})
    insert(conn, "supplier_aliases", {"supplier_id": "SUP-GVP", "alias": "GV Produce"})
    insert(conn, "supplier_aliases", {"supplier_id": "SUP-DF", "alias": "Delta Fresh"})

    insert(conn, "facilities", {"facility_id": "FAC-GVP-2", "supplier_id": "SUP-GVP",
                                "name": "Golden Valley Packing #2", "city": "Salinas", "state": "CA",
                                "lat": 36.7078, "lon": -121.6394})

    products = [
        # id, name, category, upc, cost/lb, zone, shelf_days, allergens
        ("PROD-ONION-Y", "Fresh yellow onions", "produce", "041331092609", 0.52, "ambient", 21, ""),
        ("PROD-CARROT", "Carrots", "produce", "033383661011", 0.60, "refrigerated", 21, ""),
        ("PROD-CABBAGE", "Green cabbage", "produce", "033383601020", 0.55, "refrigerated", 14, ""),
        ("PROD-APPLE", "Apples", "produce", "033383322019", 0.85, "refrigerated", 30, ""),
        ("PROD-POTATO", "Russet potatoes", "produce", "033383651014", 0.45, "ambient", 45, ""),
        ("PROD-ONION-FRZ", "Frozen diced onions", "produce", "071179011234", 1.30, "frozen", 240, ""),
        ("PROD-ONION-PWD", "Onion powder", "shelf_stable", "052100010828", 4.80, "ambient", 540, ""),
        ("PROD-CHICKEN-FRZ", "Frozen chicken quarters", "protein", "023700031456", 1.85, "frozen", 270, ""),
        ("PROD-RICE", "White rice", "grain", "017400101015", 0.78, "ambient", 720, ""),
        ("PROD-PASTA", "Dry pasta", "grain", "076808501021", 0.95, "ambient", 720, "wheat"),
        ("PROD-BEANS-DRY", "Dry pinto beans", "shelf_stable", "039978013507", 0.88, "ambient", 720, ""),
        ("PROD-MILK-UHT", "UHT shelf-stable milk", "dairy", "742365200014", 1.10, "ambient", 180, "milk"),
    ]
    for pid, name, cat, upc, cost, zone, shelf, allergens in products:
        insert(conn, "products", {"product_id": pid, "name": name, "category": cat, "upc": upc,
                                  "unit_cost_per_lb": cost, "temperature_zone": zone,
                                  "shelf_life_days": shelf, "allergens": allergens})

    for wid, name, city, lat, lon in [
        ("WH-OAK", "Oakland Distribution Center", "Oakland", 37.8044, -122.2712),
        ("WH-SJ", "San Jose Annex", "San Jose", 37.3382, -121.8863),
    ]:
        insert(conn, "warehouses", {"warehouse_id": wid, "name": name, "city": city,
                                    "state": "CA", "lat": lat, "lon": lon})

    pantries = [
        # id, name, city, lat, lon, refrig, freezer, storage_lb, demand_weight, restrictions
        ("P-FRU", "Fruitvale Community Pantry", "Oakland", 37.7757, -122.2241, 1, 1, 2600, 0.22, ""),
        ("P-RIC", "Richmond Neighborhood Table", "Richmond", 37.9358, -122.3477, 1, 0, 1800, 0.18, ""),
        ("P-HAY", "Hayward Hope Center", "Hayward", 37.6688, -122.0808, 1, 1, 2200, 0.17, ""),
        ("P-SP", "San Pablo Family Center", "San Pablo", 37.9621, -122.3455, 1, 0, 1400, 0.15, ""),
        ("P-ALA", "Alameda Point Pantry", "Alameda", 37.7726, -122.2824, 1, 1, 1600, 0.15, ""),
        # Gluten-free partner program: cannot accept wheat items (PLAN.md §11
        # allergen hard constraint; grain demand stays servable via rice).
        ("P-MIL", "Milpitas Community Cupboard", "Milpitas", 37.4323, -121.8996, 1, 1, 1200, 0.13, "wheat"),
    ]
    daily_category_demand = {
        "produce": 800.0, "protein": 250.0, "grain": 300.0,
        "dairy": 100.0, "shelf_stable": 350.0,
    }
    for pid, name, city, lat, lon, refrig, freezer, storage, weight, restrict in pantries:
        insert(conn, "pantries", {"pantry_id": pid, "name": name, "city": city, "state": "CA",
                                  "lat": lat, "lon": lon, "has_refrigeration": refrig,
                                  "has_freezer": freezer, "storage_capacity_lb": storage,
                                  "service_floor": 0.6, "allergen_restrictions": restrict})
        for cat, total in daily_category_demand.items():
            insert(conn, "pantry_demand", {"pantry_id": pid, "category": cat,
                                           "daily_demand_lb": round(total * weight, 1)})

    lots = [
        # lot_id, product, supplier, facility, lot_code, qty, recv_off, exp_off, wh
        # --- implicated onions ---
        ("L-GVP-1", "PROD-ONION-Y", "SUP-GVP", "FAC-GVP-2", "GVP-8842", 900, -11, 6, "WH-OAK"),
        ("L-GVP-2", "PROD-ONION-Y", "SUP-GVP", "FAC-GVP-2", None, 1000, -8, 9, "WH-OAK"),
        ("L-RVF-1", "PROD-ONION-Y", "SUP-RVF", None, "RVF-113", 800, -6, 12, "WH-SJ"),
        # --- clean produce ---
        ("L-DF-CAR", "PROD-CARROT", "SUP-DF", None, "DF-2211", 600, -4, 15, "WH-OAK"),
        ("L-DF-CAB", "PROD-CABBAGE", "SUP-DF", None, "DF-2214", 500, -3, 10, "WH-OAK"),
        ("L-RVF-APP", "PROD-APPLE", "SUP-RVF", None, "RVF-098", 600, -9, 20, "WH-SJ"),
        ("L-DF-POT", "PROD-POTATO", "SUP-DF", None, "DF-2190", 700, -12, 30, "WH-OAK"),
        ("L-PFF-OFZ", "PROD-ONION-FRZ", "SUP-PFF", None, "PFF-771", 400, -20, 200, "WH-OAK"),
        # --- other categories ---
        ("L-PFF-CHK", "PROD-CHICKEN-FRZ", "SUP-PFF", None, "PFF-514", 2500, -15, 250, "WH-OAK"),
        ("L-MWG-RIC", "PROD-RICE", "SUP-MWG", None, "MWG-33", 1800, -30, 600, "WH-SJ"),
        ("L-MWG-PAS", "PROD-PASTA", "SUP-MWG", None, "MWG-41", 1200, -25, 650, "WH-OAK"),
        ("L-MWG-BEA", "PROD-BEANS-DRY", "SUP-MWG", None, "MWG-29", 2200, -40, 600, "WH-OAK"),
        ("L-BDD-MLK", "PROD-MILK-UHT", "SUP-BDD", None, "BDD-88", 900, -10, 120, "WH-OAK"),
        ("L-GVP-PWD", "PROD-ONION-PWD", "SUP-GVP", None, "GVP-7counter", 150, -60, 400, "WH-SJ"),
        ("L-PFF-CHK-SJ", "PROD-CHICKEN-FRZ", "SUP-PFF", None, "PFF-528", 850, -9, 260, "WH-SJ"),
        ("L-BDD-MLK-SJ", "PROD-MILK-UHT", "SUP-BDD", None, "BDD-91", 450, -7, 130, "WH-SJ"),
    ]
    for lot_id, prod, sup, fac, code, qty, recv, exp, wh in lots:
        insert(conn, "inventory_lots", {
            "lot_id": lot_id, "product_id": prod, "supplier_id": sup, "facility_id": fac,
            "supplier_lot_code": code, "quantity_lb": qty,
            "received_at": _ts(recv), "expires_at": _ts(exp, 23),
            "warehouse_id": wh, "status": "available",
        })

    pos = [
        # po_id, supplier, product, qty, cost, ordered_off, eta_off, dest_wh
        # (destination = the warehouse already stocking that product line)
        ("PO-1001", "SUP-GVP", "PROD-ONION-Y", 1200, 0.55, -5, 2, "WH-OAK"),
        ("PO-1002", "SUP-GVP", "PROD-ONION-Y", 800, 0.55, -3, 4, "WH-OAK"),
        ("PO-1003", "SUP-DF", "PROD-POTATO", 1000, 0.46, -4, 2, "WH-OAK"),
        ("PO-1004", "SUP-MWG", "PROD-RICE", 1500, 0.75, -6, 5, "WH-SJ"),
        ("PO-1005", "SUP-BDD", "PROD-MILK-UHT", 600, 1.05, -2, 3, "WH-OAK"),
    ]
    for po_id, sup, prod, qty, cost, ordered, eta, wh in pos:
        insert(conn, "purchase_orders", {
            "po_id": po_id, "supplier_id": sup, "product_id": prod, "quantity_lb": qty,
            "unit_cost_per_lb": cost, "ordered_at": _ts(ordered), "expected_delivery": _d(eta),
            "warehouse_id": wh, "status": "open",
        })

    # Curated substitution table (PLAN.md §12) — only these enter the optimizer
    # via replacement offers below.
    subs = [
        ("PROD-ONION-Y", "PROD-ONION-FRZ", 0.9, "Strong culinary match; freezer required"),
        ("PROD-ONION-Y", "PROD-CABBAGE", 0.6, "Moderate meal-plan match; broadly available"),
        ("PROD-ONION-Y", "PROD-CARROT", 0.5, "Moderate nutritional and operational match"),
        ("PROD-ONION-Y", "PROD-ONION-PWD", 0.3, "Shelf-stable; not a fresh-produce replacement"),
    ]
    for prod, sub, score, note in subs:
        insert(conn, "substitutions", {"product_id": prod, "substitute_product_id": sub,
                                       "culinary_score": score, "note": note})

    offers = [
        # offer_id, supplier, product, avail_lb, cost/lb, lead_days, receiving_wh
        # Northern suppliers deliver to the Oakland DC; the Imperial Valley
        # truck (far south) unloads at the San Jose annex -- onward movement
        # is an explicit transfer in the recovery plan.
        ("OFF-1", "SUP-PFF", "PROD-ONION-FRZ", 3000, 1.40, 2, "WH-OAK"),
        ("OFF-2", "SUP-DF", "PROD-CABBAGE", 2500, 0.55, 1, "WH-OAK"),
        ("OFF-3", "SUP-DF", "PROD-CARROT", 2000, 0.60, 1, "WH-OAK"),
        ("OFF-4", "SUP-IVO", "PROD-ONION-Y", 1500, 1.10, 3, "WH-SJ"),  # post-recall premium vs $0.52 base
    ]
    for off_id, sup, prod, avail, cost, lead, wh in offers:
        insert(conn, "replacement_offers", {"offer_id": off_id, "supplier_id": sup,
                                            "product_id": prod, "available_lb": avail,
                                            "unit_cost_per_lb": cost, "lead_time_days": lead,
                                            "receiving_warehouse_id": wh})

    # 7-day distribution plan: onion lines for 4 pantries + produce mix + staples.
    # Each pantry is served from one warehouse (synthetic assignment: south-bay
    # pantries from the San Jose annex, the East Bay from Oakland).
    serving_wh = {"P-FRU": "WH-OAK", "P-RIC": "WH-OAK", "P-HAY": "WH-OAK",
                  "P-SP": "WH-OAK", "P-ALA": "WH-OAK", "P-MIL": "WH-SJ"}
    onion_pantries = ("P-FRU", "P-RIC", "P-HAY", "P-SP")
    produce_mix = {"P-FRU": "PROD-CARROT", "P-RIC": "PROD-CABBAGE", "P-HAY": "PROD-POTATO",
                   "P-SP": "PROD-POTATO", "P-ALA": "PROD-CARROT", "P-MIL": "PROD-APPLE"}
    staple = {"P-FRU": "PROD-PASTA", "P-RIC": "PROD-BEANS-DRY", "P-HAY": "PROD-PASTA",
              "P-SP": "PROD-PASTA", "P-ALA": "PROD-BEANS-DRY", "P-MIL": "PROD-RICE"}
    n = 0
    for day in range(7):
        for pid, *_ in pantries:
            if pid in onion_pantries:
                n += 1
                insert(conn, "distribution_plans", {
                    "dist_id": f"D-{n:04d}", "pantry_id": pid, "warehouse_id": serving_wh[pid],
                    "product_id": "PROD-ONION-Y",
                    "quantity_lb": 25, "scheduled_date": _d(day), "status": "planned"})
            n += 1
            insert(conn, "distribution_plans", {
                "dist_id": f"D-{n:04d}", "pantry_id": pid, "warehouse_id": serving_wh[pid],
                "product_id": produce_mix[pid],
                "quantity_lb": 40, "scheduled_date": _d(day), "status": "planned"})
            n += 1
            insert(conn, "distribution_plans", {
                "dist_id": f"D-{n:04d}", "pantry_id": pid, "warehouse_id": serving_wh[pid],
                "product_id": staple[pid],
                "quantity_lb": 30, "scheduled_date": _d(day), "status": "planned"})

    conn.commit()
