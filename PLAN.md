# FoodShock: Food-Bank Supply Shock Radar

AISCO Hackathon (AI Objectives Institute), July 15–17. Judging: July 17, 9am–1pm.

## 1. Project Summary

FoodShock is an AI-agent early-warning and recovery system for food banks. A recall-response agent turns evolving food-safety recalls into operational consequences — affected inventory, inbound-order risk, projected shortages — and proposes a human-approved recovery plan.

### One-line pitch

When a recall drops, FoodShock traces lot- and PO-level exposure, projects the shortage, and drafts a safe recovery plan for an operator to approve.

### Core decision

The system does not infer inventory contamination from the geography of human infections. It follows the operational evidence chain:

```text
CDC or state human-case signal
        ↓
FDA or USDA implicated product, facility, or recall
        ↓
Supplier, product, lot, date, and distribution lineage
        ↓
Food-bank inventory and inbound-order exposure
        ↓
Shortage projection and recovery options
        ↓
Human-approved recovery plan
```

## 2. Problem

Food banks operate with uncertain donations, limited budgets, short shelf lives, and volatile demand. A food-safety event can simultaneously quarantine inventory, cancel inbound orders, shrink a commodity category, break planned distributions, and force urgent reallocation among pantries.

Public recall notices are narrative and use different product, supplier, facility, and unit terminology than a food bank's internal records. Staff must manually connect notices to inventory and orders before recovery planning can start.

### Quantification tasks (for the pitch — compute or cite, never assert)

- [x] **openFDA, 2024 initiation-date view:** 1,387 food-enforcement product records across 482 distinct `event_id` values. Query: [`recall_initiation_date:[20240101 TO 20241231]`, grouped by `event_id`](https://api.fda.gov/food/enforcement.json?search=recall_initiation_date:%5B20240101%2BTO%2B20241231%5D&count=event_id&limit=1000) (retrieved 2026-07-16). These are enforcement events, not necessarily one consumer notice each.
- [x] **FSIS, 2024 recall-date view:** 34 numbered recall cases (`001-2024` through `034-2024`) plus 19 distinct Public Health Alerts. Counted from the [FSIS Recall API](https://www.fsis.usda.gov/fsis/api/recall/v/1), normalizing translated duplicate rows and `-EXP` expansion records to their base recall number (retrieved 2026-07-16).
- [x] **Real-world anchor:** FDA and CDC concluded that recalled Taylor Farms yellow onions, served slivered at McDonald's, were the likely source of the 2024 *E. coli* O157:H7 outbreak. Taylor Farms initiated the recall on October 22; CDC reported 104 cases, 34 hospitalizations, one death, and 14 states. Sources: [FDA investigation](https://www.fda.gov/food/outbreaks-foodborne-illness/outbreak-investigation-e-coli-o157h7-onions-october-2024) and [CDC outbreak page](https://www.cdc.gov/ecoli/outbreaks/e-coli-O157.html).
- [x] **Manual response comparator (explicit internal estimate, not a validated fact):** 2.0 staff-hours = 20 minutes to triage/extract the notice + 60 minutes to cross-reference lots, POs, and destinations + 25 minutes to replan substitutions/allocations + 15 minutes to draft stakeholder updates. The task decomposition is informed by the [Rhode Island Community Food Bank recall workflow](https://rifoodbank.org/wp-content/uploads/2023/06/Food-Safety-Recall-Process-doc.pdf), but the minute values are FoodShock assumptions and have not been operator-validated.

## 3. Gate 0: Grounding (first gate — starts immediately, in parallel with build)

The deck scores homework: *"Research food banks supply chain teams and find out what unique problems they face."* No pitch claim ships without an interview quote or a citable document.

### Actions (descending value)

1. Contact a local food-bank supply-chain team today (SF-Marin Food Bank, Alameda County CFB, Second Harvest of Silicon Valley). Ask for 20 minutes: "walk me through the last recall you handled."
2. Fallback: published evidence only — annual reports for scale numbers, published recall procedures, public audits. Cite documents.

### Gate 0 result (published-evidence fallback)

No food-bank operator interview was completed. The pitch therefore uses only the following citable operational evidence and does **not** claim food-bank validation:

- The [SF-Marin Food Bank FY2024 annual report](https://www.sfmfoodbank.org/annual-report-2023-2024/) reports 67 million pounds distributed, nearly 70% fresh produce, 53,000 households served weekly, and 215 neighborhood pantries. These figures establish regional-network scale and perishability context; they are not inputs to the synthetic scenario.
- [MANNA FoodBank's product-recall page](https://mannafoodbank.org/agency-access-and-information/product-recalls/) states that Feeding America National Office issues network notifications for national Class I and II recalls and other recalls that may affect food supplied to members. This grounds recall-notification workflow without asserting a specific food bank's response time.

### Interview questions (hypotheses to validate — NOT facts we assert)

- How do recall notices reach you today (email, network portal, distributor, news)?
- Who checks exposure, and how long does it take? What records do they search?
- Which inventory system do you use, and what does a lot record contain?
- Which identifiers survive donation intake? Are donated/salvage loads harder to trace than purchased loads? (Hypothesis: yes — validate before claiming.)
- What happened to inbound orders during your last recall?
- How do you decide substitutions and pantry allocations under shortage?

### Labeling rule

The demo scenario is **public-data-inspired synthetic data**. If scaled to a named food bank's published figures, say "modeled on published figures from X's annual report." Never claim tailoring or validation by a food bank unless an operator actually reviewed it — one judge runs warehouse operations at ACCFB.

## 4. Users and Decisions

Primary user: a food-bank supply-chain or procurement coordinator responsible for inventory, inbound orders, pantry allocations, and disruption response. Secondary: food-safety staff, warehouse managers, pantry coordinators.

### Questions FoodShock answers

1. Which inventory lots are confirmed, probable, or possible recall matches?
2. Which inbound purchase orders and donations are at risk?
3. How many days of supply remain per food category, under optimistic and conservative scenario assumptions for unconfirmed exposure?
4. Which planned distributions become infeasible?
5. Which safe substitutions and sourcing options exist, at what cost?
6. How should unaffected inventory be allocated without abandoning harder-to-serve pantries?
7. Which facts are confirmed, which inferred, and which need human review?

## 5. Scope

### Hackathon MVP — one complete disruption-response loop

1. Load synthetic inventory, suppliers, POs, pantry demand, and capacity data (SQLite).
2. Fetch a recent openFDA or USDA FSIS incident on demand, with the curated E. coli notice as a deterministic offline fallback.
3. Agent extracts structured recall entities with source excerpts and confidence.
4. Agent matches the notice against lot-level inventory and inbound orders via tools.
5. Deterministic 7-day inventory projection per category. Confirmed recalled lots and confirmed-canceled POs are excluded from every run, unconditionally. Only unconfirmed items (probable/possible matches, at-risk POs) are toggled, as labeled scenario assumptions.
6. One small constrained recovery LP (greedy heuristic as fallback).
7. Operator reviews and approves the plan; agent drafts communications.
8. Before-and-after metrics.

### Explicitly OUT of the MVP (moved to §17 stretch)

- Monte Carlo simulation and probability distributions — replaced by the deterministic projection under two labeled scenario assumptions for unconfirmed items (optimistic: they resolve clear; conservative: they are lost). These are operator-selected assumptions, not statistical bounds — never label them "best/worst case."
- Temporal/bitemporal graph machinery — replaced by provenance fields on event records.
- Embedding-based entity resolution (tiers 1–4 + human review only).
- Pareto plan sets — one recommended plan vs. do-nothing baseline.
- Scheduled polling/alerts, Neo4j, and streaming updates.

### Explicit non-goals (unchanged)

- Diagnosing illness or predicting individual health outcomes
- Declaring food safe based on an AI confidence score
- Inferring inventory contamination from proximity to reported infections
- Automatically disposing of, releasing, purchasing, or distributing inventory
- Replacing FDA, USDA, CDC, supplier, or food-bank food-safety processes
- Production integration with warehouse-management platforms

## 6. Data Sources

### Public safety signals (live authority feeds + curated fallback)

- openFDA food enforcement: <https://open.fda.gov/apis/food/enforcement/> — fetched on demand. The API labels its results unvalidated, so FoodShock retains the source record, displays a verification warning, and links back to the authority.
- USDA FSIS Recall API: <https://www.fsis.usda.gov/science-data/developer-resources/recall-api> — fetched on demand through an independently isolated adapter.
- CDC multistate foodborne outbreak notices: <https://www.cdc.gov/foodborne-outbreaks/outbreaks/index.html> — source material for curated outbreak analogues; not polled in the MVP.

The UI caches each provider result or failure for 10 minutes. A timeout, malformed record, or unavailable feed cannot disable the other authority feed or the curated fallback.

### Food-bank operational data

Clearly labeled synthetic records: suppliers and aliases, facilities, product catalog (UPC/GTIN/SKU), inventory lots with expiration, POs with delivery dates, donations, warehouse capacity by temperature zone, pantry demand by category, pantry capabilities, vehicles, travel times.

### Data-safety rule

No individual client records, names, addresses, or PII. Demand aggregated by pantry and category.

## 7. Technical Architecture

```text
┌──────────────┐   ┌──────────────┐   ┌────────────────┐
│ openFDA API  │   │ USDA FSIS API│   │ curated notice │
└──────┬───────┘   └──────┬───────┘   └───────┬────────┘
       └──────────────┬────┴───────────────────┘
                      ▼
        ┌──────────────────────────────────────┐
        │ Incident source boundary             │
        │ deterministic mapping + provenance   │
        │ independent failure isolation        │
        └──────────────────┬───────────────────┘
                           ▼
        ┌──────────────────────────────────────┐
        │ RecallResponseAgent                  │
        │ observe → investigate → explain      │
        │ → request approval only if exposed   │
        └────┬────────┬──────────┬─────────┬───┘
             │        │          │         │ tool calls
             ▼        ▼          ▼         ▼
       extract /   inventory  7-day     recovery LP /
       verify       + PO join  projection communications
             └────────┴────┬─────┴─────────┘
                           ▼
        ┌──────────────────────────────────────┐
        │ SQLite system of record              │
        │ NetworkX lineage (derived view only) │
        └──────────────────┬───────────────────┘
                           ▼
        ┌──────────────────────────────────────┐
        │ Streamlit review + approval UI       │
        └──────────────────────────────────────┘
```

### Committed stack (decided, not optional)

- Python; SQLite; Pydantic for validated records
- On-demand openFDA and USDA FSIS adapters with deterministic source-field mapping, provenance verification, and independent failure isolation
- PuLP for the recovery LP (greedy heuristic fallback)
- NetworkX only to build the lineage graph for visualization
- Streamlit for the dashboard; pydeck/Plotly for the map view
- Pandas/NumPy for the projection

## 8. Core Data Model

Relational tables (system of record): `suppliers`, `supplier_aliases`, `facilities`, `products`, `inventory_lots`, `purchase_orders`, `donations`, `warehouses`, `pantries`, `distribution_plans`, `recall_events`, `matches`, `substitutions`, `plans`.

The lineage graph (supplier → facility → product → lot → warehouse → pantry → distribution) is **derived from these tables via joins** for the graph view; it is not a separate store.

### Recall event record (with provenance fields, replacing the temporal graph)

```json
{
  "event_id": "FDA-EXAMPLE-2026-001",
  "authority": "FDA",
  "status": "active",
  "published_at": "2026-07-16T09:00:00Z",
  "ingested_at": "2026-07-16T09:05:00Z",
  "products": ["fresh yellow onions"],
  "supplier_names": ["Example Produce LLC"],
  "lot_codes": ["LOT-8842"],
  "upcs": [],
  "production_date_start": "2026-06-20",
  "production_date_end": "2026-07-02",
  "distribution_regions": ["California", "Nevada"],
  "source_url": "https://example.invalid/recall",
  "source_excerpt": "Representative hackathon data",
  "extraction_confidence": 0.96,
  "human_confirmed": false
}
```

### Inventory lot record

```json
{
  "lot_id": "INV-1007",
  "product_id": "PROD-ONION-25LB",
  "supplier_id": "SUP-42",
  "supplier_lot_code": "LOT-8842",
  "quantity_lb": 900,
  "temperature_zone": "ambient",
  "received_at": "2026-07-05T14:30:00Z",
  "expires_at": "2026-07-22T23:59:00Z",
  "warehouse_id": "WH-SF-01",
  "status": "available"
}
```

## 9. RecallResponseAgent

One incident, one explicit loop. The demo shows the agent's tool-call transcript live — that is the "AI Agents" evidence.

```text
OBSERVE      New notice arrives (replayed for demo).
INVESTIGATE  Agent iterates with tools until exposure is resolved:
             - extract_notice(text) -> schema-validated entities + excerpts
             - query_inventory(supplier|product|lot|dates)
             - query_purchase_orders(...)
             - resolve_entity(candidate) -> match state + evidence
             - flag_gap(question) -> human review queue
EXPLAIN      Agent invokes decision tools, then narrates options:
             - project_supply(scenario) -> 7-day table; confirmed recalls
               always excluded; scenario toggles unconfirmed items only
             - optimize_recovery(constraints) -> plan vs. baseline
             Every claim carries a source excerpt or tool-call provenance.
APPROVE      Human approves or edits in the queue; only then does the
             agent draft supplier/pantry communications.
```

### Division of labor (unchanged principle)

The LLM handles unstructured language: extraction, normalization candidates, explanations, drafted communications. Deterministic code handles matching, unit conversion, status changes, projection, optimization, and metrics. Structured output is validated against a fixed schema; unknown fields stay null.

### Entity resolution (tiers 1–4 + human review)

1. Exact lot, UPC, GTIN, or facility identifier
2. Normalized supplier and product identifier
3. Supplier alias, facility address, and production-date overlap
4. Fuzzy product and supplier name similarity
5. Human review when evidence is incomplete

### Match states

- `confirmed`: authoritative identifier and applicable date or lot match
- `probable`: supplier, product, facility, and date evidence strongly agree
- `possible`: partial evidence only
- `not_matched`: evidence conflicts or does not connect
- `unknown`: records are insufficient

A score prioritizes investigation; it never declares food safe.

## 10. Disruption Propagation and Supply Projection (deterministic)

All table operations — no simulation layer.

1. Confirmed recalled lots → proposed quarantine workflow.
2. Probable/possible matches → prioritized human review.
3. Inbound POs connected to the implicated supplier/facility/product/dates → flagged at-risk.
4. **7-day projection per category**: `on_hand(day) = on_hand(day-1) + expected_inbound(day) − daily_demand`. **Safety invariant: confirmed recalled/quarantined lots and confirmed-canceled POs are excluded from every run — no toggle can re-include them.** The projection runs under two labeled scenario assumptions covering only unconfirmed items: *optimistic* (probable/possible matches resolve clear, at-risk POs arrive) and *conservative* (they are lost). These are operator-selected assumptions, not statistical bounds. The optimistic run is a display-only planning view — the optimizer never draws on it (§11). Stockout day = first day below zero.
5. Distributions and meal-box plans that fall below requirements → flagged infeasible.
6. Pantries projected below their service floor → listed.

Example impact chain (targets — replace with computed results):

```text
Onion recall
  → 1,900 lb confirmed inventory match
  → 1,200 lb possible match requiring review
  → 3 inbound orders at risk
  → produce: 8.1 days of supply → 3.6 (conservative scenario: unconfirmed matches and at-risk POs excluded)
  → 4,200 planned food boxes require substitution
  → rural pantries fall below their allocation floor
```

## 11. Recovery Optimization (one small LP)

### Decision variables

- Purchase quantity per replacement supplier × product
- Transfer quantity between warehouses
- Substitute quantity assigned per distribution plan
- Unaffected inventory allocated per pantry

### Supply pool (safety boundary)

The LP allocates only the **conservative pool**: cleared inventory plus inbound not flagged at-risk.

- Confirmed recalled or quarantined lots are removed before the model builds — never decision variables in any scenario.
- Probable/possible lots and at-risk POs are also excluded from allocatable supply. They enter the pool only when a **clearance action** in the exposure queue changes their status. Operator approval of a recovery plan is *not* food-safety clearance of a lot.
- Contingent-on-clearance plan lines (activate if a lot clears) are a stretch goal, not MVP.

### Hard constraints

- Conservative supply pool (above); procurement budget
- Supplier availability and **lead time, enforced by time-indexing**: allocations are per pantry × product × day; cumulative allocations of a product through day *d* cannot exceed supply arrived by day *d* (on-hand at day 0, safe inbound POs at expected delivery, purchases at offer lead time). Service each day is capped at that day's demand (no backlog), so a late arrival can never cover an earlier day's distribution.
- Storage capacity; temperature compatibility (filtered pre-LP)
- Product expiration; allergen restrictions in the demo data

### Objective (single weighted LP — linearized)

```text
minimize  α × Σ_{p,day} unmet_{p,day}
        + β × z                          (equity term, epigraph variable)
        + γ × procurement cost
        + δ × spoilage proxy (allocations past expiry window)

s.t.      z ≥ unmet_p / demand_p         for every pantry p with demand_p > 0
```

A raw `max` is not expressible in PuLP; the auxiliary variable `z` with one ratio constraint per pantry is the linear form (`demand_p` is a constant parameter, so each constraint is linear in `unmet_p`). Pantries with zero demand are excluded from the ratio constraints.

Output: **one recommended plan vs. the do-nothing baseline.** The operator approves; the software never executes autonomously. Fallback if the LP fights back: greedy fill by pantry shortfall priority — an honest heuristic beats a broken solver.

## 12. Substitution Model

A small curated table feeding the LP: nutritional category, cost, shelf life, temperature requirement, allergen constraints, pantry capability. AI may suggest candidates; only curated rows enter the optimizer.

```text
Fresh onions
  ├─ Frozen diced onions: strong culinary match; freezer required
  ├─ Cabbage: moderate meal-plan match; broadly available
  ├─ Carrots: moderate nutritional and operational match
  └─ Onion powder: shelf-stable; not a fresh-produce replacement
```

## 13. User Experience (Streamlit)

Five views:

1. **Exposure queue** — confirmed / probable / possible / cleared / missing-info, each row showing evidence and source excerpt; incident facts and human confirmations fold in here (no separate timeline view).
2. **Impact dashboard** — inventory by match state, POs at risk, days of supply under both labeled scenarios (confirmed recalls excluded from each), boxes disrupted, pantry service levels.
3. **Recovery-plan comparison** — recommended plan vs. do-nothing, before/after metrics, approve button, audit record.
4. **Supply-chain graph** — derived lineage from implicated supplier/facility through lots, orders, warehouses, pantries, planned distributions.
5. **Geographic map** — suppliers, facilities, warehouses, pantries, replacement sources, routes. Must not imply that proximity to a human infection indicates contaminated inventory.

## 14. Demonstration (3-minute arc)

1. **Official alert lands** — select a current openFDA or FSIS incident; the normalized source snapshot, retrieval time, trust warning, and authority link remain visible.
2. **Evidence join** — the agent maps cited source fields and checks exact lot and purchase-order lineage. A real incident with no linked operational record is reported as zero exposure, with no quarantine, hold, recovery plan, or communications.
3. **Positive-exposure path** — replay the curated E. coli analogue to show the deterministic exposure queue, seven-day projection, and recommended recovery plan.
4. **Approve only when warranted** — the operator reviews the positive-exposure plan, approves it, and receives scoped draft communications. The no-exposure path exposes no approval action.

The positive-exposure fallback is anchored to the real 2024 onion E. coli recall as an analogue (verified citation, §2). Authority incidents are real; inventory, purchase orders, demand, and every displayed operational impact remain synthetic and are labeled as such.

### Initial state

One regional food bank (public-data-inspired scale, per §3 labeling rule), two warehouses, six pantries, 15–30 inventory lots, five inbound POs, seven-day distribution plan.

### Deliverable shown to judges

- **Primary: live demo** — the Streamlit app fetches openFDA or FSIS incidents on demand and joins them only to the clearly labeled synthetic food-bank network. A zero-match incident ends in an explicit no-exposure/no-action result. The curated notice remains the deterministic offline fallback for the complete positive-exposure and approval arc; local SQLite and cached/template extraction keep that fallback network-independent.
- **Shareable URL as take-away** — deploy the same app to Streamlit Community Cloud from the repo; if the deploy succeeds, judges get a clickable link for their own devices. The onsite demo never depends on it.
- **Slide deck** (problem quantification, Gate 0 evidence, architecture, computed results) + **fallback screen recording** of the working demo.

## 15. Evaluation (two claims, no harness)

1. Extraction validated against N hand-labeled notices (report exact-match rate for lots/dates/suppliers).
2. Zero hard-constraint violations in the recovery plan; reproducible outputs for fixed inputs.

Human-factors bar (checked manually): operator can see why each lot matched, distinguish confirmed facts from estimates, and nothing material happens without explicit approval.

## 16. Implementation Plan

**Gate 0 (parallel, one person):** food-bank contact + published-evidence collection + pitch-deck skeleton. §3.

**Phase A — vertical slice (completion gate):**
1. Synthetic scenario + SQLite schema; load data.
2. Canned notice; exact + normalized matching (tiers 1–2).
3. Exposure queue + impact dashboard with 7-day projection.
4. Recovery LP + before/after metrics.

**Phase B — agent + extraction:**
5. Add independently isolated openFDA and FSIS adapters with deterministic, provenance-verified source-field mapping.
6. Wrap source intake and operational tools in the RecallResponseAgent loop; show the live transcript and honest zero-exposure path.
7. Keep schema-validated LLM extraction with excerpts for free-form/curated notices; tiers 3–4 route ambiguous joins to `flag_gap`.

**Phase C — presentation:**
8. Graph and map views.
9. Approval flow + drafted communications + audit record.
10. Pitch deck finalized; fallback screen recording; timed rehearsal of both the live-source and positive-exposure paths.

Later phases must not compromise the working vertical slice.

## 17. Stretch Goals (post-demo only)

- Monte Carlo shortage/cost distributions and sensitivity ranking
- Temporal/bitemporal event history and superseding-notice handling
- Embedding-based resolution tier + calibration dashboard
- Pareto plan alternatives (low-cost / most-meals / most-equitable)
- Scheduled FDA/USDA polling or push alerts; streaming recalculation; Neo4j exploration
- Additional disruption types; mutual-aid matching; backhaul-aware routing

## 18. Team Split

- **Data + agent** — schema, synthetic data, extraction, resolution tools, agent loop
- **Projection + optimization** — 7-day projection, LP, substitution table, metrics
- **UI + story** — Streamlit views (incl. graph + map), approval flow, Gate 0, deck, demo rehearsal

## 19. Risks and Mitigations

- **Infection geography read as inventory exposure** → exposure only via product/supplier/facility/lot/date lineage; map carries the §13 rule.
- **False matches cause unnecessary disposal** → match states + evidence + human review; never auto-dispose.
- **AI invents missing lot/supplier data** → required source excerpts, schema validation, nulls for unknowns, deterministic identifier checks.
- **Unvalidated domain assumptions in the pitch** → §3 rule: interview quote or citation, or the claim is cut. Domain hypotheses (e.g., donation-stream traceability) stay questions until validated.
- **Scope kills the demo** → Phase A gate; Monte Carlo/temporal/embeddings are stretch by construction.
- **Demo-day network or provider failure** → independently cached feed failures, curated notice replay, local SQLite, fallback screen recording.
- **Synthetic data feels arbitrary** → public-data-inspired scale with citations; every metric computed from reproducible inputs.

## 20. Judging Narrative

- **Problem (grounded):** open with the published-evidence fallback; quantify recall frequency and show the food-bank recall workflow. Label the 2.0 staff-hour comparator as a hypothetical task model, not measured performance.
- **Solution (agentic):** current authority incidents enter through provenance-verified adapters; the RecallResponseAgent transcript then shows observe, investigate, explain, and—only when exposure exists—request approval.
- **Human value:** measured notice-to-draft runtime under one minute in the synthetic scenario versus the transparent 2.0 staff-hour internal task model, with food-safety and allocation decisions under human control.
- **Logistics:** onsite presenter confirmed; deck done the night before; fallback recording ready.

### Closing line

FoodShock does not predict illness from dots on a map; it traces a supply disruption through the food bank and helps operators recover before uncertain supply becomes empty shelves.
