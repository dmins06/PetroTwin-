"""Unit tests for src/petrotwin/collectors/locks.py.

Covers:
1. Relative PETROTWIN_LOCK_DIR resolves under repo root (not CWD).
2. Absolute PETROTWIN_LOCK_DIR works (lock file created at exact path).
3. Lock contention returns False in non-blocking mode.
4. Unexpected OSError propagates (non-contention error).
5. Invalid lock names rejected (empty, contains /, contains os.sep).
6. File descriptor is closed after success and after failure.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
from unittest.mock import patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    """Mirror the private helper so tests can compute expected paths."""
    from petrotwin.collectors.locks import _repo_root as _lr
    return _lr()


# ---------------------------------------------------------------------------
# 1. Relative PETROTWIN_LOCK_DIR resolves under repo root
# ---------------------------------------------------------------------------

class TestRelativeLockDir:
    def test_relative_override_resolves_under_repo_root(self, tmp_path, monkeypatch):
        """A relative PETROTWIN_LOCK_DIR must land under repo root, not CWD."""
        monkeypatch.chdir(tmp_path)  # change CWD to something unrelated

        repo_root = _repo_root()
        # Use a relative override path that doesn't exist yet
        rel = "logs/locks_test_relative"
        monkeypatch.setenv("PETROTWIN_LOCK_DIR", rel)

        from petrotwin.collectors import locks
        # Reload get_lock_dir to pick up fresh env
        result = locks.get_lock_dir()

        expected = (repo_root / rel).resolve()
        assert result == expected, (
            f"Expected lock dir under repo root:\n  {expected}\n"
            f"Got:\n  {result}\n"
            f"(CWD was {tmp_path})"
        )
        assert result.is_dir(), "get_lock_dir() must create the directory"
        # Cleanup
        result.rmdir()

    def test_relative_override_mkdir_uses_resolved_path(self, tmp_path, monkeypatch):
        """The directory created on disk must be the resolved path, not under CWD."""
        monkeypatch.chdir(tmp_path)

        rel = "logs/locks_test_mkdir_resolved"
        monkeypatch.setenv("PETROTWIN_LOCK_DIR", rel)

        from petrotwin.collectors import locks
        result = locks.get_lock_dir()

        # Must NOT have created a directory under CWD
        cwd_dir = tmp_path / rel
        assert not cwd_dir.exists(), (
            f"Directory must NOT be created under CWD {tmp_path}, "
            f"but {cwd_dir} exists"
        )
        # Cleanup
        result.rmdir()


# ---------------------------------------------------------------------------
# 2. Absolute PETROTWIN_LOCK_DIR works
# ---------------------------------------------------------------------------

class TestAbsoluteLockDir:
    def test_absolute_override_used_as_is(self, tmp_path, monkeypatch):
        abs_dir = tmp_path / "my_locks"
        monkeypatch.setenv("PETROTWIN_LOCK_DIR", str(abs_dir))

        from petrotwin.collectors import locks
        result = locks.get_lock_dir()

        assert result == abs_dir
        assert result.is_dir()

    def test_lock_file_created_inside_absolute_dir(self, tmp_path, monkeypatch):
        abs_dir = tmp_path / "abs_locks"
        monkeypatch.setenv("PETROTWIN_LOCK_DIR", str(abs_dir))

        from petrotwin.collectors import locks
        with locks.file_lock("brent") as acquired:
            assert acquired is True
            lock_file = abs_dir / "brent.lock"
            assert lock_file.exists()


# ---------------------------------------------------------------------------
# 3. Lock contention returns False in non-blocking mode
# ---------------------------------------------------------------------------

class TestLockContention:
    def test_contention_blocking_io_error_yields_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PETROTWIN_LOCK_DIR", str(tmp_path))

        blocking_exc = BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")

        from petrotwin.collectors import locks
        with patch("fcntl.flock", side_effect=blocking_exc):
            with locks.file_lock("fx", non_blocking=True) as acquired:
                assert acquired is False

    def test_contention_eacces_yields_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PETROTWIN_LOCK_DIR", str(tmp_path))

        eacces_exc = OSError(errno.EACCES, "Permission denied")

        from petrotwin.collectors import locks
        with patch("fcntl.flock", side_effect=eacces_exc):
            with locks.file_lock("domestic_fuel", non_blocking=True) as acquired:
                assert acquired is False

    def test_contention_blocking_non_blocking_raises_lock_busy(self, tmp_path, monkeypatch):
        """In blocking mode a contention error must raise LockBusyError."""
        monkeypatch.setenv("PETROTWIN_LOCK_DIR", str(tmp_path))

        blocking_exc = BlockingIOError(errno.EWOULDBLOCK, "Would block")

        from petrotwin.collectors import locks
        with patch("fcntl.flock", side_effect=blocking_exc):
            with pytest.raises(locks.LockBusyError):
                with locks.file_lock("runner", non_blocking=False):
                    pass


# ---------------------------------------------------------------------------
# 4. Unexpected OSError propagates
# ---------------------------------------------------------------------------

class TestUnexpectedOSError:
    def test_enospc_propagates_in_non_blocking_mode(self, tmp_path, monkeypatch):
        """ENOSPC (no space left) is not a contention error; must propagate."""
        monkeypatch.setenv("PETROTWIN_LOCK_DIR", str(tmp_path))

        enospc_exc = OSError(errno.ENOSPC, "No space left on device")

        from petrotwin.collectors import locks
        with patch("fcntl.flock", side_effect=enospc_exc):
            with pytest.raises(OSError) as exc_info:
                with locks.file_lock("brent", non_blocking=True):
                    pass
        assert exc_info.value.errno == errno.ENOSPC

    def test_erofs_propagates_in_blocking_mode(self, tmp_path, monkeypatch):
        """EROFS (read-only filesystem) is not a contention error; must propagate."""
        monkeypatch.setenv("PETROTWIN_LOCK_DIR", str(tmp_path))

        erofs_exc = OSError(errno.EROFS, "Read-only file system")

        from petrotwin.collectors import locks
        with patch("fcntl.flock", side_effect=erofs_exc):
            with pytest.raises(OSError) as exc_info:
                with locks.file_lock("fx", non_blocking=False):
                    pass
        assert exc_info.value.errno == errno.EROFS


# ---------------------------------------------------------------------------
# 5. Invalid lock names rejected
# ---------------------------------------------------------------------------

class TestLockNameValidation:
    def test_empty_name_raises_value_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PETROTWIN_LOCK_DIR", str(tmp_path))

        from petrotwin.collectors import locks
        with pytest.raises(ValueError, match="must not be empty"):
            with locks.file_lock(""):
                pass

    def test_name_with_forward_slash_raises_value_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PETROTWIN_LOCK_DIR", str(tmp_path))

        from petrotwin.collectors import locks
        with pytest.raises(ValueError, match="path separators"):
            with locks.file_lock("../../etc/passwd"):
                pass

    def test_name_with_os_sep_raises_value_error(self, tmp_path, monkeypatch):
        """os.sep (which is '/' on Linux) in a name must be rejected."""
        monkeypatch.setenv("PETROTWIN_LOCK_DIR", str(tmp_path))

        from petrotwin.collectors import locks
        bad_name = f"sub{os.sep}traversal"
        with pytest.raises(ValueError, match="path separators"):
            with locks.file_lock(bad_name):
                pass

    @pytest.mark.parametrize("valid_name", [
        "brent",
        "fx",
        "domestic_fuel",
        "runner",
        "petrotwin_pipeline",
    ])
    def test_valid_collector_names_accepted(self, tmp_path, monkeypatch, valid_name):
        monkeypatch.setenv("PETROTWIN_LOCK_DIR", str(tmp_path))

        from petrotwin.collectors import locks
        # Should not raise; just enter and exit cleanly
        with locks.file_lock(valid_name) as acquired:
            assert acquired is True


# ---------------------------------------------------------------------------
# 6. File descriptor is closed after success and after failure
# ---------------------------------------------------------------------------

class TestFdCleanup:
    def test_fd_closed_after_successful_lock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PETROTWIN_LOCK_DIR", str(tmp_path))

        from petrotwin.collectors import locks

        closed_fds: list[int] = []
        original_close = os.close

        def tracking_close(fd: int) -> None:
            closed_fds.append(fd)
            original_close(fd)

        with patch("os.close", side_effect=tracking_close):
            with locks.file_lock("brent") as acquired:
                assert acquired is True
                # fd should NOT be closed yet while lock is held
                assert len(closed_fds) == 0

        # After exiting the context manager, fd must be closed
        assert len(closed_fds) == 1

    def test_fd_closed_after_flock_raises_unexpected_oserror(self, tmp_path, monkeypatch):
        """Even when flock raises an unexpected error, the fd must be closed."""
        monkeypatch.setenv("PETROTWIN_LOCK_DIR", str(tmp_path))

        from petrotwin.collectors import locks

        closed_fds: list[int] = []
        original_close = os.close

        def tracking_close(fd: int) -> None:
            closed_fds.append(fd)
            original_close(fd)

        enospc_exc = OSError(errno.ENOSPC, "No space left on device")

        with patch("fcntl.flock", side_effect=enospc_exc), \
             patch("os.close", side_effect=tracking_close):
            with pytest.raises(OSError):
                with locks.file_lock("fx"):
                    pass

        assert len(closed_fds) == 1, "fd must be closed even when flock raises"
