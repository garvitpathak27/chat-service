""" Application level types returned by auth service client """

from dataclasses import dataclass

@dataclass(frozen=True)
class AuthIdentity:
    """Identity establieshed by the auth service """
    user_id: str
    roles: tuple[str, ...]

    
