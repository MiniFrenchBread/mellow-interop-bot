import os
import sys
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config import read_config

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.json")

# The deployed Oracle's maxAge. `getValue()` reverts once this much time has
# passed since the last write, and a revert there stops deposits, withdrawals
# and rebalancing. Hard-coded because it is the constraint the intervals below
# have to fit inside, and reading it from chain would make this test need an
# RPC to say something that is really about the configuration.
ORACLE_MAX_AGE_SECONDS = 1814400  # 21 days

# What the scheduler will burn before a broken heartbeat stops retrying at
# roughly its own interval: alert_after_failures is the alerting threshold, and
# the backoff doubles up to the interval, so a handful of consecutive failures
# is the realistic worst case to survive.
CONSECUTIVE_FAILURES_TO_SURVIVE = 10


class TestHeartbeatOutrunsExpiry(unittest.TestCase):
    """The heartbeat has to have room to fail repeatedly and still not expire.

    The oracle is now written every interval rather than only when it is close
    to expiring, so the old question -- is the warning window wider than the gap
    between checks -- no longer arises. What replaces it is the question the
    heartbeat introduces: if writes start failing, how long until the vault
    stops working? That has to be long enough for someone to notice and act.
    """

    def setUp(self):
        self.config = read_config(CONFIG_PATH)
        self.interval = self.config.scheduler.interval("oracle_update")

    def test_the_interval_is_configured(self):
        self.assertGreater(self.interval, 0)

    def test_several_consecutive_failures_cannot_expire_the_oracle(self):
        survivable = self.interval * CONSECUTIVE_FAILURES_TO_SURVIVE

        self.assertLess(
            survivable,
            ORACLE_MAX_AGE_SECONDS,
            "{} consecutive failures at {}s apart is {}s, which exceeds the "
            "{}s maxAge -- the vault would freeze before anyone had to be "
            "wrong twice".format(
                CONSECUTIVE_FAILURES_TO_SURVIVE,
                self.interval,
                survivable,
                ORACLE_MAX_AGE_SECONDS,
            ),
        )

    def test_the_expiry_threshold_still_spans_two_observations(self):
        """The threshold no longer triggers the write, but it still decides
        when a refusal is described as urgent. A window narrower than the gap
        between runs would be stepped over and never said."""
        threshold = self.config.oracle_expiry_threshold_seconds

        self.assertGreaterEqual(
            threshold,
            self.interval * 2,
            "a {}s window checked every {}s can be stepped over".format(
                threshold, self.interval
            ),
        )


class TestAscendKeepsUpWithTheOracle(unittest.TestCase):
    """Ascend must run at least as often as the oracle is written.

    The vault's assets only move when ascend transfers rewards into it, so the
    share price is a step function whose stride is the ascend interval. Writing
    the oracle more often than ascend runs does not make the price any fresher
    -- it rewrites the same number -- so an ascend interval longer than the
    oracle's would quietly undo the reason this task was made frequent.
    """

    def setUp(self):
        self.config = read_config(CONFIG_PATH)

    def test_ascend_is_at_least_as_frequent_as_the_oracle(self):
        ascend = self.config.scheduler.interval("ascend")
        oracle = self.config.scheduler.interval("oracle_update")

        self.assertGreater(ascend, 0)
        self.assertLessEqual(
            ascend,
            oracle,
            "ascend every {}s with the oracle written every {}s means most "
            "writes record a value nothing has changed".format(ascend, oracle),
        )


if __name__ == "__main__":
    unittest.main()
