# Deploying the bot on 0G Tapp

An operator runbook. Read `README.md` § "Running on 0G Tapp" first for what the
deployment *is*; this is what to type and what to expect.

The short version of why any of this exists: the bot's signing key is derived
inside the TEE and never written down, so there is no key to install. What
replaces "put the key on the server" is a registration on chain, because that
is what the KMS checks before it hands the key over.

---

## What a deployment actually does

Two of these steps cost gas. Everything else is gRPC.

| # | Step | Kind | Detail |
|---|---|---|---|
| 1 | `claim-config` | gRPC `ClaimConfig` | Signed EIP-191 over `ClaimConfig:<ts>` in metadata. **No transaction.** Sets the tapp-server's owner plus chain/verifier config. Once per boot. |
| 2 | `start-app --register-onchain` — measure | gRPC `StartApp{measure_only=true}` | Uploads the files, pulls the image, computes `compose_hash` / `volumes_hash` / `image_hash`. Containers do **not** start. |
| 3 | — signer lookup | gRPC `GetAppKey` | Reads this node's ephemeral signer for the app id. |
| 4 | — chain read | `getAppInfo`, `getNodeList` | Decides which of the writes below is needed. |
| 5 | — registration | **transaction** | `registerApp` (first time, stakes 1 0G) / `updateNode` (signer changed — stake and slot preserved) / `addNode` (several nodes) / nothing (signer already listed). |
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

```bash
TAPP=http://<host>:50051
RPC=https://evmrpc-testnet.0g.ai
REG=0x2Ce80374318B1d7Fb3345724457a182E0ad165c9
export TAPP_PRIVATE_KEY="$(tr -d '[:space:]' < ~/.config/tapp/owner.key)"

# 1. Claim the node. Chain and verifier are NOT in the CVM image, so pass them.
tapp-cli -s $TAPP claim-config \
  --chain-rpc-url $RPC --chain-contract $REG \
  --scan-url https://35.253.66.70 \
  --scan-pubkey 0x7b13d1320e7ebc93a6edf809d06cf9b44704677461c6feb2c4204e92e5587e9b

# 2. Register and start. Idempotent -- safe to re-run.
tapp-cli -s $TAPP start-app -f docker-compose.yml --app-id <app-id> \
  --register-onchain --rpc-url $RPC --contract $REG \
  --stake-wei 1000000000000000000

# 3. Wait, then read the address the bot derived.
tapp-cli -s $TAPP get-task-status --task-id <id>
tapp-cli -s $TAPP get-app-logs --app-id <app-id> -n 50
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
tapp-cli -s $TAPP stop-app  --app-id <app-id>
tapp-cli -s $TAPP start-app -f docker-compose.yml --app-id <app-id> \
  --register-onchain --rpc-url $RPC --contract $REG --stake-wei 1000000000000000000
tapp-cli -s $TAPP update-onchain --app-id <app-id> --rpc-url $RPC --contract $REG
```

The last line is the one people forget. Without it the on-chain hashes describe
the previous image, `verify-app` reports a mismatch, and the bot keeps running —
so nothing tells you until someone checks.

---

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
# 1. Restore the verifier. claim-config cannot be re-run -- it is once per boot
#    and the owner survived, so it answers ALREADY_EXISTS.
tapp-cli -s $TAPP update-trust-anchors \
  --scan-url https://35.253.66.70 \
  --scan-pubkey 0x7b13d1320e7ebc93a6edf809d06cf9b44704677461c6feb2c4204e92e5587e9b

# 2. Re-register the app. This also replaces the stale on-chain signer.
tapp-cli -s $TAPP start-app -f docker-compose.yml --app-id <app-id> \
  --register-onchain --rpc-url $RPC --contract $REG --stake-wei 1000000000000000000
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

On a dev CVM (tapp-server v0.7.0, app id `mellow-interop-bot-dev`), against the
production contracts with an address holding no gas and no roles:

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
