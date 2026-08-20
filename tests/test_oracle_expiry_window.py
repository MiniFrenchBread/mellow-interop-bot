import os
import sys
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config import read_config

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.json")


class TestExpiryWindowIsReachable(unittest.TestCase):
    """The warning window has to be wider than the gap between observations.

    `almost_expired` is `remaining_time <= threshold`, evaluated once per
    oracle-report run. If the window is narrower than that interval, the run
    before it is still comfortably fresh and the run after finds it already
    expired -- and an expired oracle makes `getValue()` revert, which stops
    deposits, withdrawals and rebalancing until the signers execute an update.
    Between reward distributions nothing else moves the value, so expiry is the
    only thing that will ask them.
    """

    def setUp(self):
        self.config = read_config(CONFIG_PATH)

    def test_the_window_spans_at_least_two_observations(self):
        threshold = self.config.oracle_expiry_threshold_seconds
        interval = self.config.scheduler.interval("oracle_report")

        self.assertGreater(interval, 0)
        self.assertGreaterEqual(
            threshold,
            interval * 2,
            "a {}s window checked every {}s can be stepped over".format(
                threshold, interval
            ),
        )

    def test_the_window_leaves_the_signers_time_to_act(self):
        """Updating means a multisig proposal a human has to approve."""
        self.assertGreaterEqual(self.config.oracle_expiry_threshold_seconds, 86400)


if __name__ == "__main__":
    unittest.main()
