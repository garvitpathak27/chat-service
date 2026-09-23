"""Application-level types returned by the Auth Service client."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AuthIdentity:
    """Identity established by the Auth Service."""

    user_id: str
    roles: tuple[str, ...]

    def has_role(self, role: str) -> bool:
        return role in self.roles
