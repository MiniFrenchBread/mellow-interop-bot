import os
import unittest
import importlib.util
from contextlib import contextmanager


@contextmanager
def temp_environ(overrides):
    old = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(old)
        os.environ.update(overrides)
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


def load_read_config_module():
    root = os.path.dirname(os.path.dirname(__file__))
    path = os.path.join(root, "src", "config", "read_config.py")
    spec = importlib.util.spec_from_file_location("read_config", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


class TestSubstituteEnvVars(unittest.TestCase):
    def setUp(self):
        self.rc = load_read_config_module()

    def test_simple_var(self):
        with temp_environ({"A": "foo"}):
            self.assertEqual(self.rc._substitute_env_vars("${A}"), "foo")

    def test_simple_default(self):
        with temp_environ({}):
            self.assertEqual(self.rc._substitute_env_vars("${B:bar}"), "bar")

    def test_simple_no_value(self):
        with temp_environ({}):
            self.assertEqual(self.rc._substitute_env_vars("${A}"), "")

    def test_nested_no_value(self):
        with temp_environ({}):
            self.assertEqual(self.rc._substitute_env_vars("${A:${B}}"), "")

    def test_nested_default_resolved(self):
        with temp_environ({"C": "baz"}):
            self.assertEqual(self.rc._substitute_env_vars("${B:${C}}"), "baz")

    def test_multi_level_fallback(self):
        with temp_environ({}):
            self.assertEqual(self.rc._substitute_env_vars("${D:${E:final}}"), "final")

    def test_deep_nesting(self):
        with temp_environ({"V3": "v3"}):
            self.assertEqual(
                self.rc._substitute_env_vars("${V1:${V2:${V3:fallback}}}"), "v3"
            )

    def test_hex_addr_with_nested_fallback(self):
        with temp_environ(
            {"SAFE_ADDRESS": "0x35b9a5EA6D8124FF2B8A72d7f67C6219864F4B5b"}
        ):
            result = self.rc._substitute_env_vars("${BSC_SAFE_ADDRESS:${SAFE_ADDRESS}}")
            self.assertEqual(result, "0x35b9a5EA6D8124FF2B8A72d7f67C6219864F4B5b")

    def test_multiple_occurrences(self):
        with temp_environ({"A": "X", "B": "Y"}):
            s = "left-${A}-mid-${B:Z}-right"
            self.assertEqual(self.rc._substitute_env_vars(s), "left-X-mid-Y-right")

    def test_empty_env_to_empty_string(self):
        with temp_environ({}):
            self.assertEqual(self.rc._substitute_env_vars("${EMPTY:}"), "")

    def test_circular_reference(self):
        with temp_environ({"X": "${Y}", "Y": "${X}"}):
            with self.assertRaises(ValueError) as cm:
                self.rc._substitute_env_vars("${X}")
            self.assertIn("Circular reference", str(cm.exception))

    def test_malformed_left_as_is(self):
        with temp_environ({"A": "foo"}):
            # No closing brace; function should leave as-is
            self.assertEqual(self.rc._substitute_env_vars("prefix-${A"), "prefix-${A")


if __name__ == "__main__":
    unittest.main()


class TestSafeApiKeyIsWired(unittest.TestCase):
    """SAFE_API_KEY is documented as a working variable; it has to be one.

    config.json had no `api-key` line, so the placeholder substitution had
    nothing to act on and SafeGlobal.api_key was always None -- the variable was
    loaded into the environment and then read by nothing. Unauthenticated
    requests still work against this chain's Safe service, but on a tenth of the
    quota, and that quota is counted per source IP rather than per key.
    """

    CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.json")

    def setUp(self):
        self.rc = load_read_config_module()
        self._previous = os.environ.get("SAFE_API_KEY")

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("SAFE_API_KEY", None)
        else:
            os.environ["SAFE_API_KEY"] = self._previous

    def test_the_env_var_reaches_the_safe_config(self):
        os.environ["SAFE_API_KEY"] = "sentinel-key"

        config = self.rc.read_config(self.CONFIG_PATH)

        self.assertEqual(config.sources[0].safe_global.api_key, "sentinel-key")

    def test_an_unset_key_stays_falsy(self):
        """_headers() omits Authorization on anything falsy, so an empty string
        has to behave the same as never having configured one."""
        os.environ.pop("SAFE_API_KEY", None)

        config = self.rc.read_config(self.CONFIG_PATH)

        self.assertFalse(config.sources[0].safe_global.api_key)
