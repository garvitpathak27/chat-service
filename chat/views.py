"""Views for rooms and memberships.

PHASE 6 STATE: create, list and retrieve are implemented. Room update/delete
(Phase 7) and every membership operation (Phase 8) are still 501 stubs, each
already carrying its real permission classes - so the authorization matrix is
enforced on them today even though the bodies are empty.

Views parse, authorize, choose a status code and render. Writes live in
chat/services.py; reads live in chat/selectors.py.
"""

from django.urls import reverse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from chat.api.exceptions import NotImplementedYet
from chat.api.pagination import DefaultPagination
from chat.authn.user import identity_of
from chat.models import RoomType
from chat.permissions import (
    CanManageMembers,
    CanManageRoom,
    CanRemoveMembership,
    IsRoomMember,
    require_room_context,
)
from chat.selectors import get_active_membership, list_rooms_for, roles_by_room_id
from chat.serializers import RoomSerializer
from chat.services import create_group_room, get_or_create_direct_room


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
    malformed JSON body with 501 instead of 400 PARSE_ERROR. Implemented
    handlers read request.data through their serializers instead.
    """
    request.data  # noqa: B018 - evaluated for its side effect


class RoomListCreateView(MethodPermissionsMixin, APIView):
    """GET  /api/rooms/   the caller's rooms          (Steps 106-107)
    POST /api/rooms/   create or open a room         (Steps 103-105)

    No room in the URL, so no room-scoped permission: any authenticated user
    may create a room, and the list is filtered by membership in the query.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        identity = identity_of(request)

        queryset = list_rooms_for(identity.user_id)

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)

        # One query for the caller's role in exactly the rooms on this page.
        # Built from `page`, not the whole queryset, so the IN list stays
        # bounded by page_size (Step 107).
        role_map = roles_by_room_id(identity.user_id, [room.pk for room in page])

        serializer = RoomSerializer(
            page,
            many=True,
            context={"request": request, "role_by_room_id": role_map},
        )
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        identity = identity_of(request)

        serializer = RoomSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data["type"] == RoomType.GROUP:
            room = create_group_room(
                name=data["name"],
                created_by=identity.user_id,   # <- the ONLY source (Step 89)
            )
            created = True
        else:
            room, created = get_or_create_direct_room(
                creator_id=identity.user_id,
                participant_id=data["participant_id"],
            )

        # Re-read the membership rather than assuming "admin": for a revived
        # direct room the caller's role is whatever the row now holds.
        membership = get_active_membership(room.pk, identity.user_id)

        body = RoomSerializer(
            room, context={"request": request, "membership": membership}
        ).data

        # An existing direct room is 200, not 201 or 409: "open a DM with X"
        # is idempotent for the client, which wants a room id either way.
        return Response(
            body,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            headers={
                "Location": reverse("chat:room-detail", kwargs={"room_id": room.pk})
            },
        )


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
        # Resolved by IsRoomMember during permission checking (Step 86).
        # Re-reading here would be a second query AND a second source of
        # truth. `room_id` is unused: the permission already consumed it.
        room, membership = require_room_context(request)
        serializer = RoomSerializer(
            room, context={"request": request, "membership": membership}
        )
        return Response(serializer.data)

    def patch(self, request, room_id):
        _parse_body(request)
        raise NotImplementedYet()   # Step 111

    def delete(self, request, room_id):
        raise NotImplementedYet()   # Step 115


class MemberListCreateView(MethodPermissionsMixin, APIView):
    """GET/POST /api/rooms/<room_id>/members/   (Steps 125, 120)"""

    permission_classes_by_method = {
        "GET": [IsAuthenticated, IsRoomMember],
        "POST": [IsAuthenticated, CanManageMembers],
    }

    def get(self, request, room_id):
        raise NotImplementedYet()   # Step 125

    def post(self, request, room_id):
        _parse_body(request)
        raise NotImplementedYet()   # Step 120


class MemberDetailView(MethodPermissionsMixin, APIView):
    """DELETE /api/rooms/<room_id>/members/<user_id>/   (Step 127)

    CanRemoveMembership allows self-leave and admin removal (Step 91).
    """

    permission_classes_by_method = {
        "DELETE": [IsAuthenticated, CanRemoveMembership],
    }

    def delete(self, request, room_id, user_id):
        raise NotImplementedYet()   # Step 127
