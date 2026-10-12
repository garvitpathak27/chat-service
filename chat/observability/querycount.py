from __future__ import annotations

import logging
import time

from django.conf import settings
from django.db import connection

logger = logging.getLogger("chat.observability")


class QueryCountMiddleware:
    """LAST in MIDDLEWARE: innermost, so it measures the view, not the stack."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = getattr(settings, "CHAT_LOG_QUERY_COUNTS", False)
        self.threshold = getattr(settings, "CHAT_QUERY_COUNT_WARN_THRESHOLD", 10)

    def __call__(self, request):
        if not self.enabled:
            return self.get_response(request)

        previous = connection.force_debug_cursor
        connection.force_debug_cursor = True
        start_index = len(connection.queries)
        started = time.perf_counter()

        try:
            response = self.get_response(request)
        finally:
            connection.force_debug_cursor = previous

        queries = connection.queries[start_index:]
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        count = len(queries)

        level = logging.WARNING if count > self.threshold else logging.DEBUG
        logger.log(
            level,
            "request query count",
            extra={
                "operation": "request_queries",
                "outcome": "over_budget" if count > self.threshold else "ok",
                "path": request.path,
                "method": request.method,
                "status": getattr(response, "status_code", None),
                "query_count": count,
                "query_duration_ms": round(
                    sum(float(query["time"]) for query in queries) * 1000, 1
                ),
                "duration_ms": duration_ms,
                "threshold": self.threshold,
            },
        )
        return response

