#!/usr/bin/env python3
"""Redact common credential forms from deploy output before display/storage."""

from __future__ import annotations

import re
import sys


KEY_VALUE = re.compile(
    r"(?i)(\b(?:password|passwd|secret(?:[_ -]?access)?[_ -]?key|access[_ -]?key|"
    r"session[_ -]?token|token|api[_ -]?key|gh[_ -]?token|authorization|database_url)\b"
    r"\s*(?:=|:|\s)\s*)([^\s,;]+)"
)
URL_CREDENTIAL = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)([^/@\s:]+):([^@/\s]+)@")
BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]+=*")
KNOWN_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|AKIA[A-Z0-9]{12,})\b")


def redact(value: str) -> str:
    value = URL_CREDENTIAL.sub(r"\1[REDACTED]@", value)
    value = BEARER.sub(r"\1[REDACTED]", value)
    value = KEY_VALUE.sub(r"\1[REDACTED]", value)
    return KNOWN_TOKEN.sub("[REDACTED]", value)


def main() -> int:
    for line in sys.stdin:
        sys.stdout.write(redact(line))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
