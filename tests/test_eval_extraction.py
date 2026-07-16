from foodshock.eval_extraction import score_records


def test_score_records_reports_per_field_precision_recall_and_unknowns():
    records = [
        (
            {
                "supplier_names": ["Supplier A"],
                "lot_codes": ["L1", "L2"],
                "production_date_start": "2024-01-01",
                "production_date_end": "2024-01-01",
            },
            {
                "supplier_names": ["Supplier A"],
                "lot_codes": ["L1", "L3"],
                "production_date_start": "2024-01-01",
                "production_date_end": "2024-01-01",
            },
        ),
        (
            {
                "supplier_names": ["Supplier B"],
                "lot_codes": [],
                "production_date_start": None,
                "production_date_end": None,
            },
            {
                "supplier_names": ["Supplier C"],
                "lot_codes": [],
                "production_date_start": "2024-02-01",
                "production_date_end": None,
            },
        ),
    ]

    result = score_records(records)

    suppliers = result["fields"]["supplier_names"]
    assert suppliers == {
        "true_positives": 1,
        "false_positives": 1,
        "false_negatives": 1,
        "precision": 0.5,
        "recall": 0.5,
        "exact_match_count": 1,
        "exact_match_rate": 0.5,
    }

    lots = result["fields"]["lot_codes"]
    assert lots["true_positives"] == 1
    assert lots["false_positives"] == 1
    assert lots["false_negatives"] == 1
    assert lots["precision"] == 0.5
    assert lots["recall"] == 0.5
    assert lots["exact_match_rate"] == 0.5

    start = result["fields"]["production_date_start"]
    assert start["precision"] == 0.5
    assert start["recall"] == 1.0
    assert start["unknown_gold_count"] == 1
    assert start["correct_unknown_count"] == 0
    assert start["spurious_value_on_unknown_count"] == 1
    assert start["unknown_accuracy"] == 0.0

    end = result["fields"]["production_date_end"]
    assert end["precision"] == 1.0
    assert end["recall"] == 1.0
    assert end["exact_match_rate"] == 1.0
    assert end["correct_unknown_count"] == 1
    assert end["unknown_accuracy"] == 1.0

    assert result["notice_count"] == 2
    assert result["all_required_fields_exact_count"] == 0
    assert result["all_required_fields_exact_rate"] == 0.0


def test_wrong_and_missing_known_dates_count_as_false_negatives():
    records = [
        (
            {
                "supplier_names": [],
                "lot_codes": [],
                "production_date_start": "2024-03-01",
                "production_date_end": "2024-03-02",
            },
            {
                "supplier_names": [],
                "lot_codes": [],
                "production_date_start": "2024-03-03",
                "production_date_end": None,
            },
        )
    ]

    result = score_records(records)
    start = result["fields"]["production_date_start"]
    end = result["fields"]["production_date_end"]

    assert (start["true_positives"], start["false_positives"], start["false_negatives"]) == (0, 1, 1)
    assert start["precision"] == 0.0
    assert start["recall"] == 0.0
    assert (end["true_positives"], end["false_positives"], end["false_negatives"]) == (0, 0, 1)
    assert end["missing_value_on_known_count"] == 1
    assert end["precision"] is None
    assert end["recall"] == 0.0
