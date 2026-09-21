#!/usr/bin/env python3
"""Run delayed-feedback estimation on an exported JSON dataset."""

from __future__ import annotations

import argparse
import json

from shared.delayed_feedback_evaluator import estimate_performance


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    args = parser.parse_args()
    with open(args.input, encoding="utf-8") as handle:
        rows = json.load(handle)
    report = [item.__dict__ for item in estimate_performance(rows)]
    with open(args.output, "x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, default=str, sort_keys=True)


if __name__ == "__main__":
    main()
