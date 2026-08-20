"""A single-holder lock shared by the scheduler and the CLI.

One account signs every transaction this bot sends: reward claims, rebalancing,
withdrawal-queue advances. Two processes using it at once read the same nonce and
one of them loses its transaction, which is how an earlier reward distribution
was dropped. Manual runs therefore have to establish that the scheduler is not
running, and this makes that check automatic rather than a thing to remember.
"""

import os


class LockHeld(Exception):
    def __init__(self, path: str, pid: int):
        super().__init__(
            "Another run holds {} (pid {}). Stop it before running this, so the "
            "two do not send transactions from the same account at once.".format(
                path, pid
            )
        )
        self.path = path
        self.pid = pid


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Owned by another user, but running.
        return True
    return True


class ProcessLock:
    def __init__(self, path: str):
        self.path = path
        self._acquired = False

    def _read_pid(self):
        try:
            with open(self.path, "r") as handle:
                return int(handle.read().strip())
        except (FileNotFoundError, ValueError):
            return None

    def acquire(self) -> "ProcessLock":
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                pid = self._read_pid()
                if pid is not None and _process_alive(pid):
                    raise LockHeld(self.path, pid)
                # The holder died without cleaning up. Drop the stale file and
                # retry rather than refusing to start after a crash.
                try:
                    os.unlink(self.path)
                except FileNotFoundError:
                    pass
                continue

            with os.fdopen(fd, "w") as handle:
                handle.write(str(os.getpid()))
            self._acquired = True
            return self

    def release(self) -> None:
        if not self._acquired:
            return
        if self._read_pid() == os.getpid():
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
        self._acquired = False

    def __enter__(self) -> "ProcessLock":
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()
