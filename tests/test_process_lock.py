import os
import sys
import tempfile
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from process_lock import LockHeld, ProcessLock

# High enough to be unassigned on both Linux and macOS.
DEAD_PID = 4194304


class TestProcessLock(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "scheduler.lock")

    def tearDown(self):
        self.directory.cleanup()

    def test_acquire_writes_our_pid_and_release_removes_it(self):
        lock = ProcessLock(self.path)
        lock.acquire()
        with open(self.path) as handle:
            self.assertEqual(int(handle.read()), os.getpid())
        lock.release()
        self.assertFalse(os.path.exists(self.path))

    def test_second_holder_is_refused(self):
        first = ProcessLock(self.path)
        first.acquire()
        with self.assertRaises(LockHeld) as caught:
            ProcessLock(self.path).acquire()
        self.assertEqual(caught.exception.pid, os.getpid())
        first.release()

    def test_a_lock_left_by_a_dead_process_is_taken_over(self):
        """A crash must not leave the bot unable to restart."""
        with open(self.path, "w") as handle:
            handle.write(str(DEAD_PID))

        ProcessLock(self.path).acquire()

        with open(self.path) as handle:
            self.assertEqual(int(handle.read()), os.getpid())

    def test_a_corrupt_lock_file_is_taken_over(self):
        with open(self.path, "w") as handle:
            handle.write("not a pid")

        ProcessLock(self.path).acquire()

        with open(self.path) as handle:
            self.assertEqual(int(handle.read()), os.getpid())

    def test_context_manager_releases(self):
        with ProcessLock(self.path):
            self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_release_does_not_remove_a_lock_we_do_not_hold(self):
        lock = ProcessLock(self.path)
        lock.acquire()
        other = ProcessLock(self.path)
        other.release()
        self.assertTrue(os.path.exists(self.path))
        lock.release()


if __name__ == "__main__":
    unittest.main()
