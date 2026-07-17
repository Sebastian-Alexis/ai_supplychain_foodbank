"""RecallResponseAgent loop (PLAN.md §9, §14): the full offline demo arc runs
with zero network, logs an ordered observe->investigate->explain->approve
transcript, routes unresolved matches to human-review gaps, and never
approves or drafts comms on its own."""

from __future__ import annotations

import json
import pytest

from foodshock.agent import RecallResponseAgent
from foodshock.datagen import generate
from foodshock.db import DATA_DIR, rows
from foodshock.demo_incidents import INCIDENTS, prepare_demo_incident
from foodshock.engine import BOX_LB, approve_plan, build_plans, incident_focus
from test_timing import _conn

EVENT_ID = "FDA-DEMO-2026-001"
PHASE_ORDER = ("observe", "investigate", "explain", "approve")


def _run_incident(key: str, conn=None, on_event=None):
    conn = conn or _conn()
    generate(conn)
    incident = INCIDENTS[key]
    prepare_demo_incident(conn, incident)
    agent = RecallResponseAgent(conn, allow_llm=False, on_event=on_event)
    return conn, agent.run(
        incident.notice_path.read_text(),
        event_id=incident.event_id,
        source_url=incident.source_url,
    )


def _run(conn=None):
    return _run_incident("onion_ecoli", conn=conn)


def test_full_offline_arc():
    conn, res = _run()
    assert 15 <= rows(conn, "SELECT COUNT(*) c FROM inventory_lots")[0]["c"] <= 30
    assert res.extraction_method == "cached-llm"  # zero-network replay (§14)
    assert res.extraction.authority == "FDA"
    assert res.match_counts.get("confirmed", 0) >= 1
    assert res.match_counts.get("probable", 0) + res.match_counts.get("possible", 0) >= 1
    assert res.propagation["quarantine_proposed_lb"] > 0
    assert res.propagation["pos_at_risk"] >= 1
    assert res.propagation["infeasible_lines"] == 28
    assert res.propagation["infeasible_lb"] == 700.0
    assert res.gaps, "unresolved matches must be routed to human review"
    assert res.narration
    assert res.narration_method in ("template", "cached-llm")  # honest labeling offline

    ev = rows(conn, "SELECT * FROM recall_events WHERE event_id=?", (EVENT_ID,))[0]
    assert ev["authority"] == "FDA"
    assert ev["extraction_json"]
    assert ev["extraction_method"] == "cached-llm"

    for plan_id in (res.baseline_id, res.recommended_id):
        p = rows(conn, "SELECT * FROM plans WHERE plan_id=?", (plan_id,))[0]
        assert p["status"] == "draft"  # the agent NEVER approves
        assert json.loads(p["metrics_json"])["hard_constraint_violations"] == 0
    assert rows(conn, "SELECT * FROM comms") == []  # comms only after approval


def test_transcript_structure():
    conn, res = _run()
    tr = rows(conn, "SELECT * FROM agent_transcript WHERE run_id=? ORDER BY seq", (res.run_id,))
    assert tr, "transcript must be written"
    assert [r["seq"] for r in tr] == list(range(1, len(tr) + 1))

    # Phases run in order and never interleave backwards.
    seen = [r["phase"] for r in tr]
    assert [p for p in PHASE_ORDER if p in seen] == list(dict.fromkeys(seen))
    last_rank = 0
    for p in seen:
        rank = PHASE_ORDER.index(p)
        assert rank >= last_rank, f"phase {p} after {PHASE_ORDER[last_rank]}"
        last_rank = rank

    # Every tool_call is answered by a tool_result of the same tool, in order.
    calls = [(r["seq"], r["name"]) for r in tr if r["kind"] == "tool_call"]
    results = [(r["seq"], r["name"]) for r in tr if r["kind"] == "tool_result"]
    assert len(calls) == len(results)
    for (cs, cn), (rs, rn) in zip(calls, results):
        assert cn == rn and rs > cs
    assert {"extract_notice", "resolve_entity", "propagate", "project_supply",
            "optimize_recovery", "before_after"} <= {n for _, n in calls}

    # Gap entries mirror the unresolved matches; narrations carry method labels.
    assert [r for r in tr if r["kind"] == "gap"]
    for r in tr:
        content = json.loads(r["content_json"])
        if r["kind"] == "narration":
            assert content["method"] in ("template", "cached-llm", "live-llm")
            assert content["text"]


def test_agent_determinism():
    _, res1 = _run()
    _, res2 = _run()
    assert res1.match_counts == res2.match_counts
    assert res1.before_after["baseline"] == res2.before_after["baseline"]
    assert res1.before_after["recommended"] == res2.before_after["recommended"]
    assert res1.days_of_supply == res2.days_of_supply
    assert res1.gaps == res2.gaps


def test_approval_drafts_comms_once_with_scoped_safety_copy():
    conn, res = _run()
    comms = approve_plan(conn, res.recommended_id, "operator-jane", allow_llm=False)
    assert {c["audience"] for c in comms} >= {"pantry_coordinators", "internal_ops"}
    stored = rows(conn, "SELECT * FROM comms")
    assert len(stored) == len(comms)
    assert all(c["method"] in ("template", "cached-llm") for c in stored)
    pantry_body = next(c["body"] for c in stored if c["audience"] == "pantry_coordinators")
    assert "L-GVP-1" in pantry_body
    assert "any onion inventory" not in pantry_body
    plan = rows(conn, "SELECT status, approved_by FROM plans WHERE plan_id=?",
                (res.recommended_id,))[0]
    assert plan["status"] == "approved" and plan["approved_by"] == "operator-jane"

    again = approve_plan(conn, res.recommended_id, "operator-other", allow_llm=False)
    assert len(again) == len(stored)
    assert len(rows(conn, "SELECT * FROM comms")) == len(stored)
    assert rows(conn, "SELECT COUNT(*) c FROM audit_log WHERE action='plan_approved'")[0]["c"] == 1
    unchanged = rows(conn, "SELECT approved_by FROM plans WHERE plan_id=?",
                     (res.recommended_id,))[0]
    assert unchanged["approved_by"] == "operator-jane"


def test_approval_rejects_unknown_baseline_and_stale_plan():
    conn, res = _run()
    with pytest.raises(ValueError, match="unknown plan"):
        approve_plan(conn, "PLAN-GHOST", "operator-jane", allow_llm=False)
    with pytest.raises(ValueError, match="recommended"):
        approve_plan(conn, res.baseline_id, "operator-jane", allow_llm=False)

    build_plans(conn)
    with pytest.raises(ValueError, match="superseded"):
        approve_plan(conn, res.recommended_id, "operator-jane", allow_llm=False)
    assert rows(conn, "SELECT status FROM plans WHERE plan_id=?",
                (res.recommended_id,))[0]["status"] == "draft"


def test_approval_re_evaluates_and_blocks_tampered_latest_plan():
    conn, res = _run()
    conn.execute("UPDATE plan_lines SET to_id='WH-SJ' "
                 "WHERE plan_id=? AND action='purchase'", (res.recommended_id,))
    conn.commit()

    with pytest.raises(ValueError, match="hard-constraint"):
        approve_plan(conn, res.recommended_id, "operator-jane", allow_llm=False)

    plan = rows(conn, "SELECT status FROM plans WHERE plan_id=?",
                (res.recommended_id,))[0]
    assert plan["status"] == "draft"
    assert rows(conn, "SELECT * FROM comms WHERE plan_id=?", (res.recommended_id,)) == []
    blocked = rows(conn, "SELECT detail_json FROM audit_log "
                         "WHERE action='plan_approval_blocked' ORDER BY id DESC LIMIT 1")
    assert blocked and json.loads(blocked[0]["detail_json"])["hard_constraint_violations"] > 0


@pytest.mark.parametrize(
    ("incident_key", "category", "dos_before", "dos_after"),
    [
        ("onion_ecoli", "produce", 4.8, None),
        ("chicken_salmonella", "protein", 0.0, 0.0),
        ("pasta_allergen", "grain", None, None),
    ],
)
def test_incident_catalog_uses_the_matching_supply_category(
    incident_key, category, dos_before, dos_after
):
    conn, res = _run_incident(incident_key)

    focus = incident_focus(conn, res.event_id)
    assert focus["category"] == category
    assert res.before_after["focus_category"] == category
    assert res.before_after["focus_dos_conservative_before"] == dos_before
    assert res.before_after["focus_dos_conservative_after"] == dos_after
    assert f"{category.title()} days of supply" in res.narration
    for metrics in (
        res.before_after["baseline"],
        res.before_after["recommended"],
    ):
        assert metrics["boxes_disrupted"] == int(
            round(metrics["unmet_demand_lb"] / BOX_LB)
        )


@pytest.mark.parametrize(
    ("incident_key", "action", "product_id", "note", "subject_product"),
    [
        (
            "chicken_salmonella",
            "purchase",
            "PROD-CHICKEN-FRZ",
            "Equivalent frozen protein from a verified alternate supplier",
            "Frozen chicken quarters",
        ),
        (
            "pasta_allergen",
            "transfer",
            "PROD-RICE",
            "inter-warehouse shuttle",
            "Dry pasta",
        ),
    ],
)
def test_alternate_incidents_produce_specific_recovery_and_comms(
    incident_key, action, product_id, note, subject_product
):
    conn, res = _run_incident(incident_key)
    comms = approve_plan(conn, res.recommended_id, "operator-jane", allow_llm=False)

    lines = rows(
        conn,
        "SELECT action, product_id, note FROM plan_lines WHERE plan_id=?",
        (res.recommended_id,),
    )
    assert any(
        line["action"] == action
        and line["product_id"] == product_id
        and line["note"] == note
        for line in lines
    )
    pantry = next(c for c in comms if c["audience"] == "pantry_coordinators")
    assert subject_product in pantry["subject"]
    assert "onion" not in f"{pantry['subject']} {pantry['body']}".lower()
    if incident_key == "pasta_allergen":
        assert "Reroute 1827 lb White rice" in pantry["body"]


def test_event_callback_covers_replay_stages_without_inflating_runtime(monkeypatch):
    clock = [0.0]
    events = []

    def fake_counter():
        return clock[0]

    def observe(event):
        events.append((event["seq"], event["phase"], event["kind"], event["name"]))
        clock[0] += 1.0

    monkeypatch.setattr("foodshock.agent.time.perf_counter", fake_counter)
    conn, res = _run_incident("onion_ecoli", on_event=observe)

    required = {
        ("observe", "tool_result", "ingest_notice"),
        ("investigate", "tool_result", "extract_notice"),
        ("investigate", "tool_result", "resolve_entity"),
        ("investigate", "tool_result", "propagate"),
        ("explain", "tool_result", "project_supply"),
        ("explain", "tool_result", "optimize_recovery"),
        ("approve", "narration", "request_approval"),
    }
    assert required <= {(phase, kind, name) for _, phase, kind, name in events}
    assert [seq for seq, *_ in events] == list(range(1, len(events) + 1))
    assert len(events) == rows(
        conn,
        "SELECT COUNT(*) c FROM agent_transcript WHERE run_id=?",
        (res.run_id,),
    )[0]["c"]
    assert res.runtime_s == 0.0


@pytest.mark.parametrize(
    ("incident_key", "action", "product_id"),
    [
        ("chicken_salmonella", "purchase", "PROD-CHICKEN-FRZ"),
        ("pasta_allergen", "transfer", "PROD-RICE"),
    ],
)
def test_solver_fallback_recovers_nonproduce_incidents(
    monkeypatch, incident_key, action, product_id
):
    def unavailable_solver(*_args):
        return {}, {}, {}, "forced-test-failure"

    monkeypatch.setattr("foodshock.engine._solve_lp", unavailable_solver)
    conn, res = _run_incident(incident_key)

    plan = rows(
        conn,
        "SELECT method, metrics_json FROM plans WHERE plan_id=?",
        (res.recommended_id,),
    )[0]
    assert plan["method"] == "greedy-fallback"
    assert json.loads(plan["metrics_json"])["hard_constraint_violations"] == 0
    lines = rows(
        conn,
        "SELECT action, product_id FROM plan_lines WHERE plan_id=?",
        (res.recommended_id,),
    )
    assert any(
        line["action"] == action and line["product_id"] == product_id
        for line in lines
    )
    fallback = rows(
        conn,
        "SELECT detail_json FROM audit_log WHERE action='lp_fallback'",
    )
    assert len(fallback) == 1
    assert json.loads(fallback[0]["detail_json"])["status"] == "forced-test-failure"
