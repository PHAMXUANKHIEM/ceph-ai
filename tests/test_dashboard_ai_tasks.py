import json

import pytest

import dashboard.routes.ai_tasks as ai_tasks


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def test_unauthenticated_ai_tasks_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/ai-tasks", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_ai_tasks_page_exposes_prompt_and_account_choices(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/ai-tasks")
    assert response.status_code == 200
    assert "AI Development Task" in response.text
    assert "Tài khoản đã cấu hình" in response.text
    assert "Profile tài khoản riêng" in response.text
    assert 'name="planner_account_source"' in response.text
    assert 'name="implementer_account_source"' in response.text


def test_create_ai_task_stores_profile_selection_and_spawns_worker(
    dashboard_client, monkeypatch, tmp_path,
):
    class FakeProcess:
        pid = 12345

    monkeypatch.setattr(ai_tasks, "TASK_ROOT", tmp_path / "ai-tasks")
    monkeypatch.setattr(ai_tasks.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    _login(dashboard_client)

    response = dashboard_client.post(
        "/ai-tasks",
        data={
            "prompt": "Thêm một tool kiểm tra health và viết regression test.",
            "planner_provider": "codex",
            "planner_model": "gpt-5-codex",
            "planner_account_source": "separate",
            "planner_account_profile": "planner-one",
            "implementer_provider": "claude",
            "implementer_model": "claude-sonnet-4-6",
            "implementer_account_source": "configured",
            "max_review_rounds": "2",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    task_id = response.headers["location"].rsplit("/", 1)[-1]
    metadata = json.loads((tmp_path / "ai-tasks" / task_id / "task.json").read_text())
    assert metadata["status"] == "RUNNING"
    assert metadata["planner_account_profile"] == "planner-one"
    assert metadata["implementer_account_profile"] == "configured"
    assert metadata["pid"] == 12345


def test_profile_name_rejects_path_traversal(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setattr(ai_tasks, "TASK_ROOT", tmp_path / "ai-tasks")
    _login(dashboard_client)
    response = dashboard_client.post(
        "/ai-tasks",
        data={
            "prompt": "test",
            "planner_account_source": "separate",
            "planner_account_profile": "../escape",
            "implementer_account_source": "configured",
        },
    )
    assert response.status_code == 400
