"""Small, refreshed runtime context for model prompts."""

from __future__ import annotations

from datetime import datetime


def current_time_context() -> str:
    """Return one concise local-time line for the system prompt."""
    now = datetime.now().astimezone()
    return f"Current local time: {now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}."
