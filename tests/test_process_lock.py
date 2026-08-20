import os
import sys
import tempfile
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from process_lock import REPO_ROOT, LockHeld, ProcessLock, resolve_lock_path

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


class TestLockPathResolution(unittest.TestCase):
    """The lock must name one file no matter where a process was started."""

    def test_relative_paths_anchor_to_the_repo(self):
        self.assertEqual(
            resolve_lock_path(".scheduler.lock"),
            str(REPO_ROOT / ".scheduler.lock"),
        )

    def test_absolute_paths_are_left_alone(self):
        self.assertEqual(resolve_lock_path("/tmp/x.lock"), "/tmp/x.lock")

    def test_the_resolved_path_does_not_follow_the_working_directory(self):
        original = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            self.assertEqual(
                ProcessLock(".scheduler.lock").path,
                str(REPO_ROOT / ".scheduler.lock"),
            )
        finally:
            os.chdir(original)


class TestStaleLocks(unittest.TestCase):
    """Exclusion comes from the kernel, so a dead holder never blocks a start."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "scheduler.lock")

    def tearDown(self):
        self.directory.cleanup()

    def test_a_file_naming_our_own_pid_does_not_block_us(self):
        """The scheduler is pid 1 inside a container.

        A leftover file naming pid 1 would otherwise read as proof that someone
        else holds the lock, and the container could never start again.
        """
        with open(self.path, "w") as handle:
            handle.write(str(os.getpid()))

        ProcessLock(self.path).acquire()

    def test_an_empty_file_does_not_block_us(self):
        with open(self.path, "w"):
            pass

        ProcessLock(self.path).acquire()

    def test_a_holder_that_never_released_still_blocks(self):
        holder = ProcessLock(self.path)
        holder.acquire()

        with self.assertRaises(LockHeld):
            ProcessLock(self.path).acquire()

        holder.release()

    def test_the_second_attempt_does_not_destroy_the_first_holders_file(self):
        """Takeover used to unlink first, so a loser could delete a live lock."""
        holder = ProcessLock(self.path)
        holder.acquire()

        with self.assertRaises(LockHeld):
            ProcessLock(self.path).acquire()

        self.assertTrue(os.path.exists(self.path))
        with open(self.path) as handle:
            self.assertEqual(int(handle.read()), os.getpid())
        holder.release()


if __name__ == "__main__":
    unittest.main()
