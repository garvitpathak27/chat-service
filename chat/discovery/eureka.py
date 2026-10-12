"""Eureka registration for Chat Service.

Everything that knows Eureka exists lives here. The rest of the codebase does
not import py_eureka_client, so removing or replacing discovery is a
single-file change.

FAIL OPEN. A registration failure is logged and swallowed. Chat serves HTTP
perfectly well without being in a registry - consumers reach it by Docker
service name - so treating Eureka as a startup prerequisite would make it a
single point of failure for a system that does not otherwise need one. This
is the deliberate opposite of the Auth Service rule (Phase 3), where being
unable to verify identity means the request must not proceed.
"""

from __future__ import annotations

import atexit
import logging
import os 
import threading

from django.conf import settings

logger = logging.getLogger("chat.discovery")

RENEWAL_INTERVAL_SECONDS = 30
LEASE_DURATION_SECONDS = 90

_lock = threading.Lock()
_registered = False
_shutdown_hook_installed = False

def eureka_enabled() -> bool:
    """registeration is off when no server url is configured """
    return bool((settings.EUREKA_SERVER_URL or "").strip())

def base_url() -> str:
    return  f"http://{settings.EUREKA_INSTANCE_HOST}:{settings.EUREKA_INSTANCE_PORT}"

def instance_id() ->str:
    """Spring Cloud's conventional shape: host:app:port.

    Stable across restarts and identical in every Gunicorn worker, so N
    workers renew ONE lease rather than registering N instances (Step 154).
    """
    

    return (
        f"{settings.EUREKA_INSTANCE_HOST}"
        f":{settings.EUREKA_APP_NAME}"
        f":{settings.EUREKA_INSTANCE_PORT}"
    )
    
def register_with_eureka() -> bool:
    """register this instance . return true only on a fresh registration 
       idempotent and safe to call from several workers or twice in one process . 
    """
    
    global _registered
    if _registered:
        logger.debug("eureka already registered - skipping")
        return False
    if not eureka_enabled():
        logger.info("eureka registration disabled : EUREKA_SERVER_URL is empty")
        return False

    
    try:
        from py_eureka_client import eureka_client

        home_page_url = f"{base_url()}/"
        status_page_url = f"{base_url()}/health/"
        health_check_url = f"{base_url()}/health/"

        eureka_client.init(
            eureka_server=settings.EUREKA_SERVER_URL,
            app_name=settings.EUREKA_APP_NAME,
            instance_id=instance_id(),
            instance_host=settings.EUREKA_INSTANCE_HOST,
            instance_port=settings.EUREKA_INSTANCE_PORT,
            home_page_url=home_page_url,
            status_page_url=status_page_url,
            health_check_url=health_check_url,
            renewal_interval_in_secs=RENEWAL_INTERVAL_SECONDS,
            duration_in_secs=LEASE_DURATION_SECONDS,
            metadata={
                "service": settings.EUREKA_APP_NAME,
                "pid": str(os.getpid()),
            },
        )
    except Exception:
        logger.exception(
            "Eureka registration FAILED server=%s app=%s instance=%s "
            "- continuing without discovery",
            settings.EUREKA_SERVER_URL,
            settings.EUREKA_APP_NAME,
            instance_id(),
        )
        return False

    _registered = True
    _install_shutdown_hook()
    logger.info(
        "registered with eureka server=%s app=%s instance=%s health=%s",
        settings.EUREKA_SERVER_URL,
        settings.EUREKA_APP_NAME,
        instance_id(),
        health_check_url,
    )
    return True


def deregister_from_eureka() -> None:
    """best effort removal from the registery on shutdown """
    global _registered
    
    with _lock:
        if not _registered:
            return
        _registered = False
        
    try:
        from py_eureka_client import eureka_client
        eureka_client.stop()
        logger.info("deregistered from Eureka instance=%s", instance_id())
    except Exception:
        # Nothing useful to do while exiting; the lease expires on its own.
        logger.warning("Eureka deregistration failed", exc_info=True)
        
        
def _install_shutdown_hook() ->None:
    global _shutdown_hook_installed
    if _shutdown_hook_installed:
        return
    atexit.register(deregister_from_eureka)
    _shutdown_hook_installed = True
    
    


