#!/usr/bin/env python3
"""Score captured Failure Lab replay outcomes without contacting Ceph.

Input is a JSON document with ``schema_version``, ``campaign_id`` and a
``runs`` array. Replay runs contain ``scenario_id`` and their observed
outcome; no-fault controls use ``kind: "control"`` and record whether an
incident was created, detected, or acted on.

Usage::

    python -m scripts.lab.score_replay_report campaign.json
    cat campaign.json | python -m scripts.lab.score_replay_report -
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from shared.synthetic_incidents import SyntheticInjectionError, score_replay_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="campaign JSON file, or '-' to read stdin")
    args = parser.parse_args(argv)

    try:
        if args.input == "-":
            document = json.load(sys.stdin)
        else:
            document = json.loads(Path(args.input).read_text(encoding="utf-8"))
        report = score_replay_report(document)
    except (OSError, json.JSONDecodeError, SyntheticInjectionError) as exc:
        print(f"replay report error: {exc}", file=sys.stderr)
        return 2

    json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
