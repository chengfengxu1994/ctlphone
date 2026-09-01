"""Cross-boundary single-owner claim for the USB adb device.

The ctlphone gateway (regular user) and phonebroker (credential-isolated
system service) each run their own adb server, but one USB device can only
be attached to one adb server at a time. Without coordination the two adb
servers silently steal the device from each other and the losing side sees
the phone "vanish". This module replaces that race with an explicit
flock-based claim on a shared filesystem location.

Protocol:

- phonebroker holds an exclusive flock on ``broker.lock`` for the whole
  duration of one broker request, and runs its private adb server only
  while the claim is held (start-server on entry, kill-server on exit).
- While the claim is held, the ctlphone gateway refuses new operations
  with DEVICE_BUSY, reports ``broker_active`` in status, and a watcher
  thread kills the gateway-side adb server (never mid-operation, thanks to
  the gateway operation lock) so the broker's server can attach the device.
- When the broker releases the claim, the next gateway operation starts a
  fresh adb server which re-attaches the device.

The lock state lives in ``/run/lock/phone-device`` and is provisioned as a
setgid ``root:plugdev`` directory. Both services can use it, while unrelated
local users cannot replace the lock inode or hold the device indefinitely.
Override with ``PHONE_CLAIM_DIR`` for tests. flock is released by the kernel
if either side dies, so a crash cannot wedge the phone.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from pathlib import Path
from typing import IO

DEFAULT_CLAIM_DIR = Path("/run/lock/phone-device")
BROKER_LOCK_NAME = "broker.lock"
BROKER_MARKER_NAME = "broker_claim.json"
CLAIM_DIR_ENV = "PHONE_CLAIM_DIR"


def claim_dir() -> Path:
    configured = os.environ.get(CLAIM_DIR_ENV)
    return Path(configured) if configured else DEFAULT_CLAIM_DIR


def _open_lock(directory: Path) -> IO[str] | None:
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o770)
        directory_info = directory.lstat()
        if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_mode & 0o002:
            return None
        path = directory / BROKER_LOCK_NAME
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o660)
        opened = os.fstat(fd)
        linked = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != linked.st_dev
            or opened.st_ino != linked.st_ino
            or opened.st_mode & 0o002
        ):
            os.close(fd)
            return None
    except OSError:
        return None
    handle = os.fdopen(fd, "a+")
    return handle


def broker_claim_active(directory: Path | None = None) -> bool | None:
    """Return True while phonebroker holds the device claim, False if free.

    Returns None when the shared claim location is unusable. Callers that
    guard a credential boundary must treat None as "claimed" (fail closed).
    """
    handle = _open_lock(directory or claim_dir())
    if handle is None:
        return None
    try:
        try:
            # A shared probe coexists with gateway operations but is blocked
            # by the broker's exclusive claim, so the two holders are not
            # confused with each other.
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


class GatewayDeviceClaim:
    """Non-blocking shared claim held for one complete gateway ADB operation."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or claim_dir()
        self._handle: IO[str] | None = None

    def __enter__(self) -> "GatewayDeviceClaim | None":
        handle = _open_lock(self.directory)
        if handle is None:
            return None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        self._handle = handle
        return self

    def __exit__(self, *exc: object) -> bool:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
        return False


class BrokerDeviceClaim:
    """Phonebroker-side exclusive device claim for the scope of one request.

    Used as a context manager; ``__enter__`` returns None when the shared
    claim location is unusable, in which case the caller must fail closed.
    """

    def __init__(self, directory: Path | None = None, timeout: float = 15.0):
        self.directory = directory or claim_dir()
        self.timeout = max(0.0, float(timeout))
        self._handle: IO[str] | None = None

    def __enter__(self) -> "BrokerDeviceClaim | None":
        handle = _open_lock(self.directory)
        if handle is None:
            return None
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.close()
                    return None
                time.sleep(0.05)
        self._handle = handle
        self._write_marker()
        return self

    def __exit__(self, *exc: object) -> bool:
        if self._handle is not None:
            try:
                (self.directory / BROKER_MARKER_NAME).unlink(missing_ok=True)
            except OSError:
                pass
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
        return False

    def _write_marker(self) -> None:
        marker = {
            "holder": "phonebroker",
            "pid": os.getpid(),
            "claimed_at": round(time.time(), 3),
        }
        try:
            (self.directory / BROKER_MARKER_NAME).write_text(
                json.dumps(marker, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass


__all__ = [
    "BrokerDeviceClaim",
    "GatewayDeviceClaim",
    "broker_claim_active",
    "claim_dir",
    "DEFAULT_CLAIM_DIR",
    "BROKER_LOCK_NAME",
    "BROKER_MARKER_NAME",
    "CLAIM_DIR_ENV",
]
