"""Live SSH storage backend test — makes a real SFTP connection to
`backup_target_a` (slot "a"). Excluded by default (see pyproject.toml's
`addopts = "-m 'not live'"`); run explicitly with `pytest -m live`.

Skips gracefully if `backup_target_a` isn't configured with `transport=ssh`
— there is no real site-2 host configured yet (PRD Open Question #1), so
this test is a placeholder until an operator provides one.
"""

import io
import uuid

import pytest

from config.settings import settings
from worker.backup.storage.factory import get_backend

pytestmark = pytest.mark.live


def _ssh_target_a_configured() -> bool:
    return settings.backup_target_a_transport == "ssh" and bool(settings.backup_target_a_ssh_host)


@pytest.mark.skipif(not _ssh_target_a_configured(), reason="backup_target_a is not configured as an ssh target")
def test_live_ssh_backend_upload_verify_delete_roundtrip():
    backend = get_backend("a", settings)
    key = f"live-test/{uuid.uuid4()}.bin"
    data = b"live ssh backend smoke test payload"

    result = backend.upload(io.BytesIO(data), key)

    assert backend.verify(key, result.size, result.sha256) is True

    backend.delete(key)
