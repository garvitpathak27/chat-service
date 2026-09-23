import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class RoomQuerySet(models.QuerySet):
    def active(self):
        return self.filter(deleted_at__isnull=True)

    def for_user(self, user_id):
        """Active rooms in which user_id holds an active membership."""
        return self.active().filter(
            memberships__user_id=user_id,
            memberships__left_at__isnull=True,
        )


# from_queryset() copies RoomQuerySet's methods onto the manager, so
# Room.objects.for_user(...) works. With a plain models.Manager subclass,
# only Room.objects.all().for_user(...) would.
class ActiveRoomManager(models.Manager.from_queryset(RoomQuerySet)):
    """Default manager: soft-deleted rooms are invisible, full stop."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class AllRoomsManager(models.Manager.from_queryset(RoomQuerySet)):
    """Escape hatch for maintenance/admin code that must see deleted rows."""



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

    def soft_delete(self):
        """ADR-008. Returns True only on the FIRST deletion, so callers can
        make ROOM_DELETED publication idempotent (Step 118)."""
        if self.deleted_at is not None:
            return False
        self.deleted_at = timezone.now()
        # updated_at is auto_now: it must be listed or it will go stale.
        self.save(update_fields=["deleted_at", "updated_at"])
        return True


class ActiveMembershipManager(models.Manager):
    """Default manager: memberships of users who left are invisible."""

    def get_queryset(self):
        return super().get_queryset().filter(left_at__isnull=True)


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

    objects = ActiveMembershipManager()
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
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

    def __str__(self):
        return f"{self.user_id}@{self.room_id} ({self.role})"

    @property
    def is_active(self):
        return self.left_at is None

    def clean(self):
        super().clean()
        if self.role not in MembershipRole.values:
            raise ValidationError({"role": f"Unsupported role: {self.role!r}"})
        if self.room_id and self.room.deleted_at is not None:
            raise ValidationError("Cannot modify membership of a deleted room.")

    def deactivate(self):
        """Soft removal. Returns True only on first deactivation (Step 130)."""
        if self.left_at is not None:
            return False
        self.left_at = timezone.now()
        self.save(update_fields=["left_at"])
        return True

    def reactivate(self, role=MembershipRole.MEMBER):
        """Rejoin semantics (Step 129): reuse the row, reset joined_at."""
        self.left_at = None
        self.role = role
        self.joined_at = timezone.now()
        self.save(update_fields=["left_at", "role", "joined_at"])
        return self
