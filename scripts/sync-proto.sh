#!/usr/bin/env bash

set -euo pipefail
# this is so that we can see errors properly 

AUTH_PROTO_SRC="${AUTH_PROTO_SRC:-../auth-service/proto/auth.proto}"

DEST="contracts/auth.proto"
CHECKSUM="contracts/auth.proto.sha256"

if [[ ! -f "$AUTH_PROTO_SRC" ]]; then
    echo "ERROR: canonical proto not found at $AUTH_PROTO_SRC" >&2
    exit 1
fi

cp "$AUTH_PROTO_SRC" "$DEST"

sha256sum "$DEST" | awk '{print $1}' > "$CHECKSUM"

echo "Synced $AUTH_PROTO_SRC -> $DEST"
echo "sha256: $(cat "$CHECKSUM")"
echo "Now regenerate gRPC stubs."
