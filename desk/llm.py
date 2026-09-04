"""The one place that talks to the Claude API. Everything else depends on the `TextCompleter` protocol so
tests inject a fake and never touch the network."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

from desk.config import Settings, get_settings

log = logging.getLogger(__name__)


class TextCompleter(Protocol):
    def complete(
        self, system: str, content: list[dict[str, Any]], *, max_tokens: int = 16000
    ) -> str: ...


class AnthropicCompleter:
    """Streams a single-turn request and returns the concatenated text blocks."""

    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self.settings = settings or get_settings()
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        self.client = client

    def complete(
        self, system: str, content: list[dict[str, Any]], *, max_tokens: int = 16000
    ) -> str:
        with self.client.messages.stream(
            model=self.settings.claude_model,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": content}],
        ) as stream:
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            raise RuntimeError(f"model refused: {getattr(details, 'explanation', None) or details}")
        text = "".join(b.text for b in msg.content if b.type == "text")
        log.info(
            "claude %s: in=%s out=%s stop=%s",
            self.settings.claude_model,
            msg.usage.input_tokens,
            msg.usage.output_tokens,
            msg.stop_reason,
        )
        if msg.stop_reason == "max_tokens":
            raise RuntimeError("model output truncated at max_tokens")
        return text


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_json_text(text: str) -> Any:
    """Tolerate a code fence or stray prose around the JSON object."""
    m = _FENCE.match(text)
    if m:
        text = m.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def get_completer(settings: Settings | None = None) -> TextCompleter:
    settings = settings or get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return AnthropicCompleter(settings)
