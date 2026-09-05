"""framework independent client for the auth service"""

import logging
import os
import threading
import random
import time
import hashlib
import grpc
from django.conf import settings

from chat.grpc_clients.generated import auth_pb2_grpc, auth_pb2
from chat.grpc_clients.types import AuthIdentity
from chat.grpc_clients.exceptions import (
    InvalidTokenError,
    AuthUnavailableError,
    AuthProtocolError,
)
from django.conf import settings

logger = logging.getLogger(__name__)

RETRY_BASE_DELAY_SECONDS = 0.05
RETRY_MAX_DELAY_SECONDS = 0.40

RETRYABLE_CODES = (grpc.StatusCode.UNAVAILABLE,)

INVALID_TOKEN_CODES = frozenset(
    {
        grpc.StatusCode.UNAUTHENTICATED,
    }
)

UNAVAILABLE_CODES = frozenset(
    {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
    }
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
    ceiling = min(RETRY_BASE_DELAY_SECONDS * (2**attempt - 1), RETRY_MAX_DELAY_SECONDS)
    return random.uniform(ceiling / 2, ceiling)


def get_channel() -> grpc.Channel:
    """Return the process-wide channel, creating it on first use."""

    global _channel, _channel_pid

    pid = (
        os.getpid()
    )  # if a channet was created before the fork the workers , could inherit a channel that belongs to the parent process

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
    """client for authentication calls to the auth service"""

    def __init__(
        self,
        *,
        channel: grpc.Channel | None = None,
        stub=None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ):
        if stub is not None:
            self._stub = stub
        else:
            if channel is None:
                channel = get_channel()

            self._stub = auth_pb2_grpc.AuthServiceStub(channel)

        self._timeout = (
            settings.AUTH_GRPC_TIMEOUT_SECONDS if timeout is None else timeout
        )

        self._max_retries = (
            settings.AUTH_GRPC_MAX_RETRIES if max_retries is None else max_retries
        )

        self._target = settings.AUTH_GRPC_TARGET

    def verify_token(self, token: str) -> AuthIdentity:
        """validate a JWT token through the auth service"""

        if not token or not token.strip():
            raise InvalidTokenError()

        logger.debug(
            "Validating token token_fp=%s target=%s",
            token_fingerprint(token),
            self._target,
        )

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
                code_method = getattr(exc, "code", None)

                code = code_method() if callable(code_method) else None

                if code in INVALID_TOKEN_CODES:
                    raise InvalidTokenError(grpc_code=code) from exc

                if code in UNAVAILABLE_CODES:
                    if code != grpc.StatusCode.UNAVAILABLE or attempt >= max_retries:
                        raise AuthUnavailableError(grpc_code=code) from exc

                if code not in RETRYABLE_CODES or attempt >= max_retries:
                    raise AuthProtocolError(grpc_code=code) from exc

                delay = _backoff_delay(attempt + 1)
                logger.warning(
                    "Auth Service unavailable; retrying attempt=%s/%s delay=%.3fs target=%s token_fp=%s",
                    attempt + 1,
                    max_retries,
                    delay,
                    self._target,
                    token_fingerprint(token),
                )

                time.sleep(delay)

        if not response.active:
            raise InvalidTokenError()

        if not response.user_id or not response.user_id.strip():
            raise AuthProtocolError()

        return AuthIdentity(
            user_id=response.user_id,
            roles=tuple(role for role in response.roles if role),
        )

def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]
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


poetry run python manage.py shell -c "
import grpc
from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.generated import auth_pb2

class FakeRpcError(grpc.RpcError):
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code

class FakeStub:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def ValidateToken(self, request, timeout=None):
        self.calls += 1

        if self.error:
            raise self.error

        return self.response


# Test 1: Auth explicitly says active=False
stub1 = FakeStub(
    response=auth_pb2.TokenValidationResponse(active=False)
)

client1 = AuthServiceClient.__new__(AuthServiceClient)
client1._stub = stub1
client1._timeout = 2.0
client1._max_retries = 2
client1._target = 'fake:50051'

try:
    client1.verify_token('bad-token')
except Exception as exc:
    print('active=False:')
    print('  exception:', type(exc).__name__)
    print('  grpc_code:', exc.grpc_code)
    print('  calls:', stub1.calls)


# Test 2: Auth returns UNAUTHENTICATED
stub2 = FakeStub(
    error=FakeRpcError(grpc.StatusCode.UNAUTHENTICATED)
)

client2 = AuthServiceClient.__new__(AuthServiceClient)
client2._stub = stub2
client2._timeout = 2.0
client2._max_retries = 2
client2._target = 'fake:50051'

try:
    client2.verify_token('bad-token')
except Exception as exc:
    print('UNAUTHENTICATED:')
    print('  exception:', type(exc).__name__)
    print('  grpc_code:', exc.grpc_code)
    print('  calls:', stub2.calls)
"



poetry run python manage.py shell -c "
import grpc
from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.generated import auth_pb2

class FakeRpcError(grpc.RpcError):
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code

class FakeStub:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def ValidateToken(self, request, timeout=None):
        self.calls += 1
        raise self.error

for code in [
    grpc.StatusCode.DEADLINE_EXCEEDED,
    grpc.StatusCode.RESOURCE_EXHAUSTED,
    grpc.StatusCode.UNAVAILABLE,
]:
    stub = FakeStub(FakeRpcError(code))

    client = AuthServiceClient.__new__(AuthServiceClient)
    client._stub = stub
    client._timeout = 2.0
    client._max_retries = 2
    client._target = 'fake:50051'

    try:
        client.verify_token('test-token')
    except Exception as exc:
        print(f'{code.name}:')
        print(f'  exception: {type(exc).__name__}')
        print(f'  grpc_code: {exc.grpc_code}')
        print(f'  calls: {stub.calls}')
"


"""
