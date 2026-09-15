from __future__ import annotations 

import logging 
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from chat.authn.user import RemoteUser
from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.exceptions import (
    InvalidTokenError,
    AuthClientError,
    AuthProtocolError,
    AuthUnavailableError
)
from rest_framework.exceptions import AuthenticationFailed 
from chat.grpc_clients.exceptions import AuthServiceUnavailable
logger = logging.getLogger("chat.authn")

AUTH_SCHEME = b"bearer"

class ChatJWTAuthentication(BaseAuthentication):
    """ authenticates autorization : bearer <jwt >  against auth service """
    auth_realm = "chat-service"
    def authenticate(self , request):
        auth = get_authorization_header(request)

        if not auth:
            return 

        auth_parts = auth.split()


        if len(auth_parts) != 2:
            raise AuthenticationFailed(
                detail="AUTH_HEADER_MALFORMED",
                code="AUTH_HEADER_MALFORMED",
            )

        if auth_parts[0].lower() != AUTH_SCHEME:
            raise AuthenticationFailed(
                detail="AUTH_HEADER_MALFORMED",
                code="AUTH_HEADER_MALFORMED",
            )

        token = auth_parts[1]
        if not token:
            raise AuthenticationFailed(
                detail="AUTH_HEADER_MALFORMED",
                code="AUTH_HEADER_MALFORMED",
            )

        return self.verify(token)

    def authenticate_header(self, request):
        return f'Bearer realm="{self.auth_realm}"'

    def verify(self , token: str):
        """askl auth serive to verify the token"""
        client = AuthServiceClient()

        try:
            identity = client.verify_token(token)
        except InvalidTokenError:
            raise AuthenticationFailed(
                detail="Authentication credentials are invalid",
                code="TOKEN_INVALID",
            ) 
        except AuthUnavailableError:
            raise AuthServiceUnavailable()
        except AuthProtocolError:
            raise

        return RemoteUser(identity), identity
