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

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def liveness(request):
    """200 with a fixed body if this process can serve HTTP. Nothing else."""
    return JsonResponse({"status": "UP", "service": settings.SERVICE_NAME})
