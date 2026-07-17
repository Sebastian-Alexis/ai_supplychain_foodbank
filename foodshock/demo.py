"""Demo runner (PLAN.md §14): reset the scenario DB, replay the canned E. coli
onion recall through the RecallResponseAgent, and print the transcript with
before/after numbers. Zero network by default (cached extraction, template
narration); the Streamlit app then reads the same SQLite file.

Usage:
    python -m foodshock.demo [--db data/foodshock.db] [--live-llm]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import RecallResponseAgent
from .datagen import generate
from .db import DATA_DIR, DEFAULT_DB, get_conn, rows

EVENT_ID = "FDA-DEMO-2026-001"
NOTICE = DATA_DIR / "notice_ecoli_onions.txt"


def _fmt_dos(v: float | None) -> str:
    return "7+" if v is None else f"{v:g}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None,
                    help=f"SQLite path (default: $FOODSHOCK_DB or {DEFAULT_DB})")
    ap.add_argument("--live-llm", action="store_true",
                    help="allow live Anthropic calls for uncached extraction/narration "
                         "(default: cached + template only, zero network)")
    args = ap.parse_args(argv)

    db_path = Path(args.db or os.environ.get("FOODSHOCK_DB", DEFAULT_DB))
    if db_path.exists():
        db_path.unlink()  # demo runner always starts from the seeded scenario
    conn = get_conn(db_path)
    generate(conn)
    print(f"[demo] scenario loaded into {db_path} "
          f"({rows(conn, 'SELECT COUNT(*) c FROM inventory_lots')[0]['c']} lots, "
          f"{rows(conn, 'SELECT COUNT(*) c FROM purchase_orders')[0]['c']} POs, "
          f"{rows(conn, 'SELECT COUNT(*) c FROM pantries')[0]['c']} pantries)")

    raw = NOTICE.read_text()
    agent = RecallResponseAgent(conn, allow_llm=args.live_llm)
    res = agent.run(raw, event_id=EVENT_ID, source_url="https://example.invalid/recall")

    print(f"\n[transcript] run {res.run_id}")
    for t in rows(conn, "SELECT * FROM agent_transcript WHERE run_id=? ORDER BY seq", (res.run_id,)):
        c = json.loads(t["content_json"])
        if t["kind"] == "tool_call":
            print(f"  {t['seq']:>3} {t['phase']:<11} -> {t['name']}({json.dumps(c['args'])})")
        elif t["kind"] == "tool_result":
            n = c["result"].get("rows") if isinstance(c["result"], dict) else None
            print(f"  {t['seq']:>3} {t['phase']:<11} <- {t['name']}"
                  + (f": {n} row(s)" if n is not None else ""))
        elif t["kind"] == "gap":
            print(f"  {t['seq']:>3} {t['phase']:<11} ?? {c['question']}")
        else:
            print(f"  {t['seq']:>3} {t['phase']:<11} :: [{c['method']}] {c['text'][:110]}")

    ba = res.before_after
    base, rec = ba["baseline"], ba["recommended"]
    print(f"\n[extraction] method={res.extraction_method} "
          f"confidence={res.extraction.confidence:g} dropped={res.dropped_fields or 'none'}")
    print(f"[matches] {res.match_counts}")
    print(f"[impact] quarantine proposed {res.propagation['quarantine_proposed_lb']:g} lb · "
          f"review {res.propagation['review_lb']:g} lb · "
          f"{res.propagation['pos_at_risk']} PO(s) at risk · "
          f"{res.propagation['infeasible_lines']} infeasible distribution line(s)")
    focus_category = ba["focus_category"]
    focus_label = focus_category.replace("_", " ")
    print(f"[{focus_label} days of supply] conservative "
          f"{_fmt_dos(ba['focus_dos_conservative_before'])} "
          f"-> {_fmt_dos(ba['focus_dos_conservative_after'])} with plan "
          f"(optimistic {_fmt_dos(res.days_of_supply['optimistic'].get(focus_category))})")
    print(f"[plans] baseline {res.baseline_id}: served {base['served_lb']:g} lb, "
          f"unmet {base['unmet_demand_lb']:g} lb, ${base['procurement_cost']:g}")
    print(f"[plans] recommended {res.recommended_id}: served {rec['served_lb']:g} lb, "
          f"unmet {rec['unmet_demand_lb']:g} lb, ${rec['procurement_cost']:g}, "
          f"{rec['hard_constraint_violations']} hard-constraint violation(s)")
    print(f"[agent runtime] {res.runtime_s:g} s (comparator: 2.0 staff-hours from a "
          "hypothetical task model; not operator-validated)")
    print(f"[gaps] {len(res.gaps)} open review question(s) routed to the exposure queue")
    print(f"\n[next] plans are DRAFTS; approve in the app:  streamlit run streamlit_app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
