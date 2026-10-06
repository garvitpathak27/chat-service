from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.permissions import BasePermission

from chat.api.errors import ErrorCode
from chat.authn.user import identity_of
from chat.models import MembershipRole
from chat.selectors import get_active_membership, get_active_room_or_none, is_creator

ROOM_NOT_FOUND_DETAIL = "No room matches the given identifier."

# Fields the server owns absolutely. Serializers mark these read-only
# (Step 93), and views populate them from the verified identity (Step 103).
# Listed here rather than in a serializer so the guard tests have a single
# importable source of truth. (Step 89)
SERVER_CONTROLLED_FIELDS = frozenset(
    {
        "id",
        "created_by",
        "created_at",
        "updated_at",
        "deleted_at",
        "joined_at",
        "left_at",
    }
)

_CONTEXT_ATTR = "_chat_room_context"
def resolve_room_context(request, room_id, user_id):
    """Return (room, membership), or raise NotFound."""
    cached = getattr(request, _CONTEXT_ATTR, None)

    if cached is not None and str(cached[0].pk) == str(room_id):
        return cached

    room = get_active_room_or_none(room_id)

    if room is None:
        raise NotFound(ROOM_NOT_FOUND_DETAIL, code=ErrorCode.ROOM_NOT_FOUND)

    membership = get_active_membership(room.pk, user_id)

    if membership is None:
        raise NotFound(ROOM_NOT_FOUND_DETAIL, code=ErrorCode.ROOM_NOT_FOUND)

    context = (room, membership)
    setattr(request, _CONTEXT_ATTR, context)

    return context


def room_context(request):
    """Return the room context already resolved by the permission."""
    return getattr(request, _CONTEXT_ATTR, None)


class RoomScopedPermission(BasePermission):
    """Base permission for operations inside a specific room."""

    room_url_kwargs = "room_id"

    def has_permission(self, request , view):
        identity = identity_of(request)

        if identity is None:
            return False

        room_id = view.kwargs.get(self.room_url_kwargs)

        room , membership = resolve_room_context(
            request,
            room_id,
            identity.user_id,
        )

        return self.check_membership(
            request,
            view,
            room,
            membership
        )

    def check_membership( self , request , view , room , membership) -> bool:
        return True


class IsRoomMember(RoomScopedPermission):
    """active memebership is sufficient for room access"""
    message = "You are not a member of this room."


class CanManageRoom(RoomScopedPermission):
    """ only admin or the room creator mayh modify the room """

    message = "Only a room admin or its creator may modify this room."

    def check_membership(self, request, view, room, membership) -> bool:
        return membership.role == MembershipRole.ADMIN or is_creator(
            room,
            membership.user_id,
        )

class CanManageMembers(RoomScopedPermission):
    """ only an admin may0 manage room membership """
    message = "Only a room admin may manage membership."

    def check_membership(self, request, view, room, membership) -> bool:
        return membership.role == MembershipRole.ADMIN


class CanRemoveMembership(RoomScopedPermission):
    """allow self-leave or admin removal of another member ."""

    message = "Only a room admin may remove another member."
    target_url_kwarg = "user_id"

    def check_membership(self, request, view, room, membership) -> bool:
        target_user_id = view.kwargs.get(self.target_url_kwarg)

        if(
            target_user_id is not None and str(target_user_id) == str(membership.user_id)
        ):
            return True

        return membership.role == MembershipRole.ADMIN
