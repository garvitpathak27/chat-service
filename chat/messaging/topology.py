"""AMQP topology owned by Chat Service.

ADR-006: Chat declares the EXCHANGE and nothing else. Queues and bindings
belong to consumers (WebSocket Service, Mail Service, Chat Messages Service),
which keeps producer and consumer lifecycles independent - Chat can be
deployed without knowing who is listening, and a consumer can add a binding
without a Chat release.

Declaration is idempotent: re-declaring an exchange with identical arguments
is a no-op, so this runs on every channel open with no cost.
"""

from django.conf import settings

EXCHANGE_TYPE = "topic"
EXCHANGE_DURABLE = True
EXCHANGE_AUTO_DELETE = False


def declare_topology(channel) -> None:
    """Declare the chat.events exchange. Safe to call repeatedly."""
    channel.exchange_declare(
        exchange=settings.RABBITMQ_EXCHANGE,
        exchange_type=EXCHANGE_TYPE,
        durable=EXCHANGE_DURABLE,
        auto_delete=EXCHANGE_AUTO_DELETE,
    )
