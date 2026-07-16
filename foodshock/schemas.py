"""Pydantic schemas and shared literals. Single source of truth for
match states, scenarios, and the extraction contract (PLAN.md §8-§11)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

MatchState = Literal["confirmed", "probable", "possible", "not_matched", "unknown"]
Scenario = Literal["optimistic", "conservative"]
TargetType = Literal["lot", "po", "donation"]

SCENARIOS: tuple[str, ...] = ("optimistic", "conservative")

# Exact UI labels (PLAN.md §10: operator-selected assumptions, not bounds).
SCENARIO_LABELS: dict[str, str] = {
    "optimistic": "Optimistic scenario — unconfirmed matches resolve clear, at-risk POs arrive (assumption, not a bound)",
    "conservative": "Conservative scenario — unconfirmed matches and at-risk POs are lost (assumption, not a bound)",
}


class RecallExtraction(BaseModel):
    """Schema-validated output of notice extraction (PLAN.md §9).

    Unknown fields stay None/empty — the model never invents values.
    `excerpts` maps a field name to the exact supporting quote from the notice.
    """

    authority: Literal["FDA", "USDA", "CDC", "OTHER"]
    products: list[str] = Field(default_factory=list)
    supplier_names: list[str] = Field(default_factory=list)
    facility_names: list[str] = Field(default_factory=list)
    lot_codes: list[str] = Field(default_factory=list)
    upcs: list[str] = Field(default_factory=list)
    production_date_start: date | None = None
    production_date_end: date | None = None
    distribution_regions: list[str] = Field(default_factory=list)
    pathogen: str | None = None
    action_required: str | None = None
    excerpts: dict[str, str] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)


class MatchEvidence(BaseModel):
    """Why a lot/PO/donation matched. Stored as JSON in matches.evidence_json."""

    tier: int
    reasons: list[str]
    matched_fields: dict[str, str] = Field(default_factory=dict)
    notice_excerpts: dict[str, str] = Field(default_factory=dict)


class PlanMetrics(BaseModel):
    """Evaluation of one plan (PLAN.md §11, §15). Stored in plans.metrics_json."""

    served_lb: float
    unmet_demand_lb: float
    worst_pantry_fulfillment: float = Field(ge=0.0, le=1.0)
    procurement_cost: float
    spoilage_lb: float
    boxes_disrupted: int
    hard_constraint_violations: int = 0
