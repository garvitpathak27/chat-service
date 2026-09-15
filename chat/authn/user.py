from __future__ import annotations

from chat.grpc_clients.auth_client import AuthIdentity


class RemoteUser:
    """A verified caller, adapted to the small part of Django's user contract
    that DRF actually reads.
    """

    __slots__ = ("identity",)

    is_authenticated = True
    is_anonymous = False
    is_active = True

    is_staff = False
    is_superuser = False

    def __init__(self, identity: AuthIdentity):
        self.identity = identity

    @property
    def user_id(self) -> str:
        return self.identity.user_id

    @property
    def roles(self) -> tuple[str, ...]:
        return self.identity.roles

    @property
    def pk(self) -> str:
        return self.identity.user_id

    @property
    def id(self) -> str:
        return self.identity.user_id

    def get_username(self) -> str:
        return self.identity.user_id

    def has_role(self, role: str) -> bool:
        return self.identity.has_role(role)

    def save(self, *args, **kwargs):
        raise NotImplementedError(
            "RemoteUser is not persistable: Auth Service owns users."
        )

    def delete(self, *args, **kwargs):
        raise NotImplementedError(
            "RemoteUser is not persistable: Auth Service owns users."
        )

    def __str__(self) -> str:
        return f"RemoteUser({self.identity.user_id})"

    __repr__ = __str__

    def __eq__(self, other) -> bool:
        return isinstance(other, RemoteUser) and other.identity == self.identity

    def __hash__(self) -> int:
        return hash(("RemoteUser", self.identity.user_id))


def identity_of(request) -> AuthIdentity | None:
    """Return the verified AuthIdentity for this request, or None."""
    return getattr(request, "auth", None)


    """
poetry run python -c "
from chat.grpc_clients.types import AuthIdentity
from chat.authn.user import RemoteUser

u = RemoteUser(
    AuthIdentity(
        user_id='u-42',
        roles=('USER',)
    )
)

print(u, u.is_authenticated, u.pk, type(u.pk).__name__, u.has_role('USER'))

try:
    u.save()
except NotImplementedError as e:
    print('not persistable:', e)
"
    """