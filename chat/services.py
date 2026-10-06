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

from chat.models import Membership, MembershipRole, Room, RoomType

logger = logging.getLogger("chat.services")


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
