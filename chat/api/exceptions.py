"""Chat Service's own API exceptions.

Each one sets default_code to a value from ErrorCode, which is what
_extract_code() picks up - so raising one of these anywhere produces the right
envelope with no per-view code.

This module must not import chat.serializers or chat.views: they import it.
"""

from rest_framework import status
from rest_framework.exceptions import APIException, NotFound

from chat.api.errors import ErrorCode


class MemberAlreadyExists(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "That user is already a member of this room."
    default_code = ErrorCode.MEMBER_ALREADY_EXISTS


class DirectRoomImmutable(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Membership of a direct room cannot be changed."
    default_code = ErrorCode.DIRECT_ROOM_IMMUTABLE


class LastAdminError(APIException):
    """Raised at Step 128 when a removal would leave a room with no admin.

    409, not 403: the caller is permitted to do this, the room's state
    forbids it. A 403 would tell them to ask for permission they already have.
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = (
        "This room would be left without an administrator. "
        "Promote another member first."
    )
    default_code = ErrorCode.LAST_ADMIN


class MemberNotFound(NotFound):
    default_detail = "No active membership for that user in this room."
    default_code = ErrorCode.MEMBER_NOT_FOUND


class NotImplementedYet(APIException):
    """Placeholder for the Phase 5 view stubs. Deleted in Phase 6/8."""

    status_code = status.HTTP_501_NOT_IMPLEMENTED
    default_detail = "This endpoint is not implemented yet."
    default_code = ErrorCode.NOT_IMPLEMENTED
