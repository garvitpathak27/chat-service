"""The error envelope: Step 98, and the routes from Steps 99-102."""

from unittest import mock

import pytest
from django.db import IntegrityError
from django.urls import reverse
from rest_framework.exceptions import (
    AuthenticationFailed,
    MethodNotAllowed,
    NotAuthenticated,
    NotFound,
    ParseError,
    PermissionDenied,
    ValidationError,
)
from rest_framework.test import APIClient

from chat.api.errors import (
    ALL_CODES,
    DEFAULT_CODE_BY_STATUS,
    DRF_CODE_MAP,
    ErrorCode,
    chat_exception_handler,
)
from chat.api.exceptions import (
    DirectRoomImmutable,
    LastAdminError,
    MemberAlreadyExists,
    MemberNotFound,
    NotImplementedYet,
)
from chat.authn import authentication
from chat.authn.exceptions import AuthServiceProtocolError, AuthServiceUnavailable
from chat.grpc_clients.exceptions import AuthUnavailableError, InvalidTokenError
from chat.grpc_clients.types import AuthIdentity


def handle(exc):
    return chat_exception_handler(exc, {"request": None})


# --- envelope shape --------------------------------------------------------


def test_envelope_has_exactly_four_keys():
    response = handle(PermissionDenied())
    assert set(response.data) == {"error"}
    assert set(response.data["error"]) == {"code", "message", "details", "request_id"}


def test_request_id_is_null_until_step_161():
    assert handle(PermissionDenied()).data["error"]["request_id"] is None


# --- code mapping ----------------------------------------------------------


@pytest.mark.parametrize(
    "exc, expected_status, expected_code",
    [
        (ValidationError({"name": ["required"]}), 400, ErrorCode.VALIDATION_FAILED),
        (ParseError(), 400, ErrorCode.PARSE_ERROR),
        (DirectRoomImmutable(), 400, ErrorCode.DIRECT_ROOM_IMMUTABLE),
        (NotAuthenticated(), 401, ErrorCode.AUTH_HEADER_MISSING),
        (
            AuthenticationFailed(detail="x", code="AUTH_HEADER_MALFORMED"),
            401,
            ErrorCode.AUTH_HEADER_MALFORMED,
        ),
        (
            AuthenticationFailed(detail="x", code="TOKEN_INVALID"),
            401,
            ErrorCode.TOKEN_INVALID,
        ),
        (PermissionDenied(), 403, ErrorCode.PERMISSION_DENIED),
        (NotFound("x", code="ROOM_NOT_FOUND"), 404, ErrorCode.ROOM_NOT_FOUND),
        (MemberNotFound(), 404, ErrorCode.MEMBER_NOT_FOUND),
        (NotFound(), 404, ErrorCode.NOT_FOUND),
        (MethodNotAllowed("PUT"), 405, ErrorCode.METHOD_NOT_ALLOWED),
        (MemberAlreadyExists(), 409, ErrorCode.MEMBER_ALREADY_EXISTS),
        (LastAdminError(), 409, ErrorCode.LAST_ADMIN),
        (NotImplementedYet(), 501, ErrorCode.NOT_IMPLEMENTED),
        (AuthServiceProtocolError(), 502, ErrorCode.AUTH_DEPENDENCY_ERROR),
        (AuthServiceUnavailable(), 503, ErrorCode.AUTH_UNAVAILABLE),
    ],
)
def test_exception_to_envelope(exc, expected_status, expected_code):
    response = handle(exc)
    assert response.status_code == expected_status
    assert response.data["error"]["code"] == expected_code


def test_validation_details_are_plain_types():
    response = handle(ValidationError({"name": ["A group room requires a non-empty name."]}))
    details = response.data["error"]["details"]
    assert details == {"name": ["A group room requires a non-empty name."]}
    assert type(details["name"][0]) is str  # not ErrorDetail


def test_non_validation_errors_have_no_details():
    assert handle(PermissionDenied()).data["error"]["details"] is None


def test_unhandled_exception_returns_none():
    """So Django produces a 500 rather than a misleading 4xx."""
    assert chat_exception_handler(RuntimeError("boom"), {"request": None}) is None


# --- IntegrityError mapping ------------------------------------------------


@pytest.mark.parametrize(
    "message, expected_status, expected_code",
    [
        (
            "(1062, \"Duplicate entry 'a-b' for key 'uniq_membership_room_user'\")",
            409,
            ErrorCode.MEMBER_ALREADY_EXISTS,
        ),
        (
            "(1062, \"Duplicate entry 'u1:u2' for key 'chat_room.direct_key'\")",
            409,
            ErrorCode.DIRECT_ROOM_EXISTS,
        ),
        (
            "(3819, \"Check constraint 'room_type_valid' is violated.\")",
            400,
            ErrorCode.VALIDATION_FAILED,
        ),
        (
            "(3819, \"Check constraint 'membership_role_valid' is violated.\")",
            400,
            ErrorCode.VALIDATION_FAILED,
        ),
        # Regression: this name CONTAINS "direct_key", so it must be matched
        # before the bare "direct_key" needle or it comes back as a 409.
        (
            "(3819, \"Check constraint 'room_direct_key_matches_type' is violated.\")",
            400,
            ErrorCode.VALIDATION_FAILED,
        ),
    ],
)
def test_integrity_errors_are_mapped(message, expected_status, expected_code):
    response = handle(IntegrityError(message))
    assert response.status_code == expected_status
    assert response.data["error"]["code"] == expected_code


def test_unmapped_integrity_error_is_500_and_leaks_nothing():
    response = handle(IntegrityError("(1452, 'some FK nobody mapped')"))
    assert response.status_code == 500
    assert response.data["error"]["code"] == ErrorCode.INTERNAL_ERROR
    assert "1452" not in response.data["error"]["message"]


# --- registry hygiene ------------------------------------------------------


def test_all_codes_are_upper_snake_case():
    for code in ALL_CODES:
        assert code == code.upper()
        assert " " not in code


def test_status_defaults_reference_real_codes():
    for code in DEFAULT_CODE_BY_STATUS.values():
        assert code in ALL_CODES


def test_drf_code_map_references_real_codes():
    for code in DRF_CODE_MAP.values():
        assert code in ALL_CODES


# --- end to end over HTTP --------------------------------------------------


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def auth_ok(monkeypatch):
    fake = mock.Mock()
    fake.verify_token.return_value = AuthIdentity(user_id="u-1", roles=("USER",))
    monkeypatch.setattr(authentication, "AuthServiceClient", lambda: fake)
    return fake


def test_unauthenticated_request_returns_the_envelope_and_a_challenge(client):
    response = client.get(reverse("chat:room-list"))
    assert response.status_code == 401
    assert response["WWW-Authenticate"] == 'Bearer realm="chat-service"'
    assert response.json()["error"]["code"] == ErrorCode.AUTH_HEADER_MISSING


def test_auth_unavailable_keeps_its_retry_after_header(client, auth_ok):
    auth_ok.verify_token.side_effect = AuthUnavailableError()
    response = client.get(reverse("chat:room-list"), HTTP_AUTHORIZATION="Bearer a.b.c")
    assert response.status_code == 503
    assert response["Retry-After"] == "5"
    assert response.json()["error"]["code"] == ErrorCode.AUTH_UNAVAILABLE


def test_invalid_token_returns_token_invalid(client, auth_ok):
    auth_ok.verify_token.side_effect = InvalidTokenError()
    response = client.get(reverse("chat:room-list"), HTTP_AUTHORIZATION="Bearer a.b.c")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == ErrorCode.TOKEN_INVALID


def test_authenticated_request_reaches_the_stub(client, auth_ok):
    response = client.get(reverse("chat:room-list"), HTTP_AUTHORIZATION="Bearer a.b.c")
    assert response.status_code == 501
    assert response.json()["error"]["code"] == ErrorCode.NOT_IMPLEMENTED


@pytest.mark.django_db
def test_unknown_room_is_room_not_found(client, auth_ok):
    url = reverse(
        "chat:room-detail",
        kwargs={"room_id": "11111111-1111-1111-1111-111111111111"},
    )
    response = client.get(url, HTTP_AUTHORIZATION="Bearer a.b.c")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.ROOM_NOT_FOUND


def test_wrong_method_is_405(client, auth_ok):
    response = client.put(reverse("chat:room-list"), HTTP_AUTHORIZATION="Bearer a.b.c")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == ErrorCode.METHOD_NOT_ALLOWED


def test_malformed_json_is_parse_error(client, auth_ok):
    response = client.post(
        reverse("chat:room-list"),
        data="{bad json",
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer a.b.c",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.PARSE_ERROR


def test_unmatched_url_returns_json_not_html(client):
    response = client.get("/api/nonsense/")
    assert response.status_code == 404
    assert response["Content-Type"].startswith("application/json")
    assert response.json()["error"]["code"] == ErrorCode.NOT_FOUND


# --- Steps 100-101: per-method permission tables --------------------------


def test_room_detail_permissions_differ_by_method():
    from chat.permissions import CanManageRoom, IsRoomMember
    from chat.views import RoomDetailView

    table = RoomDetailView.permission_classes_by_method
    assert IsRoomMember in table["GET"]
    assert CanManageRoom in table["PATCH"]
    assert CanManageRoom in table["DELETE"]
    assert "PUT" not in table


def test_member_routes_carry_room_and_user_kwargs():
    import uuid

    from django.urls import resolve

    room_id = uuid.uuid4()
    url = reverse("chat:room-member-detail", kwargs={"room_id": room_id, "user_id": "u-2"})
    match = resolve(url)
    assert match.func.view_class.__name__ == "MemberDetailView"
    assert match.kwargs == {"room_id": room_id, "user_id": "u-2"}


def test_non_uuid_room_id_is_json_404(client):
    response = client.get("/api/rooms/nope/")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.NOT_FOUND


@pytest.mark.django_db
def test_adr_014_matrix_over_the_real_routes(client, auth_ok):
    """The payoff of the stubs: 403 vs 501 decided by the real permissions."""
    from chat.models import Membership, MembershipRole, Room, RoomType

    room = Room.objects.create(type=RoomType.GROUP, name="G", created_by="admin")
    Membership.objects.create(room=room, user_id="admin", role=MembershipRole.ADMIN)
    Membership.objects.create(room=room, user_id="u-1", role=MembershipRole.MEMBER)

    def as_(user_id):
        auth_ok.verify_token.return_value = AuthIdentity(user_id=user_id, roles=("USER",))
        return {"HTTP_AUTHORIZATION": "Bearer a.b.c"}

    detail = reverse("chat:room-detail", kwargs={"room_id": room.pk})
    members = reverse("chat:room-member-list", kwargs={"room_id": room.pk})

    def member(user_id):
        return reverse("chat:room-member-detail", kwargs={"room_id": room.pk, "user_id": user_id})

    cases = [
        ("u-1", "get", detail, 501),
        ("u-1", "patch", detail, 403),
        ("u-1", "delete", detail, 403),
        ("admin", "patch", detail, 501),
        ("u-1", "get", members, 501),
        ("u-1", "post", members, 403),
        ("admin", "post", members, 501),
        ("u-1", "delete", member("u-1"), 501),    # self-leave
        ("u-1", "delete", member("admin"), 403),
        ("admin", "delete", member("u-1"), 501),
        ("stranger", "get", detail, 404),
    ]
    for user_id, method, url, expected in cases:
        response = getattr(client, method)(url, format="json", **as_(user_id))
        assert response.status_code == expected, (user_id, method, url, response.json())
        assert "error" in response.json()
