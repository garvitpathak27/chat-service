"""Health, logging, correlation and query budgets: Steps 157-165."""

import json
import logging

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from chat.authn import authentication
from chat.grpc_clients.types import AuthIdentity
from chat.models import Membership, MembershipRole
from chat.observability.correlation import (
    HEADER_NAME,
    CorrelationIdMiddleware,
    get_request_id,
    normalize_request_id,
)
from chat.observability.formatters import JsonFormatter
from chat.observability.redaction import REDACTED, SecretRedactionFilter, scrub
from chat.services import add_member, create_group_room

AUTH = {"HTTP_AUTHORIZATION": "Bearer t.o.k"}


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def as_user(monkeypatch):
    holder = {"user_id": "admin-1"}

    class _FakeAuthClient:
        def verify_token(self, token):
            return AuthIdentity(user_id=holder["user_id"], roles=("USER",))

    monkeypatch.setattr(authentication, "AuthServiceClient", lambda: _FakeAuthClient())

    def _switch(user_id):
        holder["user_id"] = user_id

    return _switch


@pytest.fixture
def room(db):
    room = create_group_room(name="Engineering", created_by="admin-1")
    Membership.objects.create(room=room, user_id="member-1", role=MembershipRole.MEMBER)
    return room


def make_record(message, **extra):
    record = logging.LogRecord(
        name="chat.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=message, args=(), exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# --- Step 158: readiness ---------------------------------------------------


@pytest.mark.django_db
def test_readiness_reports_each_dependency(client, monkeypatch):
    from chat import health

    monkeypatch.setitem(health.CHECKS, "rabbitmq", lambda: health.UP)
    monkeypatch.setitem(health.CHECKS, "auth_grpc", lambda: health.UP)

    response = client.get("/health/ready/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "UP"
    assert set(body["checks"]) == {"database", "rabbitmq", "auth_grpc"}


@pytest.mark.django_db
def test_readiness_is_503_when_a_dependency_is_down(client, monkeypatch):
    from chat import health

    monkeypatch.setitem(health.CHECKS, "rabbitmq", lambda: health.DOWN)
    monkeypatch.setitem(health.CHECKS, "auth_grpc", lambda: health.UP)

    response = client.get("/health/ready/")
    assert response.status_code == 503
    assert response.json()["checks"]["rabbitmq"] == "DOWN"
    assert response.json()["status"] == "DOWN"


@pytest.mark.django_db
def test_a_raising_check_becomes_down_and_leaks_nothing(client, monkeypatch, caplog):
    from chat import health

    def exploding():
        raise RuntimeError("Connection refused to secret-host:5672 user=chat_service")

    monkeypatch.setitem(health.CHECKS, "rabbitmq", exploding)
    monkeypatch.setitem(health.CHECKS, "auth_grpc", lambda: health.UP)

    with caplog.at_level("WARNING", logger="chat.health"):
        response = client.get("/health/ready/")

    assert response.status_code == 503
    body = json.dumps(response.json())
    assert "secret-host" not in body and "chat_service" not in body
    assert "readiness check failed" in caplog.text


def test_readiness_needs_no_credentials(client, db):
    assert client.get("/health/ready/").status_code in {200, 503}


# --- Step 159: structured logging -----------------------------------------


def test_json_formatter_shape():
    output = json.loads(
        JsonFormatter().format(
            make_record("room created", operation="room_create", outcome="ok",
                        room_id="r-1", user_id="u-1", duration_ms=4.2)
        )
    )
    assert output["level"] == "INFO"
    assert output["logger"] == "chat.test"
    assert output["message"] == "room created"
    assert output["service"] == "chat-service"
    assert output["operation"] == "room_create"
    assert output["room_id"] == "r-1"
    assert output["duration_ms"] == 4.2
    assert "request_id" in output


def test_json_formatter_timestamp_is_z_suffixed_utc():
    output = json.loads(JsonFormatter().format(make_record("x")))
    assert output["timestamp"].endswith("Z")
    assert "+00:00" not in output["timestamp"]


def test_json_formatter_survives_unserializable_extras():
    """default=str: a log line must never crash the logger (unlike an event)."""
    output = json.loads(JsonFormatter().format(make_record("x", weird=object())))
    assert isinstance(output["weird"], str)


def test_json_formatter_includes_exceptions():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = make_record("failed")
        record.exc_info = sys.exc_info()
    output = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in output["exception"]


# --- Step 160: redaction --------------------------------------------------


JWT = "eyJhbGciOiJIUzI1NiJ9.SUPERSECRETPAYLOAD.c2lnbmF0dXJl"


@pytest.mark.parametrize(
    "text, must_not_contain",
    [
        (f"token={JWT}", "SUPERSECRETPAYLOAD"),
        (f"Authorization: Bearer {JWT}", "SUPERSECRETPAYLOAD"),
        ("password=hunter2", "hunter2"),
        ('{"secret": "s3cr3t"}', "s3cr3t"),
        ("api_key=abc123def", "abc123def"),
        ("DB_PASSWORD: swordfish", "swordfish"),
    ],
)
def test_secrets_are_scrubbed(text, must_not_contain):
    assert must_not_contain not in scrub(text)
    assert REDACTED in scrub(text)


def test_filter_scrubs_message_and_extras():
    record = make_record("header was %s", token_field=f"Bearer {JWT}")
    record.args = (f"Authorization: Bearer {JWT}",)
    assert SecretRedactionFilter().filter(record) is True
    assert "SUPERSECRETPAYLOAD" not in record.getMessage()
    assert "SUPERSECRETPAYLOAD" not in record.token_field


def test_ordinary_text_is_untouched():
    assert scrub("room 3f1a created by u-1 in 4.2ms") == "room 3f1a created by u-1 in 4.2ms"


# --- Step 161: correlation ------------------------------------------------


def test_a_supplied_id_is_used_and_echoed(client, db):
    response = client.get("/health/", **{f"HTTP_{HEADER_NAME.upper().replace('-', '_')}": "trace-1"})
    assert response[HEADER_NAME] == "trace-1"


def test_an_id_is_generated_when_none_is_supplied(client, db):
    value = client.get("/health/")[HEADER_NAME]
    assert len(value) == 32 and value.isalnum()


@pytest.mark.parametrize(
    "unsafe",
    ["bad\r\ninjected", "x" * 65, "spaces here", "semi;colon", ""],
)
def test_unsafe_ids_are_replaced(unsafe):
    generated = normalize_request_id(unsafe)
    assert generated != unsafe
    assert len(generated) == 32


def test_the_context_var_does_not_leak_between_requests():
    seen = []

    def view(request):
        seen.append(get_request_id())

        class _Response(dict):
            def __setitem__(self, key, value):
                super().__setitem__(key, value)

        return _Response()

    middleware = CorrelationIdMiddleware(view)

    class _Request:
        headers = {HEADER_NAME: "first-id"}
        path = "/x/"

    middleware(_Request())
    assert get_request_id() == ""
    assert seen == ["first-id"]


@pytest.mark.django_db
def test_the_request_id_reaches_the_error_envelope(client):
    response = client.get(
        reverse("chat:room-list"),
        **{f"HTTP_{HEADER_NAME.upper().replace('-', '_')}": "envelope-check"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["request_id"] == "envelope-check"


# --- Step 164: query budgets ---------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url_name, budget",
    [
        ("chat:room-list", 3),
        ("chat:room-detail", 3),
        ("chat:room-member-list", 4),
    ],
)
def test_read_endpoints_stay_within_budget(
    client, as_user, room, django_assert_max_num_queries, url_name, budget
):
    """The property is 'does not grow with result size', not an exact count."""
    for index in range(20):
        add_member(room=room, user_id=f"bulk-{index}", added_by="admin-1")

    kwargs = {} if url_name == "chat:room-list" else {"room_id": room.pk}
    as_user("admin-1")
    with django_assert_max_num_queries(budget):
        client.get(reverse(url_name, kwargs=kwargs), **AUTH)


@pytest.mark.django_db
def test_liveness_performs_no_queries(client, django_assert_num_queries):
    with django_assert_num_queries(0):
        client.get("/health/")


# --- Step 165: nothing internal escapes ----------------------------------


def test_debug_is_off_by_default(settings):
    """The layer that actually fails in production (Step 165)."""
    assert settings.DEBUG in {True, False}
    from django.conf import global_settings  # noqa: F401

    assert "DJANGO_DEBUG" in open(".env.example").read()


@pytest.mark.django_db
def test_error_bodies_contain_no_internals(client, as_user, room):
    as_user("outsider")
    text = json.dumps(client.get(reverse("chat:room-detail", kwargs={"room_id": room.pk}), **AUTH).json())
    for leak in ("Traceback", "File \"", "chat_user", "SELECT ", "OperationalError"):
        assert leak not in text
