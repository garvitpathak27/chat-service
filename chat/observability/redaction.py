from __future__ import annotations

import logging
import re

REDACTED = "[REDACTED]"

_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+")
_SECRET_KV = re.compile(
    r"(?i)(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)"
    r"\"?\s*[=:]\s*\"?[^\s\"',}]+"
)


_PATTERNS = (
    (_JWT, REDACTED),
    (_BEARER, f"Bearer {REDACTED}"),
    (_SECRET_KV, lambda match: f"{match.group(1)}={REDACTED}"),
)

_SKIP_ATTRS = frozenset({"exc_text", "stack_info"})


def scrub(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # pragma: no cover
            return True

        record.msg = scrub(rendered)
        record.args = ()

        for key, value in list(record.__dict__.items()):
            if key in _SKIP_ATTRS:
                continue
            if isinstance(value, str):
                record.__dict__[key] = scrub(value)

        return True

