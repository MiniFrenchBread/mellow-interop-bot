# --- Stage 1: generate the tapp gRPC stubs ----------------------------------
# Separate so grpcio-tools and protoc -- a build dependency and nothing else --
# stay out of the image that holds the signing key.
FROM python:3.13-slim AS proto

WORKDIR /build
# Kept in lockstep with the grpcio pin in requirements.txt: generated stubs
# carry a minimum-runtime assertion, so tools newer than the runtime fail at
# import with a message about the generated code rather than the pin.
RUN pip install --no-cache-dir grpcio-tools==1.83.0

COPY proto/ ./proto/
COPY scripts/gen_proto.sh ./scripts/
RUN mkdir -p src/tapp && ./scripts/gen_proto.sh


# --- Stage 2: the bot -------------------------------------------------------
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY abi/ ./abi/
COPY src/ ./src/
COPY config.json .
COPY --from=proto /build/src/tapp/ ./src/tapp/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

# The scheduler records when each task last ran so a restart cannot skip a due
# slot. Without a volume that record dies with the container and a redeploy
# just after an interval boundary silently forfeits that run -- eight hours of
# unclaimed rewards, for the reward distribution.
#
# A named volume rather than a bind mount: Docker seeds a fresh named volume
# from this directory, ownership included, which is what lets the container
# keep a non-root uid. It holds nothing but "task -> last run" timestamps, so
# living on the CVM's unencrypted /data disk costs nothing.
RUN mkdir -p /state && chown 10001:0 /state && chmod 770 /state
VOLUME ["/state"]
ENV SCHEDULER_STATE_FILE=/state/scheduler-state.json

# The lock must die with the container -- it exists to stop two schedulers
# sharing one nonce sequence, and a stale file on a persistent volume would
# outlive the process that held it.
ENV SCHEDULER_LOCK_FILE=/tmp/.scheduler.lock

# Points the image back at the source it was built from. GHCR reads this to link
# the package to the repository, but the reason it matters here is the tapp: the
# image digest is measured and registered on chain, so anyone can see WHICH image
# a node runs. Without a way back to the source, that digest is an opaque number
# and the attestation says only "it runs something". Placed after the copies so a
# change here does not invalidate the dependency layer.
LABEL org.opencontainers.image.source=https://github.com/MiniFrenchBread/mellow-interop-bot

# Non-root. Under a tapp this pairs with `group_add: ["0"]` in the compose,
# which is how a container that is not root still opens the 0660 tapp socket.
USER 10001

# The scheduler, not the one-shot oracle report: main.py runs a single pass
# and exits, which is not what a container of this bot should do.
CMD ["python", "-u", "src/scheduler.py"]
