from rest_framework.test import APIRequestFactory

from chat.grpc_clients.types import AuthIdentity
from chat.models import RoomType
from chat.serializers import RoomSerializer


def make_request(user_id="user-123"):
    factory = APIRequestFactory()
    request = factory.post("/api/rooms/")

    request.auth = AuthIdentity(
        user_id=user_id,
        roles=(),
    )

    return request


def test_group_room_requires_name():
    serializer = RoomSerializer(
        data={
            "type": RoomType.GROUP,
        },
        context={"request": make_request()},
    )

    assert not serializer.is_valid()
    assert "name" in serializer.errors


def test_group_room_rejects_participant_id():
    serializer = RoomSerializer(
        data={
            "type": RoomType.GROUP,
            "name": "Engineering",
            "participant_id": "user-456",
        },
        context={"request": make_request()},
    )

    assert not serializer.is_valid()
    assert "participant_id" in serializer.errors


def test_valid_group_room():
    serializer = RoomSerializer(
        data={
            "type": RoomType.GROUP,
            "name": "  Engineering  ",
        },
        context={"request": make_request()},
    )

    assert serializer.is_valid()
    assert serializer.validated_data["name"] == "Engineering"


def test_direct_room_requires_participant_id():
    serializer = RoomSerializer(
        data={
            "type": RoomType.DIRECT,
        },
        context={"request": make_request()},
    )

    assert not serializer.is_valid()
    assert "participant_id" in serializer.errors


def test_direct_room_rejects_name():
    serializer = RoomSerializer(
        data={
            "type": RoomType.DIRECT,
            "name": "Private Chat",
            "participant_id": "user-456",
        },
        context={"request": make_request()},
    )

    assert not serializer.is_valid()
    assert "name" in serializer.errors


def test_direct_room_rejects_self():
    serializer = RoomSerializer(
        data={
            "type": RoomType.DIRECT,
            "participant_id": "user-123",
        },
        context={"request": make_request("user-123")},
    )

    assert not serializer.is_valid()
    assert "participant_id" in serializer.errors


def test_valid_direct_room():
    serializer = RoomSerializer(
        data={
            "type": RoomType.DIRECT,
            "participant_id": "  user-456  ",
        },
        context={"request": make_request()},
    )

    assert serializer.is_valid()
    assert serializer.validated_data["participant_id"] == "user-456"