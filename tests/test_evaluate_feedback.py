from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

import pytest

_module_path = Path(__file__).resolve().parents[1] / "tools" / "evaluate_feedback.py"
_spec = importlib.util.spec_from_file_location("bombamod_evaluate_feedback", _module_path)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
evaluate = _module.evaluate


def test_feedback_evaluator_reports_accuracy_and_matrix(tmp_path) -> None:
    path = tmp_path / "feedback.csv"
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["predicted_action", "corrected_action", "categories", "created_at_utc"],
        )
        writer.writeheader()
        writer.writerow({"predicted_action": "delete", "corrected_action": "delete"})
        writer.writerow({"predicted_action": "ban", "corrected_action": "escalate"})
    result = evaluate(path)
    assert "Cases: 2" in result
    assert "Action accuracy: 0.500" in result
    assert "actual\\predicted,ban,delete,escalate" in result
    assert "escalate,1,0,0" in result


def test_feedback_evaluator_rejects_empty_or_incomplete_file(tmp_path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("other,value\nx,y\n", encoding="utf-8")
    with pytest.raises(ValueError):
        evaluate(path)
