"""Authentication: Steps 75-82, against ADR-013's mapping table.

No database and no Auth Service - the gRPC client is patched out. What is
under test is Chat's translation layer, not the Phase 3 client (which has its
own suite in test_auth_client.py).
"""

from unittest import mock

import pytest
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from chat.authn import authentication
from chat.authn.authentication import ChatJWTAuthentication
from chat.authn.user import RemoteUser, identity_of
from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.exceptions import (
    AuthProtocolError,
    AuthUnavailableError,
    InvalidTokenError,
)
from chat.grpc_clients.generated import auth_pb2
from chat.grpc_clients.types import AuthIdentity

factory = APIRequestFactory()


class ProbeView(APIView):
    authentication_classes = [ChatJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        identity = identity_of(request)
        return Response(
            {
                "user_id": identity.user_id,
                "roles": list(identity.roles),
                "user_type": type(request.user).__name__,
            }
        )


probe = ProbeView.as_view()


@pytest.fixture
def auth_service(monkeypatch):
    client = mock.Mock()
    client.verify_token.return_value = AuthIdentity(user_id="u-42", roles=("USER",))
    monkeypatch.setattr(authentication, "AuthServiceClient", lambda: client)
    return client


def call(header=None):
    kwargs = {"HTTP_AUTHORIZATION": header} if header is not None else {}
    response = probe(factory.get("/probe/", **kwargs))
    response.render()
    return response


# --- happy path ------------------------------------------------------------


def test_valid_bearer_token_authenticates(auth_service):
    response = call("Bearer header.payload.signature")
    assert response.status_code == 200
    assert response.data["user_id"] == "u-42"
    assert response.data["roles"] == ["USER"]


def test_bearer_scheme_is_case_insensitive(auth_service):
    assert call("bearer header.payload.signature").status_code == 200
    assert call("BEARER header.payload.signature").status_code == 200


def test_token_is_forwarded_without_the_bearer_prefix(auth_service):
    call("Bearer abc.def.ghi")
    assert auth_service.verify_token.call_args[0][0] == "abc.def.ghi"


def test_token_is_forwarded_as_str_not_bytes(auth_service):
    """Regression: the header arrives as bytes; the client needs str."""
    call("Bearer abc.def.ghi")
    assert isinstance(auth_service.verify_token.call_args[0][0], str)


def test_real_client_accepts_the_token_from_the_header(monkeypatch):
    """End to end through the real AuthServiceClient, with only the gRPC stub
    faked. With a bytes token this used to crash with a 500 inside
    token_fingerprint() before the RPC was ever made."""

    class FakeStub:
        def __init__(self):
            self.tokens = []

        def ValidateToken(self, request, timeout=None):
            self.tokens.append(request.access_token)
            return auth_pb2.TokenValidationResponse(
                active=True, user_id="u-7", roles=["USER"]
            )

    stub = FakeStub()
    monkeypatch.setattr(
        authentication, "AuthServiceClient", lambda: AuthServiceClient(stub=stub)
    )

    response = call("Bearer abc.def.ghi")

    assert response.status_code == 200
    assert response.data["user_id"] == "u-7"
    assert stub.tokens == ["abc.def.ghi"]


def test_auth_service_is_called_exactly_once_per_request(auth_service):
    call("Bearer abc.def.ghi")
    assert auth_service.verify_token.call_count == 1


def test_request_user_is_a_remote_user_not_a_django_user(auth_service):
    assert call("Bearer abc.def.ghi").data["user_type"] == RemoteUser.__name__


# --- Step 80: missing credentials -----------------------------------------


def test_missing_header_is_401_with_challenge(auth_service):
    response = call()
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="chat-service"'
    assert auth_service.verify_token.call_count == 0


def test_empty_header_is_401(auth_service):
    assert call("").status_code == 401


# --- Step 76: malformed credentials ---------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        "Basic dXNlcjpwYXNz",
        "Token abc.def.ghi",
        "JWT abc.def.ghi",
        "Bearer",
        "Bearer ",
        "Bearer abc def",
        "Bearer Bearer abc.def.ghi",
        "Bearer ünicode",
        "   ",
    ],
)
def test_malformed_header_variants_are_401(auth_service, header):
    response = call(header)
    assert response.status_code == 401
    # Step 98: the envelope carries the code (was data["detail"].code).
    assert response.data["error"]["code"] == "AUTH_HEADER_MALFORMED"
    # Never forwarded to Auth: a malformed header is rejected locally.
    assert auth_service.verify_token.call_count == 0


# --- Step 81: invalid credentials -----------------------------------------


def test_invalid_token_is_401(auth_service):
    auth_service.verify_token.side_effect = InvalidTokenError()
    response = call("Bearer expired.jwt.here")
    assert response.status_code == 401
    # Step 98: the envelope carries the code (was data["detail"].code).
    assert response.data["error"]["code"] == "TOKEN_INVALID"


def test_401_body_leaks_no_validation_detail(auth_service):
    auth_service.verify_token.side_effect = InvalidTokenError(
        "jwt expired at 2026-08-01T00:00:00Z"
    )
    body = str(call("Bearer expired.jwt.here").data).lower()
    for leak in ("expired", "signature", "2026-08-01", "unauthenticated"):
        assert leak not in body


# --- Step 82: dependency failures, fail closed ----------------------------


def test_auth_unavailable_is_503_with_retry_after(auth_service):
    auth_service.verify_token.side_effect = AuthUnavailableError()
    response = call("Bearer perfectly.good.token")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"


def test_auth_protocol_error_is_502(auth_service):
    auth_service.verify_token.side_effect = AuthProtocolError("empty user_id")
    response = call("Bearer perfectly.good.token")
    assert response.status_code == 502
    assert "Retry-After" not in response.headers


@pytest.mark.parametrize(
    "error", [InvalidTokenError(), AuthUnavailableError(), AuthProtocolError("x")]
)
def test_no_auth_failure_ever_reaches_the_view(auth_service, error):
    """The fail-closed property, asserted from the outside."""
    auth_service.verify_token.side_effect = error
    response = call("Bearer perfectly.good.token")
    assert response.status_code != 200
    assert "user_id" not in (response.data or {})


# --- Step 75: configuration -----------------------------------------------


def test_no_local_authentication_fallback_is_configured():
    names = [cls.__name__ for cls in api_settings.DEFAULT_AUTHENTICATION_CLASSES]
    assert names == ["ChatJWTAuthentication"]
    assert "SessionAuthentication" not in names
    assert "BasicAuthentication" not in names


def test_endpoints_are_protected_by_default():
    names = [cls.__name__ for cls in api_settings.DEFAULT_PERMISSION_CLASSES]
    assert "IsAuthenticated" in names
    assert "AllowAny" not in names


def test_grpc_client_package_does_not_depend_on_drf():
    """Layering (Phase 3): chat.grpc_clients stays framework-free; only
    chat.authn translates its errors into HTTP responses."""
    import pathlib

    import chat.grpc_clients as pkg

    root = pathlib.Path(pkg.__file__).parent
    offenders = [
        str(path)
        for path in root.rglob("*.py")
        if "generated" not in path.parts and "rest_framework" in path.read_text()
    ]
    assert not offenders, offenders
