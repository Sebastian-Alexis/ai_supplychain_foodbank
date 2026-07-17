"""Notice extraction (PLAN.md §9 `extract_notice` tool).

Cache-first: the demo notice replays with zero network from
data/extraction_cache.json (a cached LLM extraction, key = sha256 of the
normalized notice text). If a notice is not cached and ANTHROPIC_API_KEY is
set, a live structured-output call to Claude produces the extraction and is
written through to the cache.

Provenance rule (PLAN.md §19): schema validation alone is not evidence.
Every populated field MUST carry an excerpt that is a verbatim substring of
the notice, and extracted identifiers/names must themselves appear in the
notice. Fields that fail verification are CLEARED (routed to human review as
gaps), never silently trusted — a valid Pydantic object with an invented
citation must not reach the judge-facing UI.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import unicodedata
from pathlib import Path

from .db import DATA_DIR
from .schemas import RecallExtraction

STATIC_CACHE_PATH = DATA_DIR / "extraction_cache.json"
RUNTIME_CACHE_PATH = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    / "farms-for-food"
    / "claude-extractions.json"
)
CACHE_PATH = STATIC_CACHE_PATH
LIVE_MODEL = "claude-opus-4-8"
_CACHE_LOCK = threading.Lock()

# Fields whose *values* must appear verbatim (case-insensitive) in the notice.
_VALUE_CHECKED_LIST_FIELDS = (
    "products", "supplier_names", "facility_names",
    "lot_codes", "upcs", "distribution_regions",
)
# Fields whose values are normalized (dates) or paraphrased (action), so only
# their supporting excerpt is verified.
_EXCERPT_ONLY_FIELDS = (
    "production_date_start", "production_date_end", "pathogen", "action_required",
)

_LIVE_PROMPT = """Extract the recall entities from the food-safety notice below.

Rules:
- Copy values exactly as written in the notice; normalize dates to YYYY-MM-DD.
- For every populated field, put the exact supporting quote (a verbatim
  substring of the notice) into `excerpts` under the field's name.
- If the notice does not state a field, leave it null or empty. Never guess.
- `authority` is the agency channel the notice was posted through.

NOTICE:
{notice}
"""


class ExtractionUnavailable(RuntimeError):
    """Raised when a notice is not cached and no live LLM path is available."""


def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n"))


def _legacy_key(raw_text: str) -> str:
    """Read the committed pre-contract cache without invalidating demo replays."""
    return hashlib.sha256(raw_text.strip().encode()).hexdigest()


def _key(raw_text: str, *, cache_identity: str | None = None) -> str:
    """Hash the complete effective extraction contract and stable input."""
    contract = {
        "namespace": "recall-extraction-v2",
        "model": LIVE_MODEL,
        "prompt": _LIVE_PROMPT,
        "schema": RecallExtraction.model_json_schema(),
        "input": cache_identity or _norm(raw_text).strip(),
        "max_tokens": 16000,
        "thinking": {"type": "adaptive"},
        "effort": "low",
    }
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _load_cache(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _cache_layers(cache_path: Path | None) -> tuple[Path, ...]:
    if cache_path is not None:
        return (cache_path,)
    return (RUNTIME_CACHE_PATH, STATIC_CACHE_PATH)


def _store_cache(path: Path, key: str, extraction: RecallExtraction) -> None:
    """Merge and atomically persist one schema-valid, untrusted response."""
    with _CACHE_LOCK:
        try:
            cache = _load_cache(path)
        except (OSError, json.JSONDecodeError):
            cache = {}
        cache[key] = json.loads(extraction.model_dump_json())
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(
            f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temp_path.write_text(json.dumps(cache, indent=2))
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)


def verify_provenance(extraction: RecallExtraction, raw_text: str) -> tuple[RecallExtraction, list[str]]:
    """Clear any populated field whose evidence is not verbatim in the notice.

    Returns (cleaned extraction, dropped field names). A dropped field means
    the model produced a value the notice does not literally support.
    """
    text = _norm(raw_text)
    text_ci = text.lower()
    data = extraction.model_dump()
    dropped: list[str] = []

    def excerpt_ok(field: str) -> bool:
        exc = extraction.excerpts.get(field, "")
        return bool(exc) and _norm(exc) in text

    for field in _VALUE_CHECKED_LIST_FIELDS:
        values = data.get(field) or []
        if not values:
            continue
        values_ok = all(_norm(v).lower() in text_ci for v in values)
        if not (values_ok and excerpt_ok(field)):
            data[field] = []
            dropped.append(field)

    for field in _EXCERPT_ONLY_FIELDS:
        if data.get(field) is None:
            continue
        if not excerpt_ok(field):
            data[field] = None
            dropped.append(field)

    if dropped:
        data["excerpts"] = {k: v for k, v in data["excerpts"].items() if k not in dropped}
        return RecallExtraction.model_validate(data), dropped
    return extraction, dropped


def extract_notice(
    raw_text: str,
    *,
    allow_llm: bool = True,
    cache_path: Path | None = None,
    cache_identity: str | None = None,
) -> tuple[RecallExtraction, str, list[str]]:
    """Return (extraction, method, dropped_fields).

    Responses are cached by the complete request contract. `cache_identity`
    may remove volatile transport metadata, but provenance is always checked
    against the full current source snapshot before a cached value is used.
    """
    contract_key = _key(raw_text, cache_identity=cache_identity)
    legacy_key = _legacy_key(raw_text)
    for path in _cache_layers(cache_path):
        try:
            cache = _load_cache(path)
        except (OSError, json.JSONDecodeError):
            continue
        lookup_keys = (
            (contract_key, legacy_key)
            if path == STATIC_CACHE_PATH
            else (contract_key,)
        )
        for key in lookup_keys:
            if key not in cache:
                continue
            try:
                extraction = RecallExtraction.model_validate(cache[key])
            except (TypeError, ValueError):
                continue
            extraction, dropped = verify_provenance(extraction, raw_text)
            return extraction, "cached-llm", dropped

    if allow_llm and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            raw_extraction = _live_extract(raw_text)
        except Exception as exc:
            raise ExtractionUnavailable(
                "Claude extraction failed; deterministic source mapping remains available."
            ) from exc
        extraction, dropped = verify_provenance(raw_extraction, raw_text)
        write_path = cache_path or RUNTIME_CACHE_PATH
        try:
            _store_cache(write_path, contract_key, raw_extraction)
        except OSError:
            pass
        return extraction, "live-llm", dropped

    raise ExtractionUnavailable(
        "Notice is not cached and live Claude extraction is not configured."
    )


_MERGE_LIST_FIELDS = (
    "products",
    "supplier_names",
    "facility_names",
    "lot_codes",
    "upcs",
    "distribution_regions",
)
_MERGE_SCALAR_FIELDS = (
    "production_date_start",
    "production_date_end",
    "pathogen",
    "action_required",
)


def _stable_source_identity(raw_text: str) -> str:
    """Exclude retrieval time while retaining every authority-provided field."""
    return "\n".join(
        line for line in _norm(raw_text).splitlines()
        if not line.startswith("Retrieved at:")
    ).strip()


def enrich_source_extraction(
    raw_text: str,
    base: RecallExtraction,
    *,
    allow_llm: bool,
    cache_path: Path | None = None,
) -> tuple[RecallExtraction, str | None, list[str]]:
    """Fill missing API-mapped fields with a grounded, cached Claude result.

    Deterministic source fields always win. Model output can only fill empty
    operational-entity fields, and every addition must survive the same
    verbatim-provenance verifier. API/model/cache failures return the verified
    deterministic base rather than breaking incident analysis.
    """
    verified_base, base_dropped = verify_provenance(base, raw_text)
    try:
        candidate, method, candidate_dropped = extract_notice(
            raw_text,
            allow_llm=allow_llm,
            cache_path=cache_path,
            cache_identity=_stable_source_identity(raw_text),
        )
    except ExtractionUnavailable:
        return verified_base, None, base_dropped

    data = verified_base.model_dump()
    candidate_data = candidate.model_dump()
    filled: list[str] = []
    for field in _MERGE_LIST_FIELDS:
        if not data[field] and candidate_data[field]:
            data[field] = candidate_data[field]
            filled.append(field)
    for field in _MERGE_SCALAR_FIELDS:
        if data[field] is None and candidate_data[field] is not None:
            data[field] = candidate_data[field]
            filled.append(field)

    excerpts = dict(verified_base.excerpts)
    for field in filled:
        excerpt = candidate.excerpts.get(field)
        if excerpt:
            excerpts[field] = excerpt
    data["excerpts"] = excerpts
    data["authority"] = verified_base.authority
    data["confidence"] = min(verified_base.confidence, candidate.confidence)
    merged = RecallExtraction.model_validate(data)
    merged, merge_dropped = verify_provenance(merged, raw_text)
    contributed = any(
        getattr(merged, field) not in (None, [], "") for field in filled
    )
    relevant_candidate_dropped = [
        field for field in candidate_dropped
        if getattr(verified_base, field, None) in (None, [], "")
    ]
    dropped = list(
        dict.fromkeys(base_dropped + relevant_candidate_dropped + merge_dropped)
    )
    return merged, method if contributed else None, dropped


def _create_live_response(raw_text: str):
    """Call the configured live extractor and retain response metadata."""
    import anthropic  # deferred: offline demo path must not require the package at import time

    client = anthropic.Anthropic()
    return client.messages.parse(
        model=LIVE_MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": _LIVE_PROMPT.format(notice=raw_text)}],
        output_format=RecallExtraction,
    )


def _live_extract(raw_text: str) -> RecallExtraction:
    response = _create_live_response(raw_text)
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude refused the grounded extraction request.")
    if response.parsed_output is None:
        raise RuntimeError("Claude returned no structured extraction.")
    return RecallExtraction.model_validate(response.parsed_output)
