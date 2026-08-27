import hashlib
import os
import sys
import unittest
from concurrent import futures

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from eth_account import Account
from web3 import Web3

import tapp.signer as signer

try:
    import grpc
    from tapp import tapp_service_pb2 as pb
    from tapp import tapp_service_pb2_grpc as pb_grpc

    STUBS = True
except ImportError:  # pragma: no cover - depends on scripts/gen_proto.sh
    STUBS = False

APP_ID = "mellow-interop-bot"

# Unix socket paths are capped near 104 bytes, and pytest's tmp dirs blow
# through that on macOS. A short fixed root keeps the test runnable anywhere.
SOCKET_ROOT = "/tmp/mellow-tapp-test"


class KeyEnv:
    """Save and restore every variable the signer reads or writes."""

    NAMES = (
        "TAPP_APP_ID",
        "TAPP_SOCKET",
        "TAPP_KEY_MATERIAL",
        "OPERATOR_PK",
        "ORACLE_UPDATER_PK",
        "SAFE_PROPOSER_PK",
    )

    def __enter__(self):
        self.saved = {n: os.environ.get(n) for n in self.NAMES}
        for n in self.NAMES:
            os.environ.pop(n, None)
        return self

    def __exit__(self, *exc):
        for n, v in self.saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v


class TestNotUnderATapp(unittest.TestCase):
    """Without TAPP_APP_ID the bot must behave exactly as it did before.

    This is what keeps local development and the current server deployment
    working off .env while the tapp path exists alongside them.
    """

    def test_returns_none_and_touches_nothing(self):
        with KeyEnv():
            os.environ["OPERATOR_PK"] = "ab" * 32
            self.assertIsNone(signer.inject_tee_keys())
            self.assertEqual(os.environ["OPERATOR_PK"], "ab" * 32)
            self.assertNotIn("ORACLE_UPDATER_PK", os.environ)
            self.assertIsNone(signer.derived_address())


class TestDerivation(unittest.TestCase):
    def test_label_is_mixed_in_and_output_is_a_valid_key(self):
        secret = b"\x01" * 32
        expected = Web3.keccak(signer._DERIVATION_LABEL + secret)
        # Not the bare secret: the label both normalises the length and keeps
        # this key independent of anything else derived from the same app.
        self.assertNotEqual(bytes(expected), secret)
        self.assertEqual(len(bytes(expected)), 32)
        Account.from_key(expected)

    def test_short_and_long_secrets_both_yield_a_key(self):
        for secret in (b"\x02" * 8, b"\x03" * 96):
            key = Web3.keccak(signer._DERIVATION_LABEL + secret)
            self.assertEqual(len(bytes(key)), 32)
            Account.from_key(key)

    def test_default_material_is_the_hex_of_the_label(self):
        self.assertEqual(bytes.fromhex(signer._DEFAULT_MATERIAL), b"mellow-operator")

    def test_safe_proposer_is_not_a_target(self):
        # Proposing Safe transactions stays outside the TEE, so the TEE address
        # must never be presented as a Safe signer.
        self.assertNotIn("SAFE_PROPOSER_PK", signer._KEY_ENV_VARS)


@unittest.skipUnless(STUBS, "run scripts/gen_proto.sh first")
class TestTheChannelAuthority(unittest.TestCase):
    """Pins the one channel option that the test server below cannot check.

    grpc derives :authority from the target, so a Unix socket path lands there
    verbatim -- and an authority containing "/" is not valid HTTP/2. tapp's
    server is tonic over hyper and resets the stream with a bare PROTOCOL_ERROR,
    which reads like a refusal rather than a malformed request. A Python gRPC
    server accepts it, so TestAgainstAServer passes either way; only the real
    server rejects it. Hence a test on the argument rather than the behaviour.
    """

    def test_an_explicit_authority_is_set(self):
        captured = {}

        import grpc as grpc_mod

        original = grpc_mod.insecure_channel

        def spy(target, options=None, *a, **kw):
            captured["target"] = target
            captured["options"] = dict(options or [])
            raise RuntimeError("stop here -- the channel is all we wanted to see")

        grpc_mod.insecure_channel = spy
        try:
            with KeyEnv():
                os.environ["TAPP_APP_ID"] = APP_ID
                os.environ["TAPP_SOCKET"] = "/run/tapp/tapp.sock"
                signer._RETRY_INITIAL_SECONDS = 0.01
                # Unbounded retry by design, so let it fail once and bail out.
                import threading

                done = threading.Event()

                def run():
                    try:
                        signer.inject_tee_keys()
                    except BaseException:
                        pass
                    finally:
                        done.set()

                t = threading.Thread(target=run, daemon=True)
                t.start()
                done.wait(timeout=2) or t.join(0.1)
        finally:
            grpc_mod.insecure_channel = original

        self.assertEqual(captured.get("target"), "unix:///run/tapp/tapp.sock")
        self.assertEqual(
            captured.get("options", {}).get("grpc.default_authority"), "localhost"
        )


@unittest.skipUnless(STUBS, "run scripts/gen_proto.sh first")
class TestAgainstAServer(unittest.TestCase):
    """The whole path: gRPC over a Unix socket, retry, derive, inject."""

    def setUp(self):
        os.makedirs(SOCKET_ROOT, exist_ok=True)
        self.socket = os.path.join(SOCKET_ROOT, "{}.sock".format(id(self)))
        if os.path.exists(self.socket):
            os.remove(self.socket)
        self.calls = []
        self.reject_first = 0
        test = self

        class Svc(pb_grpc.TappServiceServicer):
            def GetSecretResource(self, request, context):
                test.calls.append((request.app_id, request.material))
                if len(test.calls) <= test.reject_first:
                    context.abort(grpc.StatusCode.UNAUTHENTICATED, "401 not registered")
                digest = hashlib.sha256(
                    (request.app_id + "|" + request.material).encode()
                ).digest()
                return pb.GetSecretResourceResponse(success=True, secret=digest)

        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        pb_grpc.add_TappServiceServicer_to_server(Svc(), self.server)
        self.server.add_insecure_port("unix://" + self.socket)
        self.server.start()

        self._initial_delay = signer._RETRY_INITIAL_SECONDS
        signer._RETRY_INITIAL_SECONDS = 0.01

    def tearDown(self):
        signer._RETRY_INITIAL_SECONDS = self._initial_delay
        self.server.stop(None)
        if os.path.exists(self.socket):
            os.remove(self.socket)

    def env(self, **extra):
        os.environ["TAPP_APP_ID"] = APP_ID
        os.environ["TAPP_SOCKET"] = self.socket
        os.environ.update(extra)

    def test_injects_both_key_slots_and_not_the_safe_proposer(self):
        with KeyEnv():
            self.env()
            address = signer.inject_tee_keys()

            secret = hashlib.sha256(
                (APP_ID + "|" + signer._DEFAULT_MATERIAL).encode()
            ).digest()
            expected = Account.from_key(
                Web3.keccak(signer._DERIVATION_LABEL + secret)
            ).address

            self.assertEqual(address, expected)
            self.assertEqual(signer.derived_address(), expected)
            self.assertEqual(os.environ["OPERATOR_PK"], os.environ["ORACLE_UPDATER_PK"])
            self.assertNotIn("SAFE_PROPOSER_PK", os.environ)

    def test_key_is_unprefixed_hex(self):
        # The rest of the bot's keys are unprefixed, and the secret masker shows
        # a fixed number of leading characters -- a stray 0x would spend two of
        # them saying nothing.
        with KeyEnv():
            self.env()
            signer.inject_tee_keys()
            key = os.environ["OPERATOR_PK"]
            self.assertEqual(len(key), 64)
            self.assertFalse(key.startswith("0x"))

    def test_same_app_and_material_give_the_same_address(self):
        # The property the whole design rests on: fund and authorise once, then
        # survive every restart.
        with KeyEnv():
            self.env()
            first = signer.inject_tee_keys()
        with KeyEnv():
            self.env()
            second = signer.inject_tee_keys()
        self.assertEqual(first, second)

    def test_material_changes_the_address(self):
        # What a future operator/oracle split would use, without a code change.
        with KeyEnv():
            self.env()
            operator = signer.inject_tee_keys()
        with KeyEnv():
            self.env(TAPP_KEY_MATERIAL="oracle".encode().hex())
            oracle = signer.inject_tee_keys()
        self.assertNotEqual(operator, oracle)

    def test_retries_until_the_app_is_registered(self):
        # A fresh registration 401s until the KMS's view of the chain catches
        # up, so giving up on the first refusal would mean a crash loop through
        # exactly the window the deploy runbook says to expect.
        self.reject_first = 2
        seen = []
        with KeyEnv():
            self.env()
            address = signer.inject_tee_keys(on_retry=seen.append)
        self.assertIsNotNone(address)
        self.assertEqual(len(self.calls), 3)
        # Alerted on the first failure, not on every one.
        self.assertEqual(len(seen), 1)
        self.assertIn("401", seen[0])

    def test_a_failure_response_is_not_treated_as_a_key(self):
        class Failing(pb_grpc.TappServiceServicer):
            def __init__(self):
                self.n = 0

            def GetSecretResource(self, request, context):
                self.n += 1
                if self.n == 1:
                    return pb.GetSecretResourceResponse(
                        success=False, message="KMS not configured"
                    )
                if self.n == 2:
                    return pb.GetSecretResourceResponse(success=True, secret=b"")
                return pb.GetSecretResourceResponse(success=True, secret=b"\x07" * 32)

        # A fresh path: stopping a server unlinks the socket it bound, so
        # rebinding the old one races with that cleanup.
        self.server.stop(None)
        self.socket = os.path.join(SOCKET_ROOT, "{}-b.sock".format(id(self)))
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        pb_grpc.add_TappServiceServicer_to_server(Failing(), self.server)
        self.server.add_insecure_port("unix://" + self.socket)
        self.server.start()

        reasons = []
        with KeyEnv():
            self.env()
            address = signer.inject_tee_keys(on_retry=reasons.append)

        expected = Account.from_key(
            Web3.keccak(signer._DERIVATION_LABEL + b"\x07" * 32)
        ).address
        self.assertEqual(address, expected)
        self.assertIn("KMS not configured", reasons[0])


if __name__ == "__main__":
    unittest.main()
