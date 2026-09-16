# Deploying the bot on 0G Tapp

An operator runbook. Read `README.md` § "Running on 0G Tapp" first for what the
deployment *is*; this is what to type and what to expect.

Written against **tapp-server 0.8.0**. Match your `tapp-cli` to the server's
`get-tapp-info` version — a MAJOR.MINOR mismatch warns on connect, and the
flags below (`--tls-pin`, node-level `get-evidence`, `x-tapp`) do not exist on
older servers.

The short version of why any of this exists: the bot's signing key is derived
inside the TEE and never written down, so there is no key to install. What
replaces "put the key on the server" is a registration on chain, because that
is what the KMS checks before it hands the key over.

---

## Reaching the node at all

A hardened image has no SSH. The gRPC surface is the whole of it, on two ports
that serve the *same* service:

| port | | |
|---|---|---|
| `50052` | TLS | **use this.** Its key derives from the node's common signer, and the node's own attestation commits to that key — so the channel can be pinned to the TEE with no CA in the loop. |
| `50051` | plaintext | works, and shouldn't be reachable. `start-app` payloads — the compose, `bot.env`, every mounted file — cross it in the clear. Close it in the cloud firewall. |

Bootstrap the pin from attestation, then use it for everything:

```bash
export TAPP=https://<host>:50052
# --insecure only here: before the pin there is nothing trustworthy to compare.
# Omitting --app-id asks for the NODE's evidence (server >= 0.8.0).
export PIN=$(tapp-cli -s $TAPP --insecure get-evidence \
  | grep -oE '"tls_public_key":"0x[0-9a-f]+"' | grep -oE '0x[0-9a-f]+')
tapp-cli -s $TAPP --tls-pin $PIN get-tapp-info
```

**Re-fetch `PIN` after any reboot.** It is derived per boot, so a stale one is
indistinguishable from an interception — which is the point.

Three RPCs never cross either port: `GetSecretResource`, `GetAppSecretKey` and
`GetAppTlsCert` are served only on the node's Unix socket. Key material does not
travel the network at all.

## What a deployment actually does

Two of these steps cost gas. Everything else is gRPC.

| # | Step | Kind | Detail |
|---|---|---|---|
| 1 | `claim-config` | gRPC `ClaimConfig` | Signed EIP-191 over `ClaimConfig:<ts>` in metadata. **No transaction.** Sets the tapp-server's owner plus chain/verifier config. Once per boot. |
| 2 | `start-app --register-onchain` — measure | gRPC `StartApp{measure_only=true}` | Uploads the files, pulls the image, computes `compose_hash` / `volumes_hash` / `image_hash`. Containers do **not** start. |
| 3 | — signer lookup | gRPC `GetAppKey` | Reads this node's ephemeral signer for the app id. |
| 4 | — chain read | `getAppInfo`, `getNodeList` | Decides which of the writes below is needed. |
| 5 | — registration | **transaction** | `registerApp` (first time, stakes `minStakeAmount()`) / `updateNode` (signer changed — stake and slot preserved) / `addNode` (several nodes) / nothing (signer already listed). |
| 6 | — start | gRPC `StartApp` | Uploads the files again, writes the compose, `docker compose pull` + `up -d`. |
| 7 | `get-task-status` | gRPC | Poll until `Completed`. Step 6 is async. |
| 8 | Bot fetches its key | gRPC `GetSecretResource` over the Unix socket | **No signature** — reaching the socket is the authorization. |
| 9 | — tapp → KMS | HTTPS `POST /app-key` | tapp signs with its ephemeral key; the KMS node is pinned against the verifier. |
| 10 | — KMS checks the caller | chain read, by the KMS | `ecrecover` the signature, require the address in `getNodeList(app_id)`. |
| 11 | — decrypt | in-TEE | ECIES, using the ephemeral key. Returns 32 plaintext bytes. |
| 12 | Bot derives its key | local | `keccak256("mellow-interop-bot/operator/v1" ‖ secret)`. Never leaves the process. |
| 13 | Bot preflight | `eth_call` + `eth_getBalance` | Read-only against SourceCore / TargetCore on both chains. **No transaction.** |

So: **one transaction on a first deployment** (`registerApp`, 1 0G stake), **one
more** whenever the node's signer has changed (`updateNode`, no additional
stake), and **none at all** for an ordinary redeploy.

Steps 2 and 6 each upload the files listed under `volumes:` — that is the only
way `bot.env` reaches the CVM, and it travels over plaintext gRPC unless the
server has TLS configured.

---

## Prerequisites

- `tapp-cli` built from the tag matching the server (`tapp-cli --version` must
  match `get-tapp-info`'s `Version`).
- One private key that is **all three at once**: the tapp-server owner, the
  app's on-chain owner, and funded on 0G Galileo (1 0G stake + gas). There is no
  owner transfer — `registerApp` writes it permanently.
- `bot.env` next to the compose, from `bot.env.example`. No `*_PK` entries.
- The image published and pinned **by digest** in `docker-compose.yml`. Built by
  CI, not locally — see `.github/workflows/build-image.yml`.

---

## First deployment

Pick the network first — every value below moves together, and mixing them is
the failure this section exists to prevent.

| | mainnet (16661) | testnet Galileo (16602) |
|---|---|---|
| `RPC` | `https://evmrpc.0g.ai` | `https://evmrpc-testnet.0g.ai` |
| `REG` | `0x54874F536301c993922Dd95097e3902e7FBfe612` | `0x2Ce80374318B1d7Fb3345724457a182E0ad165c9` |
| `SCAN` | `https://tappscan.0g.ai/mainnet` | `https://tappscan.0g.ai` |
| KMS `group_pubkey` | `8c30fd0e713be395…` | `8fbb1b3f6309f35e…` |

The KMS node lists are in 0g-tapp's `docs/KMS.md`. **Do not identify a cluster by
its addresses** — both networks run five nodes in the same five GCP zones under
the same app id, and only the group key tells them apart:

```bash
curl -sk https://<kms-node>:9443/peers \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['self']['group_pubkey'][:16])"
```

Getting this wrong does not fail loudly at the KMS: the two clusters have
different masters, so the same `(app_id, material)` simply derives a *different*
key. What catches it is the startup gate, one layer later — the wrong address
holds none of the roles.

`SCAN` is a **path prefix**, not a query parameter. `?net=mainnet` switches the
web UI and nothing else; the API ignores it, and tapp builds the URL as
`{scan_url}/api/apps/{app_id}/cert`, where a query string cannot go.

```bash
export RPC=… REG=… SCAN=… KBS="https://…:9443,https://…:9443,…"
export SCANPIN=$(echo | openssl s_client -connect tappscan.0g.ai:443 -servername tappscan.0g.ai 2>/dev/null \
  | openssl x509 -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -r | cut -d' ' -f1)
export TAPP_PRIVATE_KEY="$(tr -d '[:space:]' < ~/.config/tapp/owner.key)"

# 1. Claim. No transaction, no gas. Once per boot.
#    --kbs-urls is not optional if the image baked the wrong network's cluster;
#    check `get-tapp-info` against the table above before trusting the default.
tapp-cli -s $TAPP --tls-pin $PIN claim-config \
  --chain-rpc-url $RPC --chain-contract $REG \
  --kbs-urls "$KBS" --scan-url $SCAN --scan-pubkey 0x$SCANPIN

# 2. Register and start. Idempotent -- safe to re-run.
#    --stake-wei must be >= the registry's minStakeAmount(); read it, do not guess.
tapp-cli -s $TAPP --tls-pin $PIN start-app -f docker-compose.yml --app-id <app-id> \
  --register-onchain --rpc-url $RPC --contract $REG \
  --stake-wei $(cast call $REG "minStakeAmount()(uint256)" --rpc-url $RPC)

# 3. Wait, then read the address the bot derived.
tapp-cli -s $TAPP --tls-pin $PIN get-task-status --task-id <id>
tapp-cli -s $TAPP --tls-pin $PIN get-app-logs --app-id <app-id> -n 50
```

`--kbs-urls` is omitted because the KMS cluster is baked into the CVM image.
It is also discoverable on chain, if you ever need to check it:
`getNodeList("0g-kms")` → each node's `getNode(...).teeUrl`, then swap the tapp
port `:50051` for the KMS port `:9443`.

The verifier address and pin above are 0G infrastructure, not ours, and an
address written into a runbook rots quietly. Re-derive them rather than trusting
this file if anything looks wrong — the symptom of a stale pin is the misleading
KMS error described under "Recovering from a tapp-server restart":

```bash
# the pin is the sha256 of the verifier's attested TLS public key
echo | openssl s_client -connect <verifier-host>:443 2>/dev/null \
  | openssl x509 -pubkey -noout | openssl pkey -pubin -outform der \
  | openssl dgst -sha256
# and the KMS pins it serves, in curl's --pinnedpubkey format
curl -sk https://<verifier-host>/api/apps/0g-kms/cert
```

Expect a compose lint warning about the `/run/tapp/tapp.sock` bind mount. It is
a false positive — a socket stores nothing — and the app starts anyway.

Expect the key fetch to fail for a while on a first deployment: the KMS's view
of the chain lags a fresh registration and answers `401 ... not in on-chain
signer list`. The bot retries indefinitely and says so; this is not a fault.

---

## Reading the signer address

The address is derived from `(app_id, material)` inside the TEE, so **nothing
outside can compute it** — the bot has to report it. It logs it at startup and
announces it on Telegram:

```
TEE signer ready: app_id=… material=… address=0x…
```

Two consequences worth internalising:

- **A different app id is a different address.** A dev deployment and a
  production one share no key, which is the point: a box with shell access must
  never be registered under the production app id, or anyone with sudo there can
  read the production key off the socket — permanently, since that key cannot be
  rotated without changing the address and re-doing every grant.
- `get-app-key` returns a *different* address. That is the node's ephemeral
  signer, not the bot's key. Do not fund it.

---

## Updating the bot

```bash
# CI publishes the image; put its digest in docker-compose.yml, then:
tapp-cli -s $TAPP --tls-pin $PIN stop-app  --app-id <app-id>
tapp-cli -s $TAPP --tls-pin $PIN start-app -f docker-compose.yml --app-id <app-id> \
  --register-onchain --rpc-url $RPC --contract $REG \
  --stake-wei $(cast call $REG "minStakeAmount()(uint256)" --rpc-url $RPC)
tapp-cli -s $TAPP --tls-pin $PIN update-onchain --app-id <app-id> --rpc-url $RPC --contract $REG
```

The last line is the one people forget. Without it the on-chain hashes describe
the previous image, `verify-app` reports a mismatch, and the bot keeps running —
so nothing tells you until someone checks.

**`stop-app` destroys the logs.** It runs `docker compose down`, which removes
the containers and their log files, and then deletes the app directory — the
uploaded compose and `bot.env` with it. There is no shell to recover them from.
When something has crashed, `get-app-logs` first and stop second; there is no
second chance at the evidence.

---

## Where the bot's state lives

`docker-compose.yml` declares `x-tapp: {data: encrypted}` (server >= 0.8.0). The
named volume then lands on a LUKS volume whose passphrase the KMS derives under
the `fde` namespace — kept nowhere, re-derived by any registered node, and so
still readable after a reboot.

The declaration is part of the compose, so it is hashed, registered on-chain and
measured like everything else: the storage mode is a reviewable property of the
deployment rather than a host detail. The alternatives — `plain` (persists, not
confidential), `ram` and `scratch` (confidential, gone on reboot) — each give up
one of the two properties the scheduler's state wants, and losing it silently
forfeits a reward cycle.

Secrets never belong here regardless: the signing key is derived per start and
lives only in memory.

## What survives a restart

The bot's address survives all of these. What changes is the node's ephemeral
signer, and therefore whether the on-chain registration still matches it.

| Event | Node signer | Bot address | Action needed |
|---|---|---|---|
| Container restart | unchanged | unchanged | none |
| `stop-app` + `start-app` | unchanged | unchanged | none (the CLI reports "already registered, skipping") |
| New image or compose | unchanged | unchanged | `update-onchain` |
| **tapp-server restart** | **new** | unchanged | see below |
| **CVM reboot** | **new**, node UNCLAIMED | unchanged | `claim-config`, then the recovery below |

### Recovering from a tapp-server restart

`Restart=always` is in the unit, so a crash does this on its own. A restart
restores the owner and **nothing else `claim-config` set** — the verifier and
the chain config are gone, and the app registry is empty even though the
containers are still running.

The first symptom is misleading. With no verifier, tapp falls back to ordinary
TLS validation against the KMS nodes' self-signed certificates, fails the
handshake, and reports:

```
KMS request failed: KMS https://<node>:9443 unreachable: error sending request
```

The node is reachable. The pin is missing. In that order:

```bash
# 0. The TLS pin is per boot. The old one is now wrong.
export PIN=$(tapp-cli -s $TAPP --insecure get-evidence \
  | grep -oE '"tls_public_key":"0x[0-9a-f]+"' | grep -oE '0x[0-9a-f]+')

# 1. Restore the KMS cluster AND the verifier. Both, in one call -- an image that
#    baked the other network's cluster silently reverts to it here, and a missing
#    cluster and a missing verifier produce the same misleading error.
#    claim-config cannot be re-run: it is once per boot and the owner survived,
#    so it answers ALREADY_EXISTS.
tapp-cli -s $TAPP --tls-pin $PIN update-trust-anchors \
  --kbs-urls "$KBS" --scan-url $SCAN --scan-pubkey 0x$SCANPIN

# 2. Re-register the app. This also replaces the stale on-chain signer.
tapp-cli -s $TAPP --tls-pin $PIN start-app -f docker-compose.yml --app-id <app-id> \
  --register-onchain --rpc-url $RPC --contract $REG \
  --stake-wei $(cast call $REG "minStakeAmount()(uint256)" --rpc-url $RPC)
```

Do not reach for `update-node-onchain` first: the app is no longer in the
server's memory, so it answers `App not found`. `start-app --register-onchain`
puts it back and replaces the signer in one go (`updateNode` — the stake and the
slot are preserved, nothing is staked twice).

The chain rpc/contract cannot be restored at all: `update-trust-anchors` has no
chain fields. This is cosmetic in practice — tapp-server never connects to the
chain itself, and only reports the values and writes them into the measured
event — but `get-tapp-info` and the event log will show them empty until the
next reboot and re-claim.

---

## Verifying a deployment

```bash
tapp-cli verify-app --app-id <app-id> --rpc-url $RPC --contract $REG
```

Expect `signer✓ compose✓ volumes✓ image✓ owner✓`. Anything else means the chain
and the attestation disagree about what this node is running.

---

## What has been exercised

On a dev CVM (tapp-server v0.7.0, a suffixed app id), against the production
contracts with an address holding no gas and no roles:

- The full path above, including on-chain registration and the KMS fetch.
- **Address stability**, which is what the whole design rests on: the address
  was unchanged across a container restart, a `stop-app`/`start-app`, and a
  `systemctl restart tapp-server` that re-derived the node signer and required
  re-registration. A CVM reboot was not exercised; it adds only a re-claim on
  top of the case that was.
- The startup gate holding before the first cycle and naming all eight unmet
  requirements — six role grants and two gas balances — with the `grantRole`
  call to make for each.

Not exercised: the tasks themselves. With no gas and no roles nothing can be
sent, which is deliberate — that address exists to be refused.

Not exercised on 0.8.0: everything above was learned on 0.7.0 over the plaintext
port. The 0.8.0 differences documented here — the pinned TLS channel, node-level
`get-evidence`, `x-tapp` data modes, and a tapp-server restart also reverting the
KMS cluster — come from the server source and from claiming a 0.8.0 node, not
from a full redeployment. Treat the restart-recovery sequence as reasoned rather
than replayed until someone has watched it work on 0.8.0.
