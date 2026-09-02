import uuid

from django.db import models
from django.utils import timezone

from django.core.exceptions import ValidationError
class RoomQuerySet(models.QuerySet):
    def active(self):
        return self.filter(deleted_at__isnull=True)

    def for_user(self, user_id):
        """Active rooms in which user_id holds an active membership."""
        return self.active().filter(
            memberships__user_id=user_id,
            memberships__left_at__isnull=True,
        )


class ActiveRoomManager(models.Manager):
    """Default manager: soft-deleted rooms are invisible, full stop."""

    def get_queryset(self):
        return RoomQuerySet(self.model, using=self._db).filter(deleted_at__isnull=True)


class AllRoomsManager(models.Manager):
    """Escape hatch for maintenance/admin code that must see deleted rows."""

    def get_queryset(self):
        return RoomQuerySet(self.model, using=self._db)



class RoomType(models.TextChoices):
    DIRECT = "direct", "Direct"
    GROUP = "group", "Group"


class MembershipRole(models.TextChoices):
    ADMIN = "admin", "Admin"
    MEMBER = "member", "Member"


class Room(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    name = models.CharField(
        max_length=150,
        null=True,
        blank=True,
    )
    type = models.CharField(
        max_length=16,
        choices=RoomType.choices,
    )
    # ---------------------------------------------------------------
    # CROSS-SERVICE BOUNDARY.
    # created_by holds an Auth Service user ID and is NOT a ForeignKey.
    #
    # Auth owns the user record in a separate database.
    # Chat must not couple its database directly to Auth.
    #
    # Treat this value as opaque:
    # - do not parse it
    # - do not assume it is an integer
    # - do not assume it is a UUID
    # ---------------------------------------------------------------
    created_by = models.CharField(
        db_index=True,
        max_length=64,
    )
    direct_key = models.CharField(
        max_length=140,
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        auto_now=True,
    )
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        default=None,
    )
    objects = ActiveRoomManager()
    all_objects = AllRoomsManager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        constraints = [
            models.CheckConstraint(
                check=models.Q(
                    type__in=[RoomType.DIRECT, RoomType.GROUP]
                ),
                name="room_type_valid",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(
                        type=RoomType.DIRECT,
                        direct_key__isnull=False,
                    )
                    | models.Q(
                        type=RoomType.GROUP,
                        direct_key__isnull=True,
                    )
                ),
                name="room_direct_key_matches_type",
            ),
        ]
        indexes = [
            models.Index(
                fields=["type", "deleted_at"],
                name="room_type_deleted_idx",
            ),
        ]

    def __str__(self):
        return f"{self.type}:{self.name or self.direct_key or self.pk}"

    @property
    def is_active(self):
        return self.deleted_at is None

    @staticmethod
    def build_direct_key(user_a, user_b):
        a, b = str(user_a), str(user_b)

        if a == b:
            raise ValidationError("A direct room requires two distinct users.")

        return ":".join(sorted([a, b]))

    def clean(self):
        super().clean()
        errors = {}

        if self.type == RoomType.GROUP:
            if not (self.name or "").strip():
                errors["name"] = "Group rooms require a name"
            if self.direct_key:
                errors["direct_key"]= "Groups rooms must not have a direct key"

        elif self.type == RoomType.DIRECT:
            if not self.direct_key:
                errors["direct_key"]="Direct rooms require a direct_key"
        else:
            errors["type"]= f"Unsupported room type: {self.type!r}"

        if errors:
            raise ValidationError(errors)
                

class Membership(models.Model):
    id = models.BigAutoField(primary_key=True)
    room = models.ForeignKey(
        "chat.Room",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    # ---------------------------------------------------------------
    # CROSS-SERVICE BOUNDARY.
    # This is an Auth Service user ID and is NOT a ForeignKey.
    #
    # Auth owns the user record. Chat only stores the identifier.
    # Treat it as an opaque value:
    # - do not parse it
    # - do not assume it is an integer
    # - do not assume it is a UUID
    #
    # It must come from a trusted authenticated identity, never
    # directly from an unauthenticated request body.
    # ---------------------------------------------------------------
    user_id = models.CharField(max_length=64)
    role = models.CharField(
        max_length=16,
        choices=MembershipRole.choices,
        default=MembershipRole.MEMBER,
    )
    joined_at = models.DateTimeField(default=timezone.now)
    left_at = models.DateTimeField(
        null=True,
        blank=True,
        default=None,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["room", "user_id"],
                name="uniq_membership_room_user",
            ),
            models.CheckConstraint(
                check=models.Q(
                    role__in=[
                        MembershipRole.ADMIN,
                        MembershipRole.MEMBER,
                    ]
                ),
                name="membership_role_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user_id", "left_at"],
                name="membership_user_active_idx",
            ),
        ]

