"""Contracts for the disclosed incident-linked synthetic trace sample."""

from __future__ import annotations

from foodshock.datagen import generate
from foodshock.db import get_conn, insert, now_iso, rows
from foodshock.demo_incidents import (
    LIVE_SAMPLE_LOT_ID,
    LIVE_SAMPLE_PO_ID,
    LIVE_SAMPLE_PRODUCT_ID,
    prepare_live_incident_overlay,
)
from foodshock.engine import resolve_event
from foodshock.schemas import RecallExtraction


def _record_event(conn, event_id: str, extraction: RecallExtraction) -> None:
    insert(conn, "recall_events", {
        "event_id": event_id,
        "authority": extraction.authority,
        "status": "active",
        "ingested_at": now_iso(),
        "raw_text": "normalized authority snapshot",
        "extraction_json": extraction.model_dump_json(),
    })
    conn.commit()


def test_trace_sample_exercises_exact_upc_without_claiming_complete_dates():
    conn = get_conn(":memory:")
    generate(conn)
    extraction = RecallExtraction(
        authority="FDA",
        products=["Frozen chicken nuggets, UPC 028989101105"],
        supplier_names=["Example Foods"],
        upcs=["028989101105"],
        excerpts={
            "products": "Frozen chicken nuggets, UPC 028989101105",
            "supplier_names": "Example Foods",
            "upcs": "Frozen chicken nuggets, UPC 028989101105",
        },
        confidence=0.99,
    )

    metadata = prepare_live_incident_overlay(conn, extraction)
    _record_event(conn, "EV-LIVE-SAMPLE", extraction)
    matches = resolve_event(conn, "EV-LIVE-SAMPLE")

    sample_matches = [
        match for match in matches
        if match["target_id"] in {LIVE_SAMPLE_LOT_ID, LIVE_SAMPLE_PO_ID}
    ]
    assert metadata == {
        "basis": "authority UPC",
        "lot_id": LIVE_SAMPLE_LOT_ID,
        "po_id": LIVE_SAMPLE_PO_ID,
    }
    assert len(sample_matches) == 2
    assert {match["state"] for match in sample_matches} == {"unknown"}
    assert {match["tier"] for match in sample_matches} == {1}
    product = rows(
        conn,
        "SELECT category, temperature_zone, upc FROM products WHERE product_id=?",
        (LIVE_SAMPLE_PRODUCT_ID,),
    )[0]
    assert product == {
        "category": "protein",
        "temperature_zone": "frozen",
        "upc": "028989101105",
    }


def test_trace_sample_does_not_pair_flattened_upc_with_unrelated_product():
    conn = get_conn(":memory:")
    generate(conn)
    extraction = RecallExtraction(
        authority="FDA",
        products=["Plain vegan bites", "Seasoned vegan patties"],
        supplier_names=["Example Foods"],
        upcs=["028989101105"],
        excerpts={
            "products": "Plain vegan bites",
            "supplier_names": "Example Foods",
            "upcs": "Code information: case UPC 028989101105",
        },
        confidence=0.99,
    )

    metadata = prepare_live_incident_overlay(conn, extraction)
    _record_event(conn, "EV-NO-FALSE-PAIR", extraction)
    matches = resolve_event(conn, "EV-NO-FALSE-PAIR")

    product = rows(
        conn,
        "SELECT name, upc FROM products WHERE product_id=?",
        (LIVE_SAMPLE_PRODUCT_ID,),
    )[0]
    assert product == {"name": "Plain vegan bites", "upc": None}
    assert metadata is not None
    assert metadata["basis"] == "authority supplier alias and product"
    sample_matches = [
        match for match in matches
        if match["target_id"] in {LIVE_SAMPLE_LOT_ID, LIVE_SAMPLE_PO_ID}
    ]
    assert len(sample_matches) == 2
    assert {match["state"] for match in sample_matches} == {"unknown"}
    assert {match["tier"] for match in sample_matches} == {3}


def test_disabling_trace_sample_preserves_honest_zero_exposure():
    conn = get_conn(":memory:")
    generate(conn)
    extraction = RecallExtraction(
        authority="FDA",
        products=["Frozen chicken nuggets, UPC 028989101105"],
        supplier_names=["Example Foods"],
        upcs=["028989101105"],
        confidence=0.99,
    )
    _record_event(conn, "EV-UNTOUCHED", extraction)

    matches = resolve_event(conn, "EV-UNTOUCHED")

    assert not any(match["state"] != "not_matched" for match in matches)
    assert not rows(
        conn,
        "SELECT 1 FROM inventory_lots WHERE lot_id=?",
        (LIVE_SAMPLE_LOT_ID,),
    )
    assert not rows(
        conn,
        "SELECT 1 FROM purchase_orders WHERE po_id=?",
        (LIVE_SAMPLE_PO_ID,),
    )
