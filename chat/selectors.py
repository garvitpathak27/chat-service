"""Query helpers for room authorization (Phase 2 Step 49, Phase 4 Steps 83-85).

Each helper answers one question with one query. Phases 5-8 import these
from `chat.selectors`.
"""

from django.core.exceptions import ValidationError as DjangoValidationError

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
