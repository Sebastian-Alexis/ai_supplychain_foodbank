from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from foodshock.datagen import generate
from foodshock.db import insert, now_iso, rows
from foodshock.engine import resolve_event
from foodshock.schemas import RecallExtraction
from test_timing import _conn


def _seeded_lot():
    conn = _conn()
    generate(conn)
    lot = rows(
        conn,
        """
        SELECT l.*, p.name product_name, p.upc product_upc, p.category,
               s.name supplier_name, s.state supplier_state
        FROM inventory_lots l
        JOIN products p ON p.product_id=l.product_id
        JOIN suppliers s ON s.supplier_id=l.supplier_id
        WHERE p.upc IS NOT NULL AND l.supplier_lot_code IS NOT NULL
        ORDER BY l.lot_id
        LIMIT 1
        """,
    )[0]
    return conn, lot


def _seeded_po():
    conn = _conn()
    generate(conn)
    po = rows(
        conn,
        """
        SELECT po.*, p.name product_name, p.upc product_upc, p.category,
               s.name supplier_name
        FROM purchase_orders po
        JOIN products p ON p.product_id=po.product_id
        JOIN suppliers s ON s.supplier_id=po.supplier_id
        WHERE p.upc IS NOT NULL
        ORDER BY po.po_id
        LIMIT 1
        """,
    )[0]
    return conn, po


def _store_event(conn, event_id: str, extraction: RecallExtraction) -> None:
    insert(
        conn,
        "recall_events",
        {
            "event_id": event_id,
            "authority": extraction.authority,
            "status": "active",
            "published_at": "2026-07-16",
            "ingested_at": now_iso(),
            "source_url": "https://example.invalid/recall",
            "raw_text": "resolver test notice",
            "extraction_json": extraction.model_dump_json(),
            "extraction_confidence": extraction.confidence,
            "extraction_method": "test",
            "human_confirmed": 0,
        },
    )


def _window_for(received_at: str, applicability: str):
    received = date.fromisoformat(received_at[:10])
    if applicability == "overlap":
        return received - timedelta(days=1), received
    if applicability == "conflict":
        return received - timedelta(days=40), received - timedelta(days=30)
    if applicability == "unknown":
        return None, None
    raise AssertionError(f"unexpected applicability: {applicability}")


@pytest.mark.parametrize(
    ("applicability", "expected_state", "expected_tier"),
    [
        ("overlap", "confirmed", 1),
        ("unknown", "unknown", 1),
        ("conflict", "not_matched", 0),
    ],
)
def test_upc_requires_applicable_date_evidence(
    applicability, expected_state, expected_tier
):
    conn, lot = _seeded_lot()
    start, end = _window_for(lot["received_at"], applicability)
    event_id = f"UPC-{applicability}"
    extraction = RecallExtraction(
        authority="FDA",
        products=[lot["product_name"]],
        upcs=[lot["product_upc"]],
        production_date_start=start,
        production_date_end=end,
        confidence=0.99,
    )
    _store_event(conn, event_id, extraction)

    resolve_event(conn, event_id)
    match = rows(
        conn,
        "SELECT state, tier, evidence_json FROM matches "
        "WHERE event_id=? AND target_type='lot' AND target_id=?",
        (event_id, lot["lot_id"]),
    )[0]
    evidence = json.loads(match["evidence_json"])

    assert (match["state"], match["tier"]) == (expected_state, expected_tier)
    if applicability == "unknown":
        assert any("date applicability unresolved" in reason for reason in evidence["reasons"])
        assert not any("falls within" in reason for reason in evidence["reasons"])
    elif applicability == "overlap":
        assert any("falls within" in reason for reason in evidence["reasons"])




def test_far_future_po_is_not_confirmed_by_product_upc_alone():
    conn, po = _seeded_po()
    delivery = date.fromisoformat(po["expected_delivery"][:10])
    event_id = "UPC-FAR-FUTURE-PO"
    extraction = RecallExtraction(
        authority="FDA",
        products=[po["product_name"]],
        upcs=[po["product_upc"]],
        production_date_start=delivery - timedelta(days=400),
        production_date_end=delivery - timedelta(days=365),
        confidence=0.99,
    )
    _store_event(conn, event_id, extraction)

    resolve_event(conn, event_id)
    match = rows(
        conn,
        "SELECT state, tier FROM matches "
        "WHERE event_id=? AND target_type='po' AND target_id=?",
        (event_id, po["po_id"]),
    )[0]

    assert match == {"state": "not_matched", "tier": 0}


def test_exact_lot_code_remains_decisive_without_a_date_window():
    conn, lot = _seeded_lot()
    event_id = "LOT-CODE-EXACT"
    extraction = RecallExtraction(
        authority="FDA",
        lot_codes=[lot["supplier_lot_code"]],
        confidence=0.99,
    )
    _store_event(conn, event_id, extraction)

    resolve_event(conn, event_id)
    match = rows(
        conn,
        "SELECT state, tier FROM matches WHERE event_id=? AND target_id=?",
        (event_id, lot["lot_id"]),
    )[0]

    assert match == {"state": "confirmed", "tier": 1}


@pytest.mark.parametrize(
    ("applicability", "expected_state"),
    [("overlap", "probable"), ("unknown", "unknown"), ("conflict", "not_matched")],
)
def test_exact_supplier_tier_does_not_treat_missing_dates_as_overlap(
    applicability, expected_state
):
    conn, lot = _seeded_lot()
    start, end = _window_for(lot["received_at"], applicability)
    event_id = f"SUPPLIER-{applicability}"
    extraction = RecallExtraction(
        authority="FDA",
        products=[lot["product_name"]],
        supplier_names=[lot["supplier_name"]],
        production_date_start=start,
        production_date_end=end,
        confidence=0.99,
    )
    _store_event(conn, event_id, extraction)

    resolve_event(conn, event_id)
    match = rows(
        conn,
        "SELECT state, tier, evidence_json FROM matches "
        "WHERE event_id=? AND target_type='lot' AND target_id=?",
        (event_id, lot["lot_id"]),
    )[0]
    reasons = json.loads(match["evidence_json"])["reasons"]

    assert match["state"] == expected_state
    if applicability == "overlap":
        assert match["tier"] == 2
        assert any("falls within" in reason for reason in reasons)
    elif applicability == "unknown":
        assert match["tier"] == 2
        assert any("date applicability unresolved" in reason for reason in reasons)
        assert not any("overlap" in reason or "falls within" in reason for reason in reasons)


@pytest.mark.parametrize("region", ["Texas", "Nationwide"])
def test_distribution_geography_is_not_standalone_exposure_evidence(region):
    conn, lot = _seeded_lot()
    event_id = f"REGION-{region.upper()}"
    extraction = RecallExtraction(
        authority="FDA",
        products=[lot["product_name"]],
        distribution_regions=[region],
        confidence=0.99,
    )
    _store_event(conn, event_id, extraction)

    resolve_event(conn, event_id)
    states = {
        match["state"]
        for match in rows(conn, "SELECT state FROM matches WHERE event_id=?", (event_id,))
    }

    assert states == {"not_matched"}
