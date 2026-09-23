"""The single authentication mechanism for Chat Service.

Every protected request passes through here, and here calls Auth Service.
There is no local credential check, no session fallback, and no cache: if
Auth cannot answer, the request does not proceed (fail closed).
"""

from __future__ import annotations

import logging

from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from chat.authn.exceptions import AuthServiceProtocolError, AuthServiceUnavailable
from chat.authn.user import RemoteUser
from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.exceptions import (
    AuthProtocolError,
    AuthUnavailableError,
    InvalidTokenError,
)

logger = logging.getLogger("chat.authn")

AUTH_SCHEME = b"bearer"


class ChatJWTAuthentication(BaseAuthentication):
    """Authenticates `Authorization: Bearer <jwt>` against Auth Service."""

    auth_realm = "chat-service"

    def authenticate(self, request):
        auth = get_authorization_header(request)  # bytes, b"" when absent

        if not auth:
            return None  # -> 401 via authenticate_header() (Step 80)

        auth_parts = auth.split()

        if not auth_parts or auth_parts[0].lower() != AUTH_SCHEME:
            raise AuthenticationFailed(
                detail="Authorization header must use the Bearer scheme.",
                code="AUTH_HEADER_MALFORMED",
            )

        if len(auth_parts) != 2:
            raise AuthenticationFailed(
                detail="Authorization header is malformed.",
                code="AUTH_HEADER_MALFORMED",
            )

        try:
            # The header arrives as bytes. The Phase 3 client expects str
            # (token_fingerprint() calls token.encode()). JWTs are base64url,
            # so strictly ASCII; anything else is a corrupted header.
            token = auth_parts[1].decode("ascii")
        except UnicodeDecodeError:
            raise AuthenticationFailed(
                detail="Authorization header is malformed.",
                code="AUTH_HEADER_MALFORMED",
            )

        return self.verify(token)

    def authenticate_header(self, request):
        """Non-empty return is what makes DRF answer 401 rather than 403."""
        return f'Bearer realm="{self.auth_realm}"'

    def verify(self, token: str):
        """Ask Auth Service to verify the token."""
        client = AuthServiceClient()

        try:
            identity = client.verify_token(token)

        except InvalidTokenError as exc:
            # ADR-013: expired, forged and revoked are indistinguishable here.
            raise AuthenticationFailed(
                detail="Authentication credentials are invalid.",
                code="TOKEN_INVALID",
            ) from exc

        except AuthUnavailableError as exc:
            logger.error(
                "authentication failed: Auth Service unavailable (%s)",
                getattr(exc.grpc_code, "name", exc.grpc_code),
            )
            raise AuthServiceUnavailable() from exc

        except AuthProtocolError as exc:
            logger.error("authentication failed: Auth Service protocol error: %s", exc)
            raise AuthServiceProtocolError() from exc

        return RemoteUser(identity), identity
