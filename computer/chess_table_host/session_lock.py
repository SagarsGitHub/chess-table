"""Advisory process locks for a chess checkpoint and physical serial port.

The GUI and CLI both hold these locks for their full session. Lock files are
kept because the OS releases the advisory lock even after an unclean exit.
"""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path


def _lock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("another process owns this chess-table session") from exc
    else:
        import fcntl
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("another process owns this chess-table session") from exc


def _unlock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SessionLock:
    def __init__(self, checkpoint: str | Path, serial_port: str | None = None):
        checkpoint = Path(checkpoint).resolve()
        self.paths = [checkpoint.with_name(checkpoint.name + ".session.lock")]
        if serial_port:
            digest = sha256(serial_port.casefold().encode("utf-8")).hexdigest()[:16]
            common = Path(__file__).resolve().parent.parent / "runs" / ".session-locks"
            self.paths.append(common / f"serial-{digest}.lock")
        self._handles = []

    def acquire(self) -> None:
        if self._handles:
            raise RuntimeError("session lock is already acquired")
        try:
            for path in sorted(self.paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open("a+b")
                try:
                    if handle.seek(0, 2) == 0:
                        handle.write(b"1")
                        handle.flush()
                    _lock_file(handle)
                except BaseException:
                    handle.close()
                    raise
                self._handles.append(handle)
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        for handle in reversed(self._handles):
            try:
                _unlock_file(handle)
            finally:
                handle.close()
        self._handles.clear()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, _type, _value, _traceback):
        self.release()
