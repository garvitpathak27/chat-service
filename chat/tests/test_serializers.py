"""Serializer behaviour: Steps 93-97."""

import pytest
from rest_framework import serializers as drf_serializers
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from chat.api.exceptions import DirectRoomImmutable, MemberAlreadyExists, MemberNotFound
from chat.grpc_clients.types import AuthIdentity
from chat.models import Membership, MembershipRole, Room, RoomType
from chat.permissions import SERVER_CONTROLLED_FIELDS
from chat.serializers import (
    MembershipCreateSerializer,
    MembershipSerializer,
    RoomSerializer,
    RoomUpdateSerializer,
    validate_membership_removal,
)

pytestmark = pytest.mark.django_db

factory = APIRequestFactory()


def request_as(user_id="u-1"):
    request = Request(factory.post("/api/rooms/"))
    request._authenticator = None
    request._user = None
    request._auth = AuthIdentity(user_id=user_id, roles=("USER",))
    return request


@pytest.fixture
def ctx():
    return {"request": request_as("u-1")}


@pytest.fixture
def group_room():
    room = Room.objects.create(type=RoomType.GROUP, name="Engineering", created_by="u-1")
    Membership.objects.create(room=room, user_id="u-1", role=MembershipRole.ADMIN)
    return room


@pytest.fixture
def direct_room():
    return Room.objects.create(
        type=RoomType.DIRECT,
        created_by="u-1",
        direct_key=Room.build_direct_key("u-1", "u-2"),
    )


# --- Step 93: field exposure ----------------------------------------------


def test_internal_columns_are_never_exposed():
    fields = RoomSerializer().fields
    assert "direct_key" not in fields
    assert "deleted_at" not in fields


def test_server_owned_fields_are_read_only():
    fields = RoomSerializer().fields
    for name in ("id", "created_by", "created_at", "updated_at"):
        assert fields[name].read_only is True


def test_participant_id_is_write_only():
    assert RoomSerializer().fields["participant_id"].write_only is True


def test_client_cannot_set_created_by(ctx):
    serializer = RoomSerializer(
        data={"type": "group", "name": "G", "created_by": "somebody-else"}, context=ctx
    )
    assert serializer.is_valid(), serializer.errors
    assert "created_by" not in serializer.validated_data


def test_client_cannot_set_id(ctx):
    serializer = RoomSerializer(
        data={"type": "group", "name": "G", "id": "11111111-1111-1111-1111-111111111111"},
        context=ctx,
    )
    assert serializer.is_valid(), serializer.errors
    assert "id" not in serializer.validated_data


def test_no_server_controlled_field_is_writable():
    """Mirrors the import-time guard, so the failure is a named test."""
    writable = {
        name for name, field in RoomSerializer().fields.items() if not field.read_only
    }
    assert not (writable & SERVER_CONTROLLED_FIELDS)


# --- Step 95: room creation validation ------------------------------------


@pytest.mark.parametrize(
    "payload, expect_valid, expected_error_field",
    [
        ({"type": "group", "name": "Engineering"}, True, None),
        ({"type": "group", "name": "   "}, False, "name"),
        ({"type": "group"}, False, "name"),
        ({"type": "group", "name": "G", "participant_id": "u-2"}, False, "participant_id"),
        ({"type": "direct", "participant_id": "u-2"}, True, None),
        ({"type": "direct"}, False, "participant_id"),
        ({"type": "direct", "participant_id": "u-1"}, False, "participant_id"),
        ({"type": "direct", "name": "nope", "participant_id": "u-2"}, False, "name"),
        ({"type": "broadcast", "name": "x"}, False, "type"),
        ({"name": "x"}, False, "type"),
    ],
)
def test_room_creation_validation(ctx, payload, expect_valid, expected_error_field):
    serializer = RoomSerializer(data=payload, context=ctx)
    assert serializer.is_valid() is expect_valid, serializer.errors
    if not expect_valid:
        assert expected_error_field in serializer.errors


def test_room_name_is_stripped(ctx):
    serializer = RoomSerializer(data={"type": "group", "name": "  Eng  "}, context=ctx)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["name"] == "Eng"


def test_serializer_without_request_context_fails_loudly():
    serializer = RoomSerializer(data={"type": "direct", "participant_id": "u-2"})
    with pytest.raises(RuntimeError, match="requires an authenticated request"):
        serializer.is_valid(raise_exception=True)


# --- Step 93: computed fields ---------------------------------------------


def test_member_count_falls_back_to_a_query(group_room):
    assert RoomSerializer(group_room).data["member_count"] == 1


def test_member_count_prefers_the_annotation(group_room):
    group_room.active_member_count = 99
    assert RoomSerializer(group_room).data["member_count"] == 99


def test_my_role_from_a_resolved_membership(group_room):
    membership = Membership.objects.get(room=group_room, user_id="u-1")
    data = RoomSerializer(group_room, context={"membership": membership}).data
    assert data["my_role"] == MembershipRole.ADMIN


def test_my_role_from_a_role_map(group_room):
    context = {"role_by_room_id": {str(group_room.pk): MembershipRole.MEMBER}}
    assert RoomSerializer(group_room, context=context).data["my_role"] == "member"


def test_my_role_is_none_without_context(group_room):
    assert RoomSerializer(group_room).data["my_role"] is None


def test_my_role_ignores_a_membership_for_a_different_room(group_room):
    other = Room.objects.create(type=RoomType.GROUP, name="Other", created_by="u-9")
    membership = Membership.objects.create(
        room=other, user_id="u-1", role=MembershipRole.ADMIN
    )
    assert RoomSerializer(group_room, context={"membership": membership}).data["my_role"] is None


# --- RoomUpdateSerializer --------------------------------------------------


def test_update_accepts_a_new_group_name(group_room):
    serializer = RoomUpdateSerializer(group_room, data={"name": "  Platform "}, partial=True)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["name"] == "Platform"


def test_update_rejects_a_blank_name(group_room):
    serializer = RoomUpdateSerializer(group_room, data={"name": "   "}, partial=True)
    assert not serializer.is_valid()
    assert "name" in serializer.errors


def test_update_rejects_renaming_a_direct_room(direct_room):
    serializer = RoomUpdateSerializer(direct_room, data={"name": "Nope"}, partial=True)
    assert not serializer.is_valid()
    assert "name" in serializer.errors


def test_update_exposes_only_name():
    assert set(RoomUpdateSerializer().fields) == {"name"}


# --- Step 94/96: membership serializers ------------------------------------


def test_membership_read_serializer_shape(group_room):
    membership = Membership.objects.get(room=group_room, user_id="u-1")
    data = MembershipSerializer(membership).data
    assert set(data) == {"user_id", "role", "joined_at"}
    assert "id" not in data


def test_membership_create_accepts_a_new_user(group_room):
    serializer = MembershipCreateSerializer(
        data={"user_id": "u-2"}, context={"room": group_room}
    )
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["user_id"] == "u-2"


def test_membership_create_ignores_a_role_in_the_body(group_room):
    serializer = MembershipCreateSerializer(
        data={"user_id": "u-2", "role": "admin"}, context={"room": group_room}
    )
    assert serializer.is_valid(), serializer.errors
    assert "role" not in serializer.validated_data


def test_membership_create_rejects_a_duplicate(group_room):
    serializer = MembershipCreateSerializer(
        data={"user_id": "u-1"}, context={"room": group_room}
    )
    with pytest.raises(MemberAlreadyExists) as exc_info:
        serializer.is_valid(raise_exception=True)
    assert exc_info.value.status_code == 409


def test_membership_create_rejects_a_direct_room(direct_room):
    serializer = MembershipCreateSerializer(
        data={"user_id": "u-3"}, context={"room": direct_room}
    )
    with pytest.raises(DirectRoomImmutable) as exc_info:
        serializer.is_valid(raise_exception=True)
    assert exc_info.value.status_code == 400


@pytest.mark.parametrize("value", ["", "   ", "x" * 65])
def test_membership_create_rejects_bad_user_ids(group_room, value):
    serializer = MembershipCreateSerializer(
        data={"user_id": value}, context={"room": group_room}
    )
    assert not serializer.is_valid()
    assert "user_id" in serializer.errors


def test_membership_create_requires_the_room_in_context():
    serializer = MembershipCreateSerializer(data={"user_id": "u-2"})
    with pytest.raises(RuntimeError, match="context=\\{'room': room\\}"):
        serializer.is_valid(raise_exception=True)


# --- Step 97: removal validation ------------------------------------------


def test_removal_resolves_an_active_membership(group_room):
    Membership.objects.create(room=group_room, user_id="u-2", role=MembershipRole.MEMBER)
    membership = validate_membership_removal(group_room, "u-2", "u-1")
    assert membership.user_id == "u-2"


def test_removal_of_a_departed_member_is_404(group_room):
    membership = Membership.objects.create(
        room=group_room, user_id="u-2", role=MembershipRole.MEMBER
    )
    membership.deactivate()
    with pytest.raises(MemberNotFound):
        validate_membership_removal(group_room, "u-2", "u-1")


def test_removal_of_a_stranger_is_404(group_room):
    with pytest.raises(MemberNotFound):
        validate_membership_removal(group_room, "ghost", "u-1")


def test_removal_from_a_direct_room_is_400(direct_room):
    with pytest.raises(DirectRoomImmutable):
        validate_membership_removal(direct_room, "u-2", "u-1")
