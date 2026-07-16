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
import unicodedata
from pathlib import Path

from .db import DATA_DIR
from .schemas import RecallExtraction

CACHE_PATH = DATA_DIR / "extraction_cache.json"
LIVE_MODEL = "claude-opus-4-8"

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


def _key(raw_text: str) -> str:
    return hashlib.sha256(raw_text.strip().encode()).hexdigest()


def _load_cache(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


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


def extract_notice(raw_text: str, *, allow_llm: bool = True,
                   cache_path: Path | None = None) -> tuple[RecallExtraction, str, list[str]]:
    """Return (extraction, method, dropped_fields).

    method: 'cached-llm' or 'live-llm'. dropped_fields lists extracted fields
    cleared by provenance verification (each becomes a human-review gap).
    """
    path = cache_path or CACHE_PATH
    cache = _load_cache(path)
    key = _key(raw_text)
    if key in cache:
        extraction = RecallExtraction.model_validate(cache[key])
        extraction, dropped = verify_provenance(extraction, raw_text)
        return extraction, "cached-llm", dropped

    if allow_llm and os.environ.get("ANTHROPIC_API_KEY"):
        extraction = _live_extract(raw_text)
        extraction, dropped = verify_provenance(extraction, raw_text)
        cache[key] = json.loads(extraction.model_dump_json())  # cache only verified content
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2))
        return extraction, "live-llm", dropped

    raise ExtractionUnavailable(
        "Notice is not in the extraction cache and no ANTHROPIC_API_KEY is set. "
        "The demo notice replays offline; live extraction of new notices needs a key."
    )


def _create_live_response(raw_text: str):
    """Call the configured live extractor and retain response metadata."""
    import anthropic  # deferred: offline demo path must not require the package at import time

    client = anthropic.Anthropic()
    return client.messages.parse(
        model=LIVE_MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": _LIVE_PROMPT.format(notice=raw_text)}],
        output_format=RecallExtraction,
    )


def _live_extract(raw_text: str) -> RecallExtraction:
    return _create_live_response(raw_text).parsed_output
