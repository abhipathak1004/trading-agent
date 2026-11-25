"""Cheap, dependency-free token estimation and trimming.

Adapted from Agent Zero's ``helpers/tokens.py`` (MIT License, Copyright (c)
2025 Agent Zero, s.r.o, https://github.com/agent0ai/agent-zero). The upstream
uses ``tiktoken``; we deliberately do not, because it pulls a multi-megabyte
tokenizer binary for an estimate that only needs to be in the right ballpark
for budgeting and trimming.

The approximation: ~4 characters per token for English prose, plus a safety
buffer. It is used only to *trim* and *budget*, never to report billed usage —
provider-reported usage is authoritative when present.
"""

import re

APPROX_CHARS_PER_TOKEN = 4.0
APPROX_BUFFER = 1.1
TRIM_BUFFER = 0.8
_EMBEDDED_IMAGE_DATA_URL = re.compile(
    r"data:(image/[A-Za-z0-9.+-]+(?:;[A-Za-z0-9.+-]+=[A-Za-z0-9.+/=_-]+)*);base64,[A-Za-z0-9+/=_-]+"
)
EMBEDDED_IMAGE_PLACEHOLDER = "[embedded image data omitted from token estimate]"


def sanitize_embedded_image_data_urls(text: str) -> str:
    if not text:
        return text
    return _EMBEDDED_IMAGE_DATA_URL.sub(
        f"data:\\1;base64,{EMBEDDED_IMAGE_PLACEHOLDER}", text
    )


def count_tokens(text: str) -> int:
    """Approximate token count for budgeting. Not a billing figure."""
    if not text:
        return 0
    return int(len(text) / APPROX_CHARS_PER_TOKEN * APPROX_BUFFER)


def approximate_tokens(text: str) -> int:
    return count_tokens(sanitize_embedded_image_data_urls(text))


def trim_to_tokens(
    text: str,
    max_tokens: int,
    *,
    direction: str = "end",
    ellipsis: str = "...",
) -> str:
    """Trim text so its approximate token count fits ``max_tokens``.

    ``direction="end"`` keeps the head, ``"start"`` keeps the tail. Mirrors the
    upstream behaviour but with the char-based estimator.
    """
    if max_tokens <= 0:
        return ellipsis
    tokens = count_tokens(text)
    if tokens <= max_tokens:
        return text
    chars = len(text)
    approx_chars = int(chars * (max_tokens / tokens) * TRIM_BUFFER)
    if direction == "start":
        return ellipsis + text[chars - approx_chars :]
    return text[:approx_chars] + ellipsis
