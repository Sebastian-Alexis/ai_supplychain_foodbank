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
from foodshock.engine import approve_plan, build_plans
from test_timing import _conn

EVENT_ID = "FDA-DEMO-2026-001"
PHASE_ORDER = ("observe", "investigate", "explain", "approve")


def _run(conn=None):
    conn = conn or _conn()
    generate(conn)
    raw = (DATA_DIR / "notice_ecoli_onions.txt").read_text()
    agent = RecallResponseAgent(conn, allow_llm=False)
    return conn, agent.run(raw, event_id=EVENT_ID,
                           source_url="https://example.invalid/recall")


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
