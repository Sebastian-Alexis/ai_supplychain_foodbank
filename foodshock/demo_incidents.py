"""Curated synthetic incidents for the Streamlit framework demonstration.

Each incident runs through the same RecallResponseAgent and deterministic
planning engine. Only the notice and incident-specific recovery market change.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .db import DATA_DIR, insert


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
