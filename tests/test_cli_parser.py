import os
import sys
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cli import parse_args


class TestSharedOptionPlacement(unittest.TestCase):
    """Shared options must work where an operator naturally types them.

    Registered on the top-level parser alone they are only accepted before the
    subcommand, so `cli.py ascend --source OG` fails -- which is the form the
    error message asking for --source invites.
    """

    def parse(self, argv):
        return parse_args(argv)

    def test_source_is_accepted_after_the_subcommand(self):
        args = self.parse(["ascend", "--source", "OG", "--dry-run"])
        self.assertEqual(args.source, "OG")
        self.assertEqual(args.command, "ascend")
        self.assertTrue(args.dry_run)

    def test_source_is_still_accepted_before_the_subcommand(self):
        args = self.parse(["--source", "OG", "ascend"])
        self.assertEqual(args.source, "OG")

    def test_no_lock_is_accepted_after_the_subcommand(self):
        self.assertTrue(self.parse(["handle-epoch", "--no-lock"]).no_lock)

    def test_every_subcommand_accepts_the_shared_options(self):
        for command in (
            "ascend",
            "handle-epoch",
            "oracle",
            "rebalance",
            "validate-config",
        ):
            with self.subTest(command=command):
                args = self.parse([command, "--source", "OG"])
                self.assertEqual(args.source, "OG")

    def test_subcommand_specific_options_still_work(self):
        self.assertTrue(self.parse(["rebalance", "-y"]).yes)


if __name__ == "__main__":
    unittest.main()
