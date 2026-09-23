"""HTTP-level translations of Auth Service dependency failures (Step 82).

The Phase 3 client (chat.grpc_clients) raises framework-free exceptions.
This module is the ONLY place that turns them into status codes, which keeps
ADR-013's mapping table in one readable spot.
"""

from rest_framework import status
from rest_framework.exceptions import APIException


class AuthServiceUnavailable(APIException):
    """Auth Service could not be reached or did not answer in time.

    503, not 401: the client's credentials may be perfectly good. 503 plus
    Retry-After tells a well-behaved client to retry the SAME token shortly.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Authentication service is temporarily unavailable."
    default_code = "AUTH_UNAVAILABLE"

    # DRF's exception handler copies `wait` into the Retry-After header.
    wait = 5


class AuthServiceProtocolError(APIException):
    """Auth answered, but the answer violates ADR-002.

    502 rather than 503, because this will not fix itself: something is
    mismatched between the two services and someone needs to look.
    """

    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "Authentication service returned an unusable response."
    default_code = "AUTH_DEPENDENCY_ERROR"
