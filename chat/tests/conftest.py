"""Test-wide defaults.


Without this, every test that creates a room would try to open an AMQP
connection, because settings.CHAT_EVENT_TRANSPORT now points at the real
publisher. The suite must run with no broker (Phase 3's rule, extended).
"""

import pytest


@pytest.fixture(autouse=True)
def _no_broker_during_tests(settings):
    settings.CHAT_EVENT_TRANSPORT = "chat.events.log_only_transport"

