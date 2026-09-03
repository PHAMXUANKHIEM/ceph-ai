import io

import pytest

import worker.backup.storage.ssh_backend as ssh_backend
from worker.backup.storage.base import RetentionPolicyLike
from worker.backup.storage.ssh_backend import SSHStorageBackend, SSHStorageBackendError


class _FakeFileAttr:
    def __init__(self, filename, size, mtime, is_dir=False):
        self.filename = filename
        self.st_size = size
        self.st_mtime = mtime
        self.st_mode = 0o040000 if is_dir else 0o100000


class _FakeRemoteFile:
    def __init__(self, fs, path, mode):
        self._fs = fs
        self._path = path
        self._mode = mode
        self._buffer = bytearray()
        self._read_pos = 0

    def set_pipelined(self, value):
        pass

    def write(self, data):
        self._buffer.extend(data)

    def read(self, size=-1):
        content = self._fs.files.get(self._path, b"")
        chunk = content[self._read_pos : self._read_pos + size] if size != -1 else content[self._read_pos :]
        self._read_pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if "w" in self._mode:
            self._fs.files[self._path] = bytes(self._buffer)
            self._fs.mtimes[self._path] = self._fs.next_mtime()
        return False


class FakeSFTPClient:
    def __init__(self, fs):
        self.files: dict = fs.files
        self.mtimes: dict = fs.mtimes
        self._fs = fs

    def next_mtime(self):
        return self._fs.next_mtime()

    def open(self, path, mode="rb"):
        if "r" in mode and path not in self.files:
            raise FileNotFoundError(path)
        return _FakeRemoteFile(self._fs, path, mode)

    def stat(self, path):
        if path not in self.files and path not in self._fs.dirs:
            raise FileNotFoundError(path)
        if path in self._fs.dirs:
            return _FakeFileAttr(path, 0, 0, is_dir=True)
        return _FakeFileAttr(path, len(self.files[path]), self.mtimes.get(path, 0))

    def listdir_attr(self, dir_path):
        if dir_path not in self._fs.dirs and not any(p.startswith(dir_path + "/") for p in self.files):
            raise FileNotFoundError(dir_path)
        results = []
        prefix = dir_path.rstrip("/") + "/"
        for path, content in self.files.items():
            if path.startswith(prefix) and "/" not in path[len(prefix) :]:
                filename = path[len(prefix) :]
                results.append(_FakeFileAttr(filename, len(content), self.mtimes.get(path, 0)))
        return results

    def remove(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]

    def posix_rename(self, old, new):
        self.files[new] = self.files.pop(old)
        self.mtimes[new] = self.mtimes.pop(old, self.next_mtime())

    def mkdir(self, path):
        self._fs.dirs.add(path)

    def close(self):
        pass


class FakeFS:
    def __init__(self):
        self.files: dict = {}
        self.mtimes: dict = {}
        self.dirs: set = {"/backups"}
        self._counter = 0

    def next_mtime(self):
        self._counter += 1
        return self._counter


class FakeSSHClient:
    fs = None  # set by fixture, shared across the fake "remote host"

    def __init__(self):
        pass

    def set_missing_host_key_policy(self, policy):
        pass

    def load_host_keys(self, path):
        pass

    def save_host_keys(self, path):
        pass

    def connect(self, hostname, username, key_filename, timeout):
        pass

    def open_sftp(self):
        return FakeSFTPClient(FakeSSHClient.fs)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_ssh(monkeypatch):
    FakeSSHClient.fs = FakeFS()
    monkeypatch.setattr(ssh_backend.paramiko, "SSHClient", FakeSSHClient)
    yield FakeSSHClient


def _backend():
    return SSHStorageBackend(host="site2.example", user="backup", key_path="/fake/key", landing_dir="/backups")


def test_upload_then_verify_roundtrip():
    backend = _backend()
    data = b"some backup bytes" * 100

    result = backend.upload(io.BytesIO(data), "full/img1.bin")

    assert result.size == len(data)
    assert backend.verify("full/img1.bin", result.size, result.sha256) is True


def test_verify_fails_on_size_mismatch():
    backend = _backend()
    backend.upload(io.BytesIO(b"abcdef"), "full/img2.bin")

    assert backend.verify("full/img2.bin", expected_size=999, expected_sha256="deadbeef") is False


def test_verify_missing_key_returns_false():
    backend = _backend()

    assert backend.verify("does/not/exist.bin", expected_size=1, expected_sha256="deadbeef") is False


def test_upload_leaves_no_partial_file_visible_under_final_key():
    """A reader must never see the `.part` staging file as if it were the
    final key — atomic rename means list()/verify() only ever see a
    complete object."""
    backend = _backend()
    backend.upload(io.BytesIO(b"payload"), "full/img3.bin")

    objects = backend.list("full")
    keys = [o.key for o in objects]
    assert "full/img3.bin" in keys
    assert not any(k.endswith(".part") for k in keys)


def test_list_returns_uploaded_objects():
    backend = _backend()
    backend.upload(io.BytesIO(b"one"), "full/a.bin")
    backend.upload(io.BytesIO(b"two"), "full/b.bin")

    objects = backend.list("full")

    assert {o.key for o in objects} == {"full/a.bin", "full/b.bin"}


def test_delete_removes_object():
    backend = _backend()
    backend.upload(io.BytesIO(b"x"), "full/todelete.bin")

    backend.delete("full/todelete.bin")

    assert backend.list("full") == []


def test_apply_retention_keeps_only_configured_count():
    backend = _backend()
    for i in range(5):
        backend.upload(io.BytesIO(f"data{i}".encode()), f"full/img_{i}.bin")

    policy = RetentionPolicyLike(keep_full_count=2, keep_incremental_count=0)
    deleted = backend.apply_retention("full", policy)

    remaining = {o.key for o in backend.list("full")}
    assert len(remaining) == 2
    assert len(deleted) == 3


def test_apply_retention_never_deletes_protected_keys():
    backend = _backend()
    for i in range(4):
        backend.upload(io.BytesIO(f"data{i}".encode()), f"full/img_{i}.bin")

    policy = RetentionPolicyLike(
        keep_full_count=1, keep_incremental_count=0, protected_keys=frozenset({"full/img_0.bin"})
    )
    backend.apply_retention("full", policy)

    remaining = {o.key for o in backend.list("full")}
    assert "full/img_0.bin" in remaining


def test_delete_of_missing_key_raises_backend_error():
    backend = _backend()

    with pytest.raises(SSHStorageBackendError):
        backend.delete("full/never-existed.bin")


@pytest.mark.parametrize("unsafe_key", ["../outside.bin", "/etc/passwd", "full/../outside.bin", "full\\outside.bin"])
def test_remote_key_cannot_escape_landing_directory(unsafe_key):
    backend = _backend()

    with pytest.raises(SSHStorageBackendError):
        backend._remote_path(unsafe_key)
