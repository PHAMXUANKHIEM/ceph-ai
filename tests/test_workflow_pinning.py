"""Supply-chain guard: third-party CI code and images are immutable refs."""

import re
from pathlib import Path


WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))


def test_workflows_exist():
    assert WORKFLOWS


def test_every_action_is_pinned_to_a_full_commit_sha():
    unpinned = []
    for workflow in WORKFLOWS:
        for number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            match = re.search(r"^\s*(?:-\s+)?uses:\s*(\S+)", line)
            if not match or match.group(1).startswith("./"):
                continue
            if not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", match.group(1)):
                unpinned.append(f"{workflow.name}:{number}: {match.group(1)}")
    assert unpinned == []


def test_service_and_tool_images_are_pinned_by_digest():
    unpinned = []
    for workflow in WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        references = re.findall(r"^\s*image:\s*(\S+)", text, flags=re.M)
        references += re.findall(r"^\s*((?:aquasec|anchore)/\S+)", text, flags=re.M)
        for reference in references:
            if not re.search(r"@sha256:[0-9a-f]{64}$", reference):
                unpinned.append(f"{workflow.name}: {reference}")
    assert unpinned == []
