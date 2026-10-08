# Staged copy of chat-service/chat/tests/test_membership_api.py (Step 131)
"""Membership API: Steps 120-130."""

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from chat import events as chat_events
from chat.api.errors import ErrorCode
from chat.authn import authentication
from chat.events import EventType
from chat.grpc_clients.types import AuthIdentity
from chat.models import Membership, MembershipRole, Room, RoomType
from chat.services import add_member, create_group_room, remove_member

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
    captured = []
    monkeypatch.setattr(
        chat_events,
        "_transport",
        lambda: (lambda envelope, routing_key: captured.append((envelope, routing_key))),
    )
    return captured


@pytest.fixture
def room():
    """admin-1 is admin; member-1 is a plain member."""
    room = create_group_room(name="Engineering", created_by="admin-1")
    Membership.objects.create(room=room, user_id="member-1", role=MembershipRole.MEMBER)
    return room


@pytest.fixture
def direct_room():
    room = Room.objects.create(
        type=RoomType.DIRECT,
        created_by="admin-1",
        direct_key=Room.build_direct_key("admin-1", "member-1"),
    )
    Membership.objects.bulk_create(
        [
            Membership(room=room, user_id="admin-1", role=MembershipRole.ADMIN),
            Membership(room=room, user_id="member-1", role=MembershipRole.ADMIN),
        ]
    )
    return room


def members_url(room):
    return reverse("chat:room-member-list", kwargs={"room_id": room.pk})


def member_url(room, user_id):
    return reverse(
        "chat:room-member-detail", kwargs={"room_id": room.pk, "user_id": user_id}
    )


def payloads_of(events, event_type):
    return [e["payload"] for e, _rk in events if e["type"] == event_type]


# --- Steps 120-123: adding -------------------------------------------------


def test_admin_can_add_a_member(client, as_user, room):
    as_user("admin-1")
    response = client.post(members_url(room), {"user_id": "u-2"}, format="json", **AUTH)
    assert response.status_code == 201
    assert response.json() == {
        "user_id": "u-2",
        "role": "member",
        "joined_at": response.json()["joined_at"],
    }
    assert response["Location"] == f"/api/rooms/{room.pk}/members/u-2/"


def test_added_members_are_always_plain_members(client, as_user, room):
    """Step 123: a role in the body is ignored, not honoured."""
    as_user("admin-1")
    response = client.post(
        members_url(room), {"user_id": "u-2", "role": "admin"}, format="json", **AUTH
    )
    assert response.status_code == 201
    assert response.json()["role"] == "member"
    assert Membership.objects.get(room=room, user_id="u-2").role == MembershipRole.MEMBER


def test_a_member_cannot_add_members(client, as_user, room):
    as_user("member-1")
    response = client.post(members_url(room), {"user_id": "u-2"}, format="json", **AUTH)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED
    assert not Membership.all_objects.filter(room=room, user_id="u-2").exists()


def test_a_nonmember_adding_gets_404(client, as_user, room):
    as_user("outsider")
    response = client.post(members_url(room), {"user_id": "u-2"}, format="json", **AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.ROOM_NOT_FOUND


def test_adding_an_active_member_again_is_409(client, as_user, room):
    as_user("admin-1")
    response = client.post(
        members_url(room), {"user_id": "member-1"}, format="json", **AUTH
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.MEMBER_ALREADY_EXISTS


def test_adding_to_a_direct_room_is_400(client, as_user, direct_room):
    as_user("admin-1")
    response = client.post(
        members_url(direct_room), {"user_id": "u-3"}, format="json", **AUTH
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.DIRECT_ROOM_IMMUTABLE


@pytest.mark.parametrize("user_id", ["", "   ", "x" * 65])
def test_invalid_user_ids_are_400(client, as_user, room, user_id):
    as_user("admin-1")
    response = client.post(members_url(room), {"user_id": user_id}, format="json", **AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED


def test_a_nonexistent_auth_user_is_accepted(client, as_user, room):
    """ADR-018: Chat does not verify that the id names a real Auth user."""
    as_user("admin-1")
    response = client.post(
        members_url(room), {"user_id": "definitely-not-real"}, format="json", **AUTH
    )
    assert response.status_code == 201


def test_adding_requires_authentication(client, room):
    assert client.post(members_url(room), {"user_id": "u-2"}, format="json").status_code == 401


def test_member_added_event(client, as_user, room, events, django_capture_on_commit_callbacks):
    as_user("admin-1")
    with django_capture_on_commit_callbacks(execute=True):
        client.post(members_url(room), {"user_id": "u-2"}, format="json", **AUTH)

    envelope, routing_key = events[-1]
    assert routing_key == "chat.events.member_added"
    assert envelope["type"] == EventType.MEMBER_ADDED
    payload = envelope["payload"]
    assert payload["room_id"] == str(room.pk)
    assert payload["user_id"] == "u-2"
    assert payload["role"] == "member"
    assert payload["added_by"] == "admin-1"
    assert payload["joined_at"].endswith("Z")


# --- Step 129: rejoin ------------------------------------------------------


def test_rejoin_reuses_the_row_and_resets_role_and_joined_at(client, as_user, room):
    original = add_member(room=room, user_id="u-2", added_by="admin-1")
    original.role = MembershipRole.ADMIN
    original.save(update_fields=["role"])
    original_id, original_joined = original.pk, original.joined_at

    remove_member(room=room, target_user_id="u-2", removed_by="admin-1")

    as_user("admin-1")
    response = client.post(members_url(room), {"user_id": "u-2"}, format="json", **AUTH)
    assert response.status_code == 201

    rejoined = Membership.objects.get(room=room, user_id="u-2")
    assert rejoined.pk == original_id
    assert rejoined.role == MembershipRole.MEMBER
    assert rejoined.joined_at > original_joined
    assert Membership.all_objects.filter(room=room, user_id="u-2").count() == 1


def test_rejoin_publishes_member_added_not_a_new_type(
    client, as_user, room, events, django_capture_on_commit_callbacks
):
    add_member(room=room, user_id="u-2", added_by="admin-1")
    remove_member(room=room, target_user_id="u-2", removed_by="admin-1")
    events.clear()

    as_user("admin-1")
    with django_capture_on_commit_callbacks(execute=True):
        client.post(members_url(room), {"user_id": "u-2"}, format="json", **AUTH)

    assert [e["type"] for e, _rk in events] == [EventType.MEMBER_ADDED]


# --- Steps 125-126: listing ------------------------------------------------


def test_a_member_can_list_the_roster(client, as_user, room):
    as_user("member-1")
    body = client.get(members_url(room), **AUTH).json()
    assert body["count"] == 2
    assert {entry["user_id"] for entry in body["results"]} == {"admin-1", "member-1"}


def test_the_roster_entries_expose_no_membership_id(client, as_user, room):
    as_user("member-1")
    entry = client.get(members_url(room), **AUTH).json()["results"][0]
    assert set(entry) == {"user_id", "role", "joined_at"}


def test_admins_are_listed_first(client, as_user, room):
    """Pins the role ordering so a rename cannot silently reshuffle it."""
    add_member(room=room, user_id="a-zzz", added_by="admin-1")
    as_user("admin-1")
    results = client.get(members_url(room), **AUTH).json()["results"]
    assert results[0]["role"] == "admin"
    assert [entry["role"] for entry in results] == ["admin", "member", "member"]


def test_departed_members_are_not_listed(client, as_user, room):
    add_member(room=room, user_id="u-2", added_by="admin-1")
    remove_member(room=room, target_user_id="u-2", removed_by="admin-1")
    as_user("admin-1")
    body = client.get(members_url(room), **AUTH).json()
    assert {entry["user_id"] for entry in body["results"]} == {"admin-1", "member-1"}


def test_a_nonmember_listing_gets_404(client, as_user, room):
    as_user("outsider")
    assert client.get(members_url(room), **AUTH).status_code == 404


def test_listing_a_deleted_rooms_members_is_404(client, as_user, room):
    room.soft_delete()
    as_user("admin-1")
    assert client.get(members_url(room), **AUTH).status_code == 404


def test_the_roster_is_paginated(client, as_user, room):
    for index in range(5):
        add_member(room=room, user_id=f"u-{index}", added_by="admin-1")
    as_user("admin-1")
    body = client.get(f"{members_url(room)}?page_size=3", **AUTH).json()
    assert set(body) == {"count", "next", "previous", "results"}
    assert body["count"] == 7
    assert len(body["results"]) == 3


def test_roster_query_count_is_constant(client, as_user, room, django_assert_num_queries):
    as_user("admin-1")
    with django_assert_num_queries(4):   # room, membership, count, page
        client.get(members_url(room), **AUTH)

    for index in range(10):
        add_member(room=room, user_id=f"u-{index}", added_by="admin-1")
    with django_assert_num_queries(4):   # still 4, not 4 + N
        client.get(members_url(room), **AUTH)


# --- Steps 127-128: removal ------------------------------------------------


def test_admin_can_remove_a_member(client, as_user, room):
    as_user("admin-1")
    response = client.delete(member_url(room, "member-1"), **AUTH)
    assert response.status_code == 204
    assert not Membership.objects.filter(room=room, user_id="member-1").exists()
    assert Membership.all_objects.get(room=room, user_id="member-1").left_at is not None


def test_a_member_can_remove_themselves(client, as_user, room):
    as_user("member-1")
    assert client.delete(member_url(room, "member-1"), **AUTH).status_code == 204


def test_a_member_cannot_remove_someone_else(client, as_user, room):
    add_member(room=room, user_id="u-2", added_by="admin-1")
    as_user("member-1")
    response = client.delete(member_url(room, "u-2"), **AUTH)
    assert response.status_code == 403
    assert Membership.objects.filter(room=room, user_id="u-2").exists()


def test_a_nonmember_removing_gets_404(client, as_user, room):
    as_user("outsider")
    assert client.delete(member_url(room, "member-1"), **AUTH).status_code == 404


def test_removing_an_unknown_user_is_member_not_found(client, as_user, room):
    as_user("admin-1")
    response = client.delete(member_url(room, "ghost"), **AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.MEMBER_NOT_FOUND


def test_removing_an_already_departed_user_is_404(client, as_user, room):
    as_user("admin-1")
    assert client.delete(member_url(room, "member-1"), **AUTH).status_code == 204
    response = client.delete(member_url(room, "member-1"), **AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.MEMBER_NOT_FOUND


def test_removing_from_a_direct_room_is_400(client, as_user, direct_room):
    as_user("admin-1")
    response = client.delete(member_url(direct_room, "member-1"), **AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.DIRECT_ROOM_IMMUTABLE


def test_a_removed_member_loses_access_to_the_room(client, as_user, room):
    as_user("admin-1")
    client.delete(member_url(room, "member-1"), **AUTH)
    as_user("member-1")
    detail = reverse("chat:room-detail", kwargs={"room_id": room.pk})
    assert client.get(detail, **AUTH).status_code == 404


def test_the_last_admin_cannot_leave(client, as_user, room):
    as_user("admin-1")
    response = client.delete(member_url(room, "admin-1"), **AUTH)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.LAST_ADMIN
    assert Membership.objects.filter(room=room, user_id="admin-1").exists()


def test_the_last_admin_cannot_be_removed_by_themselves_via_admin_path(client, as_user, room):
    """Same invariant whichever path reaches it."""
    promoted = Membership.objects.get(room=room, user_id="member-1")
    promoted.role = MembershipRole.ADMIN
    promoted.save(update_fields=["role"])

    as_user("admin-1")
    assert client.delete(member_url(room, "member-1"), **AUTH).status_code == 204
    response = client.delete(member_url(room, "admin-1"), **AUTH)
    assert response.status_code == 409


def test_an_admin_can_leave_when_another_admin_remains(client, as_user, room):
    promoted = Membership.objects.get(room=room, user_id="member-1")
    promoted.role = MembershipRole.ADMIN
    promoted.save(update_fields=["role"])

    as_user("admin-1")
    assert client.delete(member_url(room, "admin-1"), **AUTH).status_code == 204
    assert Membership.objects.filter(room=room, role=MembershipRole.ADMIN).count() == 1


def test_removing_a_plain_member_never_triggers_the_last_admin_rule(client, as_user, room):
    as_user("admin-1")
    assert client.delete(member_url(room, "member-1"), **AUTH).status_code == 204


def test_removal_takes_a_row_lock(room):
    """Step 128: the invariant is only safe if the read is locked."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        remove_member(room=room, target_user_id="member-1", removed_by="admin-1")
    assert any("FOR UPDATE" in query["sql"].upper() for query in ctx.captured_queries)


# --- Step 130: MEMBER_REMOVED ----------------------------------------------


def test_member_removed_reason_is_removed_for_admin_action(
    client, as_user, room, events, django_capture_on_commit_callbacks
):
    as_user("admin-1")
    with django_capture_on_commit_callbacks(execute=True):
        client.delete(member_url(room, "member-1"), **AUTH)

    envelope, routing_key = events[-1]
    assert routing_key == "chat.events.member_removed"
    payload = envelope["payload"]
    assert payload["user_id"] == "member-1"
    assert payload["removed_by"] == "admin-1"
    assert payload["reason"] == "removed"
    assert payload["removed_at"].endswith("Z")


def test_member_removed_reason_is_left_for_self_leave(
    client, as_user, room, events, django_capture_on_commit_callbacks
):
    as_user("member-1")
    with django_capture_on_commit_callbacks(execute=True):
        client.delete(member_url(room, "member-1"), **AUTH)

    payload = payloads_of(events, EventType.MEMBER_REMOVED)[-1]
    assert payload["reason"] == "left"
    assert payload["removed_by"] == payload["user_id"] == "member-1"


def test_a_refused_removal_publishes_nothing(
    client, as_user, room, events, django_capture_on_commit_callbacks
):
    as_user("admin-1")
    with django_capture_on_commit_callbacks(execute=True):
        response = client.delete(member_url(room, "admin-1"), **AUTH)
    assert response.status_code == 409
    assert payloads_of(events, EventType.MEMBER_REMOVED) == []


def test_room_deletion_publishes_one_event_not_one_per_member(
    client, as_user, room, events, django_capture_on_commit_callbacks
):
    """The ADR-011 amendment in Step 130, asserted."""
    add_member(room=room, user_id="u-2", added_by="admin-1")
    events.clear()

    as_user("admin-1")
    detail = reverse("chat:room-detail", kwargs={"room_id": room.pk})
    with django_capture_on_commit_callbacks(execute=True):
        assert client.delete(detail, **AUTH).status_code == 204

    types = [envelope["type"] for envelope, _rk in events]
    assert types == [EventType.ROOM_DELETED]
    assert payloads_of(events, EventType.ROOM_DELETED)[0]["member_user_ids"] == [
        "admin-1",
        "member-1",
        "u-2",
    ]
