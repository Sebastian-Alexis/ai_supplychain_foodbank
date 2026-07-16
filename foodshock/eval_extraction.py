"""Reproducible evaluation for the live recall-notice extractor.

Gold labels are frozen independently in ``data/eval/gold.json``. Predictions are
captured from the same prompt, Pydantic contract, and provenance verifier used
by the application, then bound to the exact gold-file SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import DATA_DIR
from .extraction import (
    LIVE_MODEL,
    _LIVE_PROMPT,
    _create_live_response,
    verify_provenance,
)
from .schemas import RecallExtraction

EVAL_DIR = DATA_DIR / "eval"
GOLD_PATH = EVAL_DIR / "gold.json"
PREDICTIONS_PATH = EVAL_DIR / "predictions.json"
RESULTS_PATH = EVAL_DIR / "results.json"
LIST_FIELDS = ("supplier_names", "lot_codes")
DATE_FIELDS = ("production_date_start", "production_date_end")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def generate_predictions(
    gold_path: Path = GOLD_PATH,
    predictions_path: Path = PREDICTIONS_PATH,
) -> dict[str, Any]:
    """Run the real live extraction path and persist raw plus verified outputs."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is required to generate evaluation predictions."
        )

    gold = _load_json(gold_path)
    notices = gold["notices"]
    predictions: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    for notice in notices:
        try:
            response = _create_live_response(notice["notice_text"])
            raw: RecallExtraction = response.parsed_output
            verified, dropped = verify_provenance(raw, notice["notice_text"])
            predictions.append(
                {
                    "id": notice["id"],
                    "response": {
                        "id": response.id,
                        "model": response.model,
                        "stop_reason": response.stop_reason,
                        "usage": _json_value(response.usage),
                    },
                    "raw_output": raw.model_dump(mode="json"),
                    "verified_output": verified.model_dump(mode="json"),
                    "dropped_fields": dropped,
                }
            )
        except Exception as exc:  # preserve partial-run evidence; scorer rejects it
            failures.append(
                {
                    "id": notice["id"],
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    artifact = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": "Anthropic API",
        "requested_model": LIVE_MODEL,
        "method": "foodshock.extraction._create_live_response + verify_provenance",
        "prompt_sha256": hashlib.sha256(_LIVE_PROMPT.encode()).hexdigest(),
        "gold_sha256": _sha256(gold_path),
        "expected_notice_count": len(notices),
        "completed_notice_count": len(predictions),
        "predictions": predictions,
        "failures": failures,
    }
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    predictions_path.write_text(json.dumps(artifact, indent=2) + "\n")
    return artifact


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _list_field_metrics(records: list[tuple[dict[str, Any], dict[str, Any]]], field: str) -> dict[str, Any]:
    tp = fp = fn = exact = 0
    for gold, prediction in records:
        gold_values = set(gold.get(field) or [])
        predicted_values = set(prediction.get(field) or [])
        tp += len(gold_values & predicted_values)
        fp += len(predicted_values - gold_values)
        fn += len(gold_values - predicted_values)
        exact += gold_values == predicted_values
    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "exact_match_count": exact,
        "exact_match_rate": exact / len(records),
    }


def _date_field_metrics(records: list[tuple[dict[str, Any], dict[str, Any]]], field: str) -> dict[str, Any]:
    tp = fp = fn = exact = 0
    unknown_gold = correct_unknown = spurious_on_unknown = missing_known = 0
    for gold, prediction in records:
        gold_value = gold.get(field)
        predicted_value = prediction.get(field)
        if gold_value is None:
            unknown_gold += 1
        if predicted_value == gold_value:
            exact += 1
            if gold_value is None:
                correct_unknown += 1
            else:
                tp += 1
        elif gold_value is None:
            fp += 1
            spurious_on_unknown += 1
        elif predicted_value is None:
            fn += 1
            missing_known += 1
        else:
            fp += 1
            fn += 1
    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "exact_match_count": exact,
        "exact_match_rate": exact / len(records),
        "unknown_gold_count": unknown_gold,
        "correct_unknown_count": correct_unknown,
        "spurious_value_on_unknown_count": spurious_on_unknown,
        "missing_value_on_known_count": missing_known,
        "unknown_accuracy": _ratio(correct_unknown, unknown_gold),
    }


def score_records(records: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    """Score verified predictions against gold labels for required fields."""
    if not records:
        raise ValueError("At least one gold/prediction record is required.")

    fields = {
        field: _list_field_metrics(records, field) for field in LIST_FIELDS
    }
    fields.update(
        {field: _date_field_metrics(records, field) for field in DATE_FIELDS}
    )

    all_exact = 0
    for gold, prediction in records:
        lists_exact = all(
            set(gold.get(field) or []) == set(prediction.get(field) or [])
            for field in LIST_FIELDS
        )
        dates_exact = all(gold.get(field) == prediction.get(field) for field in DATE_FIELDS)
        all_exact += lists_exact and dates_exact

    return {
        "notice_count": len(records),
        "fields": fields,
        "all_required_fields_exact_count": all_exact,
        "all_required_fields_exact_rate": all_exact / len(records),
    }


def score_predictions(
    gold_path: Path = GOLD_PATH,
    predictions_path: Path = PREDICTIONS_PATH,
) -> dict[str, Any]:
    """Validate a complete prediction artifact and score verified outputs."""
    gold = _load_json(gold_path)
    artifact = _load_json(predictions_path)
    actual_gold_sha = _sha256(gold_path)
    if artifact.get("gold_sha256") != actual_gold_sha:
        raise ValueError("Prediction artifact does not match the frozen gold SHA-256.")

    notices = gold["notices"]
    predictions = artifact.get("predictions", [])
    failures = artifact.get("failures", [])
    if failures or len(predictions) != len(notices):
        raise ValueError(
            "Prediction artifact is incomplete: "
            f"{len(predictions)}/{len(notices)} notices completed, "
            f"{len(failures)} failures."
        )

    prediction_by_id = {prediction["id"]: prediction for prediction in predictions}
    if len(prediction_by_id) != len(predictions):
        raise ValueError("Prediction artifact contains duplicate notice IDs.")
    expected_ids = {notice["id"] for notice in notices}
    if set(prediction_by_id) != expected_ids:
        raise ValueError("Prediction artifact notice IDs do not match the gold set.")

    records: list[tuple[dict[str, Any], dict[str, Any]]] = []
    dropped_by_notice: dict[str, list[str]] = {}
    response_models: set[str] = set()
    for notice in notices:
        prediction = prediction_by_id[notice["id"]]
        raw = RecallExtraction.model_validate(prediction["raw_output"])
        verified, dropped = verify_provenance(raw, notice["notice_text"])
        stored_verified = RecallExtraction.model_validate(
            prediction["verified_output"]
        )
        if verified != stored_verified or dropped != prediction["dropped_fields"]:
            raise ValueError(
                f"Stored verifier result is stale for notice {notice['id']}."
            )
        records.append((notice["gold"], verified.model_dump(mode="json")))
        dropped_by_notice[notice["id"]] = dropped
        response_models.add(prediction["response"]["model"])

    metrics = score_records(records)
    return {
        "schema_version": 1,
        "evaluation_kind": "live_extraction_accuracy",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "gold_sha256": actual_gold_sha,
        "prediction_generated_at": artifact["generated_at"],
        "provider": artifact["provider"],
        "requested_model": artifact["requested_model"],
        "response_models": sorted(response_models),
        "prompt_sha256": artifact["prompt_sha256"],
        "notice_count": metrics["notice_count"],
        "provenance_dropped_fields": dropped_by_notice,
        "fields": metrics["fields"],
        "all_required_fields_exact_count": metrics[
            "all_required_fields_exact_count"
        ],
        "all_required_fields_exact_rate": metrics[
            "all_required_fields_exact_rate"
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate", help="Call the live Anthropic extraction path."
    )
    generate.add_argument("--gold", type=Path, default=GOLD_PATH)
    generate.add_argument("--predictions", type=Path, default=PREDICTIONS_PATH)

    score = subparsers.add_parser(
        "score", help="Score a complete, SHA-bound prediction artifact."
    )
    score.add_argument("--gold", type=Path, default=GOLD_PATH)
    score.add_argument("--predictions", type=Path, default=PREDICTIONS_PATH)
    score.add_argument("--output", type=Path, default=RESULTS_PATH)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "generate":
        artifact = generate_predictions(args.gold, args.predictions)
        print(
            json.dumps(
                {
                    "gold_sha256": artifact["gold_sha256"],
                    "completed_notice_count": artifact[
                        "completed_notice_count"
                    ],
                    "expected_notice_count": artifact[
                        "expected_notice_count"
                    ],
                    "failure_count": len(artifact["failures"]),
                    "predictions_path": str(args.predictions),
                },
                indent=2,
            )
        )
        return

    results = score_predictions(args.gold, args.predictions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
