"""Defense in depth. Adapters omit free text before records cross this boundary."""

import re

PATTERNS = [
    r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----",
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]{10,})\b",
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b",
    r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret|authorization)\s*[:=]\s*[^\s,;]+",
]


def sanitize(value):
    if isinstance(value, str):
        for pattern in PATTERNS:
            value = re.sub(pattern, "[REDACTED]", value)
        return value
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, dict):
        return {sanitize(k): sanitize(v) for k, v in value.items()}
    return value
