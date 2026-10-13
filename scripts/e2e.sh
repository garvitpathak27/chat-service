#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/e2e-lib.sh
trap events_teardown EXIT
events_setup

STEP=181
ALICE_ID="$(auth_seed_user alice+e2e@example.com Alice-Password-1 | tail -1)"
ALICE_JWT="$(auth_login alice+e2e@example.com Alice-Password-1)"
expect "alice has a token" yes "$([ -n "$ALICE_JWT" ] && echo yes || echo no)"

STEP=182
expect "alice user_id matches" "$ALICE_ID" "$(auth_whoami "$ALICE_JWT")"

STEP=183
api POST /api/rooms/ "$ALICE_JWT" '{"type":"group","name":"E2E Engineering"}'
BODY="$(body)"
expect "create room status" 201 "$STATUS"
ROOM_ID="$(echo "$BODY" | jget id)"
expect "created_by is alice" "$ALICE_ID" "$(echo "$BODY" | jget created_by)"
expect "my_role admin" admin "$(echo "$BODY" | jget my_role)"

STEP=185
sleep 1
drain_events > /tmp/e2e-events.jsonl
if grep "$ROOM_ID" /tmp/e2e-events.jsonl | python3 scripts/e2e/assert_event.py \
    --type ROOM_CREATED --expect room_id="$ROOM_ID" --expect created_by="$ALICE_ID"; then
  RC=0
else
  RC=$?
fi
expect "ROOM_CREATED verified" 0 "$RC"

STEP=188
BOB_ID="$(auth_seed_user bob+e2e@example.com Bob-Password-1 | tail -1)"
BOB_JWT="$(auth_login bob+e2e@example.com Bob-Password-1)"

STEP=189
api POST "/api/rooms/$ROOM_ID/members/" "$ALICE_JWT" "{\"user_id\":\"$BOB_ID\",\"role\":\"member\"}"
expect "add bob status" 201 "$STATUS"

STEP=191
api GET "/api/rooms/$ROOM_ID/" "$BOB_JWT" > /dev/null
expect "bob reads room" 200 "$STATUS"

STEP=192
CAROL_ID="$(auth_seed_user carol+e2e@example.com Carol-Password-1 | tail -1)"
CAROL_JWT="$(auth_login carol+e2e@example.com Carol-Password-1)"
api GET "/api/rooms/$ROOM_ID/" "$CAROL_JWT" > /dev/null
expect "carol (non-member) 404" 404 "$STATUS"

STEP=193
api PATCH "/api/rooms/$ROOM_ID/" "$ALICE_JWT" '{"name":"E2E Engineering Renamed"}' > /dev/null
expect "alice renames room" 200 "$STATUS"

STEP=194
api PATCH "/api/rooms/$ROOM_ID/" "$BOB_JWT" '{"name":"Hijacked"}' > /dev/null
expect "bob rename forbidden" 403 "$STATUS"

STEP=196
api DELETE "/api/rooms/$ROOM_ID/members/$BOB_ID/" "$ALICE_JWT" > /dev/null
expect "alice removes bob" 204 "$STATUS"

STEP=197
api DELETE "/api/rooms/$ROOM_ID/members/$ALICE_ID/" "$BOB_JWT" > /dev/null
expect "bob removes alice -> 404 (bob no longer a member)" 404 "$STATUS"

STEP=199
api DELETE "/api/rooms/$ROOM_ID/" "$ALICE_JWT" > /dev/null
expect "alice deletes room" 204 "$STATUS"

STEP=201
api GET "/api/rooms/$ROOM_ID/" "$ALICE_JWT" > /dev/null
expect "deleted room now 404" 404 "$STATUS"

