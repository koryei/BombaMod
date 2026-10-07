"""Best-effort text redaction for explicitly opted-in third-party decisions.

Redaction cannot guarantee anonymity. Servers must explicitly opt in before raw
flagged content can be sent to OpenRouter; the default path never sends it.
"""

from __future__ import annotations

import re

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"<@!?\d+>"), "[USER_MENTION]"),
    (re.compile(r"<@&\d+>"), "[ROLE_MENTION]"),
    (re.compile(r"<#\d+>"), "[CHANNEL_MENTION]"),
    (re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"\b(?:\+?\d[\d().\- ]{7,}\d)\b"), "[PHONE]"),
    (re.compile(r"(?i)\b(?:discord\.gg|discord(?:app)?\.com/invite)/\S+"), "[INVITE_LINK]"),
    (re.compile(r"(?i)\b(?:https?://)?(?:www\.)?\S+\.(?:com|net|org|io|gg|co)/\S*"), "[URL]"),
    (re.compile(r"(?i)\b(?:sk|pk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b"), "[SECRET]"),
    (re.compile(r"\b\d{15,22}\b"), "[ID]"),
)


def redact_text(text: str, *, max_chars: int = 1500) -> str:
    """Redact common identifiers, bound prompt size, and remove control chars."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    cleaned = "".join(char for char in text if char in "\n\t" or char.isprintable())
    cleaned = cleaned[:max_chars]
    for pattern, replacement in _PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def policy_excerpt(text: str, *, max_chars: int = 1800) -> str:
    """Prepare policy context without logging or retaining the original content."""
    cleaned = "".join(char for char in text if char in "\n\t" or char.isprintable())
    return cleaned[:max_chars]
