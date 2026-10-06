"""Serializers for rooms and memberships."""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from chat.authn.user import identity_of
from chat.models import Membership, MembershipRole, Room, RoomType
from chat.permissions import SERVER_CONTROLLED_FIELDS


MAX_USER_ID_LENGTH = 64
MAX_ROOM_NAME_LENGTH = 150


def _reraise_as_drf(exc: DjangoValidationError):
    """Translate Django ValidationError into DRF's ValidationError."""
    if hasattr(exc, "message_dict"):
        raise serializers.ValidationError(exc.message_dict)
    raise serializers.ValidationError(exc.messages)


class RoomSerializer(serializers.ModelSerializer):
    """Read representation, and input shape for POST /api/rooms/."""

    member_count = serializers.SerializerMethodField()
    my_role = serializers.SerializerMethodField()

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
        read_only_fields = [
            "id",
            "created_by",
            "created_at",
            "updated_at",
        ]

    def get_member_count(self, room) -> int:
        """Return the number of active members."""
        annotated = getattr(room, "active_member_count", None)

        if annotated is not None:
            return annotated

        return room.memberships.filter(left_at__isnull=True).count()

    def get_my_role(self, room):
        """Return the requesting user's role in this room."""
        membership = self.context.get("membership")

        if membership is not None and str(membership.room_id) == str(room.pk):
            return membership.role

        role_map = self.context.get("role_by_room_id") or {}
        return role_map.get(str(room.pk))

    def _requester_id(self) -> str:
        request = self.context.get("request")
        identity = identity_of(request) if request is not None else None

        if identity is None:
            raise RuntimeError(
                "RoomSerializer requires an authenticated request in its context. "
                "Pass context={'request': request}."
            )

        return identity.user_id

    def validate(self, attrs):
        """Enforce ADR-010's direct/group rules."""

        room_type = attrs.get("type")
        name = attrs.get("name")
        participant_id = (attrs.get("participant_id") or "").strip() or None
        errors = {}

        if room_type == RoomType.GROUP:
            if not (name or "").strip():
                errors["name"] = [
                    "A group room requires a non-empty name."
                ]

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
                errors["participant_id"] = [
                    "A direct room requires participant_id."
                ]

            elif participant_id == str(self._requester_id()):
                errors["participant_id"] = [
                    "You cannot open a direct room with yourself."
                ]

        else:
            errors["type"] = [
                f"Unsupported room type: {room_type!r}"
            ]

        if errors:
            raise serializers.ValidationError(errors)

        if name:
            attrs["name"] = name.strip()

        if participant_id:
            attrs["participant_id"] = participant_id

        return attrs


class RoomUpdateSerializer(serializers.ModelSerializer):
    """Input shape for PATCH /api/rooms/<id>/."""

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
            raise serializers.ValidationError(
                "A group room requires a non-empty name."
            )

        return value


class MembershipSerializer(serializers.ModelSerializer):
    """Read representation of one membership."""

    class Meta:
        model = Membership
        fields = ["user_id", "role", "joined_at"]
        read_only_fields = ["user_id", "role", "joined_at"]


class MembershipCreateSerializer(serializers.Serializer):
    """Input shape for adding a member."""

    user_id = serializers.CharField(
        max_length=MAX_USER_ID_LENGTH,
        allow_blank=False,
        trim_whitespace=True,
    )

    