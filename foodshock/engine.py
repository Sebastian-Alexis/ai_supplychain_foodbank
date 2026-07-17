"""Deterministic core (PLAN.md §9-§12): entity resolution, disruption
propagation, 7-day supply projection, recovery plans, review and approval.

Everything here is conventional code — the LLM never touches matching,
projection, or optimization (PLAN.md §9 division of labor).
"""

from __future__ import annotations

import difflib
import json
import math
import re
import sqlite3
from datetime import date, timedelta

import pandas as pd
import pulp

from .datagen import BUDGET_USD, TODAY
from .db import (audit, available_lots, effective_states, expected_pos, insert,
                 now_iso, query_inventory, query_purchase_orders, rows)
from .narrate import narrate
from .schemas import MatchEvidence, PlanMetrics, RecallExtraction

TRANSIT_DAYS = 14          # receiving window after the production window
STORAGE_TURNS_PER_WEEK = 2  # weekly pantry throughput cap = storage * turns
BOX_LB = 10.0               # lb per food box (stated assumption for metrics)
HORIZON_DAYS = 7            # planning horizon; arrivals on/after day 7 are unusable

OBJECTIVE_WEIGHTS = {"alpha_unmet": 1.0, "beta_equity": 2000.0,
                     "gamma_cost": 0.05, "delta_spoilage": 0.3,
                     # anti-degeneracy tie-break on inter-warehouse transfers
                     # (NOT an operational cost claim): small enough that a
                     # transfer serving 1 lb of demand (alpha=1.0) always pays.
                     "epsilon_transfer": 0.001}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def _tokens(s: str) -> set[str]:
    return {t.rstrip("s") for t in _norm(s).split() if t}


def _product_match(ext_products: list[str], product_name: str) -> str | None:
    pt = _tokens(product_name)
    for cand in ext_products:
        if _norm(cand) == _norm(product_name) or len(_tokens(cand) & pt) >= 2:
            return cand
    return None

def incident_focus(conn: sqlite3.Connection, event_id: str | None = None) -> dict:
    """Return the product category most strongly connected to one incident."""
    if event_id is None:
        found = rows(
            conn,
            "SELECT event_id FROM recall_events WHERE status='active' "
            "ORDER BY ingested_at DESC, rowid DESC LIMIT 1",
        )
        event_id = found[0]["event_id"] if found else None
    if event_id is None:
        return {"category": "supply", "products": [], "hazard": None}

    event = rows(conn, "SELECT extraction_json FROM recall_events WHERE event_id=?",
                 (event_id,))
    extraction = (
        RecallExtraction.model_validate_json(event[0]["extraction_json"])
        if event and event[0]["extraction_json"]
        else None
    )
    linked = rows(conn, """
        SELECT pr.category, pr.name product_name, m.state
        FROM matches m
        JOIN inventory_lots l
          ON m.target_type='lot' AND m.target_id=l.lot_id
        JOIN products pr ON pr.product_id=l.product_id
        WHERE m.event_id=? AND m.state!='not_matched'
        UNION ALL
        SELECT pr.category, pr.name product_name, m.state
        FROM matches m
        JOIN purchase_orders po
          ON m.target_type='po' AND m.target_id=po.po_id
        JOIN products pr ON pr.product_id=po.product_id
        WHERE m.event_id=? AND m.state!='not_matched'
    """, (event_id, event_id))
    if linked:
        weights = {"confirmed": 4, "probable": 3, "possible": 2, "unknown": 1}
        scores: dict[str, int] = {}
        for item in linked:
            scores[item["category"]] = (
                scores.get(item["category"], 0) + weights.get(item["state"], 0)
            )
        category = max(sorted(scores), key=lambda value: scores[value])
        products = sorted({
            item["product_name"] for item in linked if item["category"] == category
        })
    elif extraction is not None:
        catalog = rows(conn, "SELECT name, category FROM products")
        hits = [item for item in catalog
                if _product_match(extraction.products, item["name"])]
        category = hits[0]["category"] if hits else "supply"
        products = sorted({item["name"] for item in hits})
    else:
        category, products = "supply", []
    return {
        "category": category,
        "products": products,
        "hazard": extraction.pathogen if extraction is not None else None,
    }


def _parse_date(ts: str) -> date:
    return date.fromisoformat(ts[:10])

def _expiry_day(expires_at: str) -> int:
    """Last calendar day (offset from TODAY) on which the stock is usable."""
    return (_parse_date(expires_at) - TODAY).days


def _usable_lots(conn: sqlite3.Connection, scenario: str) -> list[dict]:
    """available_lots minus stock already past its expiration date.

    Expiry is an operational exclusion on top of the recall-safety invariant
    that lives in db.available_lots (which no scenario can bypass).
    """
    return [l for l in available_lots(conn, scenario) if _expiry_day(l["expires_at"]) >= 0]


def _offer_expiry(offer: dict, products: dict) -> int:
    """Last usable day of a purchase from `offer`: fresh shelf life from arrival."""
    return offer["lead_time_days"] + products[offer["product_id"]]["shelf_life_days"]

# ------------------------------------------------------------ resolution

def resolve_event(conn: sqlite3.Connection, event_id: str) -> list[dict]:
    """Run resolution tiers 1-4 (PLAN.md §9) over lots and POs; write matches.

    Rows are written only for records in the implicated product categories so
    the exposure queue shows discrimination (cleared look-alikes) without
    burying the operator in irrelevant records.
    """
    ev = rows(conn, "SELECT * FROM recall_events WHERE event_id=?", (event_id,))[0]
    ext = RecallExtraction.model_validate_json(ev["extraction_json"])
    conn.execute("DELETE FROM matches WHERE event_id=?", (event_id,))

    ext_suppliers = [_norm(s) for s in ext.supplier_names]
    ext_facilities = [_norm(f) for f in ext.facility_names]
    regions = {r.strip().lower() for r in ext.distribution_regions}
    region_states = {"california": "CA", "nevada": "NV"}
    region_abbrevs = {region_states[r] for r in regions if r in region_states}

    aliases: dict[str, list[str]] = {}
    for a in rows(conn, "SELECT supplier_id, alias FROM supplier_aliases"):
        aliases.setdefault(a["supplier_id"], []).append(_norm(a["alias"]))
    suppliers = {s["supplier_id"]: s for s in rows(conn, "SELECT * FROM suppliers")}

    # Which categories are implicated (via product match against the catalog)?
    implicated_categories: set[str] = set()
    for p in rows(conn, "SELECT * FROM products"):
        if _product_match(ext.products, p["name"]):
            implicated_categories.add(p["category"])

    def supplier_evidence(supplier_id: str) -> tuple[str | None, str]:
        """Returns (level, detail): level in ('exact','alias','fuzzy',None)."""
        s = suppliers[supplier_id]
        n = _norm(s["name"])
        for e in ext_suppliers:
            if n == e:
                return "exact", s["name"]
        for al in aliases.get(supplier_id, []):
            for e in ext_suppliers:
                if al == e or al in e or e in al:
                    return "alias", al
        for e in ext_suppliers:
            if difflib.SequenceMatcher(None, n, e).ratio() >= 0.72:
                return "fuzzy", s["name"]
        return None, ""

    def date_overlap(d: date) -> bool:
        if not (ext.production_date_start and ext.production_date_end):
            return True  # cannot exclude on dates the notice does not state
        return ext.production_date_start <= d <= ext.production_date_end + timedelta(days=TRANSIT_DAYS)

    def classify(target: dict, kind: str) -> tuple[str, int, float, MatchEvidence] | None:
        prod_hit = _product_match(ext.products, target["product_name"])
        reasons: list[str] = []
        fields: dict[str, str] = {}

        # Tier 1: authoritative identifier
        if kind == "lot" and target["supplier_lot_code"] and target["supplier_lot_code"] in ext.lot_codes:
            reasons.append(f"lot code {target['supplier_lot_code']} listed in the notice")
            fields["lot_code"] = target["supplier_lot_code"]
            ev_ = MatchEvidence(tier=1, reasons=reasons, matched_fields=fields,
                                notice_excerpts={"lot_codes": ext.excerpts.get("lot_codes", "")})
            return "confirmed", 1, 1.0, ev_

        sup_level, sup_detail = supplier_evidence(target["supplier_id"])
        when = _parse_date(target["received_at"] if kind == "lot" else target["expected_delivery"])
        dates_ok = date_overlap(when) if kind == "lot" else when >= (ext.production_date_start or when)

        # Tier 2: exact supplier + product + dates
        if sup_level == "exact" and prod_hit and dates_ok:
            reasons += [f"supplier '{sup_detail}' named in the notice",
                        f"product matches '{prod_hit}'",
                        "date window overlaps the stated production window"
                        if kind == "lot" else "delivery expected after the production window began"]
            fields.update({"supplier": sup_detail, "product": prod_hit})
            ev_ = MatchEvidence(tier=2, reasons=reasons, matched_fields=fields,
                                notice_excerpts={k: ext.excerpts.get(k, "") for k in
                                                 ("supplier_names", "products", "production_date_start")})
            return "probable", 2, 0.8, ev_

        # Tier 3: alias/facility + product + dates
        fac_hit = kind == "lot" and target.get("facility_id") and any(
            _norm(f["name"]) in ext_facilities
            for f in rows(conn, "SELECT name FROM facilities WHERE facility_id=?", (target["facility_id"],)))
        if (sup_level == "alias" or fac_hit) and prod_hit and dates_ok:
            reasons += [f"supplier alias/facility evidence ('{sup_detail or 'facility'}')",
                        f"product matches '{prod_hit}'", "date window overlaps"]
            ev_ = MatchEvidence(tier=3, reasons=reasons, matched_fields=fields,
                                notice_excerpts={"facility_names": ext.excerpts.get("facility_names", "")})
            return "probable", 3, 0.75, ev_

        # Tier 4: fuzzy supplier, or product + distribution-region overlap
        if sup_level == "fuzzy" and prod_hit:
            reasons += [f"supplier name similar to a recalled supplier ({sup_detail})",
                        f"product matches '{prod_hit}'"]
            ev_ = MatchEvidence(tier=4, reasons=reasons, matched_fields=fields, notice_excerpts={})
            return "possible", 4, 0.5, ev_
        sup_state = suppliers[target["supplier_id"]]["state"]
        if prod_hit and sup_state in region_abbrevs:
            reasons += [f"product matches '{prod_hit}'",
                        f"supplier is in a stated distribution region ({sup_state})",
                        "no supplier or lot lineage connects this record"]
            ev_ = MatchEvidence(tier=4, reasons=reasons, matched_fields={"product": prod_hit},
                                notice_excerpts={"distribution_regions": ext.excerpts.get("distribution_regions", "")})
            return "possible", 4, 0.45, ev_

        if target["category"] in implicated_categories:
            ev_ = MatchEvidence(tier=0, reasons=["no identifier, supplier, product, or date lineage connects this record"],
                                matched_fields={}, notice_excerpts={})
            return "not_matched", 0, 0.0, ev_
        return None

    out: list[dict] = []
    for lot in query_inventory(conn):
        res = classify(lot, "lot")
        if res is None:
            continue
        state, tier, score, ev_ = res
        insert(conn, "matches", {"event_id": event_id, "target_type": "lot", "target_id": lot["lot_id"],
                                 "state": state, "tier": tier, "score": score,
                                 "evidence_json": ev_.model_dump_json()})
        out.append({"target_type": "lot", "target_id": lot["lot_id"], "state": state, "tier": tier})
    for po in query_purchase_orders(conn):
        res = classify(po, "po")
        if res is None:
            continue
        state, tier, score, ev_ = res
        insert(conn, "matches", {"event_id": event_id, "target_type": "po", "target_id": po["po_id"],
                                 "state": state, "tier": tier, "score": score,
                                 "evidence_json": ev_.model_dump_json()})
        out.append({"target_type": "po", "target_id": po["po_id"], "state": state, "tier": tier})
    conn.commit()
    return out


# ----------------------------------------------------------- propagation

def propagate(conn: sqlite3.Connection, event_id: str) -> dict:
    """Escalate confirmed matches, flag POs with ANY unresolved connecting
    evidence (confirmed/probable/possible/unknown) at risk, recompute
    distribution feasibility against the conservative pool. Idempotent.

    SIDE EFFECTS are GLOBAL: operational state must reflect every active
    recall's evidence, whichever incident triggered the run. The returned
    lot/PO metrics are attributed to `event_id` ONLY -- worst state per
    target from this event's own match rows (review overrides applied,
    targets deduplicated) -- so a second incident never claims the first
    one's pounds or orders. Distribution feasibility is a pool-level
    statement across all active recalls: infeasible_lines/lb report the
    global snapshot; newly_infeasible_lines is this run's delta.
    """
    lot_states = effective_states(conn, "lot")
    for lot_id, st in lot_states.items():
        if st == "confirmed":
            r = rows(conn, "SELECT status, quantity_lb FROM inventory_lots WHERE lot_id=?", (lot_id,))[0]
            if r["status"] == "available":
                conn.execute("UPDATE inventory_lots SET status='quarantine_proposed' WHERE lot_id=?", (lot_id,))
                audit(conn, "system", "quarantine_proposed", {"lot_id": lot_id, "quantity_lb": r["quantity_lb"]})

    po_states = effective_states(conn, "po")
    for po_id, st in po_states.items():
        if st in ("confirmed", "probable", "possible", "unknown"):
            r = rows(conn, "SELECT status FROM purchase_orders WHERE po_id=?", (po_id,))[0]
            if r["status"] == "open":
                conn.execute("UPDATE purchase_orders SET status='at_risk' WHERE po_id=?", (po_id,))
                audit(conn, "system", "po_flagged_at_risk", {"po_id": po_id, "state": st})

    # Incident attribution: this event's own evidence, worst state per target.
    quarantined_lb = 0.0
    review_lb = 0.0
    for lot_id, st in effective_states(conn, "lot", event_id).items():
        qty = rows(conn, "SELECT quantity_lb FROM inventory_lots WHERE lot_id=?", (lot_id,))[0]["quantity_lb"]
        if st == "confirmed":
            quarantined_lb += qty
        elif st in ("probable", "possible", "unknown"):
            review_lb += qty
    pos_at_risk = sum(1 for st in effective_states(conn, "po", event_id).values()
                      if st in ("confirmed", "probable", "possible", "unknown"))

    # Distribution feasibility is a warehouse/product FEFO timeline. Stock at
    # another site is unavailable without an explicit transfer; expired stock
    # drops out; safe conservative POs appear only on/after their ETA.
    products = {p["product_id"]: p for p in rows(conn, "SELECT * FROM products")}
    supply: dict[tuple[str, str], list[list[float]]] = {}
    for lot in _usable_lots(conn, "conservative"):
        key = (lot["warehouse_id"], lot["product_id"])
        supply.setdefault(key, []).append(
            [0, _expiry_day(lot["expires_at"]), lot["quantity_lb"]])
    for po in expected_pos(conn, "conservative"):
        arrival = (_parse_date(po["expected_delivery"]) - TODAY).days
        key = (po["warehouse_id"], po["product_id"])
        supply.setdefault(key, []).append([
            arrival,
            arrival + products[po["product_id"]]["shelf_life_days"],
            po["quantity_lb"],
        ])
    was_bad = {r["dist_id"] for r in rows(
        conn, "SELECT dist_id FROM distribution_plans WHERE status='infeasible'")}
    infeasible_lines = 0
    infeasible_lb = 0.0
    newly_infeasible = 0
    for line in rows(conn, "SELECT * FROM distribution_plans WHERE status != 'substituted' ORDER BY scheduled_date, dist_id"):
        key = (line["warehouse_id"], line["product_id"])
        day = (_parse_date(line["scheduled_date"]) - TODAY).days
        live = sorted((b for b in supply.get(key, ())
                       if b[0] <= day <= b[1] and b[2] > 0),
                      key=lambda b: b[1])
        left = sum(b[2] for b in live)
        if left >= line["quantity_lb"]:
            need = line["quantity_lb"]
            for bucket in live:
                use = min(bucket[2], need)
                bucket[2] -= use
                need -= use
                if need <= 0:
                    break
            status = "planned"
        else:
            status = "infeasible"
            infeasible_lines += 1
            infeasible_lb += line["quantity_lb"]
            if line["dist_id"] not in was_bad:
                newly_infeasible += 1
        if status != line["status"]:
            conn.execute("UPDATE distribution_plans SET status=? WHERE dist_id=?", (status, line["dist_id"]))

    conn.commit()
    return {"quarantine_proposed_lb": quarantined_lb, "review_lb": review_lb,
            "pos_at_risk": pos_at_risk, "infeasible_lines": infeasible_lines,
            "infeasible_lb": infeasible_lb, "newly_infeasible_lines": newly_infeasible}


# ------------------------------------------------------------ projection

def project_supply(conn: sqlite3.Connection, scenario: str, days: int = 7,
                   plan_id: str | None = None) -> pd.DataFrame:
    """Deterministic day-by-day category projection (PLAN.md §10).

    Safety invariant lives in db.available_lots/expected_pos: confirmed
    recalled stock and canceled POs never enter ANY scenario. Consumption is
    expiry-aware FEFO per category: stock that dies overnight is removed from
    the running level (shown as expired_lb) instead of inflating later days.
    """
    demand = {}
    for r in rows(conn, "SELECT category, SUM(daily_demand_lb) d FROM pantry_demand GROUP BY category"):
        demand[r["category"]] = r["d"]
    products = {p["product_id"]: p for p in rows(conn, "SELECT * FROM products")}

    # On-hand stock (already in the warehouse: day-0 start level) vs inbound
    # arrivals (POs / plan purchases: counted as inbound_lb on their ETA day).
    onhand: dict[str, list[list[float]]] = {c: [] for c in demand}   # [last_usable_day, qty]
    buckets: dict[str, list[tuple[int, int, float]]] = {c: [] for c in demand}
    for lot in _usable_lots(conn, scenario):
        onhand[lot["category"]].append([_expiry_day(lot["expires_at"]), lot["quantity_lb"]])
    for po in expected_pos(conn, scenario):
        d = (_parse_date(po["expected_delivery"]) - TODAY).days
        if 0 <= d < days:
            e = d + products[po["product_id"]]["shelf_life_days"]
            buckets[po["category"]].append((d, e, po["quantity_lb"]))
    if plan_id:
        offers = {o["offer_id"]: o for o in rows(conn, "SELECT * FROM replacement_offers")}
        per_offer: dict[str, float] = {}
        for line in rows(conn, "SELECT * FROM plan_lines WHERE plan_id=? AND action='purchase'", (plan_id,)):
            off = offers.get(line["from_id"])
            if (off is None or not _valid_qty(line["quantity_lb"])
                    or line["product_id"] != off["product_id"]
                    or line["day"] != off["lead_time_days"]
                    or line["unit_cost_per_lb"] != off["unit_cost_per_lb"]
                    or line["to_id"] != off["receiving_warehouse_id"]):
                continue
            per_offer[line["from_id"]] = per_offer.get(line["from_id"], 0.0) + line["quantity_lb"]
        for offer_id, qty in sorted(per_offer.items()):
            off = offers[offer_id]
            d = off["lead_time_days"]
            if 0 <= d < days:
                cat = products[off["product_id"]]["category"]
                buckets[cat].append((d, _offer_expiry(off, products), min(qty, off["available_lb"])))

    recs = []
    for cat in sorted(demand):
        live: list[list[float]] = sorted(onhand[cat])   # [last_usable_day, qty], FEFO-sorted
        deficit = 0.0                  # cumulative unmet demand (display carry)
        for d in range(days):
            expired = sum(q for e, q in live if e < d)
            live = sorted([e, q] for e, q in live if e >= d)
            start = sum(q for _, q in live) - deficit
            inb = 0.0
            for a, e, q in buckets[cat]:
                if a == d:
                    inb += q
                    live.append([e, q])
            live.sort()
            need = demand[cat]
            for b in live:
                take = min(b[1], need)
                b[1] -= take
                need -= take
                if need <= 0:
                    break
            deficit += max(need, 0.0)
            level = start + inb - demand[cat]
            recs.append({"category": cat, "day": d, "date": (TODAY + timedelta(days=d)).isoformat(),
                         "start_lb": round(start, 1), "inbound_lb": round(inb, 1),
                         "demand_lb": round(demand[cat], 1), "expired_lb": round(expired, 1),
                         "end_lb": round(level, 1),
                         "stockout": level < 0})
    return pd.DataFrame(recs)


def days_of_supply(proj: pd.DataFrame) -> dict[str, float | None]:
    """First day each category goes negative (fractional); None = lasts the horizon."""
    out: dict[str, float | None] = {}
    for cat, g in proj.groupby("category"):
        g = g.sort_values("day")
        dos: float | None = None
        for _, r in g.iterrows():
            if r["end_lb"] < 0:
                dos = round(r["day"] + max(r["start_lb"] + r["inbound_lb"], 0) / r["demand_lb"], 1)
                break
        out[str(cat)] = dos
    return out


# ------------------------------------------------------------- planning

def _valid_qty(q) -> bool:
    """A plan-line quantity the evaluator may trust: finite number > 0.
    SQLite's dynamic typing lets Inf or text hide in a REAL column."""
    return type(q) in (int, float) and math.isfinite(q) and q > 0


def _pantry_product_ok(pantry: dict, product: dict) -> bool:
    """Hard compatibility, filtered pre-LP (PLAN.md §11): temperature zone
    AND allergen restrictions (comma-separated tokens on both sides)."""
    zone = product["temperature_zone"]
    if zone == "frozen" and not pantry["has_freezer"]:
        return False
    if zone == "refrigerated" and not pantry["has_refrigeration"]:
        return False
    allergens = {t.strip().casefold() for t in product["allergens"].split(",") if t.strip()}
    restricted = {t.strip().casefold() for t in pantry["allergen_restrictions"].split(",") if t.strip()}
    return not (allergens & restricted)


EXPIRING_CUTOFF = HORIZON_DAYS - 1  # dies on/before the last planned day (0..6);
                                    # day-7 expiry outlives the horizon = NOT spoilage


def serving_warehouses(conn: sqlite3.Connection) -> dict[str, str]:
    """pantry_id -> serving warehouse (the network shape for PLAN.md §11).

    Derived from seeded operations, never guessed: the single distinct
    warehouse named on the pantry's distribution_plans rows. Several distinct
    warehouses for one pantry contradict the one-home-warehouse model ->
    ValueError (surface the data problem; don't pick a mode and discard real
    routes). A pantry with no planned distributions is served by the
    scenario's only warehouse; with several warehouses and no rows the
    routing is equally ambiguous -> ValueError."""
    whs = sorted(w["warehouse_id"] for w in rows(conn, "SELECT warehouse_id FROM warehouses"))
    assigned: dict[str, set[str]] = {}
    for r in rows(conn, "SELECT DISTINCT pantry_id, warehouse_id FROM distribution_plans"):
        assigned.setdefault(r["pantry_id"], set()).add(r["warehouse_id"])
    out: dict[str, str] = {}
    for p in rows(conn, "SELECT pantry_id FROM pantries ORDER BY pantry_id"):
        pid = p["pantry_id"]
        named = assigned.get(pid, set())
        if len(named) == 1:
            out[pid] = next(iter(named))
        elif len(named) > 1:
            raise ValueError(f"pantry {pid} has distribution_plans rows from several "
                             f"warehouses {sorted(named)}; one serving warehouse expected")
        elif len(whs) == 1:
            out[pid] = whs[0]
        else:
            raise ValueError(f"pantry {pid} has no serving warehouse: no distribution_plans "
                             f"rows and {len(whs)} warehouses to choose from")
    return out


def _planning_inputs(conn):
    """Planning inputs. Supply is expiry-aware buckets HOMED AT A WAREHOUSE:
    buckets[(warehouse_id, product_id, arrival_day, last_usable_day)] = lb.

    On-hand usable lots sit at their warehouse from day 0 and die on their
    expiration day; safe inbound POs arrive at their PO's warehouse at ETA
    with fresh shelf life (PLAN.md §11 hard constraints: supplier lead time
    AND product expiration). at_risk and canceled POs never enter
    (db.expected_pos guarantees it). Last usable days are stored raw;
    consumers clamp to the horizon where needed."""
    pantries = rows(conn, "SELECT * FROM pantries ORDER BY pantry_id")
    demand7 = {(r["pantry_id"], r["category"]): r["daily_demand_lb"] * HORIZON_DAYS
               for r in rows(conn, "SELECT * FROM pantry_demand")}
    products = {p["product_id"]: p for p in rows(conn, "SELECT * FROM products")}
    buckets: dict[tuple[str, str, int, int], float] = {}
    for lot in _usable_lots(conn, "conservative"):
        key = (lot["warehouse_id"], lot["product_id"], 0, _expiry_day(lot["expires_at"]))
        buckets[key] = buckets.get(key, 0.0) + lot["quantity_lb"]
    for po in expected_pos(conn, "conservative"):
        d = (_parse_date(po["expected_delivery"]) - TODAY).days
        if 0 <= d < HORIZON_DAYS:
            key = (po["warehouse_id"], po["product_id"], d,
                   d + products[po["product_id"]]["shelf_life_days"])
            buckets[key] = buckets.get(key, 0.0) + po["quantity_lb"]
    offers = [o for o in rows(conn, "SELECT * FROM replacement_offers")
              if o["lead_time_days"] < HORIZON_DAYS]
    return pantries, demand7, products, buckets, offers, serving_warehouses(conn)


def _write_plan(conn, kind: str, method: str, buys: dict, allocs: dict,
                transfers: dict, products: dict, offers: list[dict],
                serving: dict[str, str]) -> str:
    plan_id = f"PLAN-{kind.upper()}-{rows(conn, 'SELECT COUNT(*) c FROM plans')[0]['c'] + 1:03d}"
    insert(conn, "plans", {"plan_id": plan_id, "created_at": now_iso(), "kind": kind,
                           "method": method, "objective_json": json.dumps(OBJECTIVE_WEIGHTS),
                           "status": "draft"})
    sub_notes: dict[str, list[str]] = {}
    for substitution in rows(conn, "SELECT * FROM substitutions"):
        sub_notes.setdefault(substitution["substitute_product_id"], []).append(
            substitution["note"]
        )
    offer_by_id = {o["offer_id"]: o for o in offers}
    for offer_id, qty in sorted(buys.items()):
        if qty <= 0.5:
            continue
        o = offer_by_id[offer_id]
        note = " · ".join(sorted(set(sub_notes.get(o["product_id"], []))))
        insert(conn, "plan_lines", {"plan_id": plan_id, "action": "purchase",
                                    "product_id": o["product_id"], "from_id": offer_id,
                                    "to_id": o["receiving_warehouse_id"],
                                    "day": o["lead_time_days"],
                                    "quantity_lb": round(qty, 1),
                                    "unit_cost_per_lb": o["unit_cost_per_lb"],
                                    "note": note or f"replacement purchase, lead {o['lead_time_days']}d"})
    for (src, dst, product_id, day), qty in sorted(transfers.items()):
        if qty <= 0.5:
            continue
        insert(conn, "plan_lines", {"plan_id": plan_id, "action": "transfer",
                                    "product_id": product_id, "from_id": src,
                                    "to_id": dst, "day": day,
                                    "quantity_lb": round(qty, 1),
                                    "unit_cost_per_lb": None,
                                    "note": "inter-warehouse shuttle"})
    for (pantry_id, product_id, day), qty in sorted(allocs.items()):
        if qty <= 0.5:
            continue
        insert(conn, "plan_lines", {"plan_id": plan_id, "action": "allocate",
                                    "product_id": product_id, "from_id": serving[pantry_id],
                                    "to_id": pantry_id, "day": day,
                                    "quantity_lb": round(qty, 1),
                                    "unit_cost_per_lb": None, "note": None})
    conn.commit()
    return plan_id


def _greedy_alloc(pantries, demand7, products,
                  buckets: dict[tuple[str, str, int, int], float],
                  serving: dict[str, str]) -> tuple[dict, dict]:
    """Deterministic day-by-day FEFO pro-rata allocation over the warehouse
    network (baseline + fallback). Returns (allocs, transfers).

    Stock lives at its warehouse; a pantry is served only from its serving
    warehouse. Draws take the serving warehouse's stock first (FEFO), then
    shuttle from the remaining warehouses (sorted order, FEFO within each),
    recording every cross-warehouse pound as a transfer. Expired buckets are
    dropped at the start of each day; unused supply otherwise carries
    forward. A day's unserved demand does not carry (meals happen daily --
    no backlog, mirroring the LP's per-day service cap).
    """
    allocs: dict[tuple[str, str, int], float] = {}
    transfers: dict[tuple[str, str, str, int], float] = {}
    storage_left = {p["pantry_id"]: p["storage_capacity_lb"] * STORAGE_TURNS_PER_WEEK for p in pantries}
    live: dict[tuple[str, str], list[list[float]]] = {}  # (wh, product) -> [[last_day, qty], ...]
    for d in range(HORIZON_DAYS):
        for (wh, i, a, e), qty in sorted(buckets.items()):
            if a == d and e >= d:
                live.setdefault((wh, i), []).append([e, qty])
        for k in list(live):
            live[k] = sorted(b for b in live[k] if b[0] >= d and b[1] > 0)
        remaining = {(pid, c): d7 / HORIZON_DAYS for (pid, c), d7 in demand7.items()}
        first_exp: dict[str, int] = {}   # product -> earliest live expiry anywhere
        for (wh, i), bl in live.items():
            if bl:
                first_exp[i] = min(first_exp.get(i, 10 ** 9), int(bl[0][0]))
        for product_id in sorted(first_exp, key=lambda i: (first_exp[i], i)):
            cat = products[product_id]["category"]
            takers = sorted((p for p in pantries if _pantry_product_ok(p, products[product_id])),
                            key=lambda p: -remaining.get((p["pantry_id"], cat), 0.0))
            for p in takers:
                pid = p["pantry_id"]
                home = serving[pid]
                need = min(remaining.get((pid, cat), 0.0), storage_left[pid])
                if need <= 0:
                    continue
                sources = [home] + [wh for (wh, i2) in sorted(live)
                                    if i2 == product_id and wh != home]
                got = 0.0
                for wh in dict.fromkeys(sources):   # home first, then sorted others
                    for b in live.get((wh, product_id), ()):
                        use = min(b[1], need - got)
                        if use <= 0:
                            continue
                        b[1] -= use
                        got += use
                        if wh != home:
                            tkey = (wh, home, product_id, d)
                            transfers[tkey] = transfers.get(tkey, 0.0) + use
                        if got >= need:
                            break
                    if got >= need:
                        break
                if got > 0:
                    key = (pid, product_id, d)
                    allocs[key] = allocs.get(key, 0.0) + got
                    remaining[(pid, cat)] = remaining.get((pid, cat), 0.0) - got
                    storage_left[pid] -= got
    return allocs, transfers


def build_plans(conn: sqlite3.Connection, budget: float = BUDGET_USD) -> tuple[str, str]:
    """Write the do-nothing baseline and the recommended recovery plan.

    Both draw ONLY on the conservative pool -- cleared on-hand stock at its
    warehouse from day 0 plus safe inbound POs at their PO's warehouse at ETA
    (PLAN.md §11 supply boundary), each usable only within its expiry window.
    Pantries are served from their serving warehouse; cross-warehouse stock
    moves as explicit transfer lines. Recommended = single weighted
    day-indexed LP (linearized equity epigraph). The greedy heuristic stands
    in ONLY for solver-layer failures -- audited, never silent; engine bugs
    raise out of _solve_lp instead of hiding behind the fallback.
    """
    pantries, demand7, products, buckets, offers, serving = _planning_inputs(conn)

    baseline_allocs, baseline_xfers = _greedy_alloc(pantries, demand7, products,
                                                    buckets, serving)
    baseline_id = _write_plan(conn, "baseline", "do-nothing", {}, baseline_allocs,
                              baseline_xfers, products, offers, serving)
    evaluate_plan(conn, baseline_id)

    buys, allocs, xfers, status = _solve_lp(pantries, demand7, products, buckets,
                                            offers, budget, serving)
    if status == "Optimal":
        rec_id = _write_plan(conn, "recommended", "lp-cbc", buys, allocs, xfers,
                             products, offers, serving)
    else:  # honest heuristic beats a broken SOLVER (PLAN.md §11) -- on the record
        audit(conn, "system", "lp_fallback", {"status": status})
        arrivals = dict(buckets)
        buys = {}
        spend = 0.0
        daily_by_category: dict[str, float] = {}
        for (_, category), demand_lb in demand7.items():
            daily_by_category[category] = (
                daily_by_category.get(category, 0.0)
                + demand_lb / HORIZON_DAYS
            )
        # Per-day category headroom still fillable. Existing buckets consume
        # only their own category's headroom within their usability window,
        # so each offer is capped by matching demand it can reach on/after
        # its lead time.
        headroom = {
            category: {day: daily_lb for day in range(HORIZON_DAYS)}
            for category, daily_lb in daily_by_category.items()
        }

        def _consume(category: str, first: int, last: int, qty: float) -> None:
            category_headroom = headroom.get(category)
            if category_headroom is None:
                return
            for day in range(first, min(last, HORIZON_DAYS - 1) + 1):
                take = min(qty, category_headroom[day])
                category_headroom[day] -= take
                qty -= take
                if qty <= 0:
                    return

        for (wh, product_id, arrival, expiry), qty in sorted(
            arrivals.items(), key=lambda item: (item[0][2], item[0])
        ):
            _consume(products[product_id]["category"], arrival, expiry, qty)
        for offer in sorted(
            offers, key=lambda item: (item["unit_cost_per_lb"], item["offer_id"])
        ):
            category = products[offer["product_id"]]["category"]
            category_headroom = headroom.get(category)
            if category_headroom is None:
                continue
            cap = sum(
                category_headroom[day]
                for day in range(offer["lead_time_days"], HORIZON_DAYS)
            )
            qty = min(
                offer["available_lb"],
                cap,
                (budget - spend) / offer["unit_cost_per_lb"],
            )
            if qty <= 0.5:
                continue
            buys[offer["offer_id"]] = qty
            key = (
                offer["receiving_warehouse_id"],
                offer["product_id"],
                offer["lead_time_days"],
                min(_offer_expiry(offer, products), HORIZON_DAYS - 1),
            )
            arrivals[key] = arrivals.get(key, 0.0) + qty
            spend += qty * offer["unit_cost_per_lb"]
            _consume(
                category,
                offer["lead_time_days"],
                HORIZON_DAYS - 1,
                qty,
            )
        allocs, xfers = _greedy_alloc(pantries, demand7, products, arrivals, serving)
        rec_id = _write_plan(conn, "recommended", "greedy-fallback", buys, allocs, xfers,
                             products, offers, serving)
    evaluate_plan(conn, rec_id)
    return baseline_id, rec_id


def _solve_lp(pantries, demand7, products, buckets, offers, budget, serving):
    """One weighted LP (PLAN.md §11), time-indexed by day (0..HORIZON_DAYS-1)
    over the warehouse network. Returns (buys, allocs, transfers, status).

    Supply lives in buckets HOMED AT A WAREHOUSE, usable only inside
    [arrival_day, last_usable_day]: on-hand stock at its warehouse from day 0,
    safe inbound POs at their PO's warehouse from ETA, purchases at the
    offer's receiving warehouse from lead time. Each bucket-day has one flow
    variable per destination warehouse -- home = local service, other = a
    same-day inter-warehouse transfer (PLAN.md §11 decision variable). A
    pantry draws only from its serving warehouse, so off-node stock reaches
    it exclusively through recorded transfers; neither early service (before
    arrival) nor post-expiry service is expressible. (Aggregate window
    inequalities are NOT sufficient here; the flow formulation is exact.)
    Service each day is capped at that day's demand -- no backlog. Equity
    uses the epigraph variable z with one linear ratio constraint per pantry
    with nonzero demand. epsilon_transfer is an anti-degeneracy tie-break
    keeping zero-benefit transfer noise out of emitted plans."""
    w = OBJECTIVE_WEIGHTS
    days = range(HORIZON_DAYS)
    prob = pulp.LpProblem("foodshock_recovery", pulp.LpMinimize)

    buy = {o["offer_id"]: pulp.LpVariable(f"buy_{o['offer_id']}", 0, o["available_lb"]) for o in offers}
    offers_by_product: dict[str, list[dict]] = {}
    for o in offers:
        offers_by_product.setdefault(o["product_id"], []).append(o)
    fixed_by_product: dict[str, list[tuple[str, int, int, float]]] = {}   # (wh, a, e, q)
    for (wh, i, a, e), q in sorted(buckets.items()):
        fixed_by_product.setdefault(i, []).append((wh, a, e, q))
    supply_products = sorted(set(fixed_by_product) | set(offers_by_product))
    warehouses = sorted({k[0] for k in buckets}
                        | {o["receiving_warehouse_id"] for o in offers}
                        | set(serving.values()))

    alloc: dict[tuple[str, str, int], pulp.LpVariable] = {}
    for p in pantries:
        for i in supply_products:
            if _pantry_product_ok(p, products[i]):
                for d in days:
                    alloc[(p["pantry_id"], i, d)] = pulp.LpVariable(f"a_{p['pantry_id']}_{i}_{d}", 0)
    vars_by_wh_prod_day: dict[tuple[str, str, int], list] = {}
    for (pid, i, d), v in alloc.items():
        vars_by_wh_prod_day.setdefault((serving[pid], i, d), []).append(v)

    daily = {(pid, c): d7 / HORIZON_DAYS for (pid, c), d7 in demand7.items()}
    unmet = {(pid, c, d): pulp.LpVariable(f"u_{pid}_{c}_{d}", 0)
             for (pid, c) in demand7 for d in days}
    z = pulp.LpVariable("z_equity", 0)
    # Per-bucket spoilage epigraph vars: only fixed buckets dying in-horizon.
    # (Aggregate per-product accounting is WRONG here: allocations served from
    # long-lived or purchased supply must not erase a dead bucket's spoilage.)
    spoil: list[pulp.LpVariable] = []
    xfer_vars: list[tuple[tuple[str, str, str, int], pulp.LpVariable]] = []

    # Bucket->day->destination flow variables, definable only while the bucket
    # is alive (arrival <= d <= last usable day). Each bucket's total outflow
    # is capped by its quantity (the buy variable for offer buckets); each
    # day's allocations at a warehouse must be covered by that day's inflows
    # there. Transfers cannot park stock: cover is an equality, so a
    # transferred pound serves a pantry the day it moves.
    flow_in: dict[tuple[str, str, int], list] = {}   # (warehouse, product, day)

    def _bucket_flows(tag: str, home: str, i: str, first: int, last: int, cap):
        nonlocal prob  # `prob += ...` below is augmented assignment -> local without this
        outs = []
        for d in range(first, min(last, HORIZON_DAYS - 1) + 1):
            for dst in warehouses:
                f = pulp.LpVariable(f"f_{tag}_{dst}_{d}", 0)
                flow_in.setdefault((dst, i, d), []).append(f)
                outs.append(f)
                if dst != home:
                    xfer_vars.append(((home, dst, i, d), f))
        prob += pulp.lpSum(outs) <= cap, f"bucket_{tag}"
        return outs

    for i in supply_products:
        for bi, (wh, a, e, q) in enumerate(fixed_by_product.get(i, ())):
            outs = _bucket_flows(f"{i}_b{bi}", wh, i, a, e, q)
            if e <= EXPIRING_CUTOFF:  # unallocated outflow of a dying bucket = spoilage
                sv = pulp.LpVariable(f"spoil_{i}_b{bi}", 0)
                prob += sv >= q - pulp.lpSum(outs), f"spoil_{i}_b{bi}"
                spoil.append(sv)
        for o in offers_by_product.get(i, ()):
            _bucket_flows(f"{i}_{o['offer_id']}", o["receiving_warehouse_id"], i,
                          o["lead_time_days"], _offer_expiry(o, products),
                          buy[o["offer_id"]])
    # Conservation per warehouse/product/day is an EQUALITY: flow is
    # consumption, not capacity. With `alloc <= flow`, phantom outflow from a
    # dying bucket could zero its spoilage without serving anyone.
    for wh in warehouses:
        for i in supply_products:
            for d in days:
                lhs = vars_by_wh_prod_day.get((wh, i, d), [])
                rhs = flow_in.get((wh, i, d), [])
                if lhs or rhs:
                    prob += pulp.lpSum(lhs) == pulp.lpSum(rhs), f"cover_{wh}_{i}_{d}"
    # Budget.
    prob += pulp.lpSum(buy[o["offer_id"]] * o["unit_cost_per_lb"] for o in offers) <= budget, "budget"
    # Per-day demand accounting; service never exceeds the day's demand
    # (no backlog), which is what makes the timing constraint bind.
    cats = {c for (_, c) in demand7}
    cat_products = {c: [i for i in supply_products if products[i]["category"] == c] for c in cats}
    for (pid, c), dd in daily.items():
        for d in days:
            served = pulp.lpSum(alloc[(pid, i, d)] for i in cat_products[c] if (pid, i, d) in alloc)
            prob += unmet[(pid, c, d)] >= dd - served, f"unmet_{pid}_{c}_{d}"
            prob += served <= dd, f"cap_{pid}_{c}_{d}"
    # Storage throughput cap per pantry (weekly).
    for p in pantries:
        prob += (pulp.lpSum(v for (pid, i, d), v in alloc.items() if pid == p["pantry_id"])
                 <= p["storage_capacity_lb"] * STORAGE_TURNS_PER_WEEK), f"storage_{p['pantry_id']}"
    # Equity epigraph: z >= unmet_p / demand_p over the whole horizon.
    for p in pantries:
        d_total = sum(v for (pid, c), v in demand7.items() if pid == p["pantry_id"])
        if d_total > 0:
            prob += z >= pulp.lpSum(unmet[(p["pantry_id"], c, d)]
                                    for (pid, c) in demand7 if pid == p["pantry_id"]
                                    for d in days) / d_total, f"equity_{p['pantry_id']}"

    prob += (w["alpha_unmet"] * pulp.lpSum(unmet.values())
             + w["beta_equity"] * z
             + w["gamma_cost"] * pulp.lpSum(buy[o["offer_id"]] * o["unit_cost_per_lb"] for o in offers)
             + w["delta_spoilage"] * pulp.lpSum(spoil)
             + w["epsilon_transfer"] * pulp.lpSum(v for _, v in xfer_vars))

    try:
        status = pulp.LpStatus[prob.solve(pulp.PULP_CBC_CMD(msg=0))]
    except pulp.PulpSolverError as exc:
        # Solver-layer failure ONLY (e.g. missing/broken CBC binary). Model
        # bugs -- KeyError, TypeError, bad formulation -- propagate to the
        # caller instead of masquerading as a fallback-worthy solver failure.
        return {}, {}, {}, f"SolverError: {exc}"
    if status != "Optimal":
        return {}, {}, {}, status
    buys = {o: v.value() or 0.0 for o, v in buy.items()}
    allocs = {k: v.value() or 0.0 for k, v in alloc.items()}
    transfers: dict[tuple[str, str, str, int], float] = {}
    for key, v in xfer_vars:
        val = v.value() or 0.0
        if val > 0:
            transfers[key] = transfers.get(key, 0.0) + val
    return buys, allocs, transfers, "Optimal"


def evaluate_plan(conn: sqlite3.Connection, plan_id: str) -> PlanMetrics:
    """Deterministic re-evaluation from plan lines; also counts hard-constraint
    violations (PLAN.md §15 claim: zero), including the availability invariant
    over the warehouse network: no unit may serve demand before its supply has
    arrived AT THAT WAREHOUSE or after it has expired, and no pantry may be
    served from anywhere but its serving warehouse. Verified by a per-
    (warehouse, product) FEFO simulation with same-day transfer processing --
    independent of the LP's flow formulation, exact for the plans we emit
    (transfers are consumed the day they move; FEFO is exchange-optimal per
    demand stream), and sound against tampering: phantom stock, ghost
    warehouses/pantries/offers, teleported allocations, and transfers the
    source does not hold are all flagged. Purchase arrival day AND receiving
    warehouse are derived from the OFFER -- stored values are verified, never
    trusted -- as are product, price, and quantity vs availability. TOL
    absorbs the 0.1 lb line rounding. The FEFO sim records per-bucket
    consumption (transferred stock keeps its origin-bucket identity), giving
    the exact spoilage metric (max-credit attribution: FEFO drains dying
    stock first)."""
    TOL = 0.5
    pantries, demand7, products, buckets, _offers, serving = _planning_inputs(conn)
    pantry_by_id = {p["pantry_id"]: p for p in pantries}
    warehouses = {w["warehouse_id"] for w in rows(conn, "SELECT warehouse_id FROM warehouses")}
    all_offers = {o["offer_id"]: o for o in rows(conn, "SELECT * FROM replacement_offers")}
    lines = rows(conn, "SELECT * FROM plan_lines WHERE plan_id=?", (plan_id,))

    violations = 0
    supply_buckets: dict[tuple[str, str, int, int], float] = dict(buckets)
    bought_per_offer: dict[str, float] = {}
    cost = 0.0
    for ln in (l for l in lines if l["action"] == "purchase"):
        if not _valid_qty(ln["quantity_lb"]):
            violations += 1  # corrupt quantity: no cost, no supply credit
            continue
        off = all_offers.get(ln["from_id"])
        if off is None:
            cost += ln["quantity_lb"] * (ln["unit_cost_per_lb"] or 0.0)  # claimed cost stands
            violations += 1  # unverifiable supplier offer: grants NO supply credit
            continue
        cost += ln["quantity_lb"] * off["unit_cost_per_lb"]  # authoritative price
        if (ln["product_id"] != off["product_id"] or ln["day"] != off["lead_time_days"]
                or ln["unit_cost_per_lb"] != off["unit_cost_per_lb"]
                or ln["to_id"] != off["receiving_warehouse_id"]):
            violations += 1  # persisted line contradicts the offer it cites
        bought_per_offer[ln["from_id"]] = bought_per_offer.get(ln["from_id"], 0.0) + ln["quantity_lb"]
        arrival = off["lead_time_days"]  # authoritative arrival day AND warehouse
        if arrival < HORIZON_DAYS:
            key = (off["receiving_warehouse_id"], off["product_id"], arrival,
                   min(_offer_expiry(off, products), HORIZON_DAYS - 1))
            supply_buckets[key] = supply_buckets.get(key, 0.0) + ln["quantity_lb"]
    for off_id, qty in bought_per_offer.items():
        if qty > all_offers[off_id]["available_lb"] + TOL:
            violations += 1  # bought more than the supplier offered

    # Transfer lines: malformed movements are flagged and applied nowhere;
    # well-formed ones are replayed inside the FEFO sim below.
    transfers_by_day: dict[int, list[tuple[str, str, str, float]]] = {}
    for ln in (l for l in lines if l["action"] == "transfer"):
        day = ln["day"]
        if (not _valid_qty(ln["quantity_lb"]) or type(day) is not int
                or not 0 <= day < HORIZON_DAYS
                or ln["from_id"] not in warehouses or ln["to_id"] not in warehouses
                or ln["from_id"] == ln["to_id"]):
            violations += 1
            continue
        transfers_by_day.setdefault(day, []).append(
            (ln["from_id"], ln["to_id"], ln["product_id"], ln["quantity_lb"]))

    served_day: dict[tuple[str, str, int], float] = {}
    alloc_wh_prod_day: dict[tuple[str, str, int], float] = {}
    alloc_by_pantry: dict[str, float] = {}
    for ln in (l for l in lines if l["action"] == "allocate"):
        product = products[ln["product_id"]]
        pantry = pantry_by_id.get(ln["to_id"])
        if pantry is None:
            violations += 1  # ghost pantry: no served credit
            continue
        day = ln["day"] if ln["day"] is not None else 0  # NULL -> day 0 = strictest timing case
        if not _valid_qty(ln["quantity_lb"]) or type(day) is not int or not 0 <= day < HORIZON_DAYS:
            violations += 1  # corrupt quantity or out-of-horizon day: no served credit
            continue
        if ln["from_id"] not in warehouses:
            violations += 1  # unverifiable source warehouse: no served credit
            continue
        if ln["from_id"] != serving[ln["to_id"]]:
            violations += 1  # teleported service: not the pantry's serving warehouse
        if not _pantry_product_ok(pantry, product):
            violations += 1
        key = (ln["to_id"], product["category"], day)
        served_day[key] = served_day.get(key, 0.0) + ln["quantity_lb"]
        k = (ln["from_id"], ln["product_id"], day)
        alloc_wh_prod_day[k] = alloc_wh_prod_day.get(k, 0.0) + ln["quantity_lb"]
        alloc_by_pantry[ln["to_id"]] = alloc_by_pantry.get(ln["to_id"], 0.0) + ln["quantity_lb"]

    daily = {(pid, c): d7 / HORIZON_DAYS for (pid, c), d7 in demand7.items()}
    # Per-day over-service (no-backlog model).
    for (pid, c, d), s in served_day.items():
        if s > daily.get((pid, c), 0.0) + TOL:
            violations += 1
    # Availability: per-(warehouse, product) FEFO simulation over one global
    # day loop. Arrivals land at their warehouse; each day's transfers move
    # live stock FEFO out of the source (expiry and origin-bucket identity
    # preserved) BEFORE service; then each warehouse's allocations draw FEFO.
    # Serving from stock that is not live at that warehouse -- or moving
    # stock the source does not hold -- shows up as a shortfall.
    consumed: dict[tuple[str, str, int, int], float] = {}
    live: dict[tuple[str, str], list[list]] = {}  # (wh, product) -> [[e, qty, origin], ...]
    failed: set[tuple[str, str]] = set()
    for d in range(HORIZON_DAYS):
        for (wh, i, a, e), q in sorted(supply_buckets.items()):
            if a == d and e >= d:
                live.setdefault((wh, i), []).append([e, q, (wh, i, a, e)])
        for k in list(live):
            live[k] = sorted((b for b in live[k] if b[0] >= d and b[1] > 0),
                             key=lambda b: b[0])
        for src, dst, i, qty in sorted(transfers_by_day.get(d, ())):
            need = qty
            moved = []
            for b in live.get((src, i), ()):
                use = min(b[1], need)
                if use > 0:
                    b[1] -= use
                    need -= use
                    moved.append([b[0], use, b[2]])
                if need <= 0:
                    break
            if need > TOL:
                violations += 1  # moving stock the source does not hold
            if moved:
                dstl = live.setdefault((dst, i), [])
                dstl.extend(moved)
                dstl.sort(key=lambda b: b[0])
        for (wh, i, dd), need in sorted(alloc_wh_prod_day.items()):
            if dd != d:
                continue
            for b in live.get((wh, i), ()):
                use = min(b[1], need)
                b[1] -= use
                need -= use
                if use > 0:
                    consumed[b[2]] = consumed.get(b[2], 0.0) + use
                if need <= 0:
                    break
            if need > TOL and (wh, i) not in failed:
                violations += 1
                failed.add((wh, i))
    for pid, qty in alloc_by_pantry.items():
        if qty > pantry_by_id[pid]["storage_capacity_lb"] * STORAGE_TURNS_PER_WEEK + TOL:
            violations += 1
    if cost > BUDGET_USD + 1.0:  # $1 tolerance for 0.1 lb line rounding
        violations += 1

    unmet_total = 0.0
    worst = 1.0
    for p in pantries:
        d_total = s_total = 0.0
        for (pid, c), dd in daily.items():
            if pid != p["pantry_id"]:
                continue
            for d in range(HORIZON_DAYS):
                s = min(served_day.get((pid, c, d), 0.0), dd)
                unmet_total += max(0.0, dd - s)
                d_total += dd
                s_total += s
        if d_total > 0:
            worst = min(worst, s_total / d_total)

    # Exact spoilage: fixed supply dying inside the horizon minus what the
    # FEFO sim consumed from it (wherever it was consumed -- transferred
    # stock keeps its origin bucket). Purchases merged into the same bucket
    # key only add consumption credit -- spoilage is never overstated.
    spoilage = sum(max(0.0, q - consumed.get(k, 0.0))
                   for k, q in buckets.items() if k[3] <= EXPIRING_CUTOFF)
    served_total = sum(served_day.values())

    metrics = PlanMetrics(served_lb=round(served_total, 1), unmet_demand_lb=round(unmet_total, 1),
                          worst_pantry_fulfillment=round(worst, 3), procurement_cost=round(cost, 2),
                          spoilage_lb=round(spoilage, 1),
                          boxes_disrupted=int(round(unmet_total / BOX_LB)),
                          hard_constraint_violations=violations)
    conn.execute("UPDATE plans SET metrics_json=? WHERE plan_id=?", (metrics.model_dump_json(), plan_id))
    conn.commit()
    return metrics


# ------------------------------------------------------ review & approval

def review_match(conn: sqlite3.Connection, match_id: int, action: str, actor: str) -> None:
    """Operator clearance action (PLAN.md §11): the ONLY path back into the
    pool, and only for UNCONFIRMED evidence (probable/possible/unknown).

    A confirmed match can never be cleared here: §10's exclusion of confirmed
    recalled stock is unconditional, and disposition of confirmed recalled
    product follows the recall process, not this queue. The guard also rejects
    clearing any match whose target is effectively confirmed via ANOTHER match
    (clearing it would be meaningless and would corrupt the target's status).
    """
    if action not in ("cleared", "quarantined"):
        raise ValueError(f"unknown review action: {action}")
    m = rows(conn, "SELECT * FROM matches WHERE match_id=?", (match_id,))[0]
    if action == "cleared":
        eff = effective_states(conn, m["target_type"]).get(m["target_id"])
        if m["state"] == "confirmed" or eff == "confirmed":
            raise ValueError(
                f"{m['target_type']} {m['target_id']} has a confirmed recall match; "
                "clearance is not available (PLAN.md §10: confirmed recalled stock "
                "is excluded unconditionally -- no toggle re-includes it)")
    conn.execute("UPDATE matches SET reviewed=1, review_action=?, reviewed_at=? WHERE match_id=?",
                 (action, now_iso(), match_id))
    if m["target_type"] == "lot":
        if action == "cleared":
            conn.execute("UPDATE inventory_lots SET status='cleared' WHERE lot_id=? AND status IN ('available','quarantine_proposed')",
                         (m["target_id"],))
        else:
            conn.execute("UPDATE inventory_lots SET status='quarantined' WHERE lot_id=?", (m["target_id"],))
    elif m["target_type"] == "po":
        conn.execute("UPDATE purchase_orders SET status=? WHERE po_id=?",
                     ("open" if action == "cleared" else "canceled", m["target_id"]))
    audit(conn, actor, f"match_{action}", {"match_id": match_id, "target_type": m["target_type"],
                                           "target_id": m["target_id"]})
    conn.commit()


def approve_plan(conn: sqlite3.Connection, plan_id: str, actor: str, *,
                 allow_llm: bool = True) -> list[dict]:
    """Approve only the latest safe recommended draft, then draft comms.

    Approval is idempotent and is never food-safety clearance of a lot
    (PLAN.md §11). The stored lines are re-evaluated immediately before the
    transition so a stale or tampered plan cannot bypass §15's hard-constraint
    gate.
    """
    found = rows(conn, "SELECT * FROM plans WHERE plan_id=?", (plan_id,))
    if not found:
        raise ValueError(f"unknown plan: {plan_id}")
    plan = found[0]
    if plan["kind"] != "recommended":
        raise ValueError("only a recommended recovery plan can be approved")

    latest = rows(conn, "SELECT plan_id FROM plans WHERE kind='recommended' "
                        "ORDER BY created_at DESC, rowid DESC LIMIT 1")[0]["plan_id"]
    if latest != plan_id:
        audit(conn, actor, "plan_approval_blocked",
              {"plan_id": plan_id, "reason": "superseded", "latest_plan_id": latest})
        conn.commit()
        raise ValueError(f"plan {plan_id} is superseded by {latest}")

    if plan["status"] == "approved":
        existing = rows(conn, "SELECT * FROM comms WHERE plan_id=? ORDER BY comm_id",
                        (plan_id,))
        return existing if existing else draft_communications(
            conn, plan_id, allow_llm=allow_llm)
    if plan["status"] != "draft":
        audit(conn, actor, "plan_approval_blocked",
              {"plan_id": plan_id, "reason": f"status_{plan['status']}"})
        conn.commit()
        raise ValueError(f"plan {plan_id} is {plan['status']}; only a draft can be approved")

    metrics = evaluate_plan(conn, plan_id)
    if metrics.hard_constraint_violations:
        audit(conn, actor, "plan_approval_blocked", {
            "plan_id": plan_id,
            "reason": "hard_constraint_violations",
            "hard_constraint_violations": metrics.hard_constraint_violations,
        })
        conn.commit()
        raise ValueError(
            f"plan {plan_id} has {metrics.hard_constraint_violations} "
            "hard-constraint violation(s) and cannot be approved")

    conn.execute("UPDATE plans SET status='approved', approved_at=?, approved_by=? "
                 "WHERE plan_id=? AND status='draft'", (now_iso(), actor, plan_id))
    audit(conn, actor, "plan_approved", {"plan_id": plan_id})
    conn.commit()
    return draft_communications(conn, plan_id, allow_llm=allow_llm)


def draft_communications(conn: sqlite3.Connection, plan_id: str, *,
                         allow_llm: bool = True) -> list[dict]:
    """Drafted comms (PLAN.md §9 division of labor): deterministic templates
    built ONLY from database values are the fact source; the LLM narration
    layer rewrites each body grounded in those facts (cached for offline
    replay). Guard failures ship the template, labeled method='template' --
    no model-invented facts either way (§19)."""
    products = {p["product_id"]: p for p in rows(conn, "SELECT * FROM products")}
    suppliers = {s["supplier_id"]: s for s in rows(conn, "SELECT * FROM suppliers")}
    offers = {o["offer_id"]: o for o in rows(conn, "SELECT * FROM replacement_offers")}
    warehouses = {w["warehouse_id"]: w for w in rows(conn, "SELECT * FROM warehouses")}
    focus = incident_focus(conn)
    focus_category = focus["category"].replace("_", " ")
    focus_products = ", ".join(focus["products"]) or "implicated product"
    purchases = rows(conn, "SELECT * FROM plan_lines WHERE plan_id=? AND action='purchase'", (plan_id,))
    transfers = rows(
        conn,
        "SELECT product_id, from_id, to_id, SUM(quantity_lb) quantity_lb "
        "FROM plan_lines WHERE plan_id=? AND action='transfer' "
        "GROUP BY product_id, from_id, to_id ORDER BY product_id, from_id, to_id",
        (plan_id,),
    )
    quarantined = rows(conn, "SELECT * FROM inventory_lots "
                             "WHERE status IN ('quarantine_proposed','quarantined') "
                             "ORDER BY lot_id")
    confirmed_lot_ids = {r["target_id"] for r in rows(
        conn, "SELECT DISTINCT target_id FROM matches "
              "WHERE target_type='lot' AND state='confirmed'")}
    review_q = rows(conn, "SELECT COUNT(*) c FROM matches WHERE reviewed=0 AND state IN ('probable','possible','unknown')")[0]["c"]
    infeasible = rows(conn, "SELECT COUNT(*) c, COALESCE(SUM(quantity_lb),0) lb FROM distribution_plans WHERE status='infeasible'")[0]

    out = []

    purchase_actions = [
        f"- Purchase {ln['quantity_lb']:.0f} lb {products[ln['product_id']]['name']} "
        f"(arrives in {offers[ln['from_id']]['lead_time_days']} day(s))"
        for ln in purchases
    ]
    transfer_actions = [
        f"- Reroute {ln['quantity_lb']:.0f} lb {products[ln['product_id']]['name']} "
        f"from {warehouses[ln['from_id']]['name']} to {warehouses[ln['to_id']]['name']}"
        for ln in transfers
    ]
    recovery_actions = "\n".join(purchase_actions + transfer_actions)
    if not recovery_actions:
        recovery_actions = "- Reallocate verified safe on-hand and inbound supply"
    held_ids = ", ".join(l["lot_id"] for l in quarantined)
    hold_instruction = (
        f"Do not distribute the listed implicated lot(s) pending recall disposition or "
        f"review: {held_ids}. This hold is limited to evidence-linked {focus_products} "
        f"lots; it does not place other {focus_category} products on hold."
        if held_ids else
        "No inventory lot is placed on hold by this message."
    )
    out.append({
        "audience": "pantry_coordinators",
        "subject": f"Recall impact: {focus_products} distribution response",
        "body": (f"A supplier recall affecting {focus_products} has made "
                 f"{infeasible['c']} planned distribution line(s) "
                 f"({infeasible['lb']:.0f} lb) infeasible. Approved recovery actions:\n"
                 f"{recovery_actions}\n\nAllocations by pantry are listed in the approved "
                 f"plan. {hold_instruction}"),
    })
    for ln in purchases:
        o = offers[ln["from_id"]]
        s = suppliers[o["supplier_id"]]
        out.append({
            "audience": "replacement_suppliers",
            "subject": f"Purchase request: {ln['quantity_lb']:.0f} lb {products[ln['product_id']]['name']}",
            "body": (f"To {s['name']}: requesting {ln['quantity_lb']:.0f} lb of "
                     f"{products[ln['product_id']]['name']} at ${ln['unit_cost_per_lb']:.2f}/lb, "
                     f"needed within {o['lead_time_days']} day(s). Reference plan {plan_id}."),
        })
    q_lines = "\n".join(
        f"- {l['lot_id']} ({l['quantity_lb']:.0f} lb, status {l['status']}; "
        f"{'confirmed recall control' if l['lot_id'] in confirmed_lot_ids else 'unconfirmed operator review'})"
        for l in quarantined) or "- none"
    out.append({
        "audience": "internal_ops",
        "subject": "Recall response: quarantine and review status",
        "body": (f"Lot-specific quarantine controls:\n{q_lines}\n\n"
                 f"{review_q} match(es) still need human review in the exposure queue. "
                 "Confirmed lots remain under the recall process; unconfirmed lots return "
                 "to the pool only through an explicit clearance action. Plan approval is "
                 "not food-safety clearance."),
    })
    for c in out:
        facts = {"audience": c["audience"], "subject": c["subject"],
                 "plan_id": plan_id, "template_draft": c["body"]}
        c["body"], c["method"] = narrate(f"comm:{c['audience']}", facts, c["body"],
                                         allow_llm=allow_llm)
        insert(conn, "comms", {"plan_id": plan_id, "audience": c["audience"],
                               "subject": c["subject"], "body": c["body"],
                               "method": c["method"], "created_at": now_iso()})
    conn.commit()
    return out


# ------------------------------------------------------------ reporting

def before_after(conn: sqlite3.Connection, baseline_id: str, recommended_id: str,
                 focus_category: str, runtime_s: float | None = None) -> dict:
    plans = {p["plan_id"]: p for p in rows(conn, "SELECT * FROM plans WHERE plan_id IN (?,?)",
                                           (baseline_id, recommended_id))}
    base = json.loads(plans[baseline_id]["metrics_json"])
    rec = json.loads(plans[recommended_id]["metrics_json"])
    dos_before = days_of_supply(project_supply(conn, "conservative"))
    dos_after = days_of_supply(project_supply(conn, "conservative", plan_id=recommended_id))
    return {
        "baseline": base,
        "recommended": rec,
        "focus_category": focus_category,
        "focus_dos_conservative_before": dos_before.get(focus_category),
        "focus_dos_conservative_after": dos_after.get(focus_category),
        "runtime_s": runtime_s,
    }
