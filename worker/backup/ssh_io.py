"""Bounded I/O helpers for long-running backup and restore SSH streams."""

from __future__ import annotations

import socket
import time


class BackupSSHTimeout(TimeoutError):
    """Raised when a backup/restore SSH operation exceeds its deadline."""


def ensure_time_remaining(deadline: float, operation: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BackupSSHTimeout(f"{operation} exceeded its configured deadline")
    return remaining


def set_channel_timeout(stream_or_channel, deadline: float) -> None:
    channel = getattr(stream_or_channel, "channel", stream_or_channel)
    setter = getattr(channel, "settimeout", None)
    if setter is not None:
        setter(max(0.1, min(1.0, ensure_time_remaining(deadline, "SSH I/O"))))


def read_chunk(stream, size: int, deadline: float) -> bytes:
    ensure_time_remaining(deadline, "SSH read")
    set_channel_timeout(stream, deadline)
    try:
        try:
            return stream.read(size)
        except TypeError:
            return stream.read()
    except (socket.timeout, TimeoutError) as exc:
        raise BackupSSHTimeout("SSH read timed out") from exc


def read_all(stream, deadline: float, *, max_bytes: int = 64 * 1024) -> bytes:
    data = bytearray()
    while True:
        chunk = read_chunk(stream, 8192, deadline)
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > max_bytes:
            return bytes(data[:max_bytes])


def write_chunk(stream, data: bytes, deadline: float) -> None:
    ensure_time_remaining(deadline, "SSH write")
    set_channel_timeout(stream, deadline)
    try:
        stream.write(data)
    except (socket.timeout, TimeoutError) as exc:
        raise BackupSSHTimeout("SSH write timed out") from exc


def close_stdin(stream) -> None:
    close = getattr(stream, "close", None)
    if close is not None:
        close()


def wait_exit_status(channel, deadline: float) -> int:
    set_channel_timeout(channel, deadline)
    ready = getattr(channel, "exit_status_ready", None)
    if ready is not None:
        while not ready():
            ensure_time_remaining(deadline, "SSH command")
            time.sleep(0.05)
    ensure_time_remaining(deadline, "SSH command")
    try:
        return channel.recv_exit_status()
    except (socket.timeout, TimeoutError) as exc:
        raise BackupSSHTimeout("SSH command completion timed out") from exc
