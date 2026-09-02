import pytest 
from django.core.exceptions import ValidationError
from chat.models import MembershipRole, Membership, Room, RoomType
from django.db import IntegrityError, transaction
from django.utils import timezone
pytestmark = pytest.mark.django_db

def test_group_room_requires_a_name():
    room = Room(
        type = RoomType.GROUP,
        name="    ",
        created_by="u1",
    )

    with pytest.raises(ValidationError) as exc:
        room.full_clean()

    assert "name" in exc.value.message_dict

def test_group_room_created_utc_aware_timestamps():
    room = Room.objects.create(
        type=RoomType.GROUP,
        name="Engineering",
        created_by="u1",
    )

    assert room.created_at.tzinfo is not None
    assert room.updated_at.tzinfo is not None
    assert room.deleted_at is None
    assert room.is_active


def test_unsupported_room_type_is_rejected():
    room = Room(
        type="broadcast",
        name="nope",
        created_by="u1",
    )

    with pytest.raises(ValidationError) as exc:
        room.full_clean()

    assert "type" in exc.value.message_dict


def test_unsupported_role_is_rejected():
    room = Room.objects.create(
        type=RoomType.GROUP,
        name="R",
        created_by="u1",
    )

    membership = Membership(
        room=room,
        user_id="u1",
        role="superadmin",
    )

    with pytest.raises(ValidationError) as exc:
        membership.full_clean()

    assert "role" in exc.value.message_dict

def test_direct_key_is_order_independent():
    assert Room.build_direct_key("u9", "u2") == Room.build_direct_key("u2", "u9")
    assert Room.build_direct_key("u2", "u9") == "u2:u9"


def test_direct_room_with_self_is_rejected():
    with pytest.raises(ValidationError):
        Room.build_direct_key("u1", "u1")


def test_second_direct_room_for_same_pair_is_rejected_by_the_database():
    key = Room.build_direct_key("u1", "u2")

    Room.objects.create(
        type=RoomType.DIRECT,
        created_by="u1",
        direct_key=key,
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Room.objects.create(
                type=RoomType.DIRECT,
                created_by="u2",
                direct_key=key,
            )

def test_multiple_group_rooms_are_allowed():
    room1 = Room.objects.create(
        type=RoomType.GROUP,
        name="Engineering",
        created_by="u1",
    )

    room2 = Room.objects.create(
        type=RoomType.GROUP,
        name="Engineering",
        created_by="u1",
    )

    assert room1.pk != room2.pk


def test_duplicate_membership_is_rejected_by_database():
    room = Room.objects.create(
        type=RoomType.GROUP,
        name="Engineering",
        created_by="u1",
    )

    Membership.objects.create(
        room=room,
        user_id="u1",
        role=MembershipRole.ADMIN,
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Membership.objects.create(
                room=room,
                user_id="u1",
                role=MembershipRole.MEMBER,
            )


def test_user_can_leave_and_rejoin_room():
    room = Room.objects.create(
        type=RoomType.GROUP,
        name="Engineering",
        created_by="u1",
    )

    membership = Membership.objects.create(
        room=room,
        user_id="u1",
        role=MembershipRole.MEMBER,
    )

    membership.left_at = timezone.now()
    membership.save(update_fields=["left_at"])

    membership.left_at = None
    membership.save(update_fields=["left_at"])

    membership.refresh_from_db()

    assert membership.left_at is None