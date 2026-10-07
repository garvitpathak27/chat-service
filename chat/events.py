"""Domain events: envelope construction and after-commit dispatch.

ADR-011 defines the five v1 event types and their payloads; ADR-012 defines
the envelope; ADR-006 defines the exchange and routing keys. This module owns
all three, plus the rule that an event is dispatched only after the database
transaction that produced it has COMMITTED (Step 140).

It deliberately knows nothing about RabbitMQ. The transport is resolved from
settings.CHAT_EVENT_TRANSPORT and defaults to a logging no-op, so events are
real, well-formed and testable from Phase 7 onward. Phase 9 Step 136 points
that setting at the real publisher and nothing else changes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from django.conf import settings
from django.db import transaction
from django.utils.module_loading import import_string

logger = logging.getLogger("chat.events")

EVENT_SCHEMA_VERSION = 1
PRODUCER = "chat-service"


class EventType:
    """ADR-011. SCREAMING_SNAKE_CASE, matching the `type` field on the wire."""

    ROOM_CREATED = "ROOM_CREATED"
    ROOM_UPDATED = "ROOM_UPDATED"
    ROOM_DELETED = "ROOM_DELETED"
    MEMBER_ADDED = "MEMBER_ADDED"
    MEMBER_REMOVED = "MEMBER_REMOVED"


ALL_EVENT_TYPES = frozenset(
    value
    for name, value in vars(EventType).items()
    if not name.startswith("_") and isinstance(value, str)
)


def routing_key_for(event_type: str) -> str:
    """chat.events.room_created, etc. (ADR-006)."""
    if event_type not in ALL_EVENT_TYPES:
        raise ValueError(f"Unknown event type: {event_type!r}")
    return f"chat.events.{event_type.lower()}"


def isoformat_utc(value: datetime) -> str:
    """ADR-012: UTC, microsecond precision, literal Z - never +00:00.

    Python's datetime.isoformat() emits '+00:00', which several Java
    Instant/OffsetDateTime parsers on the consumer side reject. The
    replacement at the end is the whole reason this helper exists.

    A naive datetime is refused rather than assumed to be UTC: silently
    guessing would ship timestamps wrong by the local offset, and USE_TZ=True
    (Step 27) means a naive value here is a bug upstream.
    """
    if value.tzinfo is None:
        raise ValueError(
            "refusing to serialise a naive datetime; USE_TZ=True means every "
            "timestamp reaching an event should already be timezone-aware"
        )
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def build_envelope(event_type: str, payload: dict) -> dict:
    """ADR-012's envelope. The only place it is constructed."""
    if event_type not in ALL_EVENT_TYPES:
        raise ValueError(f"Unknown event type: {event_type!r}")
    return {
        "event_id": str(uuid.uuid4()),
        "type": event_type,
        "version": EVENT_SCHEMA_VERSION,
        "timestamp": isoformat_utc(datetime.now(timezone.utc)),
        "producer": PRODUCER,
        "payload": payload,
    }


def log_only_transport(envelope: dict, routing_key: str) -> None:
    """Default transport. Replaced at Step 136 by the RabbitMQ publisher."""
    logger.info(
        "event type=%s id=%s rk=%s (no broker configured)",
        envelope["type"],
        envelope["event_id"],
        routing_key,
    )


def _transport():
    dotted = getattr(
        settings, "CHAT_EVENT_TRANSPORT", "chat.events.log_only_transport"
    )
    return import_string(dotted)


def publish(event_type: str, payload: dict) -> dict:
    """Build and ship one event immediately. Prefer publish_on_commit().

    Transport failures are logged and swallowed - see the decision note in
    this step. Returns the envelope so callers and tests can inspect it.
    """
    envelope = build_envelope(event_type, payload)
    routing_key = routing_key_for(event_type)
    try:
        _transport()(envelope, routing_key)
    except Exception:
        logger.exception(
            "event publish FAILED type=%s id=%s rk=%s",
            event_type,
            envelope["event_id"],
            routing_key,
        )
    return envelope


def publish_on_commit(event_type: str, payload: dict) -> None:
    """Schedule publication for after the current transaction commits.

    Outside a transaction, on_commit() runs the callback immediately, so this
    is safe to call from anywhere.

    The payload is captured NOW, inside the transaction, while the objects are
    loaded and consistent - but shipped LATER, once the write is durable. That
    ordering is the entire point of Step 140: an event describing a rolled-back
    transaction is worse than no event, because consumers cannot un-react.
    """
    transaction.on_commit(lambda: publish(event_type, payload))



"""further optimisation 

• Step 2 & 3 (The Database Transaction): Instead of publishing directly to RabbitMQ during the transaction (which can cause slow rollbacks if the broker is down), write both the room update and the event payload to the same database. This is called the Transactional Outbox Pattern.
• Step 4 (Shipping to RabbitMQ): A separate background worker (or a Change Data Capture tool like Debezium) polls the database for new events and publishes them to RabbitMQ. This guarantees at-least-once delivery.
• Step 5 (Consuming Services): Because events can sometimes be duplicated during network glitches, downstream services must be idempotent. They should check if they have already processed ROOM_UPDATED for version/timestamp X before applying the change.

"""