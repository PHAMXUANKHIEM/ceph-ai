"""PR release flow: candidate branches on /test-progress (shared/release_candidates.py)."""

import json

import httpx
import pytest

from config.settings import settings
from dashboard.routes import test_progress as route
from shared import ci_control, release_candidates as rc

REPO = "org/ceph-ai"
SHA_A, SHA_B = "a" * 40, "b" * 40
TOKEN = "github_pat_" + "A1" * 20


def _commit(sha, message):
    return {"sha": sha, "commit": {"message": message, "committer": {"date": "2026-10-07T03:00:00Z"}}}


class FakeGitHub:
    """Two candidate branches; cand/a has a PR with green CI, cand/b has no PR."""

    def __init__(self, *, ci_a="success", mergeable=True):
        self.ci_a, self.mergeable = ci_a, mergeable
        self.calls = []

    def __call__(self, request):
        path, method = request.url.path, request.method
        self.calls.append((method, path, json.loads(request.content) if request.content else None))
        repo = f"/repos/{REPO}"
        if path == f"{repo}/branches":
            return httpx.Response(200, json=[{"name": "main", "commit": {"sha": "c" * 40}},
                                             {"name": "cand/a", "commit": {"sha": SHA_A}},
                                             {"name": "cand/b", "commit": {"sha": SHA_B}},
                                             {"name": "feature/x", "commit": {"sha": "d" * 40}}])
        if path == f"{repo}/pulls" and method == "GET":
            return httpx.Response(200, json=[{"number": 7, "html_url": "https://pr/7", "head": {"ref": "cand/a"}}])
        if path == f"{repo}/pulls" and method == "POST":
            return httpx.Response(201, json={"number": 8, "html_url": "https://pr/8"})
        if path == f"{repo}/pulls/7" and method == "GET":
            return httpx.Response(200, json={"mergeable": self.mergeable, "mergeable_state": "clean"})
        if path == f"{repo}/pulls/7/merge":
            return httpx.Response(200, json={"sha": "e" * 40, "merged": True})
        if path.startswith(f"{repo}/git/refs/heads/"):
            return httpx.Response(204)
        if path.startswith(f"{repo}/compare/main..."):
            branch = path.rsplit("...", 1)[1]
            if request.headers.get("accept") == "application/vnd.github.diff":
                return httpx.Response(200, text="diff --git a/x b/x\n+password = hunter2\n")
            sha = SHA_A if branch == "cand/a" else SHA_B
            return httpx.Response(200, json={
                "ahead_by": 1, "behind_by": 0, "commits": [_commit(sha, f"feat: {branch}\n\nchi tiết {branch}")],
                "files": [{"filename": "shared/x.py", "status": "modified", "additions": 3, "deletions": 1}]})
        if path == f"{repo}/actions/runs":
            head = request.url.params.get("head_sha")
            if head == SHA_A:
                return httpx.Response(200, json={"workflow_runs": [
                    {"status": "completed", "conclusion": self.ci_a, "html_url": "https://ci/a"}]})
            return httpx.Response(200, json={"workflow_runs": []})
        return httpx.Response(404, json={"message": f"unexpected {method} {path}"})


def _client(fake):
    return httpx.Client(transport=httpx.MockTransport(fake))


def test_only_cand_branches_are_listed_with_pr_ci_and_cached_summary(tmp_path):
    rc.clear_cache()
    rc.write_summary(tmp_path, SHA_A, "- Thêm tính năng A", source="ai")

    result = rc.list_candidates(REPO, TOKEN, tmp_path, client=_client(FakeGitHub()), use_cache=False)

    by_branch = {item["branch"]: item for item in result["candidates"]}
    assert set(by_branch) == {"cand/a", "cand/b"} and result["error"] is None
    assert by_branch["cand/a"]["pull"]["number"] == 7 and by_branch["cand/a"]["ci"]["conclusion"] == "success"
    assert by_branch["cand/a"]["summary"]["text"] == "- Thêm tính năng A"
    assert by_branch["cand/b"]["pull"] is None and by_branch["cand/b"]["summary"] is None


def test_the_summary_prompt_redacts_secrets_and_names_what_to_cover(tmp_path):
    rc.clear_cache()
    candidate = rc.list_candidates(REPO, TOKEN, tmp_path, client=_client(FakeGitHub()),
                                   use_cache=False)["candidates"][0]
    patch = rc.fetch_patch(REPO, candidate["branch"], TOKEN, client=_client(FakeGitHub()))

    prompt = rc.summary_prompt(candidate, patch)

    assert "hunter2" not in prompt and "[REDACTED]" in prompt
    assert "thêm hoặc sửa tính năng gì" in prompt and candidate["branch"] in prompt


def test_merge_is_a_squash_pinned_to_the_reviewed_commit_and_deletes_the_branch():
    fake = FakeGitHub()

    rc.merge_pull_request(REPO, TOKEN, number=7, head_sha=SHA_A, branch="cand/a", title="feat: a",
                          client=_client(fake))

    merge = next(body for method, path, body in fake.calls if path.endswith("/pulls/7/merge"))
    assert merge == {"merge_method": "squash", "sha": SHA_A, "commit_title": "feat: a (#7)"}
    assert ("DELETE", f"/repos/{REPO}/git/refs/heads/cand/a", None) in fake.calls
    with pytest.raises(rc.ReleaseError):
        rc.merge_pull_request(REPO, TOKEN, number=7, head_sha=SHA_A, branch="main", title="x", client=_client(fake))


def test_a_token_without_write_access_gets_a_clear_message():
    def forbidden(request):
        return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})

    with pytest.raises(rc.ReleaseError, match="Contents, Pull requests và Actions"):
        rc.merge_pull_request(REPO, TOKEN, number=7, head_sha=SHA_A, branch="cand/a", title="x",
                              client=_client(forbidden))


# --- routes -----------------------------------------------------------------------------

@pytest.fixture
def admin(dashboard_client, monkeypatch, tmp_path):
    fake = FakeGitHub()
    monkeypatch.setattr(settings, "ci_github_repo", REPO)
    monkeypatch.setattr(settings, "ci_github_token_file", str(tmp_path / "token"))
    monkeypatch.setattr(settings, "release_notes_dir", str(tmp_path / "notes"))
    ci_control.save_token(tmp_path / "token", TOKEN)
    real_client = rc._client
    monkeypatch.setattr(rc, "_client", lambda client: real_client(client or _client(fake)))
    monkeypatch.setattr(rc, "fetch_patch", lambda repo, branch, token: "diff")
    rc.clear_cache()
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    dashboard_client.fake = fake
    return dashboard_client


def test_the_api_lists_candidates_and_starts_missing_summaries(admin, monkeypatch):
    async def ai(prompt, timeout):
        return "- tóm tắt"

    monkeypatch.setattr("shared.claude_cli.run_claude_prompt", ai)

    data = admin.get("/api/test-progress/candidates").json()

    assert {item["branch"] for item in data["candidates"]} == {"cand/a", "cand/b"} and data["token_configured"]


def test_merging_needs_the_typed_count_and_green_ci(admin):
    pick = f"7:{SHA_A}"
    wrong = admin.post("/test-progress/candidates/merge", data={"pick": pick, "confirmation": "MERGE 2"},
                       follow_redirects=False)
    assert "err=" in wrong.headers["location"]

    ok = admin.post("/test-progress/candidates/merge", data={"pick": pick, "confirmation": "MERGE 1"},
                    follow_redirects=False)
    assert "ok=" in ok.headers["location"]
    assert any(path.endswith("/pulls/7/merge") for _m, path, _b in admin.fake.calls)


def test_a_pr_whose_ci_is_not_green_or_whose_head_moved_is_not_merged(admin):
    admin.fake.ci_a = "failure"
    response = admin.post("/test-progress/candidates/merge", data={"pick": f"7:{SHA_A}", "confirmation": "MERGE 1"},
                          follow_redirects=False)
    assert "err=" in response.headers["location"]
    moved = admin.post("/test-progress/candidates/merge", data={"pick": f"7:{'f' * 40}", "confirmation": "MERGE 1"},
                       follow_redirects=False)
    assert "err=" in moved.headers["location"]
    assert not any(path.endswith("/merge") for _m, path, _b in admin.fake.calls)


def test_open_prs_opens_one_for_each_branch_without_a_pr(admin):
    response = admin.post("/test-progress/candidates/open-prs", follow_redirects=False)

    assert "ok=" in response.headers["location"]
    opened = [body for method, path, body in admin.fake.calls if method == "POST" and path.endswith("/pulls")]
    assert [body["head"] for body in opened] == ["cand/b"] and opened[0]["base"] == "main"


def test_candidate_routes_are_admin_only(admin, monkeypatch):
    monkeypatch.setattr(route.auth, "is_admin_user", lambda _user: False)
    assert admin.get("/api/test-progress/candidates").status_code == 403
    assert admin.post("/test-progress/candidates/merge", data={"pick": "7:x", "confirmation": "MERGE 1"},
                      follow_redirects=False).status_code == 403
