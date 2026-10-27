"""Gunicorn configuration for Chat Service."""

import os

bind = f"0.0.0.0:{os.environ.get('SERVICE_PORT', '8001')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "3"))
worker_class = "sync"
threads = 1
timeout = 30
graceful_timeout = 30
keepalive = 30
preload_app = False
max_requests = 0
accesslog = "-"
# %(h)s is the peer address, which behind the gateway is always the gateway
# itself. xff is what the gateway forwarded, and is the only way to see a real
# client address. Keep both: when they disagree, the forwarding chain is wrong.
access_log_format = '%(h)s xff=%({x-forwarded-for}i)s "%(r)s" %(s)s %(b)s %(D)sus'
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
