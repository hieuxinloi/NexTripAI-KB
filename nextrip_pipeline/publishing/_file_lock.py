from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_WINDOWS_LOCK_RETRY_SECONDS = 0.05
_WINDOWS_RETRYABLE_ERRNOS = {
    errno.EACCES,
    errno.EAGAIN,
    errno.EDEADLK,
}


@contextmanager
def destination_file_lock(destination: Path) -> Iterator[None]:
    """Hold an exclusive cross-process lock for one destination path.

    The stable adjacent sidecar is intentional: locking ``destination`` itself
    would lock an inode that ``os.replace`` swaps on POSIX and would prevent the
    replacement on Windows. Lock files are retained so another process can
    never create and lock a different inode during cleanup.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.with_name(f".{destination.name}.lock")
    with lock_path.open("a+b") as lock_file:
        _ensure_lock_byte(lock_file)
        _acquire(lock_file)
        try:
            yield
        finally:
            _release(lock_file)


def _ensure_lock_byte(lock_file: BinaryIO) -> None:
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()


def _acquire(lock_file: BinaryIO) -> None:
    if os.name != "nt":
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return

    while True:
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if error.errno not in _WINDOWS_RETRYABLE_ERRNOS:
                raise
            time.sleep(_WINDOWS_LOCK_RETRY_SECONDS)


def _release(lock_file: BinaryIO) -> None:
    if os.name != "nt":
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return

    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
