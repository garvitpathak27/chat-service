# Chat Service — Architecture Decisions

## ADR-001 — Service boundary (ACCEPTED)

| Owned by Chat | NOT owned | Actual owner |
|---|---|---|
| Room entity + metadata | User accounts, credentials, JWT issuance | Auth Service |
| Membership | Individual chat messages, read receipts | Chat Messages Service |
| Room/membership domain events | Live socket fan-out | Messages WebSocket Service |
| Room-level authorization decisions | Email/notification delivery | Mail Service |

### Consequences

- Chat Service has no `messages` table.
- Chat Service does not own user accounts.
- Chat stores `user_id` values from Auth Service as opaque strings.
- Other services must not directly query Chat's database.


## ADR-002 — Auth token validation contract (ACCEPTED)

Chat Service validates protected requests through Auth Service's
`ValidateToken` gRPC RPC.

The canonical protobuf contract is:

```protobuf
service AuthService {
  rpc ValidateToken (TokenRequest)
      returns (TokenValidationResponse);
}

message TokenRequest {
  string access_token = 1;
}

message TokenValidationResponse {
  bool active = 1;
  string user_id = 2;
  repeated string roles = 3;
  repeated string permissions = 4;
}


## ADR-003 — Canonical auth.proto location (ACCEPTED)

- Source of truth: `auth-service/proto/auth.proto`.
- Chat keeps a vendored copy at `chat-service/contracts/auth.proto`.
- Chat also stores `contracts/auth.proto.sha256`.
- The vendored copy is refreshed using `./scripts/sync-proto.sh`.
- `contracts/auth.proto` must not be hand-edited.
- Generated Python gRPC stubs are generated from the vendored copy.

## ADR-004 — Auth gRPC addressing (ACCEPTED)

Chat Service communicates with Auth Service over gRPC.

The Auth Service gRPC server listens on port `50051`.

| Environment | Host | Port |
|---|---|---|
| Local development | `localhost` | `50051` |
| Docker Compose | `auth-service` | `50051` |

Chat Service must read the Auth gRPC host and port from configuration
rather than hard-coding the address in application code.

Inside Docker Compose, `auth-service` is resolved through Docker's
internal DNS.

The Auth Service HTTP server remains separate on port `8000`.

## ADR-005 — Chat Service Eureka registration (ACCEPTED)

Chat Service registers itself with Eureka.

### Local development

EUREKA_SERVER_URL=http://localhost:8761/eureka/
EUREKA_APP_NAME=chat-service
EUREKA_INSTANCE_HOST=localhost
EUREKA_INSTANCE_PORT=8001

### Docker Compose

EUREKA_SERVER_URL=http://eureka-server:8761/eureka/
EUREKA_APP_NAME=chat-service
EUREKA_INSTANCE_HOST=chat-service
EUREKA_INSTANCE_PORT=8001

Chat Service registers with Eureka for service discovery.

Chat does not use Eureka discovery for the Chat → Auth gRPC connection
in v1. The Auth gRPC connection uses the configured Auth address directly.

Only the Chat HTTP service registers as a Eureka instance.
Non-HTTP processes such as migration and event-consumer processes must
not register as Chat instances.



## ADR-006 — RabbitMQ topology (ACCEPTED)

Chat Service uses RabbitMQ for asynchronous domain events.

| Variable | Local development | Docker Compose |
|---|---|---|
| `RABBITMQ_HOST` | `localhost` | `rabbitmq` |
| `RABBITMQ_PORT` | `5672` | `5672` |
| `RABBITMQ_VHOST` | `/` | `/` |
| `RABBITMQ_USER` | `guest` | dedicated `chat_service` user |
| `RABBITMQ_PASSWORD` | `guest` | from secret |
| `RABBITMQ_EXCHANGE` | `chat.events` | `chat.events` |

The `chat.events` exchange is:

- type `topic`
- durable
- auto-delete disabled

Chat declares the exchange but does not declare consumer queues.

Downstream consumers own their queues and bindings.

Routing keys use the convention:

`chat.events.<snake_case_event>`

Messages are published with:

- `delivery_mode=2`
- `content_type=application/json`

RabbitMQ carries domain events. Downstream services must not read
Chat Service's database directly.



## ADR-007 — Chat MySQL database (ACCEPTED)

Chat Service uses a dedicated MySQL database.

The Chat database is physically separate from the Auth Service database.

| Setting | Local development | Docker Compose |
|---|---|---|
| `DB_NAME` | `chat_service` | `chat_service` |
| `DB_USER` | `chat_service` | `chat_service` |
| `DB_PASSWORD` | from environment/secret | from secret |
| `DB_HOST` | `localhost` | `mysql` |
| `DB_PORT` | `3306` | `3306` |

The Chat application user has access only to the Chat database.

Chat Service must not connect directly to the Auth Service database.

## ADR-008 — Soft deletion semantics (ACCEPTED)

Chat Service uses soft deletion for rooms and memberships.

### Room

A room is considered active when:

`deleted_at IS NULL`

A room is considered deleted when:

`deleted_at IS NOT NULL`

Deleting a room sets `deleted_at` rather than physically deleting
the Room row.

### Membership

A membership is considered active when:

`left_at IS NULL`

A membership is considered inactive when:

`left_at IS NOT NULL`

Removing a member sets `left_at` rather than physically deleting
the Membership row.

Normal application queries operate on active rooms and active
memberships unless a specific operation requires historical data.

Historical rows are retained for auditability and domain consistency.


## ADR-009 — Membership roles (ACCEPTED)

Exactly two membership roles exist in v1:

- `admin`
- `member`

Roles are stored lowercase in `chat_membership.role` and will be
constrained by Django choices and a database CHECK constraint.

The room creator is inserted as `admin` in the same transaction as
room creation.

New members always default to `member`.

### Admin permissions

An `admin` may:

- update room metadata
- soft-delete the room
- add members
- remove members
- promote members
- demote members

### Member permissions

A `member` may:

- read the room
- read the member list
- leave the room

### Re-adding members

When an admin adds a user who is not currently an active member,
the membership is created or reactivated with:

- `role = member`
- `left_at = NULL`

A user's previous role does not carry over when they leave and are
later re-added.

For example, if a user was previously an `admin`, leaves the room,
and is later re-added, they return as a `member`.

The user must be explicitly promoted to `admin` again if admin
privileges are required.

`creator` is not a role. The creator is represented by
`Room.created_by`.

A creator who has left the room has no remaining privileges.

An active, non-deleted room must always have at least one active
`admin`.

An admin cannot leave if doing so would leave the room without an
active admin.

There is no `owner` role in v1.


## ADR-010 — Direct room uniqueness (ACCEPTED)

At most ONE direct room may exist per unordered pair of users.

Enforcement is performed by `chat_room.direct_key`, a nullable UNIQUE
column, rather than an application-level check-then-insert.

The direct key is generated as:

`":".join(sorted([str(user_a), str(user_b)]))`

For direct rooms:

- `direct_key` is always populated.
- The two user IDs are sorted before generating the key.
- A user may not create a direct room with themselves.

For group rooms:

- `direct_key` is always `NULL`.
- The nullable UNIQUE column allows unlimited group rooms.

If a direct room already exists for the requested pair,
`POST /api/rooms/` returns the existing room with `200 OK` rather than
creating another room, and no new room event is published.

A soft-deleted direct room retains its `direct_key`. Creating the same
direct room again reactivates the existing room instead of creating a
second room, preserving message-history continuity.



## ADR-011 — Domain events and event envelope (ACCEPTED)

Chat Service publishes the following domain events in v1:

- `ROOM_CREATED`
- `ROOM_UPDATED`
- `ROOM_DELETED`
- `MEMBER_ADDED`
- `MEMBER_REMOVED`

Events are published through the `chat.events` RabbitMQ topic exchange.

Routing keys use the format:

`chat.events.<snake_case_event>`

Every event uses the following envelope:

```json
{
  "event_id": "uuid",
  "type": "ROOM_CREATED",
  "version": 1,
  "timestamp": "ISO-8601 timestamp",
  "producer": "chat-service",
  "payload": {}
}

## ADR-012 — Event envelope (ACCEPTED)

Every Chat Service domain event uses the following envelope:

```json
{
  "event_id": "0f6d2a7e-3c4b-4a1e-9d55-2b0f9a1c77e2",
  "type": "ROOM_CREATED",
  "version": 1,
  "timestamp": "2026-08-10T09:31:44.512871Z",
  "producer": "chat-service",
  "payload": {}
}

## ADR-013 — Authentication failure mapping (ACCEPTED)

Chat Service fails closed when authentication cannot be established.

| Condition | HTTP | `error.code` | Notes |
|---|---:|---|---|
| No `Authorization` header | 401 | `AUTH_HEADER_MISSING` | Send `WWW-Authenticate: Bearer` |
| Header is not `Bearer <token>` | 401 | `AUTH_HEADER_MALFORMED` | |
| Empty token after `Bearer ` | 401 | `AUTH_HEADER_MALFORMED` | |
| Auth returns `active=false` | 401 | `TOKEN_INVALID` | Do not leak token details |
| Auth returns `UNAUTHENTICATED` | 401 | `TOKEN_INVALID` | Same response as invalid token |
| Auth returns `UNAVAILABLE` after retries | 503 | `AUTH_UNAVAILABLE` | Send `Retry-After: 5` |
| Auth returns `DEADLINE_EXCEEDED` after retries | 503 | `AUTH_UNAVAILABLE` | |
| Any other gRPC error | 502 | `AUTH_DEPENDENCY_ERROR` | Log the gRPC status |
| Auth returns `active=true` but empty `user_id` | 502 | `AUTH_DEPENDENCY_ERROR` | Protocol violation |

No authentication error may result in the request being processed.

401 indicates invalid client credentials and tells the client to
authenticate again.

503 indicates that the authentication dependency is unavailable and
the client may retry the same credentials later.

Expired, invalid, and otherwise rejected tokens are not distinguished
in the external response body. Detailed authentication failure reasons
may be logged internally.


Bad request credentials
        ↓
       401

Auth is down
        ↓
       503

Auth behaves unexpectedly
        ↓
       502

Never:
Auth failure
    ↓
"let's process it anyway"




## ADR-014 — Room authorization matrix (ACCEPTED)

"Member" means an active membership (`left_at IS NULL`) in a room
with `deleted_at IS NULL`.

| Operation | Non-member | Member | Admin | Creator who left |
|---|---|---|---|---|
| `POST /api/rooms/` | allowed (any authenticated user) | — | — | — |
| `GET /api/rooms/` | own rooms only | own rooms | own rooms | — |
| `GET /api/rooms/<id>/` | 404 | 200 | 200 | 404 |
| `PATCH /api/rooms/<id>/` | 404 | 403 | 200 | 404 |
| `DELETE /api/rooms/<id>/` | 404 | 403 | 204 | 404 |
| `GET /api/rooms/<id>/members/` | 404 | 200 | 200 | 404 |
| `POST /api/rooms/<id>/members/` | 404 | 403 | 201 | 404 |
| `DELETE .../members/<other>/` | 404 | 403 | 204 | 404 |
| `DELETE .../members/<self>/` | 404 | 204 | 204 unless last admin | 404 |

Non-members receive 404 rather than 403 to avoid revealing whether a
room exists.

Members who lack the required privilege receive 403.

The room creator is not a special authorization role. `created_by`
is always taken from the verified Auth token and never from the
request body.

Every membership authorization query must be scoped by both
`room_id` and `user_id`.

### Membership addition and reactivation

Adding a user to a group room always results in an active `member`.

If the user previously belonged to the room but has left, the existing
membership row is reactivated rather than creating a new row.

Reactivation sets:

- `left_at = NULL`
- `role = member`

The user's previous role is not restored automatically.

If the user should become an admin, an explicit promotion operation
must be performed after reactivation.

Direct rooms do not permit membership modification. Membership
addition/removal operations on a direct room return:

`400 DIRECT_ROOM_IMMUTABLE`

The last-admin invariant must be preserved: an admin cannot leave,
be removed, or be demoted if doing so would leave the active room
without an active admin.



## ADR-015 — Health model (ACCEPTED)

Chat Service exposes separate liveness and readiness endpoints.

| Endpoint | Checks | Auth | Consumed by |
|---|---|---|---|
| `GET /health/` | none; 200 if the process serves HTTP | none | Eureka, Docker HEALTHCHECK, LB |
| `GET /health/ready/` | MySQL `SELECT 1`, RabbitMQ connect, Auth gRPC channel state | none | humans, deploy gates |

`GET /health/` response:

```json
{
  "status": "UP",
  "service": "chat-service"
}


`GET /health/ready/` response:

```json
{
  "status": "DOWN",
  "checks": {
    "database": "UP",
    "rabbitmq": "DOWN",
    "auth_grpc": "UP"
  }
}
