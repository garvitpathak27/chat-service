"""RabbitMQ connection and channel lifecycle.

Two pika properties drive the design (Step 132):

1. BlockingConnection is NOT thread-safe, so state is thread-local rather
    than merely process-global. Gunicorn sync workers are single-threaded
    today, but a switch to gthread workers must not silently corrupt the
    AMQP frame stream.
2. Heartbeats are only serviced while inside pika, so an idle connection is
    eventually dropped by the broker. That is normal; the publisher
    reconnects on the next failed publish rather than trying to keep the
    connection warm.

Fork safety follows the same rule as the gRPC channel in Phase 3 Step 66: a
socket inherited across fork() is unusable, so the owning PID is recorded and
a mismatch forces a rebuild.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

import pika
from django.conf import settings

from chat.messaging.topology import declare_topology

logger = logging.getLogger("chat.messaging")

# Tuning constants rather than settings: the .env list was frozen in Phase 0
# and none of these differ per environment.
HEARTBEAT_SECONDS = 30
BLOCKED_CONNECTION_TIMEOUT = 10
SOCKET_TIMEOUT = 5
STACK_TIMEOUT = 8

_local = threading.local()


@dataclass
class _ChannelState:
    pid: int
    connection: "pika.BlockingConnection"
    channel: "pika.adapters.blocking_connection.BlockingChannel"


def _connection_parameters() -> pika.ConnectionParameters:
    # ConnectionParameters, NOT URLParameters: a password containing @ / : or #
    # silently corrupts a URL and produces an AMQPConnectionError with no clue
    # as to why (Step 133).
    return pika.ConnectionParameters(
        host=settings.RABBITMQ_HOST,
        port=settings.RABBITMQ_PORT,
        virtual_host=settings.RABBITMQ_VHOST,
        credentials=pika.PlainCredentials(
            settings.RABBITMQ_USER, settings.RABBITMQ_PASSWORD
        ),
        heartbeat=HEARTBEAT_SECONDS,
        blocked_connection_timeout=BLOCKED_CONNECTION_TIMEOUT,
        socket_timeout=SOCKET_TIMEOUT,
        stack_timeout=STACK_TIMEOUT,
        # We do our own single retry in the publisher, where we can log it and
        # rebuild the channel. pika's internal retry would silently multiply
        # the latency added to a request thread.
        connection_attempts=1,
        client_properties={"connection_name": f"chat-service-{os.getpid()}"},
    )


def get_channel():
    """Return an open, confirm-enabled channel for THIS thread and process."""
    state = getattr(_local, "state", None)
    pid = os.getpid()

    if (
        state is not None
        and state.pid == pid
        and state.connection.is_open
        and state.channel.is_open
    ):
        return state.channel

    close_channel()

    logger.info(
        "opening RabbitMQ connection host=%s vhost=%s pid=%s thread=%s",
        settings.RABBITMQ_HOST,
        settings.RABBITMQ_VHOST,
        pid,
        threading.get_ident(),
    )
    connection = pika.BlockingConnection(_connection_parameters())
    channel = connection.channel()

    declare_topology(channel)

    # Publisher confirms: basic_publish then blocks until the broker acks, and
    # raises on nack. Without this, publishing is fire-and-forget and "the
    # event was published" means only "it was written to a socket".
    channel.confirm_delivery()

    _local.state = _ChannelState(pid=pid, connection=connection, channel=channel)
    return channel


def close_channel() -> None:
    """Close and forget this thread's connection. Safe to call repeatedly."""
    state = getattr(_local, "state", None)
    _local.state = None
    if state is None:
        return
    if state.pid != os.getpid():
        # Inherited across fork(). Deliberately not closed: closing a socket we
        # do not own can send a spurious close frame on the parent's behalf.
        logger.info("discarding RabbitMQ connection inherited from pid=%s", state.pid)
        return
    try:
        if state.connection.is_open:
            state.connection.close()
    except Exception:  # pragma: no cover - best effort teardown
        logger.debug("error closing RabbitMQ connection", exc_info=True)


def connection_is_open() -> bool:
    """For the readiness probe at Step 158. Does not open a connection."""
    state = getattr(_local, "state", None)
    return bool(
        state is not None and state.pid == os.getpid() and state.connection.is_open
    )