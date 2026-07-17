"""RecallResponseAgent (PLAN.md §9): one incident, one explicit loop --
OBSERVE -> INVESTIGATE -> EXPLAIN -> request approval.

Every tool call, tool result, human-review gap, and narration is logged to
agent_transcript; the live transcript is the demo's "AI agents" evidence
(§14). Division of labor: the LLM handles unstructured language (extraction
via extraction.py, explanations via narrate.py -- both cached for offline
replay); deterministic engine code does matching, propagation, projection,
and optimization. The agent NEVER approves anything: the run ends awaiting
the operator (engine.approve_plan is the human's action, not ours).
"""

from __future__ import annotations
from collections.abc import Callable

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

import pandas as pd

from .db import insert, now_iso, rows
from .engine import (before_after, build_plans, days_of_supply,
                     incident_focus, project_supply, propagate, resolve_event)
from .extraction import (ExtractionUnavailable, enrich_source_extraction,
                         extract_notice, verify_provenance)
from .narrate import narrate
from .schemas import SCENARIOS, RecallExtraction

_RESULT_ROW_CAP = 20  # transcript stores at most this many rows per tool result


@dataclass
class AgentRunResult:
    run_id: str
    event_id: str
    extraction: RecallExtraction
    extraction_method: str
    dropped_fields: list[str]
    match_counts: dict[str, int]
    propagation: dict
    days_of_supply: dict[str, dict[str, float | None]]
    baseline_id: str
    recommended_id: str
    before_after: dict
    narration: str
    narration_method: str
    runtime_s: float
    gaps: list[str] = field(default_factory=list)
    has_exposure: bool = True


class RecallResponseAgent:
    """Drives one recall incident end to end and logs the transcript."""

    def __init__(self, conn: sqlite3.Connection, *, allow_llm: bool = True,
                 actor: str = "recall-response-agent",
                 on_event: Callable[[dict], None] | None = None):
        self.conn = conn
        self.allow_llm = allow_llm
        self.actor = actor
        self.on_event = on_event
        self._seq = 0
        self.run_id = ""
        self.gaps: list[str] = []
        self._observer_s = 0.0

    # ------------------------------------------------------------ logging

    def _log(self, phase: str, kind: str, name: str | None, content: dict) -> None:
        self._seq += 1
        insert(self.conn, "agent_transcript", {
            "run_id": self.run_id, "seq": self._seq, "at": now_iso(),
            "phase": phase, "kind": kind, "name": name,
            "content_json": json.dumps(content, default=str)})
        self.conn.commit()
        if self.on_event is not None:
            observer_started = time.perf_counter()
            self.on_event({
                "run_id": self.run_id,
                "seq": self._seq,
                "phase": phase,
                "kind": kind,
                "name": name,
            })
            self._observer_s += time.perf_counter() - observer_started

    def _tool(self, phase: str, name: str, args: dict, fn):
        """Log call, execute, log compacted result, return the full result."""
        self._log(phase, "tool_call", name, {"args": args})
        out = fn()
        self._log(phase, "tool_result", name, {"result": _compact(out)})
        return out

    def _gap(self, phase: str, question: str) -> None:
        """flag_gap tool (PLAN.md §9): route a question to human review."""
        self.gaps.append(question)
        self._log(phase, "gap", "flag_gap", {"question": question})

    def _narrate(self, phase: str, kind: str, facts: dict, fallback: str) -> tuple[str, str]:
        text, method = narrate(kind, facts, fallback, allow_llm=self.allow_llm)
        self._log(phase, "narration", kind, {"text": text, "method": method})
        return text, method

    # ---------------------------------------------------------------- run

    def run(self, raw_text: str, *, source_url: str | None = None,
            event_id: str | None = None, published_at: str | None = None,
            provided_extraction: RecallExtraction | None = None,
            provided_extraction_method: str = "source-api") -> AgentRunResult:
        t0 = time.perf_counter()
        self.gaps: list[str] = []
        self._observer_s = 0.0
        event_id = event_id or f"EV-{uuid.uuid4().hex[:8].upper()}"
        self.run_id = f"RUN-{event_id}"

        # ------------------------------------------------------- OBSERVE
        self._log("observe", "narration", "notice_received",
                  {"text": f"Recall notice received ({len(raw_text)} chars, "
                           f"source: {source_url or 'manual paste'}). Beginning investigation.",
                   "method": "template"})
        self._tool("observe", "ingest_notice",
                   {"event_id": event_id, "chars": len(raw_text)},
                   lambda: self._ingest(event_id, raw_text, source_url, published_at))

        # Structured authority fields remain the deterministic base. When the
        # live language layer is enabled, Claude may fill only missing entity
        # fields; the merge is cached and every addition must survive the same
        # source-provenance verifier. Model failure falls back to the API map.
        try:
            if provided_extraction is None:
                extraction, ext_method, dropped = self._tool(
                    "investigate", "extract_notice", {"event_id": event_id},
                    lambda: extract_notice(raw_text, allow_llm=self.allow_llm))
            else:
                def source_extract():
                    verified, dropped_fields = verify_provenance(
                        provided_extraction, raw_text
                    )
                    method = provided_extraction_method
                    enriched, enrichment_method, enrichment_dropped = (
                        enrich_source_extraction(
                            raw_text,
                            verified,
                            allow_llm=self.allow_llm,
                        )
                    )
                    verified = enriched
                    dropped_fields = list(dict.fromkeys(
                        dropped_fields + enrichment_dropped
                    ))
                    if enrichment_method:
                        method = f"{provided_extraction_method}+{enrichment_method}"
                    return verified, method, dropped_fields

                extraction, ext_method, dropped = self._tool(
                    "investigate", "extract_notice",
                    {
                        "event_id": event_id,
                        "method": provided_extraction_method,
                        "claude_enrichment": self.allow_llm,
                    },
                    source_extract,
                )
        except ExtractionUnavailable as exc:
            self._gap("investigate", f"Extraction unavailable: {exc} "
                                     "Incident needs manual entity entry.")
            raise
        self.conn.execute(
            "UPDATE recall_events SET extraction_json=?, extraction_confidence=?, "
            "extraction_method=?, authority=? WHERE event_id=?",
            (extraction.model_dump_json(), extraction.confidence, ext_method,
             extraction.authority, event_id))
        self.conn.commit()
        for f in dropped:
            self._gap("investigate",
                      f"Extracted field '{f}' lacked verbatim support in the notice "
                      f"and was cleared; confirm manually against the source.")
        if not extraction.lot_codes and not extraction.upcs:
            self._gap("investigate", "Notice lists no lot codes or UPCs: no tier-1 "
                                     "identifier matching; supplier/product/date evidence only.")
        if not (extraction.production_date_start and extraction.production_date_end):
            self._gap("investigate", "Notice states no production window: date overlap "
                                     "cannot exclude any lot.")

        # Investigation queries: what could this notice touch? (The
        # authoritative matching is resolve_event below; these show the
        # evidence trail judges can follow live.)
        for term in extraction.products:
            self._tool("investigate", "query_inventory", {"product": term},
                       lambda t=term: rows(self.conn, """
                           SELECT l.lot_id, l.quantity_lb, l.status, p.name product_name
                           FROM inventory_lots l JOIN products p USING (product_id)
                           WHERE p.name LIKE '%' || ? || '%' COLLATE NOCASE""", (_last_word(t),)))
        for name in extraction.supplier_names:
            self._tool("investigate", "query_purchase_orders", {"supplier": name},
                       lambda n=name: rows(self.conn, """
                           SELECT po.po_id, po.quantity_lb, po.expected_delivery, po.status,
                                  s.name supplier_name
                           FROM purchase_orders po JOIN suppliers s USING (supplier_id)
                           WHERE s.name LIKE '%' || ? || '%' COLLATE NOCASE""",
                                    (_head_word(n),)))

        matches = self._tool("investigate", "resolve_entity",
                             {"event_id": event_id, "tiers": "1-4"},
                             lambda: resolve_event(self.conn, event_id))
        match_counts: dict[str, int] = {}
        for m in matches:
            match_counts[m["state"]] = match_counts.get(m["state"], 0) + 1
        for m in matches:
            if m["state"] in ("probable", "possible", "unknown"):
                self._gap("investigate",
                          f"{m['target_type']} {m['target_id']}: {m['state']} match "
                          f"(tier {m['tier']}) needs human review before any release.")
        has_exposure = any(m["state"] != "not_matched" for m in matches)
        focus = incident_focus(self.conn, event_id)

        propagation = self._tool("investigate", "propagate", {"event_id": event_id},
                                 lambda: propagate(self.conn, event_id))

        # ------------------------------------------------------- EXPLAIN
        dos: dict[str, dict[str, float | None]] = {}
        for scenario in SCENARIOS:
            proj = self._tool("explain", "project_supply", {"scenario": scenario, "days": 7},
                              lambda s=scenario: project_supply(self.conn, s))
            dos[scenario] = days_of_supply(proj)

        baseline_id, rec_id = self._tool(
            "explain", "optimize_recovery",
            {"objective": "weighted LP, conservative pool",
             "recovery_enabled": has_exposure},
            lambda: build_plans(self.conn, recovery_enabled=has_exposure),
        )
        runtime_s = round(max(0.0, time.perf_counter() - t0 - self._observer_s), 2)
        ba = self._tool("explain", "before_after",
                        {"baseline": baseline_id, "recommended": rec_id,
                         "focus_category": focus["category"]},
                        lambda: before_after(self.conn, baseline_id, rec_id,
                                             focus_category=focus["category"],
                                             runtime_s=runtime_s))

        facts = {
            "event_id": event_id, "authority": extraction.authority,
            "has_exposure": has_exposure,
            "hazard": extraction.pathogen,
            "recalled_products": extraction.products,
            "match_counts (lots and purchase orders, by state)": match_counts,
            "confirmed_quarantine_proposed_lb": propagation["quarantine_proposed_lb"],
            "unconfirmed_needing_review_lb": propagation["review_lb"],
            "purchase_orders_flagged_at_risk": propagation["pos_at_risk"],
            "infeasible_distribution_lines (conservative pool, all active recalls)":
                propagation["infeasible_lines"],
            "focus_supply": {
                "category": focus["category"],
                "products": focus["products"],
                "conservative_assumption_before_plan":
                    ba["focus_dos_conservative_before"],
                "conservative_assumption_after_plan":
                    ba["focus_dos_conservative_after"],
                "optimistic_assumption":
                    dos["optimistic"].get(focus["category"]),
            },
            "do_nothing_baseline": ba["baseline"],
            "recommended_plan": ba["recommended"],
            "note": "scenario figures are operator-selected assumptions, not statistical bounds",
        }
        narration, narr_method = self._narrate("explain", "explain", facts,
                                               _explain_template(facts))

        # ------------------------------------------------------- APPROVE
        if has_exposure:
            approval_text = (
                f"Recommended plan {rec_id} and do-nothing baseline "
                f"{baseline_id} are ready in the approval queue with "
                f"{len(self.gaps)} open review gap(s). No quarantine, purchase, "
                "or allocation executes without operator approval."
            )
        else:
            approval_text = (
                "No evidence-linked exposure was found in the current operational "
                "records, so no recall-triggered quarantine or recovery action is "
                "recommended. The unchanged network baseline remains visible for "
                "context; source verification and human review still apply."
            )
        self._log("approve", "narration", "request_approval",
                  {"text": approval_text, "method": "template"})

        return AgentRunResult(
            run_id=self.run_id, event_id=event_id, extraction=extraction,
            extraction_method=ext_method, dropped_fields=dropped,
            match_counts=match_counts, propagation=propagation, days_of_supply=dos,
            baseline_id=baseline_id, recommended_id=rec_id, before_after=ba,
            narration=narration, narration_method=narr_method,
            runtime_s=runtime_s, gaps=self.gaps, has_exposure=has_exposure)

    def _ingest(self, event_id: str, raw_text: str, source_url: str | None,
                published_at: str | None) -> dict:
        insert(self.conn, "recall_events", {
            "event_id": event_id, "authority": "UNKNOWN", "status": "active",
            "published_at": published_at, "ingested_at": now_iso(),
            "source_url": source_url, "raw_text": raw_text})
        self.conn.commit()
        return {"event_id": event_id, "status": "ingested",
                "published_at": published_at}


def _last_word(term: str) -> str:
    """Loose search stem for investigation queries ('fresh yellow onions' -> 'onion')."""
    w = term.strip().split()[-1] if term.strip() else term
    return w.rstrip("s")


def _head_word(name: str) -> str:
    """Supplier search stem: first word ('Golden Valley Produce LLC' -> 'Golden');
    supplier names end in LLC/Co/Farms, so the last word is useless."""
    return name.strip().split()[0] if name.strip() else name


def _explain_template(facts: dict) -> str:
    mc = facts["match_counts (lots and purchase orders, by state)"]
    dos = facts["focus_supply"]
    category_label = dos["category"].replace("_", " ").title()
    rec, base = facts["recommended_plan"], facts["do_nothing_baseline"]

    def fmt_dos(v):
        return "beyond the 7-day horizon" if v is None else f"{v} days"

    if not facts["has_exposure"]:
        return (
            f"{facts['authority']} incident {facts['event_id']}"
            f"{' (' + facts['hazard'] + ')' if facts['hazard'] else ''} produced "
            "no evidence-linked lot or inbound-order matches in the current "
            "operational records. No recall-triggered quarantine, purchase-order "
            "hold, or recovery action is indicated. The unchanged network baseline "
            f"serves {base['served_lb']:g} lb with {base['unmet_demand_lb']:g} lb "
            "of pre-existing unmet demand; it is context, not incident impact. "
            "Verify the authority source and keep food-safety decisions under "
            "human review."
        )

    return (
        f"{facts['authority']} recall {facts['event_id']}"
        f"{' (' + facts['hazard'] + ')' if facts['hazard'] else ''}: "
        f"{mc.get('confirmed', 0)} confirmed, {mc.get('probable', 0)} probable, and "
        f"{mc.get('possible', 0)} possible matches across lots and inbound orders; "
        f"{facts['confirmed_quarantine_proposed_lb']:g} lb proposed for quarantine and "
        f"{facts['unconfirmed_needing_review_lb']:g} lb awaiting human review; "
        f"{facts['purchase_orders_flagged_at_risk']} purchase order(s) flagged at risk; "
        f"{facts['infeasible_distribution_lines (conservative pool, all active recalls)']} "
        f"planned distribution line(s) infeasible against the conservative pool. "
        f"{category_label} days of supply under the conservative assumption: "
        f"{fmt_dos(dos['conservative_assumption_before_plan'])} without action, "
        f"{fmt_dos(dos['conservative_assumption_after_plan'])} with the recommended plan "
        f"(optimistic assumption: {fmt_dos(dos['optimistic_assumption'])}). "
        f"The recommended plan serves {rec['served_lb']:g} lb "
        f"(vs {base['served_lb']:g} lb doing nothing), costs ${rec['procurement_cost']:g}, "
        f"and reports {rec['hard_constraint_violations']} hard-constraint violations. "
        f"Both scenario figures are operator-selected assumptions, not statistical bounds."
    )


def _compact(out):
    """Compact a tool result for the transcript (full data stays in the DB)."""

    if isinstance(out, pd.DataFrame):
        return {"rows": len(out), "columns": list(out.columns),
                "head": out.head(_RESULT_ROW_CAP).to_dict("records")}
    if isinstance(out, list):
        return {"rows": len(out), "head": out[:_RESULT_ROW_CAP]}
    if isinstance(out, tuple):
        return [_compact(x) for x in out]
    if hasattr(out, "model_dump"):
        return out.model_dump()
    return out
