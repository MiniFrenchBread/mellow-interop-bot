"""The bot's signing key, derived inside a TEE instead of read from a file.

Running under 0G Tapp, the signing key is not configured -- it is fetched from
the tapp Unix socket at every start and only ever exists in this process's
memory. Nothing is written to disk, and no operator with shell on the host can
read it back.

Two key sources exist on a tapp and only one of them is usable here:

  GetAppSecretKey    a fresh secp256k1 key generated with OsRng and held in the
                     tapp-server process. It changes whenever that process
                     restarts -- a package upgrade, a crash, a reboot -- so gas
                     sent to it is stranded and on-chain roles granted to it
                     silently stop applying.

  GetSecretResource  a key the KMS cluster derives from (app_id, material).
                     Identical across restarts, reboots, image upgrades and
                     every node of the app. This is the one we use.

The cost of the stable source is that the KMS authenticates the caller by the
tapp node's on-chain registered signer address, so the app must be registered
in TappRegistry before a fetch succeeds, and for a short while after a fresh
registration the cluster answers 401 while its own view of the chain catches
up. Hence the unbounded retry below: at first start there is nothing to do but
wait for the operator to finish registering.

Outside a tapp (TAPP_APP_ID unset) every function here is a no-op and the bot
reads its keys from the environment exactly as before.
"""

import os
import time
from typing import Callable, Optional

from eth_account import Account
from web3 import Web3

TAPP_APP_ID_ENV = "TAPP_APP_ID"

_DEFAULT_SOCKET = "/run/tapp/tapp.sock"

# Namespaces the KMS derivation, so this key is independent of anything else the
# same app ever derives (the DPRF is one-way, so per-material keys expose
# neither each other nor the app-wide key). It is also the seam along which the
# operator and oracle keys would be split later: a second material yields a
# second, unrelated address without touching this code or redeploying the app.
_DEFAULT_MATERIAL = "mellow-operator".encode().hex()

# Mixed into the KMS secret before it becomes a private key. Two jobs: it forces
# the result to 32 bytes whatever length the KMS returns, and it separates this
# key from any other consumer of the same secret.
#
# CHANGING THIS STRING CHANGES THE BOT'S ADDRESS, which would strand its gas and
# void every role granted to it. It is part of the deployment's identity.
_DERIVATION_LABEL = b"mellow-interop-bot/operator/v1"

# The environment variables the rest of the bot reads its signing key from.
# config.json interpolates ${OPERATOR_PK} and ${ORACLE_UPDATER_PK}, read_config
# falls back to os.getenv("OPERATOR_PK"), and operator_bot reads it directly --
# setting them here covers all three without touching any of them.
#
# SAFE_PROPOSER_PK is deliberately absent. Proposing Safe transactions stays a
# human-run CLI command outside the TEE, so the TEE address needs to be neither
# a Safe owner nor a delegate.
_KEY_ENV_VARS = ("OPERATOR_PK", "ORACLE_UPDATER_PK")

_RETRY_INITIAL_SECONDS = 5.0
_RETRY_MAX_SECONDS = 300.0

# Alert on the first failure, then roughly every ten minutes, rather than on
# every attempt -- an unregistered app can sit here for as long as the operator
# takes to finish the on-chain steps.
_ALERT_EVERY_SECONDS = 600.0


def app_id() -> Optional[str]:
    """The tapp app id, or None when the bot is not running under a tapp."""
    return os.getenv(TAPP_APP_ID_ENV) or None


def derived_address() -> Optional[str]:
    """The address the injected key belongs to, once inject_tee_keys has run."""
    key = os.getenv(_KEY_ENV_VARS[0])
    if not app_id() or not key:
        return None
    return Account.from_key(key).address


def inject_tee_keys(on_retry: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """Fetch the KMS-derived key over the tapp socket and export it.

    Returns the derived address, or None when TAPP_APP_ID is unset (not running
    under a tapp -- the bot then uses whatever the environment already holds).

    Must be called after dotenv.load_dotenv() and before read_config(), which is
    where the values land.

    on_retry, if given, is called with a human-readable reason each time the
    fetch fails and is about to be retried. It exists so the wait for on-chain
    registration is visible somewhere other than the container log.
    """
    identifier = app_id()
    if not identifier:
        return None

    socket_path = os.getenv("TAPP_SOCKET", _DEFAULT_SOCKET)
    material = os.getenv("TAPP_KEY_MATERIAL", _DEFAULT_MATERIAL)

    secret = _fetch_secret(identifier, socket_path, material, on_retry)
    private_key = Web3.keccak(_DERIVATION_LABEL + secret)
    address = Account.from_key(private_key).address

    # bytes(...).hex() rather than HexBytes.hex(): the prefix that returns
    # changed between hexbytes majors, and the rest of the bot's keys are
    # unprefixed. eth_account accepts either, but the value also reaches the
    # secret masker, which shows a fixed number of leading characters.
    for name in _KEY_ENV_VARS:
        os.environ[name] = bytes(private_key).hex()

    print(
        "TEE signer ready: app_id={} material={} address={}".format(
            identifier, material or "(none)", address
        )
    )
    return address


def _fetch_secret(
    identifier: str,
    socket_path: str,
    material: str,
    on_retry: Optional[Callable[[str], None]],
) -> bytes:
    """Ask tapp for the KMS-derived secret, retrying until it answers.

    Deliberately unbounded. Every reason this fails at first start -- the app is
    not registered on chain yet, the KMS has not caught up, the cluster is
    briefly unreachable -- is one an operator fixes from outside while the bot
    waits. Exiting instead would turn each of them into a crash loop that
    reports nothing.
    """
    # Imported here, not at module scope, so a checkout without generated
    # protobuf stubs still runs locally. Outside a tapp this function is never
    # reached; inside one the stubs are baked into the image.
    from . import tapp_service_pb2 as pb
    from . import tapp_service_pb2_grpc as pb_grpc

    import grpc

    target = "unix://{}".format(socket_path)
    request = pb.GetSecretResourceRequest(app_id=identifier, material=material)

    # Without this, grpc derives :authority from the target and a socket path
    # lands there verbatim. An authority containing "/" is not valid HTTP/2, and
    # tapp's server -- tonic over hyper -- resets the stream with a bare
    # PROTOCOL_ERROR rather than a status, which reads like the server refusing
    # the call. Any valid token works; the value is meaningless over a socket.
    #
    # A Python gRPC server accepts the malformed authority, so a test that
    # stands one up cannot see this. It took the real server to surface it.
    options = [("grpc.default_authority", "localhost")]

    delay = _RETRY_INITIAL_SECONDS
    attempt = 0
    last_alert = 0.0

    while True:
        attempt += 1
        try:
            with grpc.insecure_channel(target, options=options) as channel:
                response = pb_grpc.TappServiceStub(channel).GetSecretResource(request)
            if not response.success:
                raise RuntimeError(response.message or "tapp reported failure")
            if not response.secret:
                raise RuntimeError("tapp returned an empty secret")
            return response.secret
        except Exception as e:
            reason = "{}: {}".format(type(e).__name__, e)
            print(
                "Could not fetch the TEE key from {} (attempt {}): {}. "
                "Retrying in {:.0f}s. If this is a first start, the app most "
                "likely is not registered on chain yet.".format(
                    target, attempt, reason, delay
                )
            )
            now = time.monotonic()
            if on_retry and (
                last_alert == 0.0 or now - last_alert >= _ALERT_EVERY_SECONDS
            ):
                last_alert = now
                on_retry(reason)
            time.sleep(delay)
            delay = min(delay * 2, _RETRY_MAX_SECONDS)
