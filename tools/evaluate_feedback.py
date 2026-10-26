#!/usr/bin/env python3
"""Evaluate moderator correction labels exported by `/bm export-feedback`.

Usage: python tools/evaluate_feedback.py feedback.csv

Input has predicted_action, corrected_action, categories, and created_at_utc columns.
The tool reports per-action precision/recall/F1 and a confusion matrix without
requiring or transmitting any API credentials or message text.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path


def evaluate(path: Path) -> str:
    with path.open(newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))

    required = {"predicted_action", "corrected_action"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("CSV must contain predicted_action and corrected_action columns")

    labels = sorted(
        {
            value.strip()
            for row in rows
            for value in (row["predicted_action"], row["corrected_action"])
            if value and value.strip()
        }
    )
    if not labels:
        raise ValueError("CSV contains no action labels")

    matrix: Counter[tuple[str, str]] = Counter()
    correct = 0
    for row in rows:
        predicted = row["predicted_action"].strip()
        actual = row["corrected_action"].strip()
        if not predicted or not actual:
            raise ValueError("Action labels cannot be empty")
        matrix[(actual, predicted)] += 1
        correct += predicted == actual

    output = [f"Cases: {len(rows)}", f"Action accuracy: {correct / len(rows):.3f}", ""]
    output.append("Per-action metrics (moderator correction is reference):")
    output.append("action             precision  recall     f1       support")
    for label in labels:
        true_positive = matrix[(label, label)]
        predicted_total = sum(matrix[(actual, label)] for actual in labels)
        actual_total = sum(matrix[(label, predicted)] for predicted in labels)
        precision = true_positive / predicted_total if predicted_total else 0.0
        recall = true_positive / actual_total if actual_total else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        output.append(
            f"{label:<18} {precision:>9.3f}  {recall:>7.3f}  {f1:>7.3f}  {actual_total:>10}"
        )

    output.extend(["", "Confusion matrix (rows=moderator label, columns=bot action):"])
    output.append("actual\\predicted," + ",".join(labels))
    for actual in labels:
        output.append(
            actual + "," + ",".join(str(matrix[(actual, predicted)]) for predicted in labels)
        )
    output.extend(
        [
            "",
            "Use this as a review aid, not a quality guarantee. Stratify by server/category and",
            "inspect false positives/negatives before changing thresholds or enabling actions.",
        ]
    )
    return "\n".join(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", type=Path, help="Private CSV exported by a server moderator")
    args = parser.parse_args()
    try:
        print(evaluate(args.csv_file))
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
