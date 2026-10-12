"""Liveness endpoint. ADR-015.

A PLAIN DJANGO VIEW, not a DRF APIView, on purpose:

  - DRF's DEFAULT_AUTHENTICATION_CLASSES would run ChatJWTAuthentication on
    every probe, which means a gRPC call to Auth Service every 30 seconds per
    consumer - and worse, Eureka's probe would fail with 401 whenever Auth was
    down, so an Auth outage would deregister Chat from discovery;
  - DEFAULT_PERMISSION_CLASSES is IsAuthenticated (Phase 4 Step 75), so the
    endpoint would need an explicit AllowAny opt-out that someone could later
    remove without noticing.

Using a plain view sidesteps both. It also guarantees the second rule below.

NO I/O. This handler must not touch MySQL, RabbitMQ or Auth. If it did, a slow
database would make Eureka evict a process that is serving requests fine, and
Chat would vanish from discovery at exactly the moment it was under load.
Dependency checks belong in /health/ready/ (Step 158).
"""


import logging
import time

from django.conf import settings
from django.db import connection as db_connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET
logger = logging.getLogger("chat.health")


UP = "UP"
DOWN = "DOWN"

@require_GET
def liveness(request):
    """200 with a fixed body if this process can serve HTTP. Nothing else."""
    return JsonResponse({"status": "UP", "service": settings.SERVICE_NAME})

def _check_database() -> str:
    with db_connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return UP


def _check_rabbitmq() -> str:
    from chat.messaging.connection import get_channel

    channel = get_channel()
    return UP if channel.is_open else DOWN


def _check_auth_grpc() -> str:
    from chat.grpc_clients.auth_client import channel_state

    state = channel_state()
    return DOWN if state in {"TRANSIENT_FAILURE", "SHUTDOWN"} else UP


CHECKS = {
    "database": _check_database,
    "rabbitmq": _check_rabbitmq,
    "auth_grpc": _check_auth_grpc,
}


@require_GET
def readiness(request):
    """Per-dependency status. 200 when all UP, 503 when any is DOWN (ADR-015)."""
    checks = {}

    for name, check in CHECKS.items():
        started = time.perf_counter()
        try:
            checks[name] = check()
        except Exception as exc:
            checks[name] = DOWN
            logger.warning(
                "readiness check failed",
                extra={
                    "operation": "readiness_check",
                    "check": name,
                    "outcome": "down",
                    "error_type": type(exc).__name__,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
                exc_info=True,
            )
        else:
            logger.debug(
                "readiness check ok",
                extra={
                    "operation": "readiness_check",
                    "check": name,
                    "outcome": "up",
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )

    overall = UP if all(status == UP for status in checks.values()) else DOWN
    return JsonResponse(
        {"status": overall, "checks": checks},
        status=200 if overall == UP else 503,
    )
