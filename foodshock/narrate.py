"""LLM narration layer (PLAN.md §9 division of labor: the LLM writes
explanations and drafted communications; deterministic code computes facts).

Same offline discipline as extraction.py: live responses are cached by the
complete model request contract so the demo arc replays with zero network.
When no cache entry and no API key exist -- or the LLM output fails the
numeric-grounding guard -- the deterministic template ships instead, labeled
method='template'. Template output is NEVER presented as LLM work.

Grounding guard: every number in the generated text must appear in the fact
bundle (exact rendering or numeric equality). Fail-closed: reject -> template.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

from .db import DATA_DIR

STATIC_CACHE_PATH = DATA_DIR / "narration_cache.json"
RUNTIME_CACHE_PATH = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    / "farms-for-food"
    / "claude-narrations.json"
)
CACHE_PATH = STATIC_CACHE_PATH
LIVE_MODEL = "claude-opus-4-8"
MAX_TOKENS = 1000
_CACHE_LOCK = threading.Lock()

_PROMPT = """You are the narration layer of Farms for Food, a food-bank recall-response
agent. Write {what} for a food-bank operations coordinator.

Rules:
- Use ONLY the facts in the JSON below. Never invent numbers, names, lots,
  suppliers, or identifiers.
- Use every number EXACTLY as written in the facts. Do not compute, round,
  convert units, or turn ratios into percentages.
- Confirmed facts vs. scenario assumptions are labeled in the facts; keep the
  distinction explicit.
- Plain prose, no markdown headers. {length}

Facts:
{facts}
"""

_WHAT = {
    "explain": ("a briefing that explains the recall's operational impact and the "
                "recommended recovery plan versus doing nothing", "5-8 sentences."),
    "comm": ("the body of an operational message; the facts include the audience "
             "and a template draft covering exactly the points to make", "3-6 sentences."),
}


def _canonical(facts: dict) -> str:
    return json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)


def _legacy_key(kind: str, facts: dict) -> str:
    """Read the committed pre-contract cache without invalidating demo replays."""
    return hashlib.sha256(f"{kind}\n{_canonical(facts)}".encode()).hexdigest()


def _key(kind: str, facts: dict) -> str:
    """Hash the complete effective narration request."""
    base_kind = kind.split(":", 1)[0]
    contract = {
        "namespace": "recall-narration-v1",
        "model": LIVE_MODEL,
        "prompt": _PROMPT,
        "what": _WHAT.get(base_kind),
        "kind": kind,
        "facts": facts,
        "max_tokens": MAX_TOKENS,
    }
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _load_cache(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _cache_layers(cache_path: Path | None) -> tuple[Path, ...]:
    if cache_path is not None:
        return (cache_path,)
    return (RUNTIME_CACHE_PATH, STATIC_CACHE_PATH)


def _store_cache(path: Path, key: str, text: str) -> None:
    """Merge and atomically persist one grounded response."""
    with _CACHE_LOCK:
        try:
            cache = _load_cache(path)
        except (OSError, json.JSONDecodeError):
            cache = {}
        cache[key] = text
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(
            f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temp_path.write_text(json.dumps(cache, indent=2))
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)


def _allowed_numbers(facts: dict) -> set[str]:
    """Every rendering of every number reachable in the fact bundle."""
    out: set[str] = set()

    def add_num(v: float) -> None:
        if v == int(v):
            out.add(str(int(v)))
        out.add(f"{v:g}")
        out.add(f"{v:.1f}")
        out.add(f"{v:.2f}")

    def walk(node) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            add_num(float(node))
        elif isinstance(node, str):
            for m in re.findall(r"\d[\d.,]*", node):
                out.add(m.rstrip(".,").replace(",", ""))
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(k)
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    walk(facts)
    return out


def ungrounded_numbers(text: str, facts: dict) -> list[str]:
    """Number tokens in `text` that do not appear in `facts`. Empty = grounded."""
    allowed = _allowed_numbers(facts)
    allowed_f: set[float] = set()
    for a in allowed:
        try:
            allowed_f.add(float(a))
        except ValueError:
            pass
    bad = []
    for m in re.findall(r"\d[\d.,]*", text):
        tok = m.rstrip(".,").replace(",", "")
        if tok in allowed:
            continue
        try:
            if float(tok) in allowed_f:
                continue
        except ValueError:
            pass
        bad.append(tok)
    return bad


def narrate(kind: str, facts: dict, fallback: str, *, allow_llm: bool = True,
            cache_path: Path | None = None) -> tuple[str, str]:
    """Return (text, method): 'cached-llm' | 'live-llm' | 'template'.

    `fallback` is the deterministic template text (always shippable). LLM text
    that fails the grounding guard is discarded in favor of the template.
    """
    base_kind = kind.split(":", 1)[0]
    if base_kind not in _WHAT:
        raise ValueError(f"unknown narration kind: {kind}")
    key = _key(kind, facts)
    legacy_key = _legacy_key(kind, facts)
    for path in _cache_layers(cache_path):
        try:
            cache = _load_cache(path)
        except (OSError, json.JSONDecodeError):
            continue
        lookup_keys = (
            (key, legacy_key)
            if path == STATIC_CACHE_PATH
            else (key,)
        )
        for cache_key in lookup_keys:
            text = cache.get(cache_key)
            if isinstance(text, str) and not ungrounded_numbers(text, facts):
                return text, "cached-llm"
    if allow_llm and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            text = _live_narrate(base_kind, facts)
        except Exception:
            text = None  # fail closed: network/auth/SDK errors ship the template
        if text and not ungrounded_numbers(text, facts):
            try:
                _store_cache(cache_path or RUNTIME_CACHE_PATH, key, text)
            except OSError:
                pass  # cache write failure must not lose the narration
            return text, "live-llm"
    return fallback, "template"


def _live_narrate(base_kind: str, facts: dict) -> str:
    import anthropic  # deferred: offline demo path must not require the package

    what, length = _WHAT[base_kind]
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=LIVE_MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": _PROMPT.format(
            what=what, length=length, facts=json.dumps(facts, indent=2, default=str))}],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()
