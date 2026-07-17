"""Curated synthetic incidents for the Streamlit framework demonstration.

Each incident runs through the same RecallResponseAgent and deterministic
planning engine. Only the notice and incident-specific recovery market change.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .db import DATA_DIR, insert
from .datagen import TODAY
from .schemas import RecallExtraction


@dataclass(frozen=True)
class DemoIncident:
    key: str
    event_id: str
    selector_label: str
    title: str
    authority: str
    hazard: str
    product: str
    response_angle: str
    notice_file: str
    source_url: str

    @property
    def notice_path(self) -> Path:
        return DATA_DIR / self.notice_file


DEFAULT_INCIDENT_KEY = "onion_ecoli"

INCIDENTS: dict[str, DemoIncident] = {
    DEFAULT_INCIDENT_KEY: DemoIncident(
        key=DEFAULT_INCIDENT_KEY,
        event_id="FDA-DEMO-2026-001",
        selector_label="E. coli · yellow onions",
        title="Regional onion recall",
        authority="FDA",
        hazard="E. coli O157:H7",
        product="Fresh yellow onions",
        response_angle="Trace lot lineage, stop 28 planned drops, and source produce substitutes.",
        notice_file="notice_ecoli_onions.txt",
        source_url="https://example.invalid/demo/onion-recall",
    ),
    "chicken_salmonella": DemoIncident(
        key="chicken_salmonella",
        event_id="FDA-DEMO-2026-002",
        selector_label="Salmonella · frozen chicken",
        title="Frozen protein recall",
        authority="FDA",
        hazard="Salmonella",
        product="Frozen chicken quarters",
        response_angle="Quarantine cold-chain inventory and recover protein supply from a verified alternate source.",
        notice_file="notice_salmonella_chicken.txt",
        source_url="https://example.invalid/demo/chicken-recall",
    ),
    "pasta_allergen": DemoIncident(
        key="pasta_allergen",
        event_id="FDA-DEMO-2026-003",
        selector_label="Undeclared milk · dry pasta",
        title="Undeclared-allergen recall",
        authority="FDA",
        hazard="Undeclared milk allergen",
        product="Dry pasta",
        response_angle="Isolate the named lot and replace affected grain allocations with allergen-compatible rice.",
        notice_file="notice_undeclared_milk_pasta.txt",
        source_url="https://example.invalid/demo/pasta-allergen-recall",
    ),
}


def incident_for_event(event_id: str | None) -> DemoIncident | None:
    if event_id is None:
        return None
    return next((incident for incident in INCIDENTS.values()
                 if incident.event_id == event_id), None)


def prepare_demo_incident(conn: sqlite3.Connection, incident: DemoIncident) -> None:
    """Install only the recovery market relevant to the selected incident.

    The generated operational network is shared by every demo. Alternate offers
    vary by incident so the same framework must produce a materially different
    recovery plan rather than merely swapping labels.
    """
    if incident.key == DEFAULT_INCIDENT_KEY:
        return

    conn.execute("DELETE FROM replacement_offers")
    conn.execute("DELETE FROM substitutions")

    if incident.key == "chicken_salmonella":
        insert(conn, "suppliers", {
            "supplier_id": "SUP-CVP", "name": "Central Valley Protein Partners",
            "city": "Modesto", "state": "CA", "lat": 37.6391, "lon": -120.9969,
        })
        insert(conn, "substitutions", {
            "product_id": "PROD-CHICKEN-FRZ",
            "substitute_product_id": "PROD-CHICKEN-FRZ",
            "culinary_score": 1.0,
            "note": "Equivalent frozen protein from a verified alternate supplier",
        })
        insert(conn, "replacement_offers", {
            "offer_id": "OFF-CHICKEN-ALT", "supplier_id": "SUP-CVP",
            "product_id": "PROD-CHICKEN-FRZ", "available_lb": 4000,
            "unit_cost_per_lb": 2.15, "lead_time_days": 2,
            "receiving_warehouse_id": "WH-OAK",
        })
    elif incident.key == "pasta_allergen":
        insert(conn, "suppliers", {
            "supplier_id": "SUP-SGM", "name": "Sierra Grain Milling",
            "city": "Woodland", "state": "CA", "lat": 38.6785, "lon": -121.7733,
        })
        insert(conn, "substitutions", {
            "product_id": "PROD-PASTA", "substitute_product_id": "PROD-RICE",
            "culinary_score": 0.8,
            "note": "Milk-free, wheat-free grain allocation suitable for every partner pantry",
        })
        insert(conn, "replacement_offers", {
            "offer_id": "OFF-RICE-ALT", "supplier_id": "SUP-SGM",
            "product_id": "PROD-RICE", "available_lb": 3000,
            "unit_cost_per_lb": 0.88, "lead_time_days": 1,
            "receiving_warehouse_id": "WH-OAK",
        })
    else:
        raise ValueError(f"unknown demo incident: {incident.key}")

    conn.commit()


LIVE_SAMPLE_PREFIX = "SYN-LIVE-"
LIVE_SAMPLE_SUPPLIER_ID = f"{LIVE_SAMPLE_PREFIX}SUPPLIER"
LIVE_SAMPLE_FACILITY_ID = f"{LIVE_SAMPLE_PREFIX}FACILITY"
LIVE_SAMPLE_PRODUCT_ID = f"{LIVE_SAMPLE_PREFIX}PRODUCT"
LIVE_SAMPLE_LOT_ID = f"{LIVE_SAMPLE_PREFIX}LOT"
LIVE_SAMPLE_PO_ID = f"{LIVE_SAMPLE_PREFIX}PO"
LIVE_SAMPLE_DIST_ID = f"{LIVE_SAMPLE_PREFIX}DIST"


def _sample_product_profile(product_name: str) -> tuple[str, str, int]:
    """Assign plausible attributes to a clearly synthetic operational sample."""
    name = product_name.lower()
    if any(word in name for word in (
        "beef", "chicken", "poultry", "pork", "turkey", "meat", "sausage",
        "ham", "fish", "seafood", "shrimp", "tuna", "egg", "nugget",
    )):
        category, temperature_zone, shelf_life_days = "protein", "refrigerated", 14
    elif any(word in name for word in (
        "milk", "cheese", "yogurt", "cream", "butter",
    )):
        category, temperature_zone, shelf_life_days = "dairy", "refrigerated", 14
    elif any(word in name for word in (
        "rice", "pasta", "bread", "flour", "grain", "cereal", "tortilla",
        "noodle",
    )):
        category, temperature_zone, shelf_life_days = "grain", "ambient", 180
    elif any(word in name for word in (
        "produce", "vegetable", "fruit", "onion", "potato", "lettuce",
        "spinach", "tomato", "pepper", "cucumber", "melon", "apple",
        "peach", "berry", "mango", "avocado", "sprout", "salad",
    )):
        category, temperature_zone, shelf_life_days = "produce", "refrigerated", 21
    else:
        category, temperature_zone, shelf_life_days = "shelf_stable", "ambient", 90
    if "frozen" in name:
        temperature_zone = "frozen"
        shelf_life_days = max(shelf_life_days, 180)
    return category, temperature_zone, shelf_life_days


def _iso_timestamp(value: date) -> str:
    return f"{value.isoformat()}T12:00:00+00:00"


def _paired_sample_product(
    extraction: RecallExtraction,
) -> tuple[str, str | None, str | None]:
    """Keep flattened identifiers only when the product text binds them."""
    for product_name in extraction.products:
        folded_product = product_name.casefold()
        product_digits = "".join(char for char in product_name if char.isdigit())
        lot_code = next(
            (
                value for value in extraction.lot_codes
                if len(value) >= 4 and value.casefold() in folded_product
            ),
            None,
        )
        upc = next(
            (
                value for value in extraction.upcs
                if len(digits := "".join(char for char in value if char.isdigit())) >= 8
                and digits in product_digits
            ),
            None,
        )
        if lot_code or upc:
            return product_name, upc, lot_code
    return extraction.products[0], None, None


def prepare_live_incident_overlay(
    conn: sqlite3.Connection,
    extraction: RecallExtraction,
) -> dict[str, str] | None:
    """Add one incident-linked, unmistakably synthetic trace sample.

    The overlay copies only authority identifiers already present in the
    extraction. It creates no claim about the food bank's real inventory:
    every operational identifier carries the ``SYN-LIVE-`` prefix, and the UI
    labels the resulting match as a demonstration. If the authority record has
    too little identifying evidence to exercise a matching tier, nothing is
    inserted and the honest result remains zero exposure.
    """
    if not extraction.products:
        return None
    supplier_name = extraction.supplier_names[0] if extraction.supplier_names else ""
    facility_name = extraction.facility_names[0] if extraction.facility_names else ""
    product_name, upc, lot_code = _paired_sample_product(extraction)
    if not any((supplier_name, facility_name, upc, lot_code)):
        return None
    category, temperature_zone, shelf_life_days = _sample_product_profile(product_name)
    operational_date = (
        extraction.production_date_end
        or extraction.production_date_start
        or TODAY
    )
    expiry_date = max(operational_date + timedelta(days=shelf_life_days),
                      TODAY + timedelta(days=30))

    insert(conn, "suppliers", {
        "supplier_id": LIVE_SAMPLE_SUPPLIER_ID,
        "name": (
            f"Synthetic sample · {supplier_name}"
            if supplier_name else "Synthetic sample · supplier not stated"
        ),
        "city": "Demo record",
        "state": "CA",
    })
    if supplier_name:
        insert(conn, "supplier_aliases", {
            "supplier_id": LIVE_SAMPLE_SUPPLIER_ID,
            "alias": supplier_name,
        })

    facility_id = None
    if facility_name:
        facility_id = LIVE_SAMPLE_FACILITY_ID
        insert(conn, "facilities", {
            "facility_id": facility_id,
            "supplier_id": LIVE_SAMPLE_SUPPLIER_ID,
            "name": facility_name,
            "city": "Demo record",
            "state": "CA",
        })

    insert(conn, "products", {
        "product_id": LIVE_SAMPLE_PRODUCT_ID,
        "name": product_name,
        "category": category,
        "allergens": "",
        "upc": upc,
        "unit_cost_per_lb": 1.0,
        "temperature_zone": temperature_zone,
        "shelf_life_days": shelf_life_days,
    })
    insert(conn, "inventory_lots", {
        "lot_id": LIVE_SAMPLE_LOT_ID,
        "product_id": LIVE_SAMPLE_PRODUCT_ID,
        "supplier_id": LIVE_SAMPLE_SUPPLIER_ID,
        "facility_id": facility_id,
        "supplier_lot_code": lot_code,
        "quantity_lb": 240.0,
        "received_at": _iso_timestamp(operational_date),
        "expires_at": _iso_timestamp(expiry_date),
        "warehouse_id": "WH-OAK",
        "status": "available",
    })
    insert(conn, "purchase_orders", {
        "po_id": LIVE_SAMPLE_PO_ID,
        "supplier_id": LIVE_SAMPLE_SUPPLIER_ID,
        "product_id": LIVE_SAMPLE_PRODUCT_ID,
        "quantity_lb": 360.0,
        "unit_cost_per_lb": 1.0,
        "ordered_at": _iso_timestamp(operational_date - timedelta(days=1)),
        "expected_delivery": operational_date.isoformat(),
        "warehouse_id": "WH-OAK",
        "status": "open",
    })
    insert(conn, "distribution_plans", {
        "dist_id": LIVE_SAMPLE_DIST_ID,
        "pantry_id": "P-FRU",
        "warehouse_id": "WH-OAK",
        "product_id": LIVE_SAMPLE_PRODUCT_ID,
        "quantity_lb": 50.0,
        "scheduled_date": (TODAY + timedelta(days=1)).isoformat(),
        "status": "planned",
    })
    conn.commit()

    if lot_code:
        basis = "authority lot code"
    elif upc:
        basis = "authority UPC"
    elif supplier_name:
        basis = "authority supplier alias and product"
    else:
        basis = "authority facility and product"
    return {
        "basis": basis,
        "lot_id": LIVE_SAMPLE_LOT_ID,
        "po_id": LIVE_SAMPLE_PO_ID,
    }
