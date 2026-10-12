# Staged copy of chat-service/chat/tests/test_messaging.py (Steps 142-143)
"""RabbitMQ transport: Steps 132-143.

No broker: pika's BlockingConnection is replaced with a fake. These tests must
pass with RabbitMQ stopped - the same rule as Phase 3's gRPC client suite.
"""

import json

import pika
import pytest

from chat import events as chat_events
from chat.events import ALL_EVENT_TYPES, EventType, build_envelope, routing_key_for
from chat.messaging import connection as connection_module
from chat.messaging import publisher
from chat.models import Membership, MembershipRole, Room
from chat.services import (
    add_member,
    create_group_room,
    delete_room,
    remove_member,
    update_room,
)


def exploding_transport(envelope, routing_key):
    """Referenced by dotted path in test_events_publish_swallows_failures."""
    raise RuntimeError("broker on fire")


# --- fakes -----------------------------------------------------------------


class FakeChannel:
    def __init__(self, fail_times=0, exception=None):
        self.is_open = True
        self.published = []
        self.declared_exchanges = []
        self.confirm_delivery_called = False
        self._fail_times = fail_times
        self._exception = exception or pika.exceptions.StreamLostError("simulated")

    def exchange_declare(self, **kwargs):
        self.declared_exchanges.append(kwargs)

    def confirm_delivery(self):
        self.confirm_delivery_called = True

    def basic_publish(self, **kwargs):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._exception
        self.published.append(kwargs)

    def queue_declare(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("Chat must not declare queues (ADR-006)")


class FakeConnection:
    def __init__(self, parameters=None):
        self.is_open = True
        self.parameters = parameters
        self._channel = FakeChannel()

    def channel(self):
        return self._channel

    def close(self):
        self.is_open = False


@pytest.fixture(autouse=True)
def _reset_connection_state():
    connection_module.close_channel()
    yield
    connection_module.close_channel()


@pytest.fixture
def envelope():
    return build_envelope(
        EventType.ROOM_CREATED,
        {
            "room_id": "3f1a8e2c-0000-0000-0000-000000000001",
            "type": "group",
            "name": "Café ☕",
            "created_by": "u-1",
            "created_at": "2026-08-10T09:31:44.512871Z",
            "members": [{"user_id": "u-1", "role": "admin"}],
        },
    )


# --- Step 142: envelope, properties, encoding ------------------------------


def test_envelope_has_exactly_the_adr_012_keys(envelope):
    assert set(envelope) == {
        "event_id",
        "type",
        "version",
        "timestamp",
        "producer",
        "payload",
    }
    assert envelope["version"] == 1
    assert envelope["producer"] == "chat-service"


def test_event_ids_are_unique():
    ids = {build_envelope(EventType.ROOM_CREATED, {})["event_id"] for _ in range(1000)}
    assert len(ids) == 1000


def test_timestamp_is_z_suffixed_microsecond_utc(envelope):
    assert envelope["timestamp"].endswith("Z")
    assert "+00:00" not in envelope["timestamp"]
    fractional = envelope["timestamp"].split(".")[-1]
    assert len(fractional) == 7  # six digits plus the Z


def test_amqp_properties_mirror_the_envelope(envelope):
    properties = publisher._properties(envelope)
    assert properties.content_type == "application/json"
    assert properties.content_encoding == "utf-8"
    assert properties.delivery_mode == 2           # persistent
    assert properties.message_id == envelope["event_id"]
    assert properties.type == envelope["type"]
    assert properties.app_id == "chat-service"
    assert properties.headers == {"x-event-version": 1}


def test_encoding_is_compact_utf8_json(envelope):
    body = publisher._encode(envelope)
    assert isinstance(body, bytes)
    assert b", " not in body                      # compact separators
    assert "Café ☕" in body.decode("utf-8")       # ensure_ascii=False
    assert json.loads(body) == envelope           # lossless round trip


def test_encoding_refuses_non_primitive_payloads():
    """No default=str fallback: a UUID here is a bug, not a formatting choice."""
    import uuid

    bad = build_envelope(EventType.ROOM_CREATED, {"room_id": uuid.uuid4()})
    with pytest.raises(TypeError):
        publisher._encode(bad)


@pytest.mark.django_db
def test_every_service_payload_is_encodable(
    monkeypatch, django_capture_on_commit_callbacks
):
    """The regression net for the 'stringify at the payload boundary' rule.

    Exercises all five event types through the real service functions and
    encodes each envelope. A UUID or datetime anywhere in a payload fails
    here rather than in production at 3am.
    """
    captured = []
    monkeypatch.setattr(
        chat_events,
        "_transport",
        lambda: (lambda envelope, routing_key: captured.append((envelope, routing_key))),
    )

    with django_capture_on_commit_callbacks(execute=True):
        room = create_group_room(name="Encodable", created_by="admin-1")
    with django_capture_on_commit_callbacks(execute=True):
        add_member(room=room, user_id="u-2", added_by="admin-1")
    with django_capture_on_commit_callbacks(execute=True):
        remove_member(room=room, target_user_id="u-2", removed_by="admin-1")
    with django_capture_on_commit_callbacks(execute=True):
        update_room(room=room, changes={"name": "Renamed"}, updated_by="admin-1")
    with django_capture_on_commit_callbacks(execute=True):
        delete_room(room=room, deleted_by="admin-1")

    assert {envelope["type"] for envelope, _rk in captured} == ALL_EVENT_TYPES

    for envelope, _routing_key in captured:
        body = publisher._encode(envelope)          # must not raise
        assert json.loads(body) == envelope


# --- Step 143: routing keys ------------------------------------------------


@pytest.mark.parametrize(
    "event_type, expected",
    [
        (EventType.ROOM_CREATED, "chat.events.room_created"),
        (EventType.ROOM_UPDATED, "chat.events.room_updated"),
        (EventType.ROOM_DELETED, "chat.events.room_deleted"),
        (EventType.MEMBER_ADDED, "chat.events.member_added"),
        (EventType.MEMBER_REMOVED, "chat.events.member_removed"),
    ],
)
def test_routing_key_for_each_event_type(event_type, expected):
    assert routing_key_for(event_type) == expected


def test_every_event_type_has_a_routing_key():
    keys = {routing_key_for(event_type) for event_type in ALL_EVENT_TYPES}
    assert len(keys) == len(ALL_EVENT_TYPES)
    assert all(key.startswith("chat.events.") for key in keys)


def test_unknown_routing_keys_are_refused():
    with pytest.raises(ValueError, match="Unknown event type"):
        routing_key_for("chat.events.room_created")


def test_publish_uses_the_configured_exchange_and_routing_key(
    monkeypatch, settings, envelope
):
    channel = FakeChannel()
    monkeypatch.setattr(publisher, "get_channel", lambda: channel)

    publisher.publish_to_rabbitmq(envelope, "chat.events.room_created")

    assert len(channel.published) == 1
    published = channel.published[0]
    assert published["exchange"] == settings.RABBITMQ_EXCHANGE
    assert published["routing_key"] == "chat.events.room_created"
    assert published["mandatory"] is False        # Step 136's decision
    assert json.loads(published["body"]) == envelope


# --- Steps 134/136: topology and connection lifecycle ----------------------


def test_opening_a_channel_declares_the_exchange_and_enables_confirms(
    monkeypatch, settings
):
    monkeypatch.setattr(connection_module.pika, "BlockingConnection", FakeConnection)

    channel = connection_module.get_channel()

    assert channel.confirm_delivery_called is True
    assert channel.declared_exchanges == [
        {
            "exchange": settings.RABBITMQ_EXCHANGE,
            "exchange_type": "topic",
            "durable": True,
            "auto_delete": False,
        }
    ]


def test_the_channel_is_reused_within_one_thread(monkeypatch):
    monkeypatch.setattr(connection_module.pika, "BlockingConnection", FakeConnection)
    assert connection_module.get_channel() is connection_module.get_channel()


def test_a_channel_inherited_across_fork_is_rebuilt(monkeypatch):
    monkeypatch.setattr(connection_module.pika, "BlockingConnection", FakeConnection)
    first = connection_module.get_channel()

    real_getpid = connection_module.os.getpid
    monkeypatch.setattr(connection_module.os, "getpid", lambda: real_getpid() + 1)

    assert connection_module.get_channel() is not first


def test_chat_never_declares_a_queue(monkeypatch, settings, envelope):
    """ADR-006: consumers own their queues. FakeChannel asserts on the call."""
    monkeypatch.setattr(connection_module.pika, "BlockingConnection", FakeConnection)
    monkeypatch.setattr(publisher, "get_channel", connection_module.get_channel)
    publisher.publish_to_rabbitmq(envelope, "chat.events.room_created")


# --- Step 141: transient failures ------------------------------------------


def test_publish_reconnects_and_retries_once(monkeypatch, envelope):
    failing = FakeChannel(fail_times=1)
    healthy = FakeChannel()
    closes = []

    monkeypatch.setattr(publisher, "close_channel", lambda: closes.append(True))
    monkeypatch.setattr(
        publisher, "get_channel", lambda: healthy if closes else failing
    )

    publisher.publish_to_rabbitmq(envelope, "chat.events.room_created")

    assert closes == [True]
    assert healthy.published and not failing.published


def test_publish_gives_up_after_one_retry(monkeypatch, envelope):
    channel = FakeChannel(fail_times=99)
    monkeypatch.setattr(publisher, "get_channel", lambda: channel)
    monkeypatch.setattr(publisher, "close_channel", lambda: None)

    with pytest.raises(pika.exceptions.StreamLostError):
        publisher.publish_to_rabbitmq(envelope, "chat.events.room_created")


def test_a_nack_is_not_retried(monkeypatch, envelope):
    """NackError means the broker rejected it; retrying just nacks again."""
    channel = FakeChannel(fail_times=1, exception=pika.exceptions.NackError([]))
    closes = []
    monkeypatch.setattr(publisher, "get_channel", lambda: channel)
    monkeypatch.setattr(publisher, "close_channel", lambda: closes.append(True))

    with pytest.raises(pika.exceptions.NackError):
        publisher.publish_to_rabbitmq(envelope, "chat.events.room_created")
    assert closes == []


def test_events_publish_swallows_transport_failures(settings, caplog):
    """ADR-019: the write already committed, so the request must still succeed."""
    # remove: import logging
    # remove: monkeypatch.setattr(logging.getLogger("chat"), "propagate", True)
    settings.CHAT_EVENT_TRANSPORT = "chat.tests.test_messaging.exploding_transport"

    with caplog.at_level("ERROR", logger="chat.events"):
        result = chat_events.publish(EventType.ROOM_UPDATED, {"room_id": "r-1"})

    assert result["type"] == EventType.ROOM_UPDATED
    assert "event publish FAILED" in caplog.text
