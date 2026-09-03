""" Application level types returned by auth service client """

from dataclasses import dataclass

@dataclass(frozen=True)
class AuthIdentity:
    """Identity establieshed by the auth service """
    user_id: str
    roles: tuple[str, ...]

    
"""
TESTING FILES AND CODES 


poetry run python manage.py shell -c "
from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.generated import auth_pb2

class FakeStub:
    def ValidateToken(self, request, timeout=None):
        return auth_pb2.TokenValidationResponse(
            active=True,
            user_id='123',
            roles=['admin', 'member'],
            permissions=['chat.read', 'chat.write'],
        )

client = AuthServiceClient.__new__(AuthServiceClient)
client._stub = FakeStub()
client._timeout = 2.0
client._max_retries = 2
client._target = 'fake:50051'

identity = client.verify_token('test-token')

print('type:', type(identity).__name__)
print('user_id:', identity.user_id)
print('roles:', identity.roles)
print('roles_type:', type(identity.roles).__name__)
print('has_permissions:', hasattr(identity, 'permissions'))
"
"""