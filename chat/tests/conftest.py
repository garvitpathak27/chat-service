"""Test-wide defaults.


Without this, every test that creates a room would try to open an AMQP
connection, because settings.CHAT_EVENT_TRANSPORT now points at the real
publisher. The suite must run with no broker (Phase 3's rule, extended).
"""

import pytest
import logging



@pytest.fixture(autouse=True)
def _no_broker_during_tests(settings):
    settings.CHAT_EVENT_TRANSPORT = "chat.events.log_only_transport"


@pytest.fixture(autouse=True)
def _propagate_chat_logs():
    """caplog hooks into the root logger. chat has propagate=False in LOGGING
    config (to avoid duplicate output in production), so pytest never sees
    chat.* records without this. Temporarily re-enable propagation for every
    test and restore it after."""
    logger = logging.getLogger("chat")
    original = logger.propagate
    logger.propagate = True
    yield
    logger.propagate = original
