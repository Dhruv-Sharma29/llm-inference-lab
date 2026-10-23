"""Input-size estimation for chat requests, run before the request is queued.

With a tokenizer file (the model's tokenizer.json) the count is exact for message contents plus a
small per-message allowance for the chat template. Without one, a 4-chars-per-token heuristic is
used; it is approximate, so set GATEWAY_TOKENIZER_FILE for real runs. The model server remains
the final authority and its own context-length errors are passed through.
"""

from __future__ import annotations

import math
from typing import Any

PER_MESSAGE_OVERHEAD = 8


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # OpenAI content parts
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


class TokenCounter:
    def __init__(self, tokenizer_file: str | None = None):
        self._tok = None
        if tokenizer_file:
            from tokenizers import Tokenizer

            self._tok = Tokenizer.from_file(tokenizer_file)

    @property
    def exact(self) -> bool:
        return self._tok is not None

    def count(self, text: str) -> int:
        if self._tok is not None:
            return len(self._tok.encode(text, add_special_tokens=False).ids)
        return math.ceil(len(text) / 4)

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        return sum(self.count(_message_text(m)) + PER_MESSAGE_OVERHEAD for m in messages)
