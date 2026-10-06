"""Chat Service's error envelope, error-code registry, and exception handler.

Every failure the API can return passes through chat_exception_handler() and
comes out in exactly one shape:

    {"error": {"code": ..., "message": ..., "details": ..., "request_id": ...}}

`code` is machine-readable and STABLE - it is frozen as part of the v1
contract at Step 262 and documented at Step 237. `message` is for humans and
may be reworded at any time. Clients must branch on `code`, never on
`message`.

This module imports only Django and DRF - nothing from `chat` - so that
chat.permissions and chat.api.exceptions can import it without a cycle.
"""

from __future__ import annotations

import logging

from django.db import IntegrityError
from django.http import Http404, JsonResponse
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger("chat.api")


class ErrorCode:
    """Every error code Chat Service may return. Do not inline string
    literals elsewhere - Step 237 documents this class directly."""

    # --- 400 -------------------------------------------------------------
    VALIDATION_FAILED = "VALIDATION_FAILED"
    PARSE_ERROR = "PARSE_ERROR"
    DIRECT_ROOM_IMMUTABLE = "DIRECT_ROOM_IMMUTABLE"

    # --- 401 (ADR-013) ---------------------------------------------------
    AUTH_HEADER_MISSING = "AUTH_HEADER_MISSING"
    AUTH_HEADER_MALFORMED = "AUTH_HEADER_MALFORMED"
    TOKEN_INVALID = "TOKEN_INVALID"

    # --- 403 -------------------------------------------------------------
    PERMISSION_DENIED = "PERMISSION_DENIED"

    # --- 404 (ADR-014 information hiding) --------------------------------
    ROOM_NOT_FOUND = "ROOM_NOT_FOUND"
    MEMBER_NOT_FOUND = "MEMBER_NOT_FOUND"
    NOT_FOUND = "NOT_FOUND"

    # --- 405 / 415 / 429 -------------------------------------------------
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    THROTTLED = "THROTTLED"

    # --- 409 conflicts ---------------------------------------------------
    MEMBER_ALREADY_EXISTS = "MEMBER_ALREADY_EXISTS"
    DIRECT_ROOM_EXISTS = "DIRECT_ROOM_EXISTS"
    LAST_ADMIN = "LAST_ADMIN"

    # --- 501 -------------------------------------------------------------
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"

    # --- 502 / 503 dependency failures (ADR-013) -------------------------
    AUTH_UNAVAILABLE = "AUTH_UNAVAILABLE"
    AUTH_DEPENDENCY_ERROR = "AUTH_DEPENDENCY_ERROR"

    # --- 500 -------------------------------------------------------------
    INTERNAL_ERROR = "INTERNAL_ERROR"


ALL_CODES = frozenset(
    value
    for name, value in vars(ErrorCode).items()
    if not name.startswith("_") and isinstance(value, str)
)

# DRF's own lowercase codes -> ours. Anything unmapped falls back by status.
DRF_CODE_MAP = {
    "not_authenticated": ErrorCode.AUTH_HEADER_MISSING,
    "authentication_failed": ErrorCode.TOKEN_INVALID,
    "permission_denied": ErrorCode.PERMISSION_DENIED,
    "not_found": ErrorCode.NOT_FOUND,
    "method_not_allowed": ErrorCode.METHOD_NOT_ALLOWED,
    "unsupported_media_type": ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    "parse_error": ErrorCode.PARSE_ERROR,
    "throttled": ErrorCode.THROTTLED,
    "invalid": ErrorCode.VALIDATION_FAILED,
}

DEFAULT_CODE_BY_STATUS = {
    400: ErrorCode.VALIDATION_FAILED,
    401: ErrorCode.TOKEN_INVALID,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.MEMBER_ALREADY_EXISTS,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    429: ErrorCode.THROTTLED,
    501: ErrorCode.NOT_IMPLEMENTED,
    502: ErrorCode.AUTH_DEPENDENCY_ERROR,
    503: ErrorCode.AUTH_UNAVAILABLE,
}

# Database constraint name (as it appears in MySQL's error text) -> response.
# Names come from Phase 2 Step 55; if you rename a constraint in a migration,
# rename it here too or the 409 silently becomes a 500.
#
# ORDER MATTERS: matching is substring-based and first-match-wins, so a name
# that contains another must come first. "room_direct_key_matches_type"
# contains "direct_key" - listed after it, a CHECK violation would be
# reported as a 409 DIRECT_ROOM_EXISTS.
INTEGRITY_CONSTRAINT_MAP = {
    "uniq_membership_room_user": (
        status.HTTP_409_CONFLICT,
        ErrorCode.MEMBER_ALREADY_EXISTS,
        "That user is already a member of this room.",
    ),
    "room_direct_key_matches_type": (
        status.HTTP_400_BAD_REQUEST,
        ErrorCode.VALIDATION_FAILED,
        "Room type and participants are inconsistent.",
    ),
    "room_type_valid": (
        status.HTTP_400_BAD_REQUEST,
        ErrorCode.VALIDATION_FAILED,
        "Unsupported room type.",
    ),
    "membership_role_valid": (
        status.HTTP_400_BAD_REQUEST,
        ErrorCode.VALIDATION_FAILED,
        "Unsupported membership role.",
    ),
    "direct_key": (
        status.HTTP_409_CONFLICT,
        ErrorCode.DIRECT_ROOM_EXISTS,
        "A direct room already exists for these two users.",
    ),
}


def build_error_body(code, message, details=None, request_id=None):
    """The one and only response shape. Used by the handler and by the
    non-DRF 404/500 handlers below, so even a URL that matches no route
    answers in the same envelope."""
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": request_id,
        }
    }


def chat_exception_handler(exc, context):
    """DRF EXCEPTION_HANDLER. Normalises every handled exception.

    Returning None hands the exception back to Django, which produces a 500.
    That happens only for genuinely unexpected exceptions, and Step 165 adds
    logging for them.
    """
    # IntegrityError is not a DRF exception, so DRF's handler ignores it.
    # Mapping it here means a lost race against a UNIQUE constraint returns
    # the same 409 as the friendly pre-check in Step 96, instead of a 500.
    if isinstance(exc, IntegrityError):
        return _integrity_error_response(exc, context)

    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    # IMPORTANT: mutate response.data in place. Building a new Response would
    # discard the headers DRF just set - WWW-Authenticate on 401 (Step 80)
    # and Retry-After on 503 (Step 82) - and both are part of ADR-013.
    code = _extract_code(exc, response.status_code)
    message, details = _extract_message_and_details(exc, response.data)
    response.data = build_error_body(code, message, details, _request_id(context))
    return response


def _extract_code(exc, status_code) -> str:
    if isinstance(exc, ValidationError):
        # exc.detail is a dict or list here, so it carries no single code.
        return ErrorCode.VALIDATION_FAILED

    if isinstance(exc, Http404):
        return ErrorCode.NOT_FOUND

    raw = None
    if isinstance(exc, APIException):
        raw = getattr(exc.detail, "code", None) or getattr(exc, "default_code", None)

    if raw:
        upper = str(raw).upper()
        if upper in ALL_CODES:          # our own codes, e.g. ROOM_NOT_FOUND
            return upper
        mapped = DRF_CODE_MAP.get(str(raw))
        if mapped:
            return mapped

    return DEFAULT_CODE_BY_STATUS.get(status_code, ErrorCode.INTERNAL_ERROR)


def _extract_message_and_details(exc, data):
    if isinstance(exc, ValidationError):
        return "One or more fields failed validation.", _plain(exc.detail)

    if isinstance(data, dict) and "detail" in data:
        return str(data["detail"]), None

    if isinstance(data, list):
        return "; ".join(str(item) for item in data), None

    if isinstance(data, dict):
        # e.g. a non-field APIException that produced a dict body
        return "Request failed.", _plain(data)

    return str(data), None


def _plain(value):
    """Recursively convert DRF's ErrorDetail objects to plain strings.

    ErrorDetail subclasses str, so it serialises fine - but it carries a
    `.code` attribute that leaks into some renderers and confuses equality
    checks in tests. Flattening keeps the contract boring.
    """
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return str(value)


def _request_id(context):
    request = context.get("request") if context else None
    # Populated by the correlation-id middleware at Step 161. Until then the
    # field exists in every response and is always null, so clients and log
    # tooling can be written against the final shape today.
    return getattr(request, "request_id", None)


def _integrity_error_response(exc, context):
    text = str(exc)
    for needle, (http_status, code, message) in INTEGRITY_CONSTRAINT_MAP.items():
        if needle in text:
            logger.warning("integrity constraint hit: %s (%s)", needle, text)
            return Response(
                build_error_body(code, message, request_id=_request_id(context)),
                status=http_status,
            )

    # Unmapped: a real bug. Log the full exception, tell the client nothing.
    logger.exception("unmapped IntegrityError: %s", text)
    return Response(
        build_error_body(
            ErrorCode.INTERNAL_ERROR,
            "The request could not be completed.",
            request_id=_request_id(context),
        ),
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


# --- Django-level handlers (NOT DRF) ---------------------------------------
# A URL that matches no route never reaches a DRF view, so DRF's handler is
# never called and Django returns an HTML error page. For a JSON-only service
# that is a bad surprise - a client parsing JSON gets a syntax error instead
# of a 404. These two functions are wired in config/urls.py at Step 99.


def not_found_handler(request, exception=None):
    return JsonResponse(
        build_error_body(ErrorCode.NOT_FOUND, "No endpoint matches this URL."),
        status=404,
    )


def server_error_handler(request):
    return JsonResponse(
        build_error_body(ErrorCode.INTERNAL_ERROR, "An unexpected error occurred."),
        status=500,
    )
