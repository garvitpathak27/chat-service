"""Chat Service REST routes.

Mounted at /api/ by config/urls.py (Step 102). Route names are stable and are
used by reverse() in tests - renaming one breaks tests loudly, which is the
intent.
"""

from django.urls import path

from chat import views

app_name = "chat"

urlpatterns = [
    # Step 99 - the room collection
    path("rooms/", views.RoomListCreateView.as_view(), name="room-list"),
    # Step 100 - a single room
    path("rooms/<uuid:room_id>/", views.RoomDetailView.as_view(), name="room-detail"),
    # Step 101 - memberships, nested under their room. Addressed as
    # (room_id, user_id), never by membership pk (Step 90).
    path(
        "rooms/<uuid:room_id>/members/",
        views.MemberListCreateView.as_view(),
        name="room-member-list",
    ),
    path(
        "rooms/<uuid:room_id>/members/<str:user_id>/",
        views.MemberDetailView.as_view(),
        name="room-member-detail",
    ),
]
