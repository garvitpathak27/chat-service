#!/usr/bin/env python3
"""Verify a domain event envelope read from stdin (one JSON object per line,
as produced by e2e-lib.sh's drain_events) matches the expected type/payload.

Usage:
    python3 assert_event.py --type ROOM_CREATED --expect room_id=<id> --expect created_by=<id>

--producer / --routing-prefix (defaults: chat-service / chat.events) let another
producer on the same ADR-012 contract (messages-websocket-service) be validated
by this one script. The defaults keep every existing call site unchanged.

Reads all lines from stdin, finds the first message whose envelope "type"
matches --type, then checks every --expect key=value against payload[key]
(falling back to top-level envelope[key] if not in payload). Exits 0 and
prints the matched envelope on success; exits 1 with a diagnostic on failure.
"""
import json
import sys
import argparse
import uuid
from datetime import datetime, timezone

REQUIRED_KEYS = {"event_id", "type", "version", "timestamp", "producer", "payload"}


def fail(msg, context=None):
    print(f"ASSERT FAIL: {msg}", file=sys.stderr)
    if context is not None:
        print(json.dumps(context, indent=2), file=sys.stderr)
    sys.exit(1)


def validate_envelope_shape(envelope, routing_key, message_id, producer="chat-service", routing_prefix="chat.events"):
    missing = REQUIRED_KEYS - envelope.keys()
    if missing:
        fail(f"envelope missing required keys: {sorted(missing)}", envelope)

    if envelope["version"] != 1:
        fail(f"expected version=1, got {envelope['version']!r}", envelope)

    if envelope["producer"] != producer:
        fail(f"expected producer={producer!r}, got {envelope['producer']!r}", envelope)

    ts = envelope["timestamp"]
    if not isinstance(ts, str) or not ts.endswith("Z"):
        fail(f"timestamp must be a Z-suffixed UTC string, got {ts!r}", envelope)
    try:
        datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        fail(f"timestamp is not valid ISO-8601: {ts!r}", envelope)

    try:
        uuid.UUID(str(envelope["event_id"]))
    except (ValueError, AttributeError):
        fail(f"event_id is not a valid UUID: {envelope['event_id']!r}", envelope)

    if routing_key is not None and routing_key != f"{routing_prefix}.{envelope['type'].lower()}":
        fail(
            f"routing_key {routing_key!r} does not match event type "
            f"'{routing_prefix}.{envelope['type']}'",
            envelope,
        )

    if message_id is not None and message_id != envelope["event_id"]:
        fail(
            f"AMQP message_id {message_id!r} does not equal envelope "
            f"event_id {envelope['event_id']!r}",
            envelope,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", required=True, help="expected envelope type, e.g. ROOM_CREATED")
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="payload (or envelope) field that must equal VALUE; repeatable",
    )
    parser.add_argument("--producer", default="chat-service", help="expected envelope producer")
    parser.add_argument("--routing-prefix", default="chat.events", help="routing key must be <prefix>.<lowercase type>")
    args = parser.parse_args()

    expectations = []
    for item in args.expect:
        if "=" not in item:
            fail(f"--expect must be KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        expectations.append((k, v))

    lines = [line for line in sys.stdin if line.strip()]
    if not lines:
        fail(f"no events received on stdin while looking for type={args.type!r}")

    matched = None
    seen_types = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            fail(f"could not parse JSON line: {line!r}")

        envelope = record.get("envelope", record)
        seen_types.append(envelope.get("type"))
        if envelope.get("type") == args.type:
            validate_envelope_shape(
                envelope,
                routing_key=record.get("routing_key"),
                message_id=record.get("message_id"),
                producer=args.producer,
                routing_prefix=args.routing_prefix,
            )
            matched = envelope
            break

    if matched is None:
        fail(f"no event of type {args.type!r} found; saw types: {seen_types}")

    payload = matched.get("payload", {})
    for key, expected_value in expectations:
        actual = payload.get(key, matched.get(key))
        if str(actual) != str(expected_value):
            fail(
                f"field {key!r} mismatch: expected {expected_value!r}, got {actual!r}",
                matched,
            )

    print(f"OK: {args.type} event verified")
    print(json.dumps(matched, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
