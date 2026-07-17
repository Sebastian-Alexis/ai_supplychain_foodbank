"""Official incident-source adapters for openFDA and USDA FSIS.

The source boundary is deliberately separate from the synthetic food-bank
network. These adapters normalize official API fields into the existing
RecallExtraction contract; they never manufacture inventory, purchase-order,
or supplier matches. A real incident can therefore produce zero exposure when
run against the demo network, which is the honest outcome.
"""

from __future__ import annotations

import codecs
import html
import json
import os
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPException
from json import JSONDecodeError
from typing import IO, Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .extraction import verify_provenance
from .schemas import RecallExtraction

OPENFDA_ENDPOINT = "https://api.fda.gov/food/enforcement.json"
FSIS_ENDPOINT = "https://www.fsis.usda.gov/fsis/api/recall/v/1"

OPENFDA_WARNING = (
    "openFDA says its API results are unvalidated. Verify the record against "
    "FDA and the issuing firm before any food-safety or operational decision."
)
FSIS_WARNING = (
    "Verify this API record against the current FSIS notice before any "
    "food-safety or operational decision."
)

_PROVIDER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/142.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
_FSIS_HEADERS = {
    **_PROVIDER_HEADERS,
    "Referer": "https://www.fsis.usda.gov/science-data/developer-resources/recall-api",
    "Origin": "https://www.fsis.usda.gov",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

Provider = Literal["openfda", "fsis"]


class IncidentSourceError(RuntimeError):
    """One authority feed could not be fetched or normalized."""


@dataclass(frozen=True)
class LiveIncident:
    """One official incident plus its deterministic source-field mapping."""

    key: str
    provider: Provider
    source_label: str
    event_id: str
    authority: Literal["FDA", "USDA"]
    title: str
    classification: str
    status: str
    published_at: str | None
    source_url: str
    raw_text: str
    extraction: RecallExtraction
    retrieved_at: str
    product_summary: str
    reason_summary: str
    distribution_summary: str
    trust_warning: str

    @property
    def selector_label(self) -> str:
        published = self.published_at or "date unavailable"
        return f"{published} · {self.classification} · {self.title}"



def fetch_live_incidents(provider: Provider, *, limit: int = 12,
                         timeout: float = 20.0) -> list[LiveIncident]:
    """Fetch recent incidents from one independent official source."""
    if provider == "openfda":
        return fetch_openfda_incidents(limit=limit, timeout=timeout)
    if provider == "fsis":
        return fetch_fsis_incidents(limit=limit, timeout=timeout)
    raise ValueError(f"unsupported incident provider: {provider}")



def fetch_openfda_incidents(*, limit: int = 12,
                            timeout: float = 15.0) -> list[LiveIncident]:
    """Fetch and group the latest ongoing openFDA food-enforcement records."""
    _validate_fetch_args(limit=limit, timeout=timeout)
    query = {
        "search": 'status:"Ongoing"',
        "sort": "report_date:desc",
        "limit": min(100, max(25, limit * 5)),
    }
    api_key = os.environ.get("FOODSHOCK_OPENFDA_API_KEY") or os.environ.get("OPENFDA_API_KEY")
    if api_key:
        query["api_key"] = api_key
    url = f"{OPENFDA_ENDPOINT}?{urlencode(query)}"
    payload = _request_json(url, headers=_PROVIDER_HEADERS, timeout=timeout,
                            provider="openFDA")
    try:
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise IncidentSourceError("openFDA returned an unexpected response shape")
        return _normalize_openfda(payload, limit=limit, retrieved_at=_now_iso())
    except IncidentSourceError:
        raise
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise IncidentSourceError(
            "openFDA returned malformed incident data"
        ) from exc



def fetch_fsis_incidents(*, limit: int = 12,
                         timeout: float = 30.0) -> list[LiveIncident]:
    """Fetch recent USDA FSIS recalls and public-health alerts.

    FSIS currently places browser-oriented edge protection in front of its
    documented JSON endpoint. Browser request headers are included, but this
    source remains best-effort and fails independently from openFDA.
    """
    _validate_fetch_args(limit=limit, timeout=timeout)
    request = Request(FSIS_ENDPOINT, headers=_FSIS_HEADERS)
    try:
        with urlopen(request, timeout=timeout) as response:
            records = _read_json_array_prefix(response, max_items=max(60, limit * 6))
    except (
        HTTPError,
        HTTPException,
        URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        JSONDecodeError,
    ) as exc:
        raise IncidentSourceError(f"USDA FSIS feed unavailable: {exc}") from exc
    if not records:
        raise IncidentSourceError("USDA FSIS returned no incident records")
    try:
        return _normalize_fsis(records, limit=limit, retrieved_at=_now_iso())
    except IncidentSourceError:
        raise
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise IncidentSourceError(
            "USDA FSIS returned malformed incident data"
        ) from exc



def _validate_fetch_args(*, limit: int, timeout: float) -> None:
    """Reject caller errors before entering a provider-isolation boundary."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if not 1 <= limit <= 25:
        raise ValueError("limit must be between 1 and 25")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError("timeout must be a number")
    if timeout <= 0 or not math.isfinite(timeout):
        raise ValueError("timeout must be finite and greater than zero")


def _request_json(url: str, *, headers: dict[str, str], timeout: float,
                  provider: str) -> Any:
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (
        HTTPError,
        HTTPException,
        URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        JSONDecodeError,
    ) as exc:
        raise IncidentSourceError(f"{provider} feed unavailable: {exc}") from exc



def _read_json_array_prefix(response: IO[bytes], *, max_items: int) -> list[dict]:
    """Decode only the leading objects from a large JSON array response."""
    decoder = json.JSONDecoder()
    utf8 = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    started = False
    expect_value = True
    allow_end = True
    items: list[dict] = []
    eof = False

    while len(items) < max_items:
        chunk = response.read(64 * 1024)
        if chunk:
            buffer += utf8.decode(chunk)
        else:
            buffer += utf8.decode(b"", final=True)
            eof = True

        pos = 0
        while True:
            while pos < len(buffer) and buffer[pos] in " \t\r\n":
                pos += 1
            if not started:
                if pos >= len(buffer):
                    break
                if buffer[pos] != "[":
                    raise JSONDecodeError("expected a JSON array", buffer, pos)
                started = True
                pos += 1
                continue

            while pos < len(buffer) and buffer[pos] in " \t\r\n":
                pos += 1
            if pos >= len(buffer):
                break

            if expect_value:
                if buffer[pos] == "]":
                    if allow_end:
                        return items
                    raise JSONDecodeError(
                        "trailing comma in JSON array",
                        buffer,
                        pos,
                    )
                if buffer[pos] == ",":
                    raise JSONDecodeError(
                        "unexpected comma in JSON array",
                        buffer,
                        pos,
                    )
                try:
                    value, end = decoder.raw_decode(buffer, pos)
                except JSONDecodeError:
                    break
                if not isinstance(value, dict):
                    raise JSONDecodeError("expected JSON objects", buffer, pos)
                items.append(value)
                pos = end
                if len(items) >= max_items:
                    return items
                expect_value = False
                continue

            if buffer[pos] == "]":
                return items
            if buffer[pos] != ",":
                raise JSONDecodeError(
                    "expected ',' or ']' in JSON array",
                    buffer,
                    pos,
                )
            pos += 1
            expect_value = True
            allow_end = False

        buffer = buffer[pos:]
        if eof:
            raise JSONDecodeError("truncated JSON array", buffer, 0)
    return items



def _normalize_openfda(payload: dict, *, limit: int,
                       retrieved_at: str) -> list[LiveIncident]:
    grouped: dict[str, list[dict]] = {}
    for record in payload.get("results", []):
        raw_id = _clean(record.get("event_id") or record.get("recall_number"))
        if raw_id:
            grouped.setdefault(raw_id, []).append(record)

    incidents: list[LiveIncident] = []
    dataset_updated = _clean(payload.get("meta", {}).get("last_updated")) or "unavailable"
    for raw_event_id, records in grouped.items():
        firms = _unique(_clean(r.get("recalling_firm")) for r in records)
        products = _unique(_clean(r.get("product_description")) for r in records)
        reasons = _unique(_clean(r.get("reason_for_recall")) for r in records)
        distributions = _unique(_clean(r.get("distribution_pattern")) for r in records)
        code_infos = _unique(_clean(r.get("code_info")) for r in records)
        upcs = _upcs_from_text(*products, *code_infos)
        lot_codes = _lot_codes_from_text(*code_infos)
        classifications = _unique(_clean(r.get("classification")) for r in records)
        statuses = _unique(_clean(r.get("status")) for r in records)
        reports = [_clean(r.get("report_date")) for r in records]
        published_at = _date8(max((d for d in reports if d), default=""))
        classification = ", ".join(classifications) or "Unclassified"
        status = ", ".join(statuses) or "Status unavailable"
        firm = firms[0] if firms else "Recalling firm unavailable"
        product_summary = _short(products[0] if products else "Product unavailable", 88)
        reason_summary = _short("; ".join(reasons) or "Reason unavailable", 130)
        distribution_summary = _short("; ".join(distributions) or "Not stated", 130)
        event_query = {"search": f'event_id:"{raw_event_id}"', "limit": 100}
        source_url = f"{OPENFDA_ENDPOINT}?{urlencode(event_query)}"

        lines = [
            "OFFICIAL SOURCE: openFDA Food Enforcement API",
            f"Retrieved at: {retrieved_at}",
            f"API dataset last updated: {dataset_updated}",
            f"Event ID: {raw_event_id}",
            f"Status: {status}",
            f"Classification: {classification}",
            f"Recalling firm: {', '.join(firms) or 'Not stated'}",
            f"Distribution pattern: {'; '.join(distributions) or 'Not stated'}",
        ]
        for index, record in enumerate(records, start=1):
            lines.extend([
                "",
                f"PRODUCT RECORD {index}",
                f"Recall number: {_clean(record.get('recall_number')) or 'Not stated'}",
                f"Product: {_clean(record.get('product_description')) or 'Not stated'}",
                f"Reason for recall: {_clean(record.get('reason_for_recall')) or 'Not stated'}",
                f"Code information: {_clean(record.get('code_info')) or 'Not stated'}",
                f"Recall initiation date: {_clean(record.get('recall_initiation_date')) or 'Not stated'}",
                f"Report date: {_clean(record.get('report_date')) or 'Not stated'}",
                f"Quantity: {_clean(record.get('product_quantity')) or 'Not stated'}",
            ])
        raw_text = "\n".join(lines)
        pathogen = _pathogen_from_text(*reasons)
        extraction = RecallExtraction(
            authority="FDA",
            products=products,
            supplier_names=firms,
            upcs=upcs,
            lot_codes=lot_codes,
            pathogen=pathogen,
            distribution_regions=distributions,
            excerpts={
                "products": products[0] if products else "",
                "supplier_names": firms[0] if firms else "",
                "upcs": _first_text_containing(
                    upcs[0], *products, *code_infos
                ) if upcs else "",
                "lot_codes": _first_text_containing(
                    lot_codes[0], *code_infos
                ) if lot_codes else "",
                "pathogen": pathogen or "",
                "distribution_regions": distributions[0] if distributions else "",
            },
            confidence=0.99,
        )
        extraction = _verified_source_mapping(extraction, raw_text)
        incidents.append(LiveIncident(
            key=f"openfda:{raw_event_id}",
            provider="openfda",
            source_label="openFDA food enforcement",
            event_id=f"FDA-{raw_event_id}",
            authority="FDA",
            title=f"{firm} — {product_summary}",
            classification=classification,
            status=status,
            published_at=published_at,
            source_url=source_url,
            raw_text=raw_text,
            extraction=extraction,
            retrieved_at=retrieved_at,
            product_summary=product_summary,
            reason_summary=reason_summary,
            distribution_summary=distribution_summary,
            trust_warning=OPENFDA_WARNING,
        ))
        if len(incidents) >= limit:
            break
    return incidents



def _normalize_fsis(records: list[dict], *, limit: int,
                    retrieved_at: str) -> list[LiveIncident]:
    records = [record for record in records if _clean(record.get("langcode")).lower() in ("", "english")]
    records.sort(
        key=lambda record: (
            _clean(record.get("field_active_notice")).lower() == "true",
            _clean(record.get("field_recall_date")),
        ),
        reverse=True,
    )

    incidents: list[LiveIncident] = []
    for record in records:
        recall_number = _clean(
            record.get("field_recall_number") or record.get("field_recall_number_export")
        )
        if not recall_number:
            continue
        title = _clean(record.get("field_title")) or f"FSIS incident {recall_number}"
        products = _unique(_clean(v) for v in _as_list(record.get("field_product_items")))
        establishments = _unique(_clean(v) for v in _as_list(record.get("field_establishment")))
        states = _unique(_clean(v) for v in _as_list(record.get("field_states")))
        reasons = _unique(_clean(v) for v in _as_list(record.get("field_recall_reason")))
        classification = _clean(
            record.get("field_recall_classification") or record.get("field_risk_level")
        ) or "Unclassified"
        is_active = _clean(record.get("field_active_notice")).lower() == "true"
        status = "Active" if is_active else "Recent / inactive"
        published_at = _clean(record.get("field_recall_date")) or None
        source_url = _clean(record.get("field_recall_url")) or FSIS_ENDPOINT
        if source_url.startswith("http://"):
            source_url = "https://" + source_url.removeprefix("http://")
        summary = _strip_html(_clean(record.get("field_summary")))
        product_summary = _short(products[0] if products else title, 88)
        reason_summary = _short("; ".join(reasons) or "Reason unavailable", 130)
        distribution_summary = _short(", ".join(states) or "Not stated", 130)

        raw_text = "\n".join([
            "OFFICIAL SOURCE: USDA FSIS Recall API",
            f"Retrieved at: {retrieved_at}",
            f"Recall number: {recall_number}",
            f"Published: {published_at or 'Not stated'}",
            f"Status: {status}",
            f"Classification: {classification}",
            f"Title: {title}",
            f"Establishment: {', '.join(establishments) or 'Not stated'}",
            f"Products: {'; '.join(products) or 'Not stated'}",
            f"Recall reason: {'; '.join(reasons) or 'Not stated'}",
            f"Distribution states: {', '.join(states) or 'Not stated'}",
            f"Summary: {summary or 'Not stated'}",
        ])
        pathogen = _pathogen_from_text(*reasons, summary)
        extraction = RecallExtraction(
            authority="USDA",
            products=products,
            supplier_names=establishments,
            facility_names=establishments,
            distribution_regions=states,
            pathogen=pathogen,
            excerpts={
                "products": products[0] if products else "",
                "supplier_names": establishments[0] if establishments else "",
                "facility_names": establishments[0] if establishments else "",
                "distribution_regions": ", ".join(states) if states else "",
                "pathogen": pathogen or "",
            },
            confidence=0.99,
        )
        extraction = _verified_source_mapping(extraction, raw_text)
        incidents.append(LiveIncident(
            key=f"fsis:{recall_number}",
            provider="fsis",
            source_label="USDA FSIS recalls and alerts",
            event_id=f"USDA-{recall_number}",
            authority="USDA",
            title=title,
            classification=classification,
            status=status,
            published_at=published_at,
            source_url=source_url,
            raw_text=raw_text,
            extraction=extraction,
            retrieved_at=retrieved_at,
            product_summary=product_summary,
            reason_summary=reason_summary,
            distribution_summary=distribution_summary,
            trust_warning=FSIS_WARNING,
        ))
        if len(incidents) >= limit:
            break
    return incidents



def _verified_source_mapping(extraction: RecallExtraction,
                             raw_text: str) -> RecallExtraction:
    verified, dropped = verify_provenance(extraction, raw_text)
    if dropped:
        raise IncidentSourceError(
            "source-field mapping failed provenance verification for: "
            + ", ".join(sorted(dropped))
        )
    return verified



_OPENFDA_RECORD_FIELDS = {
    "Recall number",
    "Product",
    "Reason for recall",
    "Code information",
    "Recall initiation date",
    "Report date",
    "Quantity",
}


def parse_source_snapshot(
    raw_text: str,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Parse the retained normalized snapshot for transparent UI rendering."""
    overview: dict[str, str] = {}
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        record_match = re.fullmatch(r"PRODUCT RECORD (\d+)", line)
        if record_match:
            current = {"Record": record_match.group(1)}
            records.append(current)
            continue
        if ": " not in line:
            continue
        field, value = line.split(": ", 1)
        if current is not None and field in _OPENFDA_RECORD_FIELDS:
            current[field] = value
        else:
            overview[field] = value
    return overview, records


def _upcs_from_text(*values: str) -> list[str]:
    out: list[str] = []
    for value in values:
        out.extend(re.findall(
            r"\bUPC(?:\s+(?:Code|No\.?))?\s*[:#-]?\s*(\d{8,14})\b",
            value,
            flags=re.IGNORECASE,
        ))
    return _unique(out)


def _lot_codes_from_text(*values: str) -> list[str]:
    """Extract values only from explicit singular or plural lot clauses."""
    out: list[str] = []

    def add_candidate(token: str) -> None:
        token = token.strip(".,;:()[]{}")
        if "/" in token or not any(char.isdigit() for char in token):
            return
        out.append(token)

    for value in values:
        for header in re.finditer(
            r"\blot\s+codes?\s*[:#-]?\s*",
            value,
            flags=re.IGNORECASE,
        ):
            clause = value[header.end():]
            clause = re.sub(
                r"\b(?:exp(?:iration)?\s+date|best\s+(?:if\s+)?used\s+by|"
                r"use\s+by)\s*[:#-]?\s*(?:[A-Za-z]{3}\s+)?"
                r"\d{1,4}(?:[/-]\d{1,4}){0,2}",
                " ",
                clause,
                flags=re.IGNORECASE,
            )
            clause = re.split(
                r"\b(?:UPC|GTIN|product|quantity)\b\s*"
                r"(?:code|number|no\.?)?\s*[:#-]?",
                clause,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
            for token in re.findall(
                r"(?<![A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9._/-]*(?![A-Za-z0-9])",
                clause,
            ):
                add_candidate(token)

        for match in re.finditer(
            r"\blot(?!\s+codes\b)(?:\s+(?:code|number|no\.?))?"
            r"\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9._-]{0,31})\b",
            value,
            flags=re.IGNORECASE,
        ):
            add_candidate(match.group(1))
    return _unique(out)


def _first_text_containing(needle: str, *values: str) -> str:
    return next((value for value in values if needle in value), "")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]



def _clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", html.unescape(str(value))).strip()



def _unique(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            out.append(value)
    return out



def _pathogen_from_text(*values: str) -> str | None:
    """Return only explicit biological pathogen names, verbatim from source text."""
    text = " ".join(value for value in values if value)
    patterns = (
        r"\bSalmonella\b",
        r"\bListeria(?:\s+monocytogenes)?\b",
        r"\b(?:E(?:scherichia)?\.?\s*coli)(?:\s+O\d+(?::H\d+)?)?\b",
        r"\bClostridium(?:\s+botulinum)?\b",
        r"\bCampylobacter\b",
        r"\bNorovirus\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def _short(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return value[: width - 1].rstrip() + "…"



def _date8(value: str) -> str | None:
    if not re.fullmatch(r"\d{8}", value):
        return None
    return f"{value[:4]}-{value[4:6]}-{value[6:]}"



def _strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return _clean(value)



def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
