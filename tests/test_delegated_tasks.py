import asyncio

import worker.delegated_tasks as delegated_tasks
from shared.request_context import REQUEST_ID_HEADER, get_request_id


class _Message:
    body = b'{"task_id":"task-1"}'
    headers = {REQUEST_ID_HEADER: "delegated-trace"}

    def __init__(self):
        self.ack_calls = 0
        self.reject_calls = []

    async def ack(self):
        self.ack_calls += 1

    async def reject(self, requeue=False):
        self.reject_calls.append(requeue)


def test_delegated_worker_restores_request_correlation(monkeypatch):
    message = _Message()
    seen = []

    async def execute(task_id):
        seen.append((task_id, get_request_id()))

    monkeypatch.setattr(delegated_tasks, "execute_task", execute)
    asyncio.run(delegated_tasks._process_message(message))

    assert seen == [("task-1", "delegated-trace")]
    assert message.ack_calls == 1
    assert message.reject_calls == []
