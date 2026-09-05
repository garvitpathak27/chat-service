import grpc
import pytest
import logging
from chat.grpc_clients import auth_client
from chat.grpc_clients.auth_client import AuthServiceClient , token_fingerprint
from chat.grpc_clients.exceptions import (
    AuthUnavailableError,
    InvalidTokenError,
    AuthProtocolError,
    AuthClientError,
)

from chat.grpc_clients.generated import auth_pb2
from chat.grpc_clients.generated import auth_pb2_grpc
from chat.grpc_clients.types import AuthIdentity


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
        self._outcomes = list(outcomes)
        self.calls = []

    def ValidateToken(self, request, timeout=None):
        self.calls.append({"token": request.access_token, "timeout": timeout})

        outcomes = (
            self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        )

        if isinstance(outcomes, Exception):
            raise outcomes

        return outcomes


def ok_response(user_id="u-42", roles=("USER", "ADMIN")):
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


def test_valid_token_returns_identity():
    client = build(
        FakeStub(
            ok_response(
                user_id="u-42",
                roles=("USER", "ADMIN"),
            )
        )
    )

    identity = client.verify_token("header.payload.signature")

    assert isinstance(identity, AuthIdentity)
    assert identity.user_id == "u-42"
    assert identity.roles == ("USER", "ADMIN")


def test_roles_are_a_tuple_with_empty_entries_dropped():
    client = build(
        FakeStub(
            ok_response(
                roles=("USER", "", "ADMIN"),
            )
        )
    )

    identity = client.verify_token("t")

    assert identity.roles == ("USER", "ADMIN")
    assert isinstance(identity.roles, tuple)


def test_response_without_roles_yields_empty_tuple():
    client = build(
        FakeStub(
            auth_pb2.TokenValidationResponse(
                active=True,
                user_id="u-1",
            )
        )
    )

    assert client.verify_token("t").roles == ()


def test_inactive_token_raises_invalid_token_error():
    client = build(
        FakeStub(
            auth_pb2.TokenValidationResponse(
                active=False,
            )
        )
    )

    with pytest.raises(InvalidTokenError):
        client.verify_token("expired-or-invalid-token")


def test_unauthenticated_rpc_error_raises_invalid_token_error():
    client = build(FakeStub(FakeRpcError(grpc.StatusCode.UNAUTHENTICATED)))

    with pytest.raises(InvalidTokenError):
        client.verify_token("invalid-token")


def test_unavailable_retries_and_then_gives_up():
    error = FakeRpcError(grpc.StatusCode.UNAVAILABLE)

    stub = FakeStub(error)

    client = build(stub, max_retries=2)

    with pytest.raises(AuthUnavailableError):
        client.verify_token("token")

    assert len(stub.calls) == 3


def test_unavailable_then_success_recovers():
    error = FakeRpcError(grpc.StatusCode.UNAVAILABLE)

    stub = FakeStub(
        error,
        ok_response(
            user_id="u-42",
            roles=("USER",),
        ),
    )

    client = build(stub, max_retries=2)

    identity = client.verify_token("token")

    assert identity.user_id == "u-42"
    assert identity.roles == ("USER",)
    assert len(stub.calls) == 2


def test_deadline_exceeded_is_not_retried():
    stub = FakeStub(FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED))

    client = build(stub, max_retries=2)

    with pytest.raises(AuthUnavailableError):
        client.verify_token("token")

    assert len(stub.calls) == 1


def test_resource_exhausted_is_not_retried():
    stub = FakeStub(FakeRpcError(grpc.StatusCode.RESOURCE_EXHAUSTED))

    client = build(stub, max_retries=2)

    with pytest.raises(AuthUnavailableError):
        client.verify_token("token")

    assert len(stub.calls) == 1


def test_deadline_is_passed_to_every_attempt():
    error = FakeRpcError(grpc.StatusCode.UNAVAILABLE)

    stub = FakeStub(error)

    client = build(
        stub,
        timeout=1.5,
        max_retries=2,
    )

    with pytest.raises(AuthUnavailableError):
        client.verify_token("token")

    assert len(stub.calls) == 3
    assert all(call["timeout"] == 1.5 for call in stub.calls)


def test_max_retries_zero_means_exactly_one_attempt():
    stub = FakeStub(FakeRpcError(grpc.StatusCode.UNAVAILABLE))

    client = build(stub, max_retries=0)

    with pytest.raises(AuthUnavailableError):
        client.verify_token("token")

    assert len(stub.calls) == 1


def test_client_defaults_come_from_settings(monkeypatch):
    monkeypatch.setattr(
        auth_client.settings,
        "AUTH_GRPC_TIMEOUT_SECONDS",
        3.5,
    )

    monkeypatch.setattr(
        auth_client.settings,
        "AUTH_GRPC_MAX_RETRIES",
        4,
    )

    stub = FakeStub(FakeRpcError(grpc.StatusCode.UNAVAILABLE))

    client = AuthServiceClient(stub=stub)

    with pytest.raises(AuthUnavailableError):
        client.verify_token("token")

    assert len(stub.calls) == 5
    assert all(call["timeout"] == 3.5 for call in stub.calls)


def test_unexpected_status_raises_protocol_error():
    stub = FakeStub(FakeRpcError(grpc.StatusCode.PERMISSION_DENIED))

    with pytest.raises(AuthProtocolError):
        build(stub).verify_token("token")

    assert len(stub.calls) == 1


def test_missing_token_rejected_without_rpc_call():
    stub = FakeStub(ok_response())
    client = AuthServiceClient(stub=stub)

    with pytest.raises(InvalidTokenError):
        client.verify_token("")

    assert stub.calls == []


def test_whitespace_token_rejected_without_rpc_calls():
    stub = FakeStub(ok_response())
    client = AuthServiceClient(stub=stub)

    with pytest.raises(InvalidTokenError):
        client.verify_token("   ")

    assert stub.calls == []


def test_configured_timeout_is_passed_to_rpc():
    stub = FakeStub(ok_response())
    client = AuthServiceClient(
        stub=stub,
        timeout=3.5,
    )
    client.verify_token("valid-token")

    assert stub.calls[0]["timeout"] == 3.5


def test_max_retries_zero_does_not_retry():
    stub = FakeStub(
        FakeRpcError(grpc.StatusCode.UNAVAILABLE),
        ok_response(),
    )

    client = AuthServiceClient(
        stub=stub,
        max_retries=0,
    )
    with pytest.raises(AuthUnavailableError):
        client.verify_token("valid-token")

    assert len(stub.calls) == 1


def test_client_defaults_come_from_setting(monkeypatch):
    monkeypatch.setattr(
        auth_client.settings,
        "AUTH_GRPC_TIMEOUT_SECONDS",
        3.5,
    )
    monkeypatch.setattr(
        auth_client.settings,
        "AUTH_GRPC_MAX_RETRIES",
        4,
    )

    stub = FakeStub(FakeRpcError(grpc.StatusCode.UNAVAILABLE))

    client = AuthServiceClient(stub=stub)

    with pytest.raises(AuthUnavailableError):
        client.verify_token("valid-token")

    assert len(stub.calls) == 5


def test_codeless_rpc_error_raises_protocol_error():
    stub = FakeStub(CodelessRpcError())
    client = AuthServiceClient(stub=stub)

    with pytest.raises(AuthProtocolError):
        client.verify_token("valid-token")


# def test_empty_user_id_is_protocol_error():
#     stub = FakeStub(
#         ok_response(user_id="")

#     )
#     client = AuthServiceClient(stub=stub)
#     try:
#         with pytest.raises(AuthProtocolError) as excinfo:
#             client.verify_token("valid-token")
#         assert excinfo.value.args[0] == 'your_error_message_returned_from_Auth_protocall_error'
#     except:
#         assert True


def test_empty_user_id_is_protocol_error():
    stub = FakeStub(ok_response(user_id=""))
    client = AuthServiceClient(stub=stub)

    with pytest.raises(AuthProtocolError):
        client.verify_token("valid-token")


def test_identity_can_check_roles():
    client = build(
        FakeStub(
            ok_response(
                user_id="u-42",
                roles=("USER", "ADMIN"),
            )
        )
    )

    identity = client.verify_token("valid-token")

    assert identity.has_role("ADMIN")
    assert identity.has_role("USER")
    assert not identity.has_role("SUPERADMIN")

def test_every_failure_share_one_base_class():
    for excType in (
        InvalidTokenError,
        AuthUnavailableError,
        AuthProtocolError
    ):
        assert issubclass(excType , AuthClientError)

def test_token_never_appears_in_logs(caplog):
    secret = "eyJhbGciOiJIUzI1NiJ9.SUPERSECRETPAYLOAD.signature"

    caplog.set_level(
        logging.DEBUG,
        logger="chat.grpc_clients.auth_client"
    )
    build(FakeStub(ok_response())).verify_token(secret)

    combined = "\n".join(
        record.getMessage()
        for record in caplog.records
    )

    assert secret not in combined
    assert "SUPERSECRETPAYLOAD" not in combined
    assert token_fingerprint(secret) in combined


def test_fingerprint_is_stable_short_and_not_the_token():
    token = "a.b.c"

    fingerprint = token_fingerprint(token)

    assert fingerprint == token_fingerprint(token)
    assert len(fingerprint) == 12
    assert token not in fingerprint

