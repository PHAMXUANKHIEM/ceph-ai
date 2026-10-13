"""Print read-only Failure Lab learning metrics by fault family (FL6.5).

Only completed, six-stage-scored lab proposals count. This is not a measure
of production accuracy or evidence that the model retrained itself.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from shared.failure_lab_learning_report import summarize
from shared.reproduction_approval import PROPOSALS_DIR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=PROPOSALS_DIR)
    parser.add_argument("--family", help="optional exact fault family")
    args = parser.parse_args(argv)
    print(json.dumps(summarize(args.directory, family=args.family), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
