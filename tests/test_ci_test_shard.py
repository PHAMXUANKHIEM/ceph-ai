import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "ci"))

import test_shard  # noqa: E402


def test_every_test_file_runs_in_exactly_one_shard():
    files = test_shard.test_files()
    durations = json.loads(test_shard.DURATIONS.read_text(encoding="utf-8"))

    parts = test_shard.shards(files, durations, 3)

    assigned = [name for part in parts for name in part]
    assert sorted(assigned) == files and len(set(assigned)) == len(files)
    assert "tests/test_ci_test_shard.py" in assigned


def test_shards_are_balanced_by_recorded_time_and_unknown_files_count_as_the_median():
    durations = {"a": 100.0, "b": 60.0, "c": 50.0, "d": 10.0}

    parts = test_shard.shards(["a", "b", "c", "d", "new"], durations, 2)

    # largest first to the lightest shard: a; b; new (median 55 s); c; d -> 150 s vs 125 s
    assert parts == [["a", "c"], ["b", "d", "new"]]


def test_the_cli_prints_one_shard(capsys):
    assert test_shard.main(["--index", "2", "--total", "3"]) == 0
    printed = capsys.readouterr().out.split()
    assert printed and all(name.startswith("tests/") for name in printed)
