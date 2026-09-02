"""framework independent client for the auth service """
import logging
import os
import threading

import grpc
from django.conf import settings

from chat.grpc_clients.generated import auth_pb2_grpc, auth_pb2
from chat.grpc_clients.types import AuthIdentity
from chat.grpc_clients.exceptions import InvalidTokenError


logger = logging.getLogger(__name__)

CHANNEL_OPTIONS = [
    ("grpc.keepalive_time_ms", 30000),
    ("grpc.keepalive_timeout_ms", 10000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
    ("grpc.enable_retries", 0),
    ("grpc.max_receive_message_length", 1 * 1024 * 1024),
]

_channel = None
_channel_pid = None
_channel_lock = threading.Lock()

def get_channel() -> grpc.Channel:
    """Return the process-wide channel, creating it on first use."""

    global _channel, _channel_pid

    pid = os.getpid()

    if _channel is not None and _channel_pid == pid:
        return _channel

    with _channel_lock:
        if _channel is not None and _channel_pid == pid:
            return _channel

        if _channel is not None:
            logger.info(
                "discarding gRPC channel inherited from pid=%s in pid=%s",
                _channel_pid,
                pid,
            )

        target = settings.AUTH_GRPC_TARGET

        logger.info(
            "opening gRPC channel to Auth Service target=%s pid=%s",
            target,
            pid,
        )

        _channel = grpc.insecure_channel(
            target,
            options=CHANNEL_OPTIONS,
        )

        _channel_pid = pid

        return _channel


class AuthServiceClient:
    """client for authentication calls to the auth service """

    def __init__(self, channel: grpc.Channel):
        self._stub = auth_pb2_grpc.AuthServiceStub(channel)

    def verify_token(self,token : str) -> AuthIdentity:
        """validate a JWT token through the auth service """

        request = auth_pb2.TokenRequest(access_token=token)
        response = self._stub.ValidateToken(request)


        if not response.active:
            raise InvalidTokenError()

        return AuthIdentity(
            user_id=response.user_id,
            roles=tuple(response.roles)
        )




"""
HOW TO CHECK WITHOUT RUNNING THE AUTH SERVICE 
poetry run python -c "
import grpc

from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.generated import auth_pb2


class FakeStub:
    def ValidateToken(self, request):
        print('token received:', request.access_token)

        return auth_pb2.TokenValidationResponse(
            active=True,
            user_id='123',
            roles=['member', 'moderator'],
            permissions=['messages.send'],
        )


channel = grpc.insecure_channel('127.0.0.1:1')
client = AuthServiceClient(channel)

client._stub = FakeStub()

identity = client.verify_token('fake.jwt.token')

print('identity:', identity)
print('user_id:', identity.user_id)
print('roles:', identity.roles)

channel.close()
"

poetry run python -c "
import grpc

from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.exceptions import InvalidTokenError
from chat.grpc_clients.generated import auth_pb2


class FakeStub:
    def ValidateToken(self, request):
        return auth_pb2.TokenValidationResponse(active=False)


channel = grpc.insecure_channel('127.0.0.1:1')
client = AuthServiceClient(channel)

client._stub = FakeStub()

try:
    client.verify_token('bad.jwt.token')
except InvalidTokenError as e:
    print('caught:', type(e).__name__)
    print('message:', str(e))
else:
    print('ERROR: InvalidTokenError was not raised')

channel.close()
"



"""