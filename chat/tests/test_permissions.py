"""Authorization tests for room-level permissions."""

from unittest import mock
from django.utils import timezone
import pytest
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from chat.authn import authentication
from chat.authn.authentication import ChatJWTAuthentication
from chat.grpc_clients.types import AuthIdentity
from chat.models import Membership, MembershipRole, Room, RoomType
from chat.permissions import (
    CanManageMembers,
    CanManageRoom,
    CanRemoveMembership,
    IsRoomMember,
    room_context,
)
import chat as chat_package
from chat.selector import is_admin, is_creator, is_member
import uuid
import pathlib
import re
from django.db import connection
from django.test.utils import CaptureQueriesContext

factory = APIRequestFactory()
pytestmark = pytest.mark.django_db
CHAT_DIR = pathlib.Path(chat_package.__file__).parent
factory = APIRequestFactory()


def make_view(permission_class, capture=None):
    class ProbeView(APIView):
        authentication_classes = [ChatJWTAuthentication]
        permission_classes = [IsAuthenticated, permission_class]

        def get(self, request, **kwargs):
            if capture is not None:
                capture["context"] = room_context(request)

            return Response({"ok": True})

        post = get
        delete = get

    return ProbeView.as_view()


@pytest.fixture
def as_user(monkeypatch):
    """Authenticate subsequent requests as the given user."""

    def _as(user_id, roles=("USER",)):
        client = mock.Mock()

        client.verify_token.return_value = AuthIdentity(
            user_id=user_id,
            roles=roles,
        )

        monkeypatch.setattr(
            authentication,
            "AuthServiceClient",
            lambda: client,
        )

    return _as


@pytest.fixture
def room():
    """A group room with an admin and a normal member."""

    room = Room.objects.create(
        type=RoomType.GROUP,
        name="Engineering",
        created_by="creator",
    )

    Membership.objects.create(
        room=room,
        user_id="creator",
        role=MembershipRole.ADMIN,
    )

    Membership.objects.create(
        room=room,
        user_id="member",
        role=MembershipRole.MEMBER,
    )

    return room


def hit(view, room_id, method="get", **view_kwargs):
    request = getattr(factory, method)(
        "/x/",
        HTTP_AUTHORIZATION="Bearer t.o.k",
    )

    response = view(
        request,
        room_id=str(room_id),
        **view_kwargs,
    )

    response.render()

    return response


@pytest.mark.parametrize(
    "actor, permission, expected",
    [
        ("stranger", IsRoomMember, 404),
        ("member", IsRoomMember, 200),
        ("creator", IsRoomMember, 200),
        ("stranger", CanManageRoom, 404),
        ("member", CanManageRoom, 403),
        ("creator", CanManageRoom, 200),
        ("stranger", CanManageMembers, 404),
        ("member", CanManageMembers, 403),
        ("creator", CanManageMembers, 200),
    ],
)
def test_adr_014_matrix(as_user, room, actor, permission, expected):
    as_user(actor)

    response = hit(
        make_view(permission),
        room.pk,
    )

    assert response.status_code == expected


def test_soft_deleted_room_is_404_even_for_its_admin(as_user, room):
    room.deleted_at = timezone.now()
    room.save(update_fields=["deleted_at"])

    as_user("creator")

    response = hit(
        make_view(IsRoomMember),
        room.pk,
    )

    assert response.status_code == 404


def test_unparseable_room_id_is_404_not_500(as_user):
    as_user("creator")

    response = hit(
        make_view(IsRoomMember),
        "not-a-uuid",
    )

    assert response.status_code == 404


def test_nonexistent_and_forbidden_rooms_are_indistinguishable(as_user, room):
    as_user("stranger")

    nonexistent = hit(
        make_view(IsRoomMember),
        uuid.uuid4(),
    )

    forbidden = hit(
        make_view(IsRoomMember),
        room.pk,
    )

    assert nonexistent.status_code == 404
    assert forbidden.status_code == 404
    assert nonexistent.data == forbidden.data


def test_departed_member_is_404(as_user, room):
    membership = Membership.objects.get(
        room=room,
        user_id="member",
    )

    membership.left_at = timezone.now()
    membership.save(update_fields=["left_at"])

    as_user("member")

    response = hit(
        make_view(IsRoomMember),
        room.pk,
    )

    assert response.status_code == 404


def test_demoted_creator_can_manage_room_but_not_members(
    as_user,
    room,
):
    membership = Membership.objects.get(
        room=room,
        user_id="creator",
    )

    membership.role = MembershipRole.MEMBER
    membership.save(update_fields=["role"])

    as_user("creator")

    room_response = hit(
        make_view(CanManageRoom),
        room.pk,
    )

    members_response = hit(
        make_view(CanManageMembers),
        room.pk,
    )

    assert room_response.status_code == 200
    assert members_response.status_code == 403


def test_departed_creator_cannot_manage_room(as_user, room):
    membership = Membership.objects.get(
        room=room,
        user_id="creator",
    )

    membership.left_at = timezone.now()
    membership.save(update_fields=["left_at"])

    as_user("creator")

    response = hit(
        make_view(CanManageRoom),
        room.pk,
    )

    assert response.status_code == 404


def test_member_may_remove_themselves(as_user, room):
    as_user("member")

    response = hit(
        make_view(CanRemoveMembership),
        room.pk,
        method="delete",
        user_id="member",
    )

    assert response.status_code == 200


def test_member_may_not_remove_someone_else(as_user, room):
    as_user("member")

    response = hit(
        make_view(CanRemoveMembership),
        room.pk,
        method="delete",
        user_id="creator",
    )

    assert response.status_code == 403


def test_admin_may_remove_someone_else(as_user, room):
    as_user("creator")

    response = hit(
        make_view(CanRemoveMembership),
        room.pk,
        method="delete",
        user_id="member",
    )

    assert response.status_code == 200


def test_stranger_removing_anyone_is_404(as_user, room):
    as_user("stranger")

    response = hit(
        make_view(CanRemoveMembership),
        room.pk,
        method="delete",
        user_id="member",
    )

    assert response.status_code == 404


def test_selector_predicates(room):
    assert is_member(room.pk, "member") is True
    assert is_member(room.pk, "stranger") is False

    assert is_admin(room.pk, "creator") is True
    assert is_admin(room.pk, "member") is False

    assert is_creator(room, "creator") is True
    assert is_creator(room, "member") is False


def test_permission_resolves_the_room_context_once(as_user, room):
    as_user("creator")

    view = make_view(CanManageRoom)

    request = factory.get(
        "/x/",
        HTTP_AUTHORIZATION="Bearer t.o.k",
    )

    with CaptureQueriesContext(connection) as ctx:
        view(request, room_id=str(room.pk)).render()

    # Exactly two: one room query + one membership query.
    assert len(ctx.captured_queries) == 2, [q["sql"] for q in ctx.captured_queries]


def test_view_can_reuse_the_resolved_context(as_user, room):
    as_user("member")

    capture = {}

    hit(
        make_view(IsRoomMember, capture=capture),
        room.pk,
    )

    resolved_room, resolved_membership = capture["context"]

    assert resolved_room.pk == room.pk
    assert resolved_membership.role == MembershipRole.MEMBER


def _source_files():
    for path in CHAT_DIR.rglob("*.py"):
        if "tests" in path.parts or "generated" in path.parts:
            continue

        yield path


def test_no_module_reads_ownership_fields_from_the_request_body():
    """Step 89: clients must not choose room ownership."""
    banned = re.compile(
        r"""(request|self\.request)\.data\[?\.?get?\(?['"](created_by|user_id)['"]"""
    )

    offenders = [
        str(path) for path in _source_files() if banned.search(path.read_text())
    ]

    assert not offenders, f"ownership read from request body in: {offenders}"


def test_memberships_are_never_addressed_by_primary_key():
    """Step 90: membership operations must remain room-scoped."""
    banned = re.compile(r"Membership\.(objects|all_objects)\.(get|filter)\(\s*(pk|id)=")

    offenders = [
        str(path) for path in _source_files() if banned.search(path.read_text())
    ]

    assert not offenders, f"membership addressed by pk in: {offenders}"


def test_room_permissions_never_consult_auth_service_roles():
    """Step 79: global Auth roles must not grant room privileges."""
    source = (CHAT_DIR / "permissions.py").read_text()

    assert "has_role" not in source
    assert ".roles" not in source

SERVER_CONTROLLED_FIELDS = frozenset(
    {
        "id",
        "created_by",
        "created_at",
        "updated_at",
        "deleted_at",
        "joined_at",
        "left_at",
    }
)