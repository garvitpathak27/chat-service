"""Query helpers for room authorization (Phase 2 Step 49, Phase 4 Steps 83-85).

Each helper answers one question with one query. Phases 5-8 import these
from `chat.selectors`.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, Q, Subquery

from chat.models import Membership, MembershipRole, Room


def get_active_room_or_none(room_id):
    """Return an active room, or None for a missing/malformed/deleted room."""
    try:
        # Room.objects already excludes soft-deleted rooms.
        return Room.objects.filter(pk=room_id).first()
    except (DjangoValidationError, ValueError, TypeError):
        # An unparseable id means "no such room" - a 404, never a 500.
        return None


def get_active_membership(room_id, user_id):
    """Return the active membership, or None if the user is not a member.

    Single (room_id, user_id) index seek. Always filters on BOTH columns in
    one query - never "fetch by id, then check the room" (Step 90).
    """
    return Membership.objects.filter(
        room_id=room_id,
        user_id=user_id,
        left_at__isnull=True,
    ).first()


def membership_role(room_id, user_id):
    """Return the active role, or None if the user is not an active member."""
    membership = get_active_membership(room_id, user_id)
    return membership.role if membership is not None else None


def is_member(room_id, user_id) -> bool:
    """Return True when user_id has an active membership in room_id."""
    return get_active_membership(room_id, user_id) is not None


def is_admin(room_id, user_id) -> bool:
    """Return True when user_id is an active admin of room_id."""
    return membership_role(room_id, user_id) == MembershipRole.ADMIN


def is_creator(room, user_id) -> bool:
    """Return True when user_id created the room.

    Takes a Room INSTANCE. str() on both sides: created_by is an opaque Auth
    id, and UUID('x') == 'x' is False in Python.
    """
    return room is not None and str(room.created_by) == str(user_id)


def list_rooms_for(user_id):
    """Active rooms in which user_id holds an active membership (Step 106).

    NOT written as Room.objects.filter(memberships__user_id=...).annotate(...):
    Django would reuse ONE join for the filter and the Count, and that join is
    already constrained to the caller, so every room would report
    member_count 1 - silently (Step 107). Expressing membership as a subquery
    leaves the outer join to the annotation alone.

    Room.objects is the active-only manager, so soft-deleted rooms are already
    excluded (ADR-008).
    """
    member_room_ids = Membership.objects.filter(
        user_id=user_id, left_at__isnull=True
    ).values("room_id")

    return (
        Room.objects.filter(pk__in=Subquery(member_room_ids))
        .annotate(
            active_member_count=Count(
                "memberships",
                filter=Q(memberships__left_at__isnull=True),
                distinct=True,
            )
        )
        # Deterministic and stable. `-id` is the tiebreaker: without a unique
        # final key, two rooms sharing an updated_at can swap places between
        # page 1 and page 2 and a client sees one twice and one never.
        .order_by("-updated_at", "-id")
    )


def roles_by_room_id(user_id, room_ids):
    """{room_id_str: role} for the caller, in ONE query (Step 107).

    Keys are strings because RoomSerializer.get_my_role looks up str(room.pk),
    and room_id comes back from the ORM as a UUID.
    """
    pairs = Membership.objects.filter(
        user_id=user_id, left_at__isnull=True, room_id__in=list(room_ids)
    ).values_list("room_id", "role")
    return {str(room_id): role for room_id, role in pairs}
