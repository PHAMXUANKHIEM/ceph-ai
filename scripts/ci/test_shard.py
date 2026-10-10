"""Print the test files of one CI shard, balanced by recorded run time.

CI runs the suite as SHARDS parallel jobs per Python version
(.github/workflows/ci-cd.yml); one serial job took ~30 minutes. Files are
dealt largest first to the shard with the least time so far, using
scripts/ci/test_durations.json (seconds per file from a CI junit report).
A file missing from it counts as the median, so new tests are still run;
refresh the JSON when the balance drifts.

    pytest $(python scripts/ci/test_shard.py --index 1 --total 3)
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DURATIONS = Path(__file__).with_name("test_durations.json")


def test_files(root: Path = ROOT) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in (root / "tests").rglob("test_*.py"))


def shards(files: list[str], durations: dict[str, float], total: int) -> list[list[str]]:
    """Deal every file to exactly one of ``total`` shards, longest first, to the lightest shard."""
    default = statistics.median(durations.values()) if durations else 1.0
    loads = [0.0] * total
    assigned: list[list[str]] = [[] for _ in range(total)]
    for name in sorted(files, key=lambda item: (-durations.get(item, default), item)):
        lightest = min(range(total), key=lambda index: (loads[index], index))
        assigned[lightest].append(name)
        loads[lightest] += durations.get(name, default)
    return [sorted(items) for items in assigned]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, required=True, help="1-based shard number")
    parser.add_argument("--total", type=int, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.index <= args.total:
        parser.error("--index must be between 1 and --total")
    durations = json.loads(DURATIONS.read_text(encoding="utf-8"))
    print("\n".join(shards(test_files(), durations, args.total)[args.index - 1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
