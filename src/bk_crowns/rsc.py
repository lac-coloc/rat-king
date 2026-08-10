"""Small scanners for public Next.js React Server Component payloads."""

from __future__ import annotations

import json
from collections.abc import Iterator
from html.parser import HTMLParser
from typing import Any

PUSH_MARKER = "self.__next_f.push"


class RSCParseError(ValueError):
    """Raised when an advertised Next.js push call cannot be decoded safely."""


class _ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._inside_script = False
        self._parts: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            self._inside_script = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._inside_script:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._inside_script:
            self.scripts.append("".join(self._parts))
            self._inside_script = False
            self._parts = []


def _balanced_slice(text: str, start: int, opening: str, closing: str) -> tuple[str, int]:
    if start >= len(text) or text[start] != opening:
        raise RSCParseError(f"délimiteur {opening!r} absent")

    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue

        if character in {'"', "'"}:
            quote = character
        elif character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1

    raise RSCParseError(f"bloc {opening}{closing} non équilibré")


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def decode_next_chunks(html: str) -> list[str]:
    """Decode string values passed to public ``self.__next_f.push`` calls."""

    collector = _ScriptCollector()
    collector.feed(html)
    chunks: list[str] = []
    for script in collector.scripts:
        offset = 0
        while True:
            marker = script.find(PUSH_MARKER, offset)
            if marker < 0:
                break
            opening = marker + len(PUSH_MARKER)
            while opening < len(script) and script[opening].isspace():
                opening += 1
            argument, offset = _balanced_slice(script, opening, "(", ")")
            try:
                decoded = json.loads(argument)
            except json.JSONDecodeError as exc:
                raise RSCParseError("appel self.__next_f.push JSON invalide") from exc
            chunks.extend(_strings(decoded))
    return chunks


def iter_balanced_json(text: str) -> Iterator[object]:
    """Yield valid JSON objects found with a string-aware balanced-brace scan."""

    offset = 0
    while True:
        start = text.find("{", offset)
        if start < 0:
            return
        try:
            candidate, end = _balanced_slice(text, start, "{", "}")
        except RSCParseError:
            return
        try:
            yield json.loads("{" + candidate + "}")
        except json.JSONDecodeError:
            offset = start + 1
        else:
            offset = end
