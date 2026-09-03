import grpc
import pytest

from chat.grpc_clients import auth_client
from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.exceptions import AuthUnavailableError, InvalidTokenError

from chat.grpc_clients.generated import auth_pb2
from chat.grpc_clients.generated import auth_pb2_grpc


class FakeRpcError(grpc.RpcError):
    def __init__(self, code, details="stimulated failure"):
        self._code = code
        self._details = details

    def code(self):
        return self._code

    def details(self):
        return self._details


class CodelessRpcError(grpc.RpcError):
    pass


class FakeStub:
    def __init__(self, *outcomes):
        self._outcomes = outcomes
        self.calls = []

    def ValidateToken(self, request, timeout=None):
        self.calls.append({"token": request.access_token, "timeout": timeout})

        outcomes = (
            self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        )

        if isinstance(outcomes, Exception):
            raise outcomes

        return outcomes


def ok_response(user_id="u-42", roles=("USER", 1)):
    return auth_pb2.TokenValidationResponse(
        active=True, user_id=user_id, roles=list(roles)
    )


def build(stub, **overrides):
    options = {
        "timeout": 2.0,
        "max_retries": 2,
    }
    options.update(overrides)

    return AuthServiceClient(
        stub=stub,
        **options,
    )


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(
        auth_client.time,
        "sleep",
        lambda seconds: None,
    )
