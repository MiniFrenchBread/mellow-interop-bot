"""A single-holder lock shared by the scheduler and the CLI.

One account signs every transaction this bot sends: reward claims, rebalancing,
withdrawal-queue advances. Two processes using it at once read the same nonce and
one of them loses its transaction, which is how an earlier reward distribution
was dropped. Manual runs therefore have to establish that the scheduler is not
running, and this makes that check automatic rather than a thing to remember.
"""

import fcntl
import os
from pathlib import Path

# The lock has to name the same file for every process regardless of where each
# was started from. Resolving a relative path against the working directory would
# put a scheduler started from the repo root and a command started from a home
# directory on two different locks -- each acquiring successfully, neither aware
# of the other, which is worse than having no lock at all.
REPO_ROOT = Path(__file__).resolve().parent.parent


def resolve_lock_path(configured: str) -> str:
    """Anchor a configured lock path to the repo, unless it is already absolute."""
    path = Path(configured)
    return str(path if path.is_absolute() else REPO_ROOT / path)


class LockHeld(Exception):
    def __init__(self, path: str, pid):
        super().__init__(
            "Another run holds {} (pid {}). Stop it before running this, so the "
            "two do not send transactions from the same account at once.".format(
                path, pid if pid is not None else "unknown"
            )
        )
        self.path = path
        self.pid = pid


class ProcessLock:
    """Exclusive across processes, via an advisory lock on an open file.

    The lock lives in the kernel rather than in the file's contents. Comparing
    pids instead cannot be made correct: two processes can both read a dead pid
    and both decide to take over, each deleting the other's file, and a process
    that is itself pid 1 -- which the scheduler is inside a container -- reads a
    leftover file naming pid 1 as proof that someone else holds it, and can never
    start again. An advisory lock has neither problem: the kernel releases it
    when the holder dies, however it dies.

    The pid is still written into the file, purely so an operator can see who is
    holding it.
    """

    def __init__(self, path: str):
        self.path = resolve_lock_path(path)
        self._fd = None

    def _holder_pid(self):
        try:
            with open(self.path, "r") as handle:
                return int(handle.read().strip())
        except (FileNotFoundError, ValueError, OSError):
            return None

    def acquire(self) -> "ProcessLock":
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pid = self._holder_pid()
            os.close(fd)
            raise LockHeld(self.path, pid)

        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        self._fd = fd
        return self

    def release(self) -> None:
        # The file is deliberately left in place. Unlinking it would let a
        # process that opened the path just before the unlink take a lock on an
        # inode nothing else can reach, while the next starter creates a fresh
        # file and locks that -- two holders of a lock whose whole purpose is
        # that only one of them signs. The lock lives on the open file, not on
        # the name, so leaving the name costs nothing.
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "ProcessLock":
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()
