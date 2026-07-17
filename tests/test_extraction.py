from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import foodshock.extraction as extraction_mod
from foodshock.extraction import (
    ExtractionUnavailable,
    enrich_source_extraction,
    extract_notice,
)
from foodshock.schemas import RecallExtraction


def _base_extraction() -> RecallExtraction:
    return RecallExtraction(
        authority="FDA",
        products=["Frozen peas"],
        supplier_names=["Acme Foods"],
        excerpts={
            "products": "Frozen peas",
            "supplier_names": "Acme Foods",
        },
        confidence=0.99,
    )


def _candidate_extraction() -> RecallExtraction:
    return RecallExtraction(
        authority="FDA",
        products=["Frozen peas"],
        supplier_names=["Acme Foods"],
        facility_names=["Plant 7"],
        distribution_regions=["Texas"],
        excerpts={
            "products": "Frozen peas",
            "supplier_names": "Acme Foods",
            "facility_names": "Plant 7",
            "distribution_regions": "Texas",
        },
        confidence=0.91,
    )


def _source_snapshot(retrieved_at: str, *, include_facility: bool = True) -> str:
    lines = [
        "OFFICIAL SOURCE: openFDA Food Enforcement API",
        f"Retrieved at: {retrieved_at}",
        "Product: Frozen peas",
        "Recalling firm: Acme Foods",
    ]
    if include_facility:
        lines.append("Facility: Plant 7")
    lines.append("Distribution pattern: Texas")
    return "\n".join(lines)


def test_source_enrichment_reuses_stable_cache_across_retrieval_times(
    tmp_path, monkeypatch
):
    calls: list[str] = []
    candidate = _candidate_extraction()

    def fake_live(raw_text: str) -> RecallExtraction:
        calls.append(raw_text)
        return candidate

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(extraction_mod, "_live_extract", fake_live)
    cache_path = tmp_path / "extractions.json"

    first, first_method, first_dropped = enrich_source_extraction(
        _source_snapshot("2026-07-16T10:00:00Z"),
        _base_extraction(),
        allow_llm=True,
        cache_path=cache_path,
    )
    second, second_method, second_dropped = enrich_source_extraction(
        _source_snapshot("2026-07-16T10:05:00Z"),
        _base_extraction(),
        allow_llm=True,
        cache_path=cache_path,
    )

    assert first_method == "live-llm"
    assert second_method == "cached-llm"
    assert first.facility_names == second.facility_names == ["Plant 7"]
    assert first.distribution_regions == second.distribution_regions == ["Texas"]
    assert first_dropped == second_dropped == []
    assert len(calls) == 1


def test_cached_extraction_is_revalidated_against_current_snapshot(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        extraction_mod,
        "_live_extract",
        lambda _raw_text: _candidate_extraction(),
    )
    cache_path = tmp_path / "extractions.json"

    first, method, dropped = extract_notice(
        _source_snapshot("2026-07-16T10:00:00Z"),
        cache_path=cache_path,
        cache_identity="openfda:record-1",
    )
    assert method == "live-llm"
    assert first.facility_names == ["Plant 7"]
    assert dropped == []

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    refreshed, method, dropped = extract_notice(
        _source_snapshot("2026-07-16T10:05:00Z", include_facility=False),
        allow_llm=False,
        cache_path=cache_path,
        cache_identity="openfda:record-1",
    )

    assert method == "cached-llm"
    assert refreshed.facility_names == []
    assert "facility_names" in dropped


def test_live_and_cached_runs_report_identical_provenance_drops(
    tmp_path, monkeypatch
):
    raw_text = _source_snapshot("2026-07-16T10:00:00Z")
    candidate = _candidate_extraction().model_copy(
        update={
            "facility_names": ["Ghost Plant"],
            "excerpts": {
                **_candidate_extraction().excerpts,
                "facility_names": "Ghost Plant",
            },
        }
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        extraction_mod,
        "_live_extract",
        lambda _raw_text: candidate,
    )
    cache_path = tmp_path / "extractions.json"

    first, first_method, first_dropped = extract_notice(
        raw_text,
        cache_path=cache_path,
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    second, second_method, second_dropped = extract_notice(
        raw_text,
        allow_llm=False,
        cache_path=cache_path,
    )

    assert first == second
    assert first.facility_names == []
    assert first_method == "live-llm"
    assert second_method == "cached-llm"
    assert first_dropped == second_dropped == ["facility_names"]


def test_source_enrichment_fills_only_missing_deterministic_fields(monkeypatch):
    raw_text = "\n".join([
        "Product: Deterministic peas",
        "Product alias: Model peas",
        "Facility: Plant 7",
    ])
    base = RecallExtraction(
        authority="FDA",
        products=["Deterministic peas"],
        excerpts={"products": "Deterministic peas"},
        confidence=0.99,
    )
    candidate = RecallExtraction(
        authority="FDA",
        products=["Model peas"],
        facility_names=["Plant 7"],
        excerpts={
            "products": "Model peas",
            "facility_names": "Plant 7",
        },
        confidence=0.9,
    )
    monkeypatch.setattr(
        extraction_mod,
        "extract_notice",
        lambda *_args, **_kwargs: (candidate, "cached-llm", []),
    )

    merged, method, dropped = enrich_source_extraction(
        raw_text,
        base,
        allow_llm=False,
    )

    assert merged.products == ["Deterministic peas"]
    assert merged.facility_names == ["Plant 7"]
    assert method == "cached-llm"
    assert dropped == []


def test_enrichment_method_is_none_when_model_contributes_nothing(monkeypatch):
    raw_text = "Product: Frozen peas\nRecalling firm: Acme Foods"
    base = _base_extraction()
    monkeypatch.setattr(
        extraction_mod,
        "extract_notice",
        lambda *_args, **_kwargs: (base, "cached-llm", []),
    )

    merged, method, dropped = enrich_source_extraction(
        raw_text,
        base,
        allow_llm=False,
    )

    assert merged == base
    assert method is None
    assert dropped == []


def test_enrichment_ignores_failed_candidates_for_deterministic_fields(
    monkeypatch,
):
    raw_text = "Product: Frozen peas\nRecalling firm: Acme Foods"
    base = _base_extraction()
    candidate = RecallExtraction(
        authority="FDA",
        products=["Invented peas"],
        facility_names=["Ghost Plant"],
        excerpts={
            "products": "Invented peas",
            "facility_names": "Ghost Plant",
        },
        confidence=0.5,
    )
    monkeypatch.setattr(
        extraction_mod,
        "extract_notice",
        lambda *_args, **_kwargs: (
            candidate,
            "live-llm",
            ["products", "facility_names"],
        ),
    )

    merged, method, dropped = enrich_source_extraction(
        raw_text,
        base,
        allow_llm=True,
    )

    assert merged.products == ["Frozen peas"]
    assert merged.facility_names == []
    assert method is None
    assert dropped == ["facility_names"]


def test_allow_llm_false_never_invokes_live_extraction(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        extraction_mod,
        "_live_extract",
        lambda *_args: pytest.fail("live extraction must remain disabled"),
    )

    merged, method, dropped = enrich_source_extraction(
        _source_snapshot("2026-07-16T10:00:00Z"),
        _base_extraction(),
        allow_llm=False,
        cache_path=tmp_path / "missing-cache.json",
    )

    assert merged == _base_extraction()
    assert method is None
    assert dropped == []


def test_explicit_cache_rejects_legacy_keys(tmp_path, monkeypatch):
    raw_text = _source_snapshot("2026-07-16T10:00:00Z")
    cache_path = tmp_path / "legacy.json"
    cache_path.write_text(json.dumps({
        extraction_mod._legacy_key(raw_text): json.loads(
            _candidate_extraction().model_dump_json()
        )
    }))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ExtractionUnavailable):
        extract_notice(raw_text, allow_llm=False, cache_path=cache_path)


def test_live_response_uses_supported_anthropic_sdk_contract(monkeypatch):
    captured: dict = {}
    response = object()

    def parse(**kwargs):
        captured.update(kwargs)
        return response

    fake_anthropic = SimpleNamespace(
        Anthropic=lambda: SimpleNamespace(messages=SimpleNamespace(parse=parse))
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    assert extraction_mod._create_live_response("source text") is response
    assert captured["model"] == "claude-opus-4-8"
    assert captured["max_tokens"] == 16000
    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["output_config"] == {"effort": "low"}
    assert captured["output_format"] is RecallExtraction
    assert "source text" in captured["messages"][0]["content"]


def test_live_extract_rejects_refusal_or_missing_structured_output(monkeypatch):
    monkeypatch.setattr(
        extraction_mod,
        "_create_live_response",
        lambda _raw_text: SimpleNamespace(stop_reason="refusal", parsed_output=None),
    )
    with pytest.raises(RuntimeError, match="refused"):
        extraction_mod._live_extract("source text")

    monkeypatch.setattr(
        extraction_mod,
        "_create_live_response",
        lambda _raw_text: SimpleNamespace(stop_reason="end_turn", parsed_output=None),
    )
    with pytest.raises(RuntimeError, match="no structured extraction"):
        extraction_mod._live_extract("source text")
