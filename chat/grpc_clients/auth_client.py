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


# Keepalive must stay within what the Auth gRPC server accepts. A gRPC server
# by default rejects pings on an idle connection and pings more often than
# every 5 minutes; a client that sends them gets GOAWAY "too_many_pings" and
# its channel dropped. So: ping only while a call is in flight, and no more
# often than every 5 minutes. A dead idle connection is still recovered,
# because the next call fails with UNAVAILABLE and is retried (Step 68).
CHANNEL_OPTIONS = [
    ("grpc.keepalive_time_ms", 300000),
    ("grpc.keepalive_timeout_ms", 10000),
    ("grpc.keepalive_permit_without_calls", 0),
    ("grpc.enable_retries", 0),
    ("grpc.max_receive_message_length", 1 * 1024 * 1024),
]

_channel = None
_channel_pid = None
_channel_lock = threading.Lock()


def _backoff_delay(attempt: int) -> float:
    # attempt 1 -> 0.05s, attempt 2 -> 0.10s, attempt 3 -> 0.20s, capped at 0.40s
    ceiling = min(RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)), RETRY_MAX_DELAY_SECONDS)
    return random.uniform(ceiling / 2, ceiling)


def get_channel() -> grpc.Channel:
    """Return the process-wide channel, creating it on first use."""

    global _channel, _channel_pid

    # If a channel was created before a fork, the worker could inherit a
    # channel that belongs to the parent process; the PID check discards it.
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
