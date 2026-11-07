"""Shared cross-process file locking for PetroTwin collectors and runner.

Ensures that multiple cron jobs, manual CLI invocations, or runner subprocesses
cannot concurrently collect the same data source or write to the same files.
Uses kernel-level fcntl.flock, which cooperates with shell-level flock utility.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
from pathlib import Path
from typing import Iterator


class LockBusyError(RuntimeError):
    """Raised when an operation cannot proceed because another process holds the lock."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _validate_lock_name(name: str) -> None:
    """Reject empty names and names containing path separators to prevent traversal."""
    if not name:
        raise ValueError("lock name must not be empty")
    if "/" in name or os.sep in name:
        raise ValueError(f"lock name must not contain path separators: {name!r}")


def get_lock_dir() -> Path:
    """Return the directory where lockfiles are stored."""
    override = os.environ.get("PETROTWIN_LOCK_DIR")
    if override:
        raw = Path(override).expanduser()
        # Resolve relative paths against repo root BEFORE creating the directory
        path = raw if raw.is_absolute() else (_repo_root() / raw).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    lock_dir = _repo_root() / "logs" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


@contextlib.contextmanager
def file_lock(name: str, non_blocking: bool = True) -> Iterator[bool]:
    """Acquire an exclusive kernel file lock for the given logical name.

    Yields:
        True if the lock was successfully acquired.
        False if non_blocking=True and another process already holds the lock.

    Raises:
        ValueError if the lock name is empty or contains path separators.
        LockBusyError if non_blocking=False and the lock fails to acquire.
        OSError on unexpected filesystem error.
    """
    _validate_lock_name(name)
    lock_path = get_lock_dir() / f"{name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    flags = fcntl.LOCK_EX
    if non_blocking:
        flags |= fcntl.LOCK_NB

    try:
        try:
            fcntl.flock(fd, flags)
        except OSError as exc:
            is_contention = isinstance(exc, BlockingIOError) or exc.errno in (
                errno.EAGAIN,
                errno.EWOULDBLOCK,
                errno.EACCES,
            )
            if non_blocking and is_contention:
                yield False
                return
            if not non_blocking and is_contention:
                raise LockBusyError(f"Could not acquire lock for {name}: {exc}") from exc
            raise  # propagate unexpected OSError (ENOSPC, EBADF, EROFS, …)

        # Write current PID into lock file for easy diagnosis
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        except OSError:
            pass

        yield True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
