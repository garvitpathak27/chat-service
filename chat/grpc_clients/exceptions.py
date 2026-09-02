""" failure modes of the auth service dependency """

class AuthClientError(Exception):
    """Base class for every failure of the Auth Service dependency."""

    default_message = "Auth Service call failed."

    def __init__(self, message=None, *, grpc_code=None):
        self.grpc_code = grpc_code
        super().__init__(message or self.default_message)

class InvalidTokenError(AuthClientError):
    """Auth service explicitly rejected the supplied token """
    default_message = "auth token is invalid "

class AuthUnavailableError(AuthClientError):
    """Auth service could not be reached or did not respond in time """
    default_message = "auth service is unavailable"

class AuthProtocolError(AuthClientError):
    """Auth service returned an unusable or unexpected response """
    default_message = "Authentication service returned an invalid response "

