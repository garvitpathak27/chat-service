"""Transactional writes for rooms and memberships.

Views parse and authorize; this module owns transaction boundaries,
invariants and races. Nothing here reads request.data - every caller-supplied
value arrives as an explicit keyword argument, and `created_by` / `user_id`
are only ever fed from a VerifyToken-validated identity (Step 89).

Transaction rule (Step 105): catch IntegrityError OUTSIDE the atomic() block
that produced it. Inside the block the transaction is already marked for
rollback and the next query raises TransactionManagementError.
"""

from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from chat.api.exceptions import(
    DirectRoomImmutable,
    LastAdminError,
    MemberAlreadyExists,
    MemberNotFound
)
from chat.models import Membership, MembershipRole, Room, RoomType
from chat.events import EventType , isoformat_utc , publish_on_commit
logger = logging.getLogger("chat.services")

MUTABLE_ROOM_FIELDS = frozenset({"name"})

from django.utils import timezone

@transaction.atomic
def create_group_room(*, name: str, created_by: str) -> Room:
    """Create a group room and its creator's admin membership atomically.

    `created_by` is keyword-only and is always identity_of(request).user_id.
    There is no code path that supplies it from a request body (Step 89).

    The two INSERTs are adjacent and in one transaction (Steps 104-105): a
    room without its creator's membership is unreachable by every endpoint,
    including DELETE, and could only be recovered by hand.
    """
    room = Room.objects.create(
        type=RoomType.GROUP,
        name=name,
        created_by=created_by,
    )
    Membership.objects.create(
        room=room,
        user_id=created_by,
        role=MembershipRole.ADMIN,
    )
    # Step 140 hook:
    #   transaction.on_commit(lambda: publish_room_created(room))
    # Deliberately absent until Phase 9 - publishing before the publisher
    # exists would mean either a silent no-op or an import that fails.
    logger.info("room created id=%s type=group by=%s", room.pk, created_by)
    return room


def get_or_create_direct_room(*, creator_id: str, participant_id: str) -> tuple[Room, bool]:
    """ADR-010: at most one direct room per unordered pair, ever.

    Returns (room, created). `created` is False when an existing room was
    found or revived, which the view turns into 200 rather than 201.

    Three cases, in the order they are checked:
      1. a room (active or soft-deleted) exists -> return it, reviving it
         if needed so history stays attached to the same room_id
      2. nothing exists                         -> create it, and be ready to
                                                   lose the race on the unique
                                                   direct_key index
    Both participants are admins: either may close the conversation, and a
    direct room's roster can never change (DIRECT_ROOM_IMMUTABLE).

    Not decorated with @transaction.atomic on purpose - the IntegrityError
    below must be caught outside the block that raised it (Step 105).
    """
    direct_key = Room.build_direct_key(creator_id, participant_id)

    # all_objects, not objects: a soft-deleted direct room still holds the
    # direct_key, so Room.objects (active-only) would miss it and the INSERT
    # below would then fail on the unique index.
    existing = Room.all_objects.filter(direct_key=direct_key).first()
    if existing is not None:
        return _revive_direct_room(existing, creator_id, participant_id), False

    try:
        with transaction.atomic():
            room = Room.objects.create(
                type=RoomType.DIRECT,
                name=None,
                created_by=creator_id,
                direct_key=direct_key,
            )
            Membership.objects.bulk_create(
                [
                    Membership(room=room, user_id=creator_id, role=MembershipRole.ADMIN),
                    Membership(room=room, user_id=participant_id, role=MembershipRole.ADMIN),
                ]
            )
            # Step 140 hook:
            #   transaction.on_commit(lambda: publish_room_created(room))
    except IntegrityError:
        # Another request created the same direct room between our SELECT and
        # our INSERT. The unique index on direct_key caught it - a
        # read-then-write check alone would have produced a duplicate DM
        # (ADR-010). The atomic() block has already rolled back, so the
        # connection is usable and we can simply re-read.
        logger.info("lost direct-room race for key=%s; re-reading", direct_key)
        room = Room.all_objects.filter(direct_key=direct_key).first()
        if room is None:
            # Not the constraint we expected: let the exception handler map it.
            raise
        return _revive_direct_room(room, creator_id, participant_id), False

    logger.info("room created id=%s type=direct by=%s", room.pk, creator_id)
    return room, True


@transaction.atomic
def _revive_direct_room(room: Room, creator_id: str, participant_id: str) -> Room:
    """Make an existing direct room usable again, idempotently.

    Re-opening a DM must reuse the original room so Chat Messages Service's
    history stays attached to the same room_id (ADR-008's rationale).
    A no-op (no writes) when the room and both memberships are already active.
    """
    changed = False

    if room.deleted_at is not None:
        room.deleted_at = None
        # updated_at is auto_now and must be listed or it goes stale.
        room.save(update_fields=["deleted_at", "updated_at"])
        changed = True

    for user_id in (creator_id, participant_id):
        membership = Membership.all_objects.filter(room=room, user_id=user_id).first()
        if membership is None:
            Membership.objects.create(room=room, user_id=user_id, role=MembershipRole.ADMIN)
            changed = True
        elif membership.left_at is not None:
            membership.reactivate(role=MembershipRole.ADMIN)
            changed = True

    if changed:
        logger.info("revived direct room id=%s", room.pk)
    return room


def update_room(*,room: Room , changes: dict , updated_by: str)-> tuple[Room , dict]:
    """
    Apply mutable metadata changes. Returns (room, applied_changes).

    `applied_changes` contains only fields whose value actually differs, which
    is what ADR-011's ROOM_UPDATED payload calls `changed_fields`. An empty
    dict means the request was a no-op: nothing is written, updated_at is not
    touched, and no event is scheduled.

    `changes` comes from RoomUpdateSerializer.validated_data, which can only
    ever contain `name` (Step 112). Passing anything else would raise here
    rather than silently writing it.
    """
    applied = {}
    for field, value in changes.items():
        if field not in MUTABLE_ROOM_FIELDS:
            raise ValueError(
                f"update_room refuses to write {field!r}"
                f"mutable fields are {sorted(MUTABLE_ROOM_FIELDS)}"

            )
        if getattr(room , field) != value:
            setattr(room, field , value)
            applied[field] = value
    if not applied:
        logger.debug("update room no of id=%s by= %s ", room.pk , updated_by )
        return room, {}
    
    room.save(update_fields=[*applied.keys(),"updated_at"])

    publish_on_commit(
        EventType.ROOM_UPDATED,
        {
            "room_id": str(room.pk),
            "changed_fields": applied,
            "updated_by": updated_by,
            "updated_at": isoformat_utc(room.updated_at),
        },
    )
    logger.info("room updated id=%s fields = %s by = %s" , room.pk , sorted(applied) ,updated_by)
    return room , applied


def delete_room(*, room: Room, deleted_by: str) -> bool:
    member_user_ids = list(
        Membership.objects.filter(room=room)
        .order_by("user_id")
        .values_list("user_id", flat=True)
    )

    if not room.soft_delete():
        logger.info("delete_room no-op id=%s already deleted", room.pk)
        return False

    # Queryset .update() is correct here and wrong for Room: Membership has no
    # auto_now column, so nothing is silently skipped, and one statement beats
    # N saves for a large room.
    deactivated = Membership.objects.filter(room=room).update(left_at=timezone.now())

    publish_on_commit(
        EventType.ROOM_DELETED,
        {
            "room_id": str(room.pk),
            "type": room.type,
            "deleted_by": deleted_by,
            "deleted_at": isoformat_utc(room.deleted_at),
            "member_user_ids": member_user_ids,
        },
    )
    logger.info(
        "room deleted id=%s by=%s memberships_deactivated=%s",
        room.pk,
        deleted_by,
        deactivated,
    )
    return True


@transaction.atomic
def add_member(*,room:Room , user_id:str , added_by:str) -> Membership:
    """Add a user to a group room, or reactivate them if they were removed.

      Returns the active Membership. Raises:
          DirectRoomImmutable  400 - direct rooms have a fixed roster
          MemberAlreadyExists  409 - already an ACTIVE member

      Every new or reactivated membership gets role=member (Step 123).
      """

    if room.type == RoomType.DIRECT:
        raise DirectRoomImmutable()
    
    existing = Membership.all_objects.filter(room=room , user_id = user_id).first()
    
    if existing is not None:
        if existing.left_at is None:
            raise MemberAlreadyExists()
        membership = existing.reactivate(role=MembershipRole.MEMBER)
    else:
        try:
            with transaction.atomic():
                membership = Membership.objects.create(
                    room=room,
                    user_id=user_id,
                    role = MembershipRole.MEMBER
                )
        except IntegrityError:
            logger.info("lost membership race room=%s , user=%s , re-reading",room.pk , user_id)
            existing = Membership.all_objects.filter(
                room=room,
                user_id=user_id
            ).first()
            if existing is None:
                raise
            if existing.left_at is None:
                raise MemberAlreadyExists()
            membership = existing.reactivate(role=MembershipRole.MEMBER)
    publish_on_commit(
        EventType.MEMBER_ADDED,
        {
            "room_id": str(room.pk),
            "user_id": membership.user_id,
            "role": membership.role,
            "added_by": added_by,
            "joined_at": isoformat_utc(membership.joined_at),
        },
    )
    logger.info(
        "member added room=%s user=%s by=%s", room.pk, membership.user_id, added_by
    )
    return membership


@transaction.atomic
def remove_member(*, room: Room, target_user_id: str, removed_by: str) -> Membership:
    """Deactivate a membership. Returns the deactivated Membership.

    Raises:
        DirectRoomImmutable  400 - direct rooms have a fixed roster
        MemberNotFound       404 - no ACTIVE membership for that user here
        LastAdminError       409 - would leave the room without an admin

    CONCURRENCY. The admin count and the write must be atomic with respect to
    other removals, so the roster is read with SELECT ... FOR UPDATE. Without
    it, two admins leaving simultaneously each read "2 admins", each conclude
    it is safe, and the room ends up with none - permanently, because v1 has
    no promotion endpoint (Step 123) to repair it.
    """
    if room.type == RoomType.DIRECT:
        raise DirectRoomImmutable()

    # Lock every ACTIVE membership row of this room for the rest of the
    # transaction. order_by("user_id") gives a consistent lock acquisition
    # order across concurrent transactions, which is what keeps two
    # simultaneous removals from deadlocking each other.
    memberships = list(
        Membership.objects.select_for_update()
        .filter(room=room)
        .order_by("user_id")
    )

    target = next(
        (m for m in memberships if str(m.user_id) == str(target_user_id)), None
    )
    if target is None:
        raise MemberNotFound()

    if target.role == MembershipRole.ADMIN:
        active_admins = [m for m in memberships if m.role == MembershipRole.ADMIN]
        if len(active_admins) == 1:
            raise LastAdminError()

    target.deactivate()

    reason = "left" if str(removed_by) == str(target_user_id) else "removed"
    publish_on_commit(
        EventType.MEMBER_REMOVED,
        {
            "room_id": str(room.pk),
            "user_id": target.user_id,
            "removed_by": removed_by,
            "removed_at": isoformat_utc(target.left_at),
            "reason": reason,
        },
    )
    logger.info(
        "member removed room=%s user=%s by=%s reason=%s",
        room.pk,
        target.user_id,
        removed_by,
        reason,
    )
    return target

