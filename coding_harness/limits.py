"""Output bounding helpers shared by tools, sandbox and reports."""

from __future__ import annotations

TRUNCATION_MARKER = "[... output truncated by harness: {shown} of {total} chars shown ...]"


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """Bound ``text`` to roughly ``limit`` chars, keeping head and tail.

    Returns ``(text, truncated)``. The marker makes shortening visible to the model and user.
    """
    if limit <= 0 or len(text) <= limit:
        return text, False
    head = limit * 2 // 3
    tail = limit - head
    marker = TRUNCATION_MARKER.format(shown=head + tail, total=len(text))
    return f"{text[:head]}\n{marker}\n{text[-tail:] if tail else ''}", True
