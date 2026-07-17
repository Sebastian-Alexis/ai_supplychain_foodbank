"""Offline contracts for official incident-source normalization."""

from __future__ import annotations

import json
from io import BytesIO
from http.client import IncompleteRead

import pytest

import foodshock.incident_sources as incident_sources

from foodshock.incident_sources import (
    IncidentSourceError,
    _normalize_fsis,
    _normalize_openfda,
    _read_json_array_prefix,
    parse_source_snapshot,
)


class _ChunkedResponse:
    def __init__(self, payload: bytes, chunk_size: int = 7):
        self.payload = payload
        self.chunk_size = chunk_size
        self.offset = 0

    def read(self, _size: int = -1) -> bytes:
        if self.offset >= len(self.payload):
            return b""
        end = min(len(self.payload), self.offset + self.chunk_size)
        chunk = self.payload[self.offset:end]
        self.offset = end
        return chunk


def test_openfda_groups_records_without_calling_non_pathogens_pathogens():
    payload = {
        "meta": {"last_updated": "2026-07-08"},
        "results": [
            {
                "event_id": "99292",
                "status": "Ongoing",
                "classification": "Class II",
                "recalling_firm": "Example Foods",
                "product_description": "Vegan nuggets, UPC 028989101105",
                "reason_for_recall": "Potential contamination with plastic material",
                "distribution_pattern": "Nationwide",
                "recall_number": "H-1-2026",
                "code_info": "Best by 2027-01-01",
                "recall_initiation_date": "20260618",
                "report_date": "20260708",
                "product_quantity": "10 cases",
            },
            {
                "event_id": "99292",
                "status": "Ongoing",
                "classification": "Class II",
                "recalling_firm": "Example Foods",
                "product_description": "Vegan patties, UPC 028989100948",
                "reason_for_recall": "Undeclared peanuts",
                "distribution_pattern": "Nationwide",
                "recall_number": "H-2-2026",
                "code_info": "Lot 22",
                "recall_initiation_date": "20260618",
                "report_date": "20260708",
                "product_quantity": "5 cases",
            },
        ],
    }

    incidents = _normalize_openfda(
        payload, limit=5, retrieved_at="2026-07-16T12:00:00+00:00"
    )

    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.event_id == "FDA-99292"
    assert incident.published_at == "2026-07-08"
    assert incident.classification == "Class II"
    assert incident.extraction.products == [
        "Vegan nuggets, UPC 028989101105",
        "Vegan patties, UPC 028989100948",
    ]
    assert incident.extraction.supplier_names == ["Example Foods"]
    assert incident.extraction.upcs == ["028989101105", "028989100948"]
    assert incident.extraction.pathogen is None
    assert "plastic material" in incident.reason_summary
    assert "Undeclared peanuts" in incident.raw_text
    assert "event_id%3A%2299292%22" in incident.source_url
    overview, product_records = parse_source_snapshot(incident.raw_text)
    assert overview["Event ID"] == "99292"
    assert overview["Distribution pattern"] == "Nationwide"
    assert product_records == [
        {
            "Record": "1",
            "Recall number": "H-1-2026",
            "Product": "Vegan nuggets, UPC 028989101105",
            "Reason for recall": "Potential contamination with plastic material",
            "Code information": "Best by 2027-01-01",
            "Recall initiation date": "20260618",
            "Report date": "20260708",
            "Quantity": "10 cases",
        },
        {
            "Record": "2",
            "Recall number": "H-2-2026",
            "Product": "Vegan patties, UPC 028989100948",
            "Reason for recall": "Undeclared peanuts",
            "Code information": "Lot 22",
            "Recall initiation date": "20260618",
            "Report date": "20260708",
            "Quantity": "5 cases",
        },
    ]


def test_fsis_filters_language_and_prioritizes_active_records():
    records = [
        {
            "langcode": "Spanish",
            "field_recall_number": "SPANISH-1",
            "field_active_notice": "True",
            "field_recall_date": "2026-07-10",
        },
        {
            "langcode": "English",
            "field_title": "Inactive allergen alert",
            "field_recall_number": "PHA-NEW",
            "field_active_notice": "False",
            "field_recall_date": "2026-07-09",
            "field_recall_classification": "Public Health Alert",
            "field_product_items": ["Breaded chicken product"],
            "field_establishment": ["Example Poultry"],
            "field_states": ["California"],
            "field_recall_reason": ["Unreported Allergens"],
            "field_summary": "<p>Contains undeclared milk.</p>",
            "field_recall_url": "http://www.fsis.usda.gov/recalls-alerts/new",
        },
        {
            "langcode": "English",
            "field_title": "Active pathogen recall",
            "field_recall_number": "010-2026",
            "field_active_notice": "True",
            "field_recall_date": "2026-06-01",
            "field_recall_classification": "Class I",
            "field_product_items": ["Frozen chicken portions"],
            "field_establishment": ["Example Poultry"],
            "field_states": ["Nevada"],
            "field_recall_reason": ["Product contamination"],
            "field_summary": "<p>The product may contain Salmonella.</p>",
            "field_recall_url": "http://www.fsis.usda.gov/recalls-alerts/active",
        },
    ]

    incidents = _normalize_fsis(
        records, limit=5, retrieved_at="2026-07-16T12:00:00+00:00"
    )

    assert [incident.event_id for incident in incidents] == [
        "USDA-010-2026",
        "USDA-PHA-NEW",
    ]
    assert incidents[0].status == "Active"
    assert incidents[0].extraction.pathogen == "Salmonella"
    assert incidents[0].source_url.startswith("https://")
    assert incidents[1].status == "Recent / inactive"
    assert incidents[1].extraction.pathogen is None
    assert "Contains undeclared milk." in incidents[1].raw_text


def test_fsis_prefix_decoder_handles_split_unicode_and_stops_at_limit():
    expected = [
        {"id": 1, "title": "First – alert"},
        {"id": 2, "title": "Second recall"},
        {"id": 3, "title": "Third recall"},
    ]
    payload = json.dumps(expected, ensure_ascii=False).encode("utf-8")

    decoded = _read_json_array_prefix(
        _ChunkedResponse(payload, chunk_size=5), max_items=2
    )

    assert decoded == expected[:2]


def test_fsis_prefix_decoder_rejects_unterminated_array():
    with pytest.raises(json.JSONDecodeError, match="truncated JSON array"):
        _read_json_array_prefix(
            _ChunkedResponse(b'[{"id": 1}', chunk_size=4),
            max_items=2,
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"[{}{}]",
        b"[,,{}]",
        b"[{},]",
    ],
)
def test_fsis_prefix_decoder_rejects_invalid_separators(payload):
    with pytest.raises(json.JSONDecodeError):
        _read_json_array_prefix(
            _ChunkedResponse(payload, chunk_size=2),
            max_items=3,
        )


def test_openfda_adapter_isolates_malformed_upstream_records(monkeypatch):
    malformed_payload = {
        "meta": [],
        "results": [{"event_id": "99292"}],
    }
    monkeypatch.setattr(
        incident_sources,
        "_request_json",
        lambda *_args, **_kwargs: malformed_payload,
    )

    with pytest.raises(
        IncidentSourceError,
        match="openFDA returned malformed incident data",
    ) as caught:
        incident_sources.fetch_openfda_incidents(limit=1)

    assert isinstance(caught.value.__cause__, AttributeError)


def test_fetch_argument_errors_remain_caller_errors():
    with pytest.raises(ValueError, match="limit must be between 1 and 25"):
        incident_sources.fetch_openfda_incidents(limit=0)


@pytest.mark.parametrize(
    ("fetcher", "message"),
    [
        (incident_sources.fetch_openfda_incidents, "openFDA feed unavailable"),
        (incident_sources.fetch_fsis_incidents, "USDA FSIS feed unavailable"),
    ],
)
def test_adapters_isolate_invalid_utf8(monkeypatch, fetcher, message):
    monkeypatch.setattr(
        incident_sources,
        "urlopen",
        lambda *_args, **_kwargs: BytesIO(b"\xff"),
    )

    with pytest.raises(IncidentSourceError, match=message) as caught:
        fetcher(limit=1)

    assert isinstance(caught.value.__cause__, UnicodeDecodeError)


@pytest.mark.parametrize(
    ("fetcher", "message"),
    [
        (incident_sources.fetch_openfda_incidents, "openFDA feed unavailable"),
        (incident_sources.fetch_fsis_incidents, "USDA FSIS feed unavailable"),
    ],
)
def test_adapters_isolate_truncated_http_responses(monkeypatch, fetcher, message):
    def raise_incomplete_read(*_args, **_kwargs):
        raise IncompleteRead(b"partial response")

    monkeypatch.setattr(incident_sources, "urlopen", raise_incomplete_read)

    with pytest.raises(IncidentSourceError, match=message) as caught:
        fetcher(limit=1)

    assert isinstance(caught.value.__cause__, IncompleteRead)
