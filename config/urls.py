from django.contrib import admin
from django.urls import include, path

# JSON, not HTML, for URLs that match no route and for unhandled 500s.
# Without these, a client parsing JSON gets a parse error instead of a 404,
# which is a genuinely confusing failure to debug from the frontend side.
# (Django only uses these when DEBUG=False.)
handler404 = "chat.api.errors.not_found_handler"
handler500 = "chat.api.errors.server_error_handler"

urlpatterns = [
    path("admin/", admin.site.urls),
    # ADR-016: /api/ is v1, unversioned. A future v2 is an additional mount
    # (path("api/v2/", include("chat.api.v2.urls"))), never a change to this one.
    path("api/", include("chat.api.urls")),
    # /health/ is added at Step 157, deliberately outside /api/ so that
    # Eureka and Docker probe a path with no versioning or auth attached.
]
