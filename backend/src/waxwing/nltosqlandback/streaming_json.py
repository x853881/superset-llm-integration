"""Reads one string field (or the finished items of one array) out of a JSON object that is still
being written.

The SQL-generation calls ask the model for a whole JSON object (see
``nlsql_backend.SQL_RESPONSE_FORMAT_INSTRUCTIONS``), which can't be parsed until its last brace
arrives - but the field the user is waiting to see, ``sqlText``, is written long before that. This
picks that one field's value out of the partial text as it grows, so the panel can show the query
being written instead of a spinner followed by everything at once.

Deliberately not a JSON parser: it finds the key, then decodes the string literal after it until it
runs out of text. Anything it can't make sense of yet (a half-written escape, a value that turns
out not to be a string) reads as "nothing to show yet", never as an error - this feeds a preview,
and the authoritative decode is still ``json.loads`` on the finished text.
"""

from __future__ import annotations

import json
from typing import Any, Optional

_SHORT_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class StreamingJsonString:
    """Tracks one string field of a growing JSON object, reporting only what's new each time.

    Fed the whole buffer accumulated so far (not just the latest chunk), since a value's opening
    quote may have arrived several chunks before the text being decoded now.
    """

    def __init__(self, field_name: str) -> None:
        self._field_name = field_name
        self._emitted = 0

    def feed(self, buffer: str) -> str:
        """The part of this field's value that has arrived since the last call - "" if none has."""
        value = partial_string_field(buffer, self._field_name)
        if value is None or len(value) <= self._emitted:
            return ""
        new_text = value[self._emitted:]
        self._emitted = len(value)
        return new_text


def partial_string_field(buffer: str, field_name: str) -> Optional[str]:
    """As much of `field_name`'s string value as `buffer` holds so far, or None if the value hasn't
    started yet (or isn't a string at all). Truncates at the last fully-arrived character, so a
    trailing half-written ``\\u00`` escape is left out rather than guessed at."""
    key = f'"{field_name}"'
    key_at = buffer.find(key)
    if key_at < 0:
        return None
    colon_at = buffer.find(":", key_at + len(key))
    if colon_at < 0:
        return None
    index = colon_at + 1
    while index < len(buffer) and buffer[index] in " \t\r\n":
        index += 1
    if index >= len(buffer) or buffer[index] != '"':
        return None  # not (yet) a string value - `null`, a number, or simply nothing after the colon
    index += 1

    decoded: list[str] = []
    while index < len(buffer):
        char = buffer[index]
        if char == '"':
            break  # the value is complete
        if char != "\\":
            decoded.append(char)
            index += 1
            continue
        if index + 1 >= len(buffer):
            break  # an escape whose second character hasn't arrived
        escape = buffer[index + 1]
        if escape != "u":
            decoded.append(_SHORT_ESCAPES.get(escape, escape))
            index += 2
            continue
        if index + 6 > len(buffer):
            break  # a \uXXXX escape that's still arriving
        try:
            decoded.append(chr(int(buffer[index + 2: index + 6], 16)))
        except ValueError:
            decoded.append(buffer[index: index + 6])  # not valid hex - show it as written
        index += 6
    return "".join(decoded)


def complete_array_items(buffer: str, field_name: str) -> list[Any]:
    """The items of `field_name`'s array value that have arrived in full so far, decoded - for a
    list whose entries are worth showing one by one as they're written (the breakdown's sections).
    An item still being written, and anything after it, is left for a later call; an item that
    turns out not to be valid JSON ends the list there. [] until the array has started."""
    key = f'"{field_name}"'
    key_at = buffer.find(key)
    if key_at < 0:
        return []
    colon_at = buffer.find(":", key_at + len(key))
    if colon_at < 0:
        return []
    index = _skip_space(buffer, colon_at + 1)
    if index >= len(buffer) or buffer[index] != "[":
        return []
    items: list[Any] = []
    index += 1
    while True:
        index = _skip_space(buffer, index)
        if index < len(buffer) and buffer[index] == ",":
            index = _skip_space(buffer, index + 1)
        if index >= len(buffer) or buffer[index] == "]":
            return items
        end = _value_end(buffer, index)
        if end is None:
            return items
        try:
            items.append(json.loads(buffer[index:end]))
        except ValueError:
            return items
        index = end


def _skip_space(buffer: str, index: int) -> int:
    while index < len(buffer) and buffer[index] in " \t\r\n":
        index += 1
    return index


def _value_end(buffer: str, start: int) -> Optional[int]:
    """Where the object, array or string starting at `start` ends (one past it), or None if it
    hasn't finished arriving. Scalars aren't expected in the arrays this reads, and read as unfinished."""
    opener = buffer[start]
    if opener == '"':
        index = start + 1
        while index < len(buffer):
            if buffer[index] == "\\":
                index += 2
                continue
            if buffer[index] == '"':
                return index + 1
            index += 1
        return None
    if opener not in "{[":
        return None
    depth = 0
    in_string = False
    index = start
    while index < len(buffer):
        char = buffer[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None
