
CHAT="${CHAT:-http://localhost:8001}"
AUTH="${AUTH:-http://localhost:8080}"
RMQ="${RMQ:-http://localhost:15672}"
RMQ_AUTH="${RABBITMQ_USER:-guest}:${RABBITMQ_PASSWORD:-guest}"
DEBUG_QUEUE="${DEBUG_QUEUE:-chat.debug}"

FAILURES=0
STEP="init"
STATUS=""

expect() {
    if [ "$2" = "$3" ]; then
        printf '  PASS  %-45s %s\n' "$1" "$3"
    else
        printf '  FAIL  %-45s expected %s, got %s\n' "$1" "$2" "$3"
        FAILURES=$((FAILURES + 1))
    fi
}

summary() {
    echo
    if [ "$FAILURES" -eq 0 ]; then
        echo "ALL CHECKS PASSED"
    else
        echo "$FAILURES CHECK(S) FAILED"
    fi
    return "$FAILURES"
}

api() {
    local method="$1"
    local path="$2"
    local token="${3:-}"
    local payload="${4:-}"

    local args=(
        -sS
        -o /tmp/e2e-body.json
        -w '%{http_code}'
        -X "$method"
        "$CHAT$path"
        -H "X-Request-ID: e2e-$STEP"
    )

    if [ -n "$token" ]; then
        args+=(-H "Authorization: Bearer $token")
    fi

    if [ -n "$payload" ]; then
        args+=(-H 'Content-Type: application/json' -d "$payload")
    fi

    STATUS="$(curl "${args[@]}")"
}

body() {
    cat /tmp/e2e-body.json
}

jget() {
    python3 -c '
import sys
import json

v = json.load(sys.stdin)

for k in sys.argv[1].split("."):
    v = v[int(k)] if k.isdigit() else v[k]

print(v)
' "$1"
}

auth_seed_user() {
    docker compose -f ../auth-service/docker-compose.yml \
        exec -T auth-service python manage.py shell -c \
        "from core.models import User; u, _ = User.objects.get_or_create(email='$1'); u.set_password('$2'); u.is_active = True; u.save(); print(u.id)"
}

auth_login() {
    curl -sS -X POST "$AUTH/api/v1/auth/login/" \
        -H 'Content-Type: application/json' \
        -d "{\"email\":\"$1\",\"password\":\"$2\"}" |
        jget access_token
}

auth_whoami() {
    curl -sS "$AUTH/api/v1/auth/permissions/" \
        -H "Authorization: Bearer $1" |
        jget user_id
}

events_setup() {
    curl -sS -u "$RMQ_AUTH" -X PUT \
        "$RMQ/api/queues/%2F/$DEBUG_QUEUE" \
        -H 'content-type: application/json' \
        -d '{"durable":true,"arguments":{"x-message-ttl":3600000}}' \
        > /dev/null

    curl -sS -u "$RMQ_AUTH" -X POST \
        "$RMQ/api/bindings/%2F/e/chat.events/q/$DEBUG_QUEUE" \
        -H 'content-type: application/json' \
        -d '{"routing_key":"chat.events.#"}' \
        > /dev/null
}

events_teardown() {
    curl -sS -u "$RMQ_AUTH" -X DELETE \
        "$RMQ/api/queues/%2F/$DEBUG_QUEUE" \
        > /dev/null
}

drain_events() {
    curl -sS -u "$RMQ_AUTH" -X POST \
        "$RMQ/api/queues/%2F/$DEBUG_QUEUE/get" \
        -H 'content-type: application/json' \
        -d '{"count":50,"ackmode":"ack_requeue_false","encoding":"auto"}' |
        python3 -c '
import sys
import json

for m in json.load(sys.stdin):
    p = m.get("properties") or {}
    print(json.dumps({
        "routing_key": m["routing_key"],
        "correlation_id": p.get("correlation_id"),
        "message_id": p.get("message_id"),
        "app_id": p.get("app_id"),
        "envelope": json.loads(m["payload"])
    }))
'
}
