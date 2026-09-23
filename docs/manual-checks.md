# Manual checks

Snippets that used to live as string literals inside the source files.
They were moved here unchanged, so the code stays clean and the notes are kept.
Some snippets may need small updates: `AuthServiceClient` now takes keyword-only
arguments (`AuthServiceClient(stub=...)`), and the fake stubs need `timeout=None`.

## From `chat/grpc_clients/auth_client.py`

HOW TO CHECK WITHOUT RUNNING THE AUTH SERVICE

```bash
poetry run python -c "
import grpc

from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.generated import auth_pb2


class FakeStub:
    def ValidateToken(self, request):
        print('token received:', request.access_token)

        return auth_pb2.TokenValidationResponse(
            active=True,
            user_id='123',
            roles=['member', 'moderator'],
            permissions=['messages.send'],
        )


channel = grpc.insecure_channel('127.0.0.1:1')
client = AuthServiceClient(channel)

client._stub = FakeStub()

identity = client.verify_token('fake.jwt.token')

print('identity:', identity)
print('user_id:', identity.user_id)
print('roles:', identity.roles)

channel.close()
"

poetry run python -c "
import grpc

from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.exceptions import InvalidTokenError
from chat.grpc_clients.generated import auth_pb2


class FakeStub:
    def ValidateToken(self, request):
        return auth_pb2.TokenValidationResponse(active=False)


channel = grpc.insecure_channel('127.0.0.1:1')
client = AuthServiceClient(channel)

client._stub = FakeStub()

try:
    client.verify_token('bad.jwt.token')
except InvalidTokenError as e:
    print('caught:', type(e).__name__)
    print('message:', str(e))
else:
    print('ERROR: InvalidTokenError was not raised')

channel.close()
"
```

how to test the bounded retry backof funciton lity

```bash
poetry run python manage.py shell -c "
import grpc
from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.generated import auth_pb2

class FakeRpcError(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

class FakeStub:
    def __init__(self):
        self.calls = 0

    def ValidateToken(self, request, timeout=None):
        self.calls += 1
        print('call:', self.calls)

        if self.calls < 3:
            raise FakeRpcError()

        return auth_pb2.TokenValidationResponse(
            active=True,
            user_id='123',
            roles=['member'],
        )

fake_stub = FakeStub()

client = AuthServiceClient.__new__(AuthServiceClient)
client._stub = fake_stub
client._timeout = 2.0
client._max_retries = 2
client._target = 'fake:50051'

identity = client.verify_token('fake.jwt.token')

print('identity:', identity)
print('total calls:', fake_stub.calls)
"


poetry run python manage.py shell -c "
import grpc
from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.generated import auth_pb2

class FakeRpcError(grpc.RpcError):
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code

class FakeStub:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def ValidateToken(self, request, timeout=None):
        self.calls += 1

        if self.error:
            raise self.error

        return self.response


# Test 1: Auth explicitly says active=False
stub1 = FakeStub(
    response=auth_pb2.TokenValidationResponse(active=False)
)

client1 = AuthServiceClient.__new__(AuthServiceClient)
client1._stub = stub1
client1._timeout = 2.0
client1._max_retries = 2
client1._target = 'fake:50051'

try:
    client1.verify_token('bad-token')
except Exception as exc:
    print('active=False:')
    print('  exception:', type(exc).__name__)
    print('  grpc_code:', exc.grpc_code)
    print('  calls:', stub1.calls)


# Test 2: Auth returns UNAUTHENTICATED
stub2 = FakeStub(
    error=FakeRpcError(grpc.StatusCode.UNAUTHENTICATED)
)

client2 = AuthServiceClient.__new__(AuthServiceClient)
client2._stub = stub2
client2._timeout = 2.0
client2._max_retries = 2
client2._target = 'fake:50051'

try:
    client2.verify_token('bad-token')
except Exception as exc:
    print('UNAUTHENTICATED:')
    print('  exception:', type(exc).__name__)
    print('  grpc_code:', exc.grpc_code)
    print('  calls:', stub2.calls)
"



poetry run python manage.py shell -c "
import grpc
from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.generated import auth_pb2

class FakeRpcError(grpc.RpcError):
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code

class FakeStub:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def ValidateToken(self, request, timeout=None):
        self.calls += 1
        raise self.error

for code in [
    grpc.StatusCode.DEADLINE_EXCEEDED,
    grpc.StatusCode.RESOURCE_EXHAUSTED,
    grpc.StatusCode.UNAVAILABLE,
]:
    stub = FakeStub(FakeRpcError(code))

    client = AuthServiceClient.__new__(AuthServiceClient)
    client._stub = stub
    client._timeout = 2.0
    client._max_retries = 2
    client._target = 'fake:50051'

    try:
        client.verify_token('test-token')
    except Exception as exc:
        print(f'{code.name}:')
        print(f'  exception: {type(exc).__name__}')
        print(f'  grpc_code: {exc.grpc_code}')
        print(f'  calls: {stub.calls}')
"
```

## From `chat/grpc_clients/types.py`

TESTING FILES AND CODES

```bash
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
```

## From `chat/authn/user.py`

```bash
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
```
