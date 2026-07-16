"""Narration grounding guard and honest method labeling (PLAN.md §9, §19):
LLM text ships only when every number is grounded in the fact bundle; cache
and network failures fall back to the deterministic template, and template
output is never labeled as LLM work."""

from __future__ import annotations

import json

import foodshock.narrate as narrate_mod
from foodshock.narrate import _key, narrate, ungrounded_numbers

FACTS = {"quarantined_lb": 1900.0, "dos": 3.6, "pos": 3, "share": 0.83,
         "note": "window 2026-06-20 to 2026-07-02"}


def test_grounded_numbers_pass():
    text = ("1,900 lb quarantined; 3.6 days of supply; 3 purchase orders; "
            "share 0.83; window began 2026-06-20 (June 20).")
    assert ungrounded_numbers(text, FACTS) == []


def test_invented_and_converted_numbers_flagged():
    assert ungrounded_numbers("about 2000 lb quarantined", FACTS) == ["2000"]
    # Unit/percent conversion is invention under the guard: 0.83 -> 83%.
    assert ungrounded_numbers("83% share", FACTS) == ["83"]


def test_offline_without_cache_is_template(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    text, method = narrate("explain", FACTS, "fallback text",
                           cache_path=tmp_path / "cache.json")
    assert (text, method) == ("fallback text", "template")


def test_cached_text_replays_offline(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({_key("explain", FACTS): "cached: 1900 lb, 3.6 days."}))
    text, method = narrate("explain", FACTS, "fallback", cache_path=cache)
    assert method == "cached-llm" and text.startswith("cached:")


def test_tampered_cache_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({_key("explain", FACTS): "cached: 9999 lb gone."}))
    assert narrate("explain", FACTS, "fallback", cache_path=cache) == ("fallback", "template")


def test_corrupt_cache_file_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cache = tmp_path / "cache.json"
    cache.write_text("{not json")
    assert narrate("explain", FACTS, "fallback", cache_path=cache) == ("fallback", "template")


def test_live_error_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(narrate_mod, "_live_narrate",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("network down")))
    assert narrate("explain", FACTS, "fallback",
                   cache_path=tmp_path / "c.json") == ("fallback", "template")


def test_live_ungrounded_rejected_grounded_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cache = tmp_path / "cache.json"

    monkeypatch.setattr(narrate_mod, "_live_narrate", lambda *a: "we lose 555 lb")
    assert narrate("explain", FACTS, "fallback", cache_path=cache) == ("fallback", "template")
    assert not cache.exists()  # rejected text must not be cached

    monkeypatch.setattr(narrate_mod, "_live_narrate", lambda *a: "we quarantine 1900 lb")
    text, method = narrate("explain", FACTS, "fallback", cache_path=cache)
    assert (text, method) == ("we quarantine 1900 lb", "live-llm")
    # ... and the accepted text replays from cache with no key present.
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    text2, method2 = narrate("explain", FACTS, "fallback", cache_path=cache)
    assert (text2, method2) == ("we quarantine 1900 lb", "cached-llm")
