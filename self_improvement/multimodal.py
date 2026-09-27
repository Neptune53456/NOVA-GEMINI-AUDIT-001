"""Provider-neutral multimodal message parts and bounded adapter translation."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    mime_type: str
    data: bytes = field(repr=False)


MultimodalPart = TextPart | ImagePart


def openai_chat_messages(messages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Translate neutral parts only at an OpenAI-compatible adapter boundary."""
    translated = []
    for message in messages:
        item = dict(message)
        content = item.get("content")
        if isinstance(content, (list, tuple)) and any(
                isinstance(part, (TextPart, ImagePart)) for part in content):
            parts = []
            for part in content:
                if isinstance(part, TextPart):
                    parts.append({"type": "text", "text": part.text})
                elif isinstance(part, ImagePart):
                    encoded = base64.b64encode(part.data).decode("ascii")
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{part.mime_type};base64,{encoded}"},
                    })
                else:
                    raise ValueError("unsupported_multimodal_part")
            item["content"] = parts
        translated.append(item)
    return translated
