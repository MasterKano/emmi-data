"""Single-runner lock for one machine.

Each workflow has its own concurrency group with cancel-in-progress false, so
a second run of that workflow waits. GitHub still drops an older *pending*
run when a newer one joins the same group, which is why the dry-run, live,
seed and publish workflows do not share a group. This file lock stops two
processes on the same machine. A second acquire fails immediately.
"""
from __future__ import annotations

import fcntl
from pathlib import Path


class RunLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._fh.close()
            self._fh = None
            raise RuntimeError(
                "overlapping nordic-climate run; another process holds "
                f"{self.path}"
            ) from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write("locked\n")
        self._fh.flush()

    def release(self) -> None:
        if self._fh is None:
            return
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
