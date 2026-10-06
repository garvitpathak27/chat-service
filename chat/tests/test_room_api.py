"""Room API: Steps 103-109."""

import datetime as dt
from unittest import mock

import pytest
from django.db import IntegrityError
from django.urls import reverse
from rest_framework.test import APIClient

from chat.api.errors import ErrorCode
from chat.authn import authentication
from chat.grpc_clients.types import AuthIdentity
from chat.models import Membership, MembershipRole, Room, RoomType
from chat.services import create_group_room

pytestmark = pytest.mark.django_db

AUTH = {"HTTP_AUTHORIZATION": "Bearer t.o.k"}
LIST_URL = "/api/rooms/"


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def as_user(monkeypatch):
    """Authenticate as a user id that can be switched mid-test."""
    holder = {"user_id": "u-1"}

    class _FakeAuthClient:
        def verify_token(self, token):
            return AuthIdentity(user_id=holder["user_id"], roles=("USER",))

    monkeypatch.setattr(authentication, "AuthServiceClient", lambda: _FakeAuthClient())

    def _switch(user_id):
        holder["user_id"] = user_id

    return _switch


def detail_url(room):
    return reverse("chat:room-detail", kwargs={"room_id": room.pk})


# --- Steps 103-105: creation ----------------------------------------------


def test_create_group_room_returns_201_with_location(client, as_user):
    as_user("u-1")
    response = client.post(
        LIST_URL, {"type": "group", "name": "Engineering"}, format="json", **AUTH
    )
    assert response.status_code == 201
    body = response.json()
    assert response["Location"] == f"/api/rooms/{body['id']}/"


def test_create_group_room_body_shape(client, as_user):
    as_user("u-1")
    body = client.post(
        LIST_URL, {"type": "group", "name": "  Engineering  "}, format="json", **AUTH
    ).json()
    assert body["name"] == "Engineering"          # stripped by the serializer
    assert body["type"] == "group"
    assert body["created_by"] == "u-1"
    assert body["member_count"] == 1
    assert body["my_role"] == MembershipRole.ADMIN
    assert "direct_key" not in body
    assert "deleted_at" not in body


def test_created_by_comes_from_the_token_not_the_body(client, as_user):
    """Step 89. The client's value is dropped, not rejected."""
    as_user("u-1")
    response = client.post(
        LIST_URL,
        {"type": "group", "name": "Spoof", "created_by": "somebody-else"},
        format="json",
        **AUTH,
    )
    assert response.status_code == 201
    assert response.json()["created_by"] == "u-1"
    assert Room.objects.get(name="Spoof").created_by == "u-1"


def test_creator_becomes_an_admin_member(client, as_user):
    as_user("u-1")
    room_id = client.post(
        LIST_URL, {"type": "group", "name": "G"}, format="json", **AUTH
    ).json()["id"]
    membership = Membership.objects.get(room_id=room_id, user_id="u-1")
    assert membership.role == MembershipRole.ADMIN
    assert membership.left_at is None


def test_room_creation_is_atomic(client, as_user):
    """Step 105: a failed membership insert must leave no room behind."""
    as_user("u-1")
    before = Room.all_objects.count()
    with mock.patch.object(
        Membership.objects, "create", side_effect=IntegrityError("staged failure")
    ):
        response = client.post(
            LIST_URL, {"type": "group", "name": "Doomed"}, format="json", **AUTH
        )
    assert response.status_code == 500
    assert Room.all_objects.count() == before


def test_create_direct_room_makes_two_admin_members(client, as_user):
    as_user("u-1")
    response = client.post(
        LIST_URL, {"type": "direct", "participant_id": "u-2"}, format="json", **AUTH
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] is None
    assert body["member_count"] == 2
    roles = dict(
        Membership.objects.filter(room_id=body["id"]).values_list("user_id", "role")
    )
    assert roles == {"u-1": MembershipRole.ADMIN, "u-2": MembershipRole.ADMIN}


def test_second_direct_room_returns_200_with_the_same_id(client, as_user):
    as_user("u-1")
    payload = {"type": "direct", "participant_id": "u-2"}
    first = client.post(LIST_URL, payload, format="json", **AUTH)
    second = client.post(LIST_URL, payload, format="json", **AUTH)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert Room.all_objects.filter(type=RoomType.DIRECT).count() == 1


def test_direct_room_is_pair_order_independent(client, as_user):
    as_user("u-1")
    first = client.post(
        LIST_URL, {"type": "direct", "participant_id": "u-2"}, format="json", **AUTH
    )
    as_user("u-2")
    second = client.post(
        LIST_URL, {"type": "direct", "participant_id": "u-1"}, format="json", **AUTH
    )
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_deleted_direct_room_is_revived_not_duplicated(client, as_user):
    as_user("u-1")
    first = client.post(
        LIST_URL, {"type": "direct", "participant_id": "u-2"}, format="json", **AUTH
    ).json()
    room = Room.all_objects.get(pk=first["id"])
    room.soft_delete()
    for membership in Membership.all_objects.filter(room=room):
        membership.deactivate()

    second = client.post(
        LIST_URL, {"type": "direct", "participant_id": "u-2"}, format="json", **AUTH
    )
    assert second.status_code == 200
    assert second.json()["id"] == first["id"]

    room.refresh_from_db()
    assert room.deleted_at is None
    assert Membership.objects.filter(room=room).count() == 2
    assert Room.all_objects.filter(type=RoomType.DIRECT).count() == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "group"},
        {"type": "group", "name": "   "},
        {"type": "group", "name": "G", "participant_id": "u-2"},
        {"type": "direct"},
        {"type": "direct", "participant_id": "u-1"},
        {"type": "direct", "name": "x", "participant_id": "u-2"},
        {"type": "broadcast", "name": "x"},
        {"name": "no type"},
    ],
)
def test_invalid_creation_payloads_are_400(client, as_user, payload):
    as_user("u-1")
    response = client.post(LIST_URL, payload, format="json", **AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED
    assert response.json()["error"]["details"]


def test_creation_requires_authentication(client):
    response = client.post(LIST_URL, {"type": "group", "name": "G"}, format="json")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == ErrorCode.AUTH_HEADER_MISSING


# --- Steps 106-107: listing -----------------------------------------------


def test_list_returns_only_the_callers_rooms(client, as_user):
    mine = create_group_room(name="Mine", created_by="u-1")
    create_group_room(name="Theirs", created_by="u-9")
    as_user("u-1")
    names = [r["name"] for r in client.get(LIST_URL, **AUTH).json()["results"]]
    assert names == ["Mine"]
    assert str(mine.pk) in str(client.get(LIST_URL, **AUTH).json())


def test_list_excludes_soft_deleted_rooms(client, as_user):
    create_group_room(name="Live", created_by="u-1")
    create_group_room(name="Gone", created_by="u-1").soft_delete()
    as_user("u-1")
    names = [r["name"] for r in client.get(LIST_URL, **AUTH).json()["results"]]
    assert names == ["Live"]


def test_list_excludes_rooms_the_caller_left(client, as_user):
    create_group_room(name="Stayed", created_by="u-1")
    left = create_group_room(name="Left", created_by="u-1")
    Membership.objects.get(room=left, user_id="u-1").deactivate()
    as_user("u-1")
    names = [r["name"] for r in client.get(LIST_URL, **AUTH).json()["results"]]
    assert names == ["Stayed"]


def test_list_is_paginated(client, as_user):
    create_group_room(name="One", created_by="u-1")
    as_user("u-1")
    body = client.get(LIST_URL, **AUTH).json()
    assert set(body) == {"count", "next", "previous", "results"}
    assert body["count"] == 1
    assert body["previous"] is None


def test_list_respects_page_size(client, as_user):
    for index in range(5):
        create_group_room(name=f"R{index}", created_by="u-1")
    as_user("u-1")
    body = client.get(f"{LIST_URL}?page_size=2", **AUTH).json()
    assert len(body["results"]) == 2
    assert body["count"] == 5
    assert body["next"] is not None


def test_list_is_ordered_by_updated_at_desc(client, as_user):
    rooms = [create_group_room(name=name, created_by="u-1") for name in ("A", "B", "C")]
    base = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)
    for offset, room in enumerate(rooms):
        # .update() bypasses auto_now, which is the only way to set these
        # deterministically.
        Room.objects.filter(pk=room.pk).update(updated_at=base + dt.timedelta(minutes=offset))
    as_user("u-1")
    names = [r["name"] for r in client.get(LIST_URL, **AUTH).json()["results"]]
    assert names == ["C", "B", "A"]


def test_member_count_is_correct_for_a_multi_member_room(client, as_user):
    """Regression for the shared-join bug in Step 107.

    The naive filter+annotate returns 1 here for every room, silently.
    """
    room = create_group_room(name="Crowd", created_by="u-1")
    for user_id in ("u-2", "u-3", "u-4"):
        Membership.objects.create(room=room, user_id=user_id, role=MembershipRole.MEMBER)
    Membership.objects.create(room=room, user_id="u-5", role=MembershipRole.MEMBER).deactivate()

    as_user("u-1")
    result = client.get(LIST_URL, **AUTH).json()["results"][0]
    assert result["member_count"] == 4     # 1 creator + 3 active, NOT 1, NOT 5


def test_list_includes_my_role(client, as_user):
    admin_room = create_group_room(name="AdminHere", created_by="u-1")
    member_room = create_group_room(name="MemberHere", created_by="u-9")
    Membership.objects.create(room=member_room, user_id="u-1", role=MembershipRole.MEMBER)
    assert admin_room.pk

    as_user("u-1")
    by_name = {r["name"]: r["my_role"] for r in client.get(LIST_URL, **AUTH).json()["results"]}
    assert by_name == {"AdminHere": "admin", "MemberHere": "member"}


def test_list_query_count_is_constant(client, as_user, django_assert_num_queries):
    create_group_room(name="One", created_by="u-1")
    as_user("u-1")
    with django_assert_num_queries(3):        # count, page, role map
        client.get(LIST_URL, **AUTH)

    for index in range(9):
        create_group_room(name=f"Extra{index}", created_by="u-1")
    with django_assert_num_queries(3):        # still 3, not 3 + N
        client.get(LIST_URL, **AUTH)


def test_list_requires_authentication(client):
    assert client.get(LIST_URL).status_code == 401


# --- Step 108: detail -----------------------------------------------------


def test_detail_as_a_member(client, as_user):
    room = create_group_room(name="Detail", created_by="u-9")
    Membership.objects.create(room=room, user_id="u-1", role=MembershipRole.MEMBER)
    as_user("u-1")
    body = client.get(detail_url(room), **AUTH).json()
    assert body["id"] == str(room.pk)
    assert body["my_role"] == "member"
    assert body["member_count"] == 2


def test_detail_hides_internal_columns(client, as_user):
    room = create_group_room(name="Detail", created_by="u-1")
    as_user("u-1")
    body = client.get(detail_url(room), **AUTH).json()
    assert "direct_key" not in body
    assert "deleted_at" not in body


def test_detail_for_a_nonmember_is_404(client, as_user):
    room = create_group_room(name="Private", created_by="u-9")
    as_user("u-1")
    response = client.get(detail_url(room), **AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.ROOM_NOT_FOUND


def test_detail_for_a_soft_deleted_room_is_404_even_for_its_admin(client, as_user):
    room = create_group_room(name="Gone", created_by="u-1")
    room.soft_delete()
    as_user("u-1")
    assert client.get(detail_url(room), **AUTH).status_code == 404


def test_detail_for_an_unknown_uuid_is_404(client, as_user):
    as_user("u-1")
    response = client.get("/api/rooms/11111111-1111-1111-1111-111111111111/", **AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.ROOM_NOT_FOUND


def test_detail_for_a_non_uuid_path_is_404_from_the_router(client, as_user):
    as_user("u-1")
    response = client.get("/api/rooms/not-a-uuid/", **AUTH)
    assert response.status_code == 404
    # Rejected by the URL resolver, so it never reaches a view: the code is the
    # generic NOT_FOUND from handler404, not ROOM_NOT_FOUND.
    assert response.json()["error"]["code"] == ErrorCode.NOT_FOUND


def test_detail_query_count(client, as_user, django_assert_num_queries):
    room = create_group_room(name="Detail", created_by="u-1")
    as_user("u-1")
    with django_assert_num_queries(3):        # room, membership, member count
        client.get(detail_url(room), **AUTH)


# --- Step 109: information hiding across every room-scoped endpoint --------


@pytest.mark.parametrize(
    "method, suffix",
    [
        ("get", ""),
        ("patch", ""),
        ("delete", ""),
        ("get", "members/"),
        ("post", "members/"),
        ("delete", "members/u-9/"),
    ],
)
def test_every_room_scoped_endpoint_hides_the_room_from_nonmembers(
    client, as_user, method, suffix
):
    room = create_group_room(name="Private", created_by="u-9")
    as_user("outsider")
    url = f"/api/rooms/{room.pk}/{suffix}"
    response = getattr(client, method)(url, {}, format="json", **AUTH)
    assert response.status_code == 404, f"{method.upper()} {url} returned {response.status_code}"
    assert response.json()["error"]["code"] == ErrorCode.ROOM_NOT_FOUND


# --- Step 105: the lost direct-room race ----------------------------------


def test_lost_direct_room_race_returns_the_winner(monkeypatch):
    """Simulate a concurrent request winning between our SELECT and INSERT.

    The pre-check is forced to miss, so the INSERT hits the unique direct_key
    index. The IntegrityError must be caught OUTSIDE the atomic block (so the
    connection is still usable), the winner re-read, and created=False.
    """
    from chat import services

    winner = Room.objects.create(
        type=RoomType.DIRECT, created_by="u-2", direct_key=Room.build_direct_key("u-1", "u-2")
    )
    Membership.objects.create(room=winner, user_id="u-1", role=MembershipRole.ADMIN)
    Membership.objects.create(room=winner, user_id="u-2", role=MembershipRole.ADMIN)

    real_filter = Room.all_objects.filter
    calls = {"n": 0}

    def first_lookup_misses(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return Room.all_objects.none()
        return real_filter(*args, **kwargs)

    monkeypatch.setattr(Room.all_objects, "filter", first_lookup_misses)

    room, created = services.get_or_create_direct_room(creator_id="u-1", participant_id="u-2")

    assert created is False
    assert room.pk == winner.pk
    assert calls["n"] == 2                       # pre-check, then the re-read
    assert Room.all_objects.filter(type=RoomType.DIRECT).count() == 1
    assert Membership.objects.filter(room=winner).count() == 2
