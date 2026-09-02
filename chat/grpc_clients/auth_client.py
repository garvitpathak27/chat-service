"""framework independent client for the auth service """
import logging
import os
import threading
import random
import time

import grpc
from django.conf import settings

from chat.grpc_clients.generated import auth_pb2_grpc, auth_pb2
from chat.grpc_clients.types import AuthIdentity
from chat.grpc_clients.exceptions import InvalidTokenError
from django.conf import settings

logger = logging.getLogger(__name__)

RETRY_BASE_DELAY_SECONDS = 0.05
RETRY_MAX_DELAY_SECONDS = 0.40

RETRYABLE_CODES = (
    grpc.StatusCode.UNAVAILABLE,
)

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


def _backoff_delay(attempt: int) -> float:
    ceiling = min(
        RETRY_BASE_DELAY_SECONDS * (2 ** attempt -1),
        RETRY_MAX_DELAY_SECONDS
    )
    return random.uniform(ceiling / 2, ceiling)

def get_channel() -> grpc.Channel:
    """Return the process-wide channel, creating it on first use."""

    global _channel, _channel_pid

    pid = os.getpid() # if a channet was created before the fork the workers , could inherit a channel that belongs to the parent process 

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

    def __init__(self, channel: grpc.Channel | None = None):
        if channel is None:
            channel = get_channel()

        self._stub = auth_pb2_grpc.AuthServiceStub(channel)
        self._timeout = settings.AUTH_GRPC_TIMEOUT_SECONDS
        self._max_retries = settings.AUTH_GRPC_MAX_RETRIES
        self._target = settings.AUTH_GRPC_TARGET

    def verify_token(self,token : str) -> AuthIdentity:
        """validate a JWT token through the auth service """

        request = auth_pb2.TokenRequest(access_token=token)
        # response = self._stub.ValidateToken(
        #     request,
        #     timeout=settings.AUTH_GRPC_TIMEOUT_SECONDS,
        # )


        max_retries = self._max_retries

        for attempt in range(max_retries + 1):
            try:
                response = self._stub.ValidateToken(
                    request,
                    timeout=self._timeout,
                )
                break

            except grpc.RpcError as exc:
                if exc.code() not in RETRYABLE_CODES or attempt >= max_retries:
                    raise

                delay = _backoff_delay(attempt + 1)

                logger.warning(
                    "Auth Service unavailable; retrying attempt=%s/%s delay=%.3fs target=%s",
                    attempt + 1,
                    max_retries,
                    delay,
                    self._target,
                )

                time.sleep(delay)


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


""" how to test the bounded retry backof funciton lity 
poetry run python manage.py shell -c "
import grpc
from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.generated import auth_pb2

class FakeRpcError(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

class FakeStub:
    def __init__(self):
        self.calls = 0

    def ValidateToken(self, request, timeout=None):
        self.calls += 1
        print('call:', self.calls)

        if self.calls < 3:
            raise FakeRpcError()

        return auth_pb2.TokenValidationResponse(
            active=True,
            user_id='123',
            roles=['member'],
        )

fake_stub = FakeStub()

client = AuthServiceClient.__new__(AuthServiceClient)
client._stub = fake_stub
client._timeout = 2.0
client._max_retries = 2
client._target = 'fake:50051'

identity = client.verify_token('fake.jwt.token')

print('identity:', identity)
print('total calls:', fake_stub.calls)
"



"""