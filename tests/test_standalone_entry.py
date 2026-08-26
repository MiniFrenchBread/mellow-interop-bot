import asyncio
import os
import sys
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main
from process_lock import LockHeld


class RecordingLock:
    def __init__(self, taken, raise_held=False):
        self.taken = taken
        self.raise_held = raise_held

    def __enter__(self):
        if self.raise_held:
            raise LockHeld("/tmp/x.lock", 4242)
        self.taken.append(1)
        return self

    def __exit__(self, *_exc):
        return False


class TestStandaloneEntryPoint(unittest.TestCase):
    """`python src/main.py` broadcasts now, so it has to behave like it.

    While this only queued Safe proposals it signed nothing and took no nonce,
    so running it beside the scheduler was harmless and its exit status only
    described a report. Writing the oracle directly changed both.
    """

    def setUp(self):
        self.taken = []
        self._lock = main.ProcessLock
        self._run = main.run_oracle_update
        main.ProcessLock = lambda _path: RecordingLock(self.taken)

    def tearDown(self):
        main.ProcessLock = self._lock
        main.run_oracle_update = self._run

    def _succeeds(self):
        async def run(_config):
            return main.OracleRunSummary()

        main.run_oracle_update = run

    def _fails(self, error=None):
        async def run(_config):
            raise error or RuntimeError("RPC down")

        main.run_oracle_update = run

    def test_it_takes_the_lock_before_writing(self):
        """Two processes reading the same account's nonce independently is what
        the lock exists to prevent, and tx._unreconciled is per-process."""
        self._succeeds()

        asyncio.run(main.main())

        self.assertEqual(self.taken, [1])

    def test_a_successful_run_exits_zero(self):
        self._succeeds()

        self.assertEqual(asyncio.run(main.main()), 0)

    def test_a_failed_run_exits_non_zero(self):
        """systemd or cron would otherwise record a run that wrote nothing at
        all as a success."""
        self._fails()

        self.assertEqual(asyncio.run(main.main()), 1)

    def test_a_failure_is_still_not_a_traceback(self):
        """The exit status is the change; the clean output is not."""
        self._fails()

        asyncio.run(main.main())  # must not raise

    def test_a_held_lock_is_reported_and_fails(self):
        main.ProcessLock = lambda _path: RecordingLock(self.taken, raise_held=True)
        self._succeeds()

        self.assertEqual(asyncio.run(main.main()), 1)
        self.assertEqual(self.taken, [], "nothing may run while another holds it")

    def test_the_write_happens_inside_the_lock(self):
        """Taking it and releasing it before writing would look identical from
        the outside and protect nothing."""
        held = []

        async def run(_config):
            held.append(list(self.taken))
            return main.OracleRunSummary()

        main.run_oracle_update = run

        asyncio.run(main.main())

        self.assertEqual(held, [[1]])


if __name__ == "__main__":
    unittest.main()
