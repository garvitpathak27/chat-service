import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from chat.models import Membership, MembershipRole, Room, RoomType
from chat.selectors import get_active_membership

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


# --- Step 46 + rejoin (Step 129) --------------------------------------------


def test_duplicate_is_rejected_even_after_the_member_left():
    """MySQL has no partial unique index, so the constraint spans inactive
    rows too. This is what forces reactivation-based rejoin (Step 129)."""
    room = Room.objects.create(type=RoomType.GROUP, name="R", created_by="u1")
    m = Membership.objects.create(room=room, user_id="u1", role=MembershipRole.ADMIN)
    m.deactivate()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Membership.objects.create(room=room, user_id="u1")


def test_rejoin_reactivates_the_existing_row():
    room = Room.objects.create(type=RoomType.GROUP, name="R", created_by="u1")
    m = Membership.objects.create(room=room, user_id="u7", role=MembershipRole.MEMBER)
    original_id = m.id
    assert m.deactivate() is True
    assert m.deactivate() is False  # idempotent
    assert Membership.objects.filter(room=room, user_id="u7").count() == 0
    assert Membership.all_objects.filter(room=room, user_id="u7").count() == 1

    m.reactivate(role=MembershipRole.MEMBER)
    assert m.id == original_id
    assert m.left_at is None
    assert Membership.objects.filter(room=room, user_id="u7").count() == 1


# --- ADR-008: soft deletion ------------------------------------------------


def test_soft_deleted_room_is_invisible_to_the_default_manager():
    room = Room.objects.create(type=RoomType.GROUP, name="R", created_by="u1")
    assert room.soft_delete() is True
    assert Room.objects.filter(pk=room.pk).count() == 0
    assert Room.all_objects.filter(pk=room.pk).count() == 1


def test_soft_delete_is_idempotent():
    room = Room.objects.create(type=RoomType.GROUP, name="R", created_by="u1")
    assert room.soft_delete() is True
    assert room.soft_delete() is False  # no second ROOM_DELETED event


def test_soft_delete_bumps_updated_at():
    room = Room.objects.create(type=RoomType.GROUP, name="R", created_by="u1")
    before = room.updated_at
    room.soft_delete()
    room.refresh_from_db()
    assert room.updated_at > before


def test_membership_survives_room_soft_deletion():
    room = Room.objects.create(type=RoomType.GROUP, name="R", created_by="u1")
    Membership.objects.create(room=room, user_id="u1", role=MembershipRole.ADMIN)
    room.soft_delete()
    assert Membership.all_objects.filter(room_id=room.pk).count() == 1


def test_related_access_to_a_deleted_room_still_works():
    """base_manager_name = all_objects: following the FK must not explode."""
    room = Room.objects.create(type=RoomType.GROUP, name="R", created_by="u1")
    m = Membership.objects.create(room=room, user_id="u1", role=MembershipRole.ADMIN)
    room.soft_delete()
    m.refresh_from_db()
    assert m.room.deleted_at is not None


# --- The authorization lookup (core rule) ----------------------------------


def test_for_user_returns_only_active_rooms_with_active_membership():
    active = Room.objects.create(type=RoomType.GROUP, name="Active", created_by="u1")
    deleted = Room.objects.create(type=RoomType.GROUP, name="Deleted", created_by="u1")
    left = Room.objects.create(type=RoomType.GROUP, name="Left", created_by="u1")

    Membership.objects.create(room=active, user_id="u1", role=MembershipRole.ADMIN)
    Membership.objects.create(room=deleted, user_id="u1", role=MembershipRole.ADMIN)
    Membership.objects.create(room=left, user_id="u1", role=MembershipRole.MEMBER).deactivate()

    deleted.soft_delete()

    names = set(Room.objects.for_user("u1").values_list("name", flat=True))
    assert names == {"Active"}


def test_get_active_membership_finds_the_role():
    room = Room.objects.create(type=RoomType.GROUP, name="R", created_by="u1")
    Membership.objects.create(room=room, user_id="u1", role=MembershipRole.ADMIN)
    assert get_active_membership(room.pk, "u1").role == MembershipRole.ADMIN
    assert get_active_membership(room.pk, "nobody") is None


def test_membership_of_a_deleted_room_fails_validation():
    room = Room.objects.create(type=RoomType.GROUP, name="R", created_by="u1")
    room.soft_delete()
    with pytest.raises(ValidationError):
        Membership(room=room, user_id="u2").full_clean()
