#!/usr/bin/env bash

set -euo pipefail

PROTO_DIR="contracts"
PROTO_FILE="auth.proto"
OUT_DIR="chat/grpc_clients/generated"

if [[ ! -f "$PROTO_DIR/$PROTO_FILE" ]]; then
  echo "ERROR: $PROTO_DIR/$PROTO_FILE missing. Run ./scripts/sync-proto.sh first." >&2
  exit 1
fi

# Never generate from a tampered proto.
./scripts/check-proto-drift.sh

mkdir -p "$OUT_DIR"

poetry run python -m grpc_tools.protoc \
  -I "$PROTO_DIR" \
  --python_out="$OUT_DIR" \
  --pyi_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "$PROTO_FILE"

# protoc emits `import auth_pb2 as auth__pb2`, an absolute import.
# Rewrite it to a package-relative import so the generated stubs work as:
# chat.grpc_clients.generated.auth_pb2_grpc
poetry run python - "$OUT_DIR" << 'PYEOF'
import pathlib
import re
import sys

out_dir = pathlib.Path(sys.argv[1])
path = out_dir / "auth_pb2_grpc.py"

text = path.read_text()

old = "import auth_pb2 as auth__pb2"
new = "from . import auth_pb2 as auth__pb2"

if old not in text:
    if new in text:
        print("OK: auth_pb2_grpc.py already uses a relative import.")
    else:
        raise SystemExit(
            "ERROR: expected generated auth_pb2 import not found; "
            "inspect auth_pb2_grpc.py"
        )
else:
    path.write_text(text.replace(old, new, 1))
    print("OK: rewrote auth_pb2 import to package-relative form.")
PYEOF