#!/usr/bin/env python3

"""Manual end-to-end check of Chat -> Auth Service gRPC."""

from __future__ import annotations

import argparse
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from chat.grpc_clients.auth_client import AuthServiceClient
from chat.grpc_clients.exceptions import (
    AuthProtocolError,
    AuthUnavailableError,
    InvalidTokenError,
)


EXIT_VERIFIED = 0
EXIT_REJECTED = 1
EXIT_UNAVAILABLE = 2
EXIT_PROTOCOL = 3


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a JWT through Auth Service gRPC."
    )
    parser.add_argument(
        "token",
        nargs="?",
        default=os.environ.get("AUTH_JWT", ""),
        help="Raw JWT, without the Bearer prefix. Or set AUTH_JWT.",
    )

    args = parser.parse_args()

    if not args.token:
        print(
            "No token supplied. Pass it as an argument or set AUTH_JWT.",
            file=sys.stderr,
        )
        return EXIT_REJECTED

    started = time.monotonic()

    try:
        identity = AuthServiceClient().verify_token(args.token)

    except InvalidTokenError:
        elapsed = time.monotonic() - started
        print(f"REJECTED elapsed={elapsed:.3f}s")
        return EXIT_REJECTED

    except AuthUnavailableError as exc:
        elapsed = time.monotonic() - started
        print(
            f"UNAVAILABLE code={exc.grpc_code} elapsed={elapsed:.3f}s",
            file=sys.stderr,
        )
        return EXIT_UNAVAILABLE

    except AuthProtocolError as exc:
        elapsed = time.monotonic() - started
        print(
            f"PROTOCOL code={exc.grpc_code} elapsed={elapsed:.3f}s",
            file=sys.stderr,
        )
        return EXIT_PROTOCOL

    elapsed = time.monotonic() - started

    print("VERIFIED")
    print(f"user_id={identity.user_id}")
    print(f"roles={','.join(identity.roles)}")
    print(f"elapsed={elapsed:.3f}s")

    return EXIT_VERIFIED


if __name__ == "__main__":
    raise SystemExit(main())