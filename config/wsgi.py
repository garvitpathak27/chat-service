
"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.0/howto/deployment/wsgi/

Registration lives HERE rather than in AppConfig.ready() because this module
is imported exactly when a process is about to serve HTTP, and by nothing
else: manage.py migrate/makemigrations/shell/collectstatic and the test suite
never import it, so none of them register a short-lived process as a live
instance (Phase 10, Step 154).
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_wsgi_application()

# Imported after get_wsgi_application() so the app registry is ready.
from chat.discovery.eureka import register_with_eureka  # noqa: E402

register_with_eureka()