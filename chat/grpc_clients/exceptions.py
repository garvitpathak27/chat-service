"""Failure modes of the Auth Service dependency.

Framework-free on purpose: nothing in chat.grpc_clients imports DRF or
Django's HTTP layer. chat.authn.exceptions maps these to status codes.
"""


class AuthClientError(Exception):
    """Base class for every failure of the Auth Service dependency."""

    default_message = "Auth Service call failed."

    def __init__(self, message=None, *, grpc_code=None):
        self.grpc_code = grpc_code
        super().__init__(message or self.default_message)


class InvalidTokenError(AuthClientError):
    """Auth Service explicitly rejected the supplied token."""

    default_message = "Auth token is invalid."


class AuthUnavailableError(AuthClientError):
    """Auth Service could not be reached or did not respond in time."""

    default_message = "Auth Service is unavailable."


class AuthProtocolError(AuthClientError):
    """Auth Service returned an unusable or unexpected response."""

    default_message = "Auth Service returned an invalid response."
