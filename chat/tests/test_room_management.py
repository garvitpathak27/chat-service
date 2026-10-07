"""Room update and delete: Steps 111-118."""

import datetime as dt

import pytest
from django.db import transaction
from django.urls import reverse
from rest_framework.test import APIClient

from chat import events as chat_events
from chat.api.errors import ErrorCode
from chat.authn import authentication
from chat.events import EventType, build_envelope, isoformat_utc, routing_key_for
from chat.grpc_clients.types import AuthIdentity
from chat.models import Membership, MembershipRole, Room, RoomType
from chat.services import create_group_room, delete_room, update_room

pytestmark = pytest.mark.django_db

AUTH = {"HTTP_AUTHORIZATION": "Bearer t.o.k"}


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def as_user(monkeypatch):
    holder = {"user_id": "admin-1"}

    class _FakeAuthClient:
        def verify_token(self, token):
            return AuthIdentity(user_id=holder["user_id"], roles=("USER",))

    monkeypatch.setattr(authentication, "AuthServiceClient", lambda: _FakeAuthClient())

    def _switch(user_id):
        holder["user_id"] = user_id

    return _switch


@pytest.fixture
def events(monkeypatch):
    """Capture what the transport would have shipped.

    Note this alone is not enough for API tests: publish_on_commit() registers
    a transaction.on_commit callback, and pytest-django wraps each test in a
    transaction that never commits. Wrap the request in
    django_capture_on_commit_callbacks(execute=True) as well.
    """
    captured = []
    monkeypatch.setattr(
        chat_events,
        "_transport",
        lambda: (lambda envelope, routing_key: captured.append((envelope, routing_key))),
    )
    return captured


@pytest.fixture
def room():
    """A group room: admin-1 is admin, member-1 is a plain member."""
    room = create_group_room(name="Engineering", created_by="admin-1")
    Membership.objects.create(room=room, user_id="member-1", role=MembershipRole.MEMBER)
    return room


def url_for(room):
    return reverse("chat:room-detail", kwargs={"room_id": room.pk})


def types_of(events):
    return [envelope["type"] for envelope, _rk in events]


# --- Step 111/119: PATCH authorization ------------------------------------


def test_admin_can_rename_a_room(client, as_user, room):
    as_user("admin-1")
    response = client.patch(url_for(room), {"name": "Platform"}, format="json", **AUTH)
    assert response.status_code == 200
    assert response.json()["name"] == "Platform"
    room.refresh_from_db()
    assert room.name == "Platform"


def test_a_member_cannot_rename_a_room(client, as_user, room):
    as_user("member-1")
    response = client.patch(url_for(room), {"name": "Hijacked"}, format="json", **AUTH)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED
    room.refresh_from_db()
    assert room.name == "Engineering"


def test_a_nonmember_gets_404_not_403(client, as_user, room):
    as_user("outsider")
    response = client.patch(url_for(room), {"name": "Hijacked"}, format="json", **AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.ROOM_NOT_FOUND


def test_a_demoted_creator_can_still_rename(client, as_user, room):
    """ADR-009 + Step 85: creator-or-admin, provided membership is active."""
    membership = Membership.objects.get(room=room, user_id="admin-1")
    membership.role = MembershipRole.MEMBER
    membership.save(update_fields=["role"])
    as_user("admin-1")
    assert client.patch(url_for(room), {"name": "Still"}, format="json", **AUTH).status_code == 200


def test_a_departed_creator_gets_404(client, as_user, room):
    Membership.objects.get(room=room, user_id="admin-1").deactivate()
    as_user("admin-1")
    assert client.patch(url_for(room), {"name": "Nope"}, format="json", **AUTH).status_code == 404


def test_patch_requires_authentication(client, room):
    assert client.patch(url_for(room), {"name": "x"}, format="json").status_code == 401


def test_patch_on_a_deleted_room_is_404(client, as_user, room):
    room.soft_delete()
    as_user("admin-1")
    assert client.patch(url_for(room), {"name": "x"}, format="json", **AUTH).status_code == 404


# --- Step 112: immutable and unknown fields --------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("id", "11111111-1111-1111-1111-111111111111"),
        ("type", "direct"),
        ("created_by", "someone-else"),
        ("created_at", "2020-01-01T00:00:00Z"),
        ("updated_at", "2020-01-01T00:00:00Z"),
        ("deleted_at", "2020-01-01T00:00:00Z"),
        ("direct_key", "a:b"),
        ("member_count", 99),
    ],
)
def test_immutable_fields_are_rejected(client, as_user, room, field, value):
    as_user("admin-1")
    response = client.patch(url_for(room), {field: value}, format="json", **AUTH)
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == ErrorCode.VALIDATION_FAILED
    assert field in body["details"]


def test_unknown_fields_are_rejected(client, as_user, room):
    as_user("admin-1")
    response = client.patch(url_for(room), {"colour": "blue"}, format="json", **AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["details"] == {"colour": ["Unknown field."]}


def test_blank_name_is_rejected(client, as_user, room):
    as_user("admin-1")
    response = client.patch(url_for(room), {"name": "   "}, format="json", **AUTH)
    assert response.status_code == 400
    assert "name" in response.json()["error"]["details"]


def test_a_direct_room_cannot_be_renamed(client, as_user):
    direct = Room.objects.create(
        type=RoomType.DIRECT,
        created_by="admin-1",
        direct_key=Room.build_direct_key("admin-1", "member-1"),
    )
    Membership.objects.create(room=direct, user_id="admin-1", role=MembershipRole.ADMIN)
    as_user("admin-1")
    response = client.patch(url_for(direct), {"name": "Nope"}, format="json", **AUTH)
    assert response.status_code == 400
    assert "name" in response.json()["error"]["details"]


def test_the_service_refuses_non_mutable_fields_directly(room):
    """The guard for callers with no serializer in front (Phase 8, Phase 17)."""
    with pytest.raises(ValueError, match="refuses to write 'type'"):
        update_room(room=room, changes={"type": "direct"}, updated_by="admin-1")


# --- Step 113: updated_at --------------------------------------------------


def test_rename_bumps_updated_at(room):
    before = room.updated_at
    update_room(room=room, changes={"name": "Bumped"}, updated_by="admin-1")
    room.refresh_from_db()
    assert room.updated_at > before


def test_a_no_op_patch_does_not_bump_updated_at(room):
    before = room.updated_at
    _room, applied = update_room(room=room, changes={"name": "Engineering"}, updated_by="admin-1")
    room.refresh_from_db()
    assert applied == {}
    assert room.updated_at == before


def test_a_renamed_room_moves_to_the_top_of_the_list(client, as_user):
    """The user-visible consequence of Step 113, via ADR-017's ordering."""
    first = create_group_room(name="First", created_by="admin-1")
    second = create_group_room(name="Second", created_by="admin-1")
    base = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)
    Room.objects.filter(pk=first.pk).update(updated_at=base)
    Room.objects.filter(pk=second.pk).update(updated_at=base + dt.timedelta(minutes=1))

    as_user("admin-1")
    names = [r["name"] for r in client.get("/api/rooms/", **AUTH).json()["results"]]
    assert names == ["Second", "First"]

    client.patch(url_for(first), {"name": "First renamed"}, format="json", **AUTH)
    names = [r["name"] for r in client.get("/api/rooms/", **AUTH).json()["results"]]
    assert names == ["First renamed", "Second"]


# --- Step 114: ROOM_UPDATED ------------------------------------------------


def test_room_updated_is_published_after_commit(
    client, as_user, room, events, django_capture_on_commit_callbacks
):
    as_user("admin-1")
    with django_capture_on_commit_callbacks(execute=True):
        client.patch(url_for(room), {"name": "Platform"}, format="json", **AUTH)

    envelope, routing_key = events[-1]
    assert routing_key == "chat.events.room_updated"
    assert set(envelope) == {"event_id", "type", "version", "timestamp", "producer", "payload"}
    assert envelope["type"] == EventType.ROOM_UPDATED
    assert envelope["version"] == 1
    assert envelope["producer"] == "chat-service"
    assert envelope["timestamp"].endswith("Z")

    payload = envelope["payload"]
    assert payload["room_id"] == str(room.pk)
    assert payload["changed_fields"] == {"name": "Platform"}
    assert payload["updated_by"] == "admin-1"


def test_a_no_op_patch_publishes_nothing(
    client, as_user, room, events, django_capture_on_commit_callbacks
):
    as_user("admin-1")
    with django_capture_on_commit_callbacks(execute=True):
        client.patch(url_for(room), {"name": "Engineering"}, format="json", **AUTH)
    assert EventType.ROOM_UPDATED not in types_of(events)


def test_a_rolled_back_transaction_publishes_nothing(
    room, events, django_capture_on_commit_callbacks
):
    """Step 140's whole point, demonstrated."""
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                update_room(room=room, changes={"name": "Ghost"}, updated_by="admin-1")
                raise RuntimeError("forced rollback")

    assert EventType.ROOM_UPDATED not in types_of(events)
    room.refresh_from_db()
    assert room.name == "Engineering"


# --- Steps 115-118: DELETE -------------------------------------------------


def test_admin_can_delete_a_room(client, as_user, room):
    as_user("admin-1")
    response = client.delete(url_for(room), **AUTH)
    assert response.status_code == 204
    assert response.content == b""
    stored = Room.all_objects.get(pk=room.pk)
    assert stored.deleted_at is not None


def test_a_member_cannot_delete_a_room(client, as_user, room):
    as_user("member-1")
    assert client.delete(url_for(room), **AUTH).status_code == 403
    assert Room.all_objects.get(pk=room.pk).deleted_at is None


def test_a_nonmember_deleting_gets_404(client, as_user, room):
    as_user("outsider")
    assert client.delete(url_for(room), **AUTH).status_code == 404
    assert Room.all_objects.get(pk=room.pk).deleted_at is None


def test_a_second_delete_is_404(client, as_user, room):
    as_user("admin-1")
    assert client.delete(url_for(room), **AUTH).status_code == 204
    assert client.delete(url_for(room), **AUTH).status_code == 404


def test_deletion_deactivates_every_membership(client, as_user, room):
    as_user("admin-1")
    client.delete(url_for(room), **AUTH)
    assert Membership.objects.filter(room=room).count() == 0      # none active
    assert Membership.all_objects.filter(room=room).count() == 2  # none destroyed


def test_a_deleted_room_disappears_from_the_list_and_detail(client, as_user, room):
    as_user("admin-1")
    client.delete(url_for(room), **AUTH)
    assert client.get("/api/rooms/", **AUTH).json()["count"] == 0
    assert client.get(url_for(room), **AUTH).status_code == 404


def test_room_deleted_payload(
    client, as_user, room, events, django_capture_on_commit_callbacks
):
    as_user("admin-1")
    with django_capture_on_commit_callbacks(execute=True):
        client.delete(url_for(room), **AUTH)

    envelope, routing_key = events[-1]
    assert routing_key == "chat.events.room_deleted"
    payload = envelope["payload"]
    assert payload["room_id"] == str(room.pk)
    assert payload["type"] == "group"
    assert payload["deleted_by"] == "admin-1"
    assert payload["deleted_at"].endswith("Z")
    # Captured BEFORE deactivation - the reason for the ordering in Step 116.
    assert payload["member_user_ids"] == ["admin-1", "member-1"]


def test_repeated_deletion_publishes_exactly_one_event(room, events):
    """Step 118, at the service layer where retries actually happen."""
    assert delete_room(room=room, deleted_by="admin-1") is True
    stamp = Room.all_objects.get(pk=room.pk).deleted_at
    assert delete_room(room=room, deleted_by="admin-1") is False
    assert delete_room(room=room, deleted_by="admin-1") is False

    assert types_of(events).count(EventType.ROOM_DELETED) == 1
    assert Room.all_objects.get(pk=room.pk).deleted_at == stamp


# --- Step 114: the events module itself ------------------------------------


def test_envelope_shape():
    envelope = build_envelope(EventType.ROOM_CREATED, {"room_id": "x"})
    assert set(envelope) == {"event_id", "type", "version", "timestamp", "producer", "payload"}
    assert envelope["producer"] == "chat-service"
    assert envelope["version"] == 1
    assert len(envelope["event_id"]) == 36


def test_timestamps_use_a_literal_z_not_an_offset():
    envelope = build_envelope(EventType.ROOM_CREATED, {})
    assert envelope["timestamp"].endswith("Z")
    assert "+00:00" not in envelope["timestamp"]


def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError, match="naive datetime"):
        isoformat_utc(dt.datetime(2026, 8, 1, 12, 0))


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
def test_routing_keys(event_type, expected):
    assert routing_key_for(event_type) == expected


def test_unknown_event_types_are_refused():
    with pytest.raises(ValueError, match="Unknown event type"):
        build_envelope("ROOM_EXPLODED", {})
    with pytest.raises(ValueError, match="Unknown event type"):
        routing_key_for("room_created")   # lowercase is not a type


def test_a_transport_failure_does_not_break_the_caller(monkeypatch, caplog):
    """The write already committed; raising here would 500 a successful call."""

    def exploding_transport():
        def _transport(envelope, routing_key):
            raise RuntimeError("broker down")

        return _transport

    monkeypatch.setattr(chat_events, "_transport", exploding_transport)
    with caplog.at_level("ERROR", logger="chat.events"):
        envelope = chat_events.publish(EventType.ROOM_UPDATED, {"room_id": "x"})

    assert envelope["type"] == EventType.ROOM_UPDATED
    assert "event publish FAILED" in caplog.text
