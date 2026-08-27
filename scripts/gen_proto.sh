#!/usr/bin/env bash
# Generate the tapp gRPC stubs into src/tapp/.
#
# Not committed: generated protobuf code is pinned to the grpcio version that
# produced it, and a stale checked-in stub against a newer runtime fails in ways
# that read like a protocol bug. The Docker build runs this; run it yourself for
# local work that exercises the TEE path.
#
#   pip install grpcio-tools && ./scripts/gen_proto.sh

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="$root/src/tapp"

python -m grpc_tools.protoc \
  -I "$root/proto" \
  --python_out="$out" \
  --grpc_python_out="$out" \
  "$root/proto/tapp_service.proto"

# protoc emits `import tapp_service_pb2` -- a top-level import that only
# resolves if the output directory happens to be on sys.path. Rewrite it to a
# package-relative import so src/tapp stays an ordinary package.
python - "$out/tapp_service_pb2_grpc.py" <<'PY'
import sys, pathlib
path = pathlib.Path(sys.argv[1])
text = path.read_text()
fixed = text.replace(
    "\nimport tapp_service_pb2 as", "\nfrom . import tapp_service_pb2 as", 1
)
if fixed == text:
    raise SystemExit("expected import not found in {} -- protoc output changed".format(path))
path.write_text(fixed)
PY

echo "generated: $out/tapp_service_pb2.py, $out/tapp_service_pb2_grpc.py"
