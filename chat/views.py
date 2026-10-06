"""Views for rooms and memberships.

PHASE 5 STATE: every handler returns 501. The authentication and permission
wiring is real, so these stubs already enforce ADR-014 - curl them and you get
401 / 404 / 403 / 501 exactly as the matrix says. Phase 6 (rooms) and Phase 8
(members) replace the bodies only; the class names, routes and permission
tables below do not change.
"""

from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from chat.api.exceptions import NotImplementedYet
from chat.permissions import (
    CanManageMembers,
    CanManageRoom,
    CanRemoveMembership,
    IsRoomMember,
)


class MethodPermissionsMixin:
    """Per-HTTP-method permission classes.

    ADR-014 gives GET, PATCH and DELETE on the same URL different
    requirements, and DRF's single `permission_classes` list cannot express
    that. Without this mixin you would either over-restrict GET (members
    could not read the room) or under-restrict PATCH (members could rename
    it) - the second being an actual privilege escalation.

    Any method missing from the table falls back to `permission_classes`,
    which must never be empty.
    """

    permission_classes = [IsAuthenticated]
    permission_classes_by_method: dict[str, list] = {}

    def get_permissions(self):
        classes = self.permission_classes_by_method.get(
            self.request.method, self.permission_classes
        )
        return [permission() for permission in classes]


def _parse_body(request):
    """Force DRF to parse the body now.

    DRF parses lazily, so a stub that never reads request.data would answer a
    malformed JSON body with 501 instead of 400 PARSE_ERROR. Phase 6/8 read
    request.data through their serializers, which makes this redundant there.
    """
    request.data  # noqa: B018 - evaluated for its side effect


class RoomListCreateView(MethodPermissionsMixin, APIView):
    """GET  /api/rooms/   list the caller's rooms      (Step 106)
    POST /api/rooms/   create a room                  (Step 103)

    No room in the URL, so no room-scoped permission: any authenticated user
    may create a room, and the list is filtered by membership in the queryset.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        raise NotImplementedYet()

    def post(self, request):
        _parse_body(request)
        raise NotImplementedYet()


class RoomDetailView(MethodPermissionsMixin, APIView):
    """GET/PATCH/DELETE /api/rooms/<room_id>/   (Steps 108, 111, 115)

    No PUT: a room's type, creator and id are immutable, so "replace the
    whole resource" has no honest meaning. DRF answers PUT with 405.
    """

    permission_classes_by_method = {
        "GET": [IsAuthenticated, IsRoomMember],      # ADR-014 row 3
        "PATCH": [IsAuthenticated, CanManageRoom],   # ADR-014 row 4
        "DELETE": [IsAuthenticated, CanManageRoom],  # ADR-014 row 5
    }

    def get(self, request, room_id):
        raise NotImplementedYet()

    def patch(self, request, room_id):
        _parse_body(request)
        raise NotImplementedYet()

    def delete(self, request, room_id):
        raise NotImplementedYet()


class MemberListCreateView(MethodPermissionsMixin, APIView):
    """GET/POST /api/rooms/<room_id>/members/   (Steps 125, 120)"""

    permission_classes_by_method = {
        "GET": [IsAuthenticated, IsRoomMember],
        "POST": [IsAuthenticated, CanManageMembers],
    }

    def get(self, request, room_id):
        raise NotImplementedYet()

    def post(self, request, room_id):
        _parse_body(request)
        raise NotImplementedYet()


class MemberDetailView(MethodPermissionsMixin, APIView):
    """DELETE /api/rooms/<room_id>/members/<user_id>/   (Step 127)

    CanRemoveMembership allows self-leave and admin removal (Step 91).
    """

    permission_classes_by_method = {
        "DELETE": [IsAuthenticated, CanRemoveMembership],
    }

    def delete(self, request, room_id, user_id):
        raise NotImplementedYet()
