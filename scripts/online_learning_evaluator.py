#!/usr/bin/env python3
"""Run the read-only online-learning evaluator on a JSON row export."""

from __future__ import annotations

import argparse
import json

from shared.online_learning_evaluator import evaluate_rows, write_artifact


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="JSON array of forecast/audit rows")
    parser.add_argument("output", help="new JSON report path; must not exist")
    args = parser.parse_args()
    with open(args.input, encoding="utf-8") as handle:
        rows = json.load(handle)
    if not isinstance(rows, list):
        parser.error("input must be a JSON array")
    write_artifact(evaluate_rows(rows), args.output)


if __name__ == "__main__":
    main()
