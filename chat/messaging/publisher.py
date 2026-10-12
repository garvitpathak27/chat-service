from __future__ import annotations

import json
import logging

import pika
from django.conf import settings

from chat.messaging.connection import close_channel, get_channel
from chat.observability.correlation import get_request_id

logger = logging.getLogger("chat.messaging")

# Failures worth one reconnect-and-retry. All of them mean "this connection is
# gone", not "this message is bad", so a fresh channel is likely to succeed.
RECONNECTABLE = (
    pika.exceptions.AMQPConnectionError,
    pika.exceptions.ChannelClosed,
    pika.exceptions.ChannelWrongStateError,
    pika.exceptions.ConnectionWrongStateError,
    pika.exceptions.StreamLostError,
)


def _properties(envelope: dict) -> pika.BasicProperties:
    return pika.BasicProperties(
        content_type="application/json",
        content_encoding="utf-8",
        delivery_mode=2,
        message_id=envelope["event_id"],
        correlation_id=get_request_id() or None,
        type=envelope["type"],
        app_id=envelope["producer"],
        headers={"x-event-version": envelope["version"]},
    )


def _encode(envelope: dict) -> bytes:
    # No `default=` fallback on purpose: ADR-012 says payloads contain only
    # JSON primitives. A UUID or datetime here is a bug in the service that
    # built the payload, and it should surface as a loud TypeError rather than
    # being silently str()-ed into a shape consumers did not agree to.
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def publish_to_rabbitmq(envelope: dict, routing_key: str) -> None:
    body = _encode(envelope)
    properties = _properties(envelope)

    try:
        _basic_publish(routing_key, body, properties)
    except RECONNECTABLE as exc:
        # One retry on a fresh connection. Idle connections are dropped by the
        # broker as a matter of course (Step 132), so the first publish after
        # a quiet period failing is routine rather than exceptional.
        logger.warning(
            "RabbitMQ publish failed (%s); reconnecting and retrying once",
            type(exc).__name__,
        )
        close_channel()
        _basic_publish(routing_key, body, properties)


def _basic_publish(routing_key: str, body: bytes, properties) -> None:
    channel = get_channel()
    channel.basic_publish(
        exchange=settings.RABBITMQ_EXCHANGE,
        routing_key=routing_key,
        body=body,
        properties=properties,
        # mandatory=False on purpose. Chat declares no queues (ADR-006), so
        # before a consumer binds one, EVERY message is unroutable. With
        # mandatory=True plus confirms, pika raises UnroutableError and Chat
        # would fail to publish whenever a consumer is down or not yet built.
        # Unroutable messages being dropped is the intended behaviour here.
        mandatory=False,
    )
    logger.debug(
        "published %s id=%s rk=%s bytes=%s",
        properties.type,
        properties.message_id,
        routing_key,
        len(body),
    )
