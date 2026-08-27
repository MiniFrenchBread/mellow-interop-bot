import os
import re
import sys
import unittest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config.read_config import read_config
from web3_scripts.operator_bot import parse_deployments

REPO = os.path.join(os.path.dirname(__file__), "..")
COMPOSE = os.path.join(REPO, "docker-compose.yml")
CONFIG = os.path.join(REPO, "config.json")


class TestTheShippedDeploymentsValue(unittest.TestCase):
    """The value in docker-compose.yml has to be one config.json accepts.

    DEPLOYMENTS reaches operator_bot straight from the environment, never
    through read_config, so nothing at startup rejects a malformed one -- the
    bot runs, the gate reports the signer ready, and the rebalance task raises
    "No valid deployments found" every two hours instead. A bare source name
    ("OG" rather than "OG:OG") does exactly that, and it looks correct.
    """

    def deployments_from_compose(self) -> str:
        # A line match rather than a YAML parse: pyyaml is not a dependency of
        # this project, and adding one to read a single literal we control is a
        # worse trade than a match that fails loudly if the line moves.
        with open(COMPOSE) as f:
            found = re.findall(r"^\s*DEPLOYMENTS:\s*[\"']?([^\"'\s#]+)", f.read(), re.M)
        self.assertEqual(
            len(found), 1, "expected exactly one DEPLOYMENTS line, got {}".format(found)
        )
        return found[0]

    def test_the_compose_value_parses_against_the_real_config(self):
        config = read_config(CONFIG)
        raw = self.deployments_from_compose()
        parsed = parse_deployments(config, raw)
        self.assertTrue(
            parsed,
            "docker-compose.yml ships DEPLOYMENTS={!r}, which parse_deployments "
            "rejects -- rebalance would fail on every run".format(raw),
        )

    def test_it_is_pairs_not_bare_source_names(self):
        for pair in self.deployments_from_compose().split(","):
            self.assertIn(
                ":",
                pair,
                "{!r} is a bare source name, not SOURCE:DEPLOYMENT".format(pair),
            )


if __name__ == "__main__":
    unittest.main()
