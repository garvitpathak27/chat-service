"""Gunicorn configuration for Chat Service."""

import os

bind = f"0.0.0.0:{os.environ.get('SERVICE_PORT', '8001')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "3"))
worker_class = "sync"
threads = 1
timeout = 30
graceful_timeout = 30
keepalive = 5
preload_app = False
max_requests = 0
accesslog = None
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
