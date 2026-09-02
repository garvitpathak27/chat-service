#!/usr/bin/env bash

set -euo pipefail

DEST="contracts/auth.proto"
CHECKSUM="contracts/auth.proto.sha256"
AUTH_PROTO_SRC="${AUTH_PROTO_SRC:-../auth-service/proto/auth.proto}"

if [[ ! -f "$DEST" || ! -f "$CHECKSUM" ]]; then
    echo "FAIL: vendored proto or checksum missing. Run ./scripts/sync-proto.sh" >&2
    exit 1
fi

recorded="$(cat "$CHECKSUM")"
actual="$(sha256sum "$DEST" | awk '{print $1}')"

if [[ "$recorded" != "$actual" ]]; then
    echo "FAIL: contracts/auth.proto was hand-edited." >&2
    echo "  recorded: $recorded" >&2
    echo "  actual:   $actual" >&2
    echo "  ADR-003 forbids editing the vendored copy. Fix Auth Service, then re-sync." >&2
    exit 1
fi

if [[ -f "$AUTH_PROTO_SRC" ]]; then
    upstream="$(sha256sum "$AUTH_PROTO_SRC" | awk '{print $1}')"

    if [[ "$upstream" != "$actual" ]]; then
        echo "FAIL: Auth Service's auth.proto has changed upstream." >&2
        echo "  Run ./scripts/sync-proto.sh && ./scripts/generate-proto.sh, then re-run tests." >&2
        exit 1
    fi
else
    echo "WARN: canonical proto not reachable at $AUTH_PROTO_SRC; checked vendored integrity only." >&2
fi

echo "OK: contracts/auth.proto matches canonical source."