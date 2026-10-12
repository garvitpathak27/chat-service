"""Eureka registration: Steps 148-154.

No Eureka server: py_eureka_client is replaced with a fake. These tests must
pass with the whole stack stopped.
"""

import sys
import types

import pytest

from chat.discovery import eureka


class FakeEurekaClient:
    def __init__(self):
        self.init_calls = []
        self.stop_calls = 0
        self.init_error = None

    def init(self, **kwargs):
        if self.init_error is not None:
            raise self.init_error
        self.init_calls.append(kwargs)

    def stop(self):
        self.stop_calls += 1


@pytest.fixture
def fake_eureka(monkeypatch):
    """Stand in for `from py_eureka_client import eureka_client`."""
    client = FakeEurekaClient()
    package = types.ModuleType("py_eureka_client")
    package.eureka_client = client
    monkeypatch.setitem(sys.modules, "py_eureka_client", package)
    monkeypatch.setitem(sys.modules, "py_eureka_client.eureka_client", client)
    return client


@pytest.fixture(autouse=True)
def _reset_registration_state(monkeypatch):
    monkeypatch.setattr(eureka, "_registered", False)
    monkeypatch.setattr(eureka, "_shutdown_hook_installed", True)  # skip atexit
    yield
    monkeypatch.setattr(eureka, "_registered", False)


# --- Step 148: the derived on/off switch ----------------------------------


def test_registration_is_skipped_when_no_server_is_configured(settings, fake_eureka):
    settings.EUREKA_SERVER_URL = ""
    assert eureka.register_with_eureka() is False
    assert fake_eureka.init_calls == []


def test_whitespace_only_server_url_also_disables(settings, fake_eureka):
    settings.EUREKA_SERVER_URL = "   "
    assert eureka.eureka_enabled() is False
    assert eureka.register_with_eureka() is False


# --- Steps 150-152: what gets registered -----------------------------------


def test_registration_passes_the_expected_identity(settings, fake_eureka):
    settings.EUREKA_SERVER_URL = "http://eureka-server:8761/eureka/"
    settings.EUREKA_APP_NAME = "chat-service"
    settings.EUREKA_INSTANCE_HOST = "chat-service"
    settings.EUREKA_INSTANCE_PORT = 8001

    assert eureka.register_with_eureka() is True

    kwargs = fake_eureka.init_calls[0]
    assert kwargs["eureka_server"] == "http://eureka-server:8761/eureka/"
    assert kwargs["app_name"] == "chat-service"
    assert kwargs["instance_host"] == "chat-service"
    assert kwargs["instance_port"] == 8001
    assert kwargs["instance_id"] == "chat-service:chat-service:8001"
    assert kwargs["health_check_url"] == "http://chat-service:8001/health/"
    assert kwargs["status_page_url"] == "http://chat-service:8001/health/"
    assert kwargs["home_page_url"] == "http://chat-service:8001/"


def test_instance_id_is_deterministic(settings):
    """So N Gunicorn workers renew ONE lease rather than registering N."""
    settings.EUREKA_INSTANCE_HOST = "chat-service"
    settings.EUREKA_APP_NAME = "chat-service"
    settings.EUREKA_INSTANCE_PORT = 8001
    assert eureka.instance_id() == eureka.instance_id() == "chat-service:chat-service:8001"


def test_lease_settings_match_springs_defaults(settings, fake_eureka):
    settings.EUREKA_SERVER_URL = "http://eureka-server:8761/eureka/"
    eureka.register_with_eureka()
    kwargs = fake_eureka.init_calls[0]
    assert kwargs["renewal_interval_in_secs"] == 30
    assert kwargs["duration_in_secs"] == 90
    # three intervals per lease: two consecutive misses are tolerated
    assert kwargs["duration_in_secs"] == 3 * kwargs["renewal_interval_in_secs"]


# --- Step 149: idempotence and fail-open -----------------------------------


def test_registration_is_idempotent(settings, fake_eureka):
    settings.EUREKA_SERVER_URL = "http://eureka-server:8761/eureka/"
    assert eureka.register_with_eureka() is True
    assert eureka.register_with_eureka() is False
    assert eureka.register_with_eureka() is False
    assert len(fake_eureka.init_calls) == 1


def test_registration_failure_does_not_raise(settings, fake_eureka, caplog):
    """FAIL OPEN: Eureka being down must not stop Chat from serving."""
    settings.EUREKA_SERVER_URL = "http://eureka-server:8761/eureka/"
    fake_eureka.init_error = ConnectionRefusedError("no eureka here")

    with caplog.at_level("ERROR", logger="chat.discovery"):
        assert eureka.register_with_eureka() is False

    assert "Eureka registration FAILED" in caplog.text
    assert eureka._registered is False    # a retry later is still possible


def test_deregistration_calls_stop_once(settings, fake_eureka):
    settings.EUREKA_SERVER_URL = "http://eureka-server:8761/eureka/"
    eureka.register_with_eureka()

    eureka.deregister_from_eureka()
    eureka.deregister_from_eureka()
    assert fake_eureka.stop_calls == 1


def test_deregistration_without_registration_is_a_no_op(fake_eureka):
    eureka.deregister_from_eureka()
    assert fake_eureka.stop_calls == 0


def test_deregistration_swallows_errors(settings, fake_eureka, monkeypatch, caplog):
    settings.EUREKA_SERVER_URL = "http://eureka-server:8761/eureka/"
    eureka.register_with_eureka()

    def exploding_stop():
        raise RuntimeError("interpreter shutting down")

    monkeypatch.setattr(fake_eureka, "stop", exploding_stop)
    with caplog.at_level("WARNING", logger="chat.discovery"):
        eureka.deregister_from_eureka()      # must not raise while exiting
    assert "deregistration failed" in caplog.text


# --- Step 149: isolation ---------------------------------------------------


def test_only_the_discovery_package_imports_py_eureka_client():
    import pathlib

    import chat as chat_package

    root = pathlib.Path(chat_package.__file__).parent
    offenders = [
        str(path)
        for path in root.rglob("*.py")
        if "tests" not in path.parts
        and "discovery" not in path.parts
        and "py_eureka_client" in path.read_text()
    ]
    assert not offenders, f"discovery leaked into: {offenders}"
