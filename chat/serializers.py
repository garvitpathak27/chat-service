"""Serializers for rooms and memberships.

Two rules run through this module.

1. SERVER-OWNED FIELDS ARE READ-ONLY. Ownership and audit columns
   (SERVER_CONTROLLED_FIELDS, Step 89) can never be set from a request body.
   The module-level guard at the bottom fails at import if that stops being
   true. The explicit `serializer.save(created_by=...)` in Phase 6 is the
   backstop.

2. VALIDATION RAISES DRF ERRORS, NOT DJANGO ONES. Model.full_clean() raises
   django.core.exceptions.ValidationError, which DRF does NOT catch - it
   escapes as a 500. Use _reraise_as_drf() anywhere model validation is
   invoked (Phase 6 Step 103 does).
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from chat.api.exceptions import (
    DirectRoomImmutable,
    MemberAlreadyExists,
    MemberNotFound,
)
from chat.authn.user import identity_of
from chat.models import Membership, Room, RoomType
from chat.permissions import SERVER_CONTROLLED_FIELDS
from chat.selectors import get_active_membership

MAX_USER_ID_LENGTH = 64      # matches Membership.user_id / Room.created_by
MAX_ROOM_NAME_LENGTH = 150   # matches Room.name


def _reraise_as_drf(exc: DjangoValidationError):
    """Translate a Django ValidationError into DRF's, preserving field names.

    Without this, a model-level failure returns HTTP 500 instead of a 400 with
    the error envelope.
    """
    if hasattr(exc, "message_dict"):
        raise serializers.ValidationError(exc.message_dict)
    raise serializers.ValidationError(exc.messages)


class RoomSerializer(serializers.ModelSerializer):
    """Read representation, and the input shape for POST /api/rooms/."""

    member_count = serializers.SerializerMethodField()
    my_role = serializers.SerializerMethodField()

    # Write-only: a direct room needs to know who the OTHER participant is,
    # but the resulting room never echoes it back (the member list does).
    participant_id = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=False,
        max_length=MAX_USER_ID_LENGTH,
        help_text="The other participant. Required when type=direct, forbidden otherwise.",
    )

    class Meta:
        model = Room
        fields = [
            "id",
            "name",
            "type",
            "created_by",
            "created_at",
            "updated_at",
            "member_count",
            "my_role",
            "participant_id",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]

    # Absent on purpose:
    #   direct_key - internal; concatenates both participants' ids (ADR-010).
    #   deleted_at - a soft-deleted room is 404 everywhere (ADR-008).

    def get_member_count(self, room) -> int:
        """Active member count.

        Reads the `active_member_count` annotation when the queryset provides
        one (Phase 6 Step 107). The per-row COUNT fallback exists so the
        serializer works in isolation - tests and the detail view - not
        because N+1 in a list is acceptable.
        """
        annotated = getattr(room, "active_member_count", None)
        if annotated is not None:
            return annotated
        return room.memberships.filter(left_at__isnull=True).count()

    def get_my_role(self, room):
        """The requesting user's role in this room, or None if unknown.

        Two context shapes are supported, both supplied by the view:
          context["membership"]        - the membership a room-scoped
                                         permission already resolved
          context["role_by_room_id"]   - {room_id_str: role} for list views
        """
        membership = self.context.get("membership")
        if membership is not None and str(membership.room_id) == str(room.pk):
            return membership.role
        role_map = self.context.get("role_by_room_id") or {}
        return role_map.get(str(room.pk))

    def _requester_id(self) -> str:
        request = self.context.get("request")
        identity = identity_of(request) if request is not None else None
        if identity is None:
            # Fail loudly: a serializer without the request cannot enforce
            # Step 89, and a silent None would let a view ship that way.
            raise RuntimeError(
                "RoomSerializer requires an authenticated request in its context. "
                "Pass context={'request': request} (DRF generic views do this "
                "automatically; APIView does not)."
            )
        return identity.user_id

    def validate(self, attrs):
        """Enforce ADR-010's direct/group rules.

        Deliberately NOT done here:
          - whether a direct room already exists for this pair: a
            read-then-write check loses the race (ADR-010); Phase 6 Step 103
            uses get_or_create against the unique direct_key index.
          - whether participant_id names a real Auth user: Chat cannot know
            (Step 121); ADR-002 has no user-existence RPC.
        """
        room_type = attrs.get("type")
        name = attrs.get("name")
        participant_id = (attrs.get("participant_id") or "").strip() or None
        errors = {}

        if room_type == RoomType.GROUP:
            if not (name or "").strip():
                errors["name"] = ["A group room requires a non-empty name."]
            if participant_id:
                errors["participant_id"] = [
                    "participant_id is only valid when type is 'direct'."
                ]

        elif room_type == RoomType.DIRECT:
            if name:
                errors["name"] = [
                    "Direct rooms are named from their participants; omit this field."
                ]
            if not participant_id:
                errors["participant_id"] = ["A direct room requires participant_id."]
            elif participant_id == str(self._requester_id()):
                errors["participant_id"] = ["You cannot open a direct room with yourself."]

        else:
            # Unreachable via the API: ChoiceField rejects unknown values first.
            # Present for direct (non-HTTP) construction.
            errors["type"] = [f"Unsupported room type: {room_type!r}"]

        if errors:
            raise serializers.ValidationError(errors)

        # Normalise so "  Engineering  " and "Engineering" are the same name.
        if name:
            attrs["name"] = name.strip()
        if participant_id:
            attrs["participant_id"] = participant_id
        return attrs


class RoomUpdateSerializer(serializers.ModelSerializer):
    """Input shape for PATCH /api/rooms/<id>/.

    DRF's default behaviour for an unrecognised key in the body is to ignore
    it silently. For an update endpoint that is a bad default: a client that
    sends {"type": "direct"} gets 200 and reasonably concludes the change was
    applied. This serializer rejects those keys instead, so a wrong assumption
    surfaces at the first request rather than as a data mystery weeks later.
    """

    MUTABLE_FIELDS = frozenset({"name"})

    # Explicit rather than derived, so adding a model column does not silently
    # become PATCHable. Anything not in MUTABLE_FIELDS is refused; this set
    # exists to give the refusal a better message than "unknown field".
    IMMUTABLE_FIELDS = frozenset(
        {
            "id",
            "type",
            "created_by",
            "created_at",
            "updated_at",
            "deleted_at",
            "direct_key",
            "participant_id",
            "member_count",
            "my_role",
        }
    )

    class Meta:
        model = Room
        fields = ["name"]

    def validate_name(self, value):
        value = (value or "").strip()
        if self.instance is not None and self.instance.type == RoomType.DIRECT:
            raise serializers.ValidationError(
                "Direct rooms are named from their participants and cannot be renamed."
            )
        if not value:
            raise serializers.ValidationError("A group room requires a non-empty name.")
        return value

    def validate(self, attrs):
        supplied = set(getattr(self, "initial_data", None) or {})

        errors = {}
        for field in sorted(supplied & self.IMMUTABLE_FIELDS):
            errors[field] = ["This field cannot be changed after the room is created."]
        for field in sorted(supplied - self.IMMUTABLE_FIELDS - self.MUTABLE_FIELDS):
            errors[field] = ["Unknown field."]

        if errors:
            raise serializers.ValidationError(errors)
        return attrs



class MembershipSerializer(serializers.ModelSerializer):
    """Read representation of one membership. Everything is read-only."""

    class Meta:
        model = Membership
        fields = ["user_id", "role", "joined_at"]
        read_only_fields = ["user_id", "role", "joined_at"]

    # Absent on purpose: id (memberships are addressed as (room_id, user_id),
    # never by pk - Step 90), room (already in the URL), left_at (always null
    # in the active roster).


class MembershipCreateSerializer(serializers.Serializer):
    """Input shape for POST /api/rooms/<id>/members/. Requires context['room'].

    A plain Serializer: `room` comes from the URL and `role` is forced to
    `member` (Step 123) - neither may be influenced by the request body.
    """

    user_id = serializers.CharField(
        max_length=MAX_USER_ID_LENGTH,
        allow_blank=False,
        trim_whitespace=True,
        help_text="Auth Service user id of the person to add.",
    )

    # `role` is deliberately not a field: new members are always `member`
    # (Step 123), and v1 has no promotion endpoint or event.

    def validate_user_id(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("user_id must not be blank.")
        return value

    def validate(self, attrs):
        """Room-aware checks.

        These raise APIExceptions, not ValidationError, on purpose:
        is_valid(raise_exception=True) only wraps ValidationError, so these
        reach the exception handler with their own status codes.
          400 DIRECT_ROOM_IMMUTABLE  - malformed for this room
          409 MEMBER_ALREADY_EXISTS  - well-formed, conflicts with state
        """
        room = self.context.get("room")
        if room is None:
            raise RuntimeError(
                "MembershipCreateSerializer requires context={'room': room}."
            )

        if room.type == RoomType.DIRECT:
            # ADR-014: a direct room's membership is fixed at creation.
            raise DirectRoomImmutable()

        # Advisory only; uniq_membership_room_user is authoritative (Step 46),
        # and a lost race is turned into the same 409 by the exception handler.
        if get_active_membership(room.pk, attrs["user_id"]) is not None:
            raise MemberAlreadyExists()

        return attrs


def validate_membership_removal(room, target_user_id, requester_id):
    """Resolve the membership a DELETE targets, or raise. Used at Step 127.

    Outcomes:
      400 DIRECT_ROOM_IMMUTABLE - direct rooms have a fixed roster (ADR-014)
      404 MEMBER_NOT_FOUND      - no ACTIVE membership for that user here
    The caller's own authority was already settled by CanRemoveMembership.

    The last-admin invariant is NOT checked here: it needs a locked read and
    the write in one transaction (Step 128), or two admins can both leave.
    """
    if room.type == RoomType.DIRECT:
        raise DirectRoomImmutable()

    membership = get_active_membership(room.pk, target_user_id)
    if membership is None:
        # Same 404 for "never joined" and "already left", so an admin cannot
        # probe the historical roster.
        raise MemberNotFound()

    return membership


# --- import-time guard for Step 89 -----------------------------------------
# If a future edit exposes a server-owned column as writable, this fails at
# startup rather than in production. Computed fields are read-only by nature.
_EXPOSED = set(RoomSerializer.Meta.fields)
_DECLARED_READ_ONLY = set(RoomSerializer.Meta.read_only_fields)
_COMPUTED = {"member_count", "my_role"}
_WRITABLE_SERVER_FIELDS = (
    (_EXPOSED & SERVER_CONTROLLED_FIELDS) - _DECLARED_READ_ONLY - _COMPUTED
)
assert not _WRITABLE_SERVER_FIELDS, (
    "server-controlled fields exposed as writable in RoomSerializer: "
    f"{sorted(_WRITABLE_SERVER_FIELDS)} (Step 89)"
)
