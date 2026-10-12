"""Liveness endpoint: Step 152, ADR-015."""

import pytest


def test_health_returns_up(client):
    response = client.get("/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "UP", "service": "chat-service"}


def test_health_requires_no_credentials(client):
    """An authenticated probe is not a probe: it fails for unrelated reasons."""
    assert client.get("/health/").status_code == 200
    assert "WWW-Authenticate" not in client.get("/health/")


@pytest.mark.django_db
def test_health_performs_no_database_queries(client, django_assert_num_queries):
    """The rule that keeps a slow database from deregistering a healthy process."""
    with django_assert_num_queries(0):
        client.get("/health/")


def test_health_is_json_not_html(client):
    assert client.get("/health/")["Content-Type"].startswith("application/json")


def test_health_body_leaks_nothing(client):
    body = client.get("/health/").json()
    assert set(body) == {"status", "service"}


def test_health_rejects_post(client):
    assert client.post("/health/").status_code == 405
