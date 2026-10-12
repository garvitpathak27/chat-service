
from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

HEADER_NAME = "X-Request-ID"

_SAFE_ID = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")

_request_id: ContextVar[str] = ContextVar("chat_request_id", default="")


def get_request_id() -> str:
    return _request_id.get()


def new_request_id() -> str:
    return uuid.uuid4().hex


def normalize_request_id(value: str | None) -> str:
    if value and _SAFE_ID.match(value):
        return value
    return new_request_id()


class CorrelationIdMiddleware:
    """MUST be first in MIDDLEWARE so everything downstream has the id."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = normalize_request_id(request.headers.get(HEADER_NAME))

        token = _request_id.set(request_id)
        request.request_id = request_id

        try:
            response = self.get_response(request)
        finally:
            _request_id.reset(token)

        response[HEADER_NAME] = request_id
        return response

