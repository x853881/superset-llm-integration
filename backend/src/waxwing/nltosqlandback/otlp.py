"""Just enough of the OTLP/HTTP trace protocol to send spans to Arize Phoenix.

Phoenix ingests traces over OTLP/HTTP at ``<server>/v1/traces`` and only accepts the protobuf
encoding (``application/x-protobuf``), not OTLP's JSON variant. The usual way to produce that is the
OpenTelemetry SDK plus its OTLP exporter, but neither ships with Superset's image and an extension
can't add Python dependencies to the host - so this writes the protobuf wire format by hand. It's a
small subset of ``opentelemetry/proto/trace/v1/trace.proto`` (resource, scope, span, attributes,
status) and the wire format for those is four field types, which is far less code than it sounds.

Field numbers below are from the OTLP .proto files (opentelemetry-proto v1).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Optional, Union

AttributeValue = Union[str, bool, int, float, list, None]

SPAN_KIND_INTERNAL = 1
SPAN_KIND_SERVER = 2
SPAN_KIND_CLIENT = 3

STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2

_VARINT = 0
_FIXED64 = 1
_LENGTH_DELIMITED = 2


@dataclass
class Span:
    """One finished span. Ids are hex strings (32 chars for a trace, 16 for a span), as the rest of
    this extension carries them; times are epoch nanoseconds."""

    trace_id: str
    span_id: str
    name: str
    start_ns: int
    end_ns: int
    parent_span_id: Optional[str] = None
    kind: int = SPAN_KIND_INTERNAL
    attributes: dict[str, AttributeValue] = field(default_factory=dict)
    status_code: int = STATUS_UNSET
    status_message: str = ""


def encode_export_request(
    spans: list[Span],
    resource_attributes: dict[str, AttributeValue],
    scope_name: str,
    scope_version: str = "",
) -> bytes:
    """An `ExportTraceServiceRequest` holding `spans`, all under one resource and one scope."""
    resource = b"".join(_key_value_field(1, key, value) for key, value in resource_attributes.items() if value is not None)
    scope = _string_field(1, scope_name) + (_string_field(2, scope_version) if scope_version else b"")
    scope_spans = _message_field(1, scope) + b"".join(_message_field(2, _encode_span(span)) for span in spans)
    resource_spans = _message_field(1, resource) + _message_field(2, scope_spans)
    return _message_field(1, resource_spans)


def _encode_span(span: Span) -> bytes:
    out = _bytes_field(1, bytes.fromhex(span.trace_id))
    out += _bytes_field(2, bytes.fromhex(span.span_id))
    if span.parent_span_id:
        out += _bytes_field(4, bytes.fromhex(span.parent_span_id))
    out += _string_field(5, span.name)
    out += _varint_field(6, span.kind)
    out += _fixed64_field(7, span.start_ns)
    out += _fixed64_field(8, span.end_ns)
    for key, value in span.attributes.items():
        if value is not None:
            out += _key_value_field(9, key, value)
    if span.status_code != STATUS_UNSET or span.status_message:
        status = (_string_field(2, span.status_message) if span.status_message else b"") + _varint_field(3, span.status_code)
        out += _message_field(15, status)
    return out


def _key_value_field(number: int, key: str, value: AttributeValue) -> bytes:
    return _message_field(number, _string_field(1, key) + _message_field(2, _any_value(value)))


def _any_value(value: Any) -> bytes:
    # bool before int: a bool *is* an int in Python, and would otherwise go out as 0/1.
    if isinstance(value, bool):
        return _varint_field(2, 1 if value else 0)
    if isinstance(value, int):
        return _varint_field(3, value)
    if isinstance(value, float):
        return _tag(4, _FIXED64) + struct.pack("<d", value)
    if isinstance(value, (list, tuple)):
        array = b"".join(_message_field(1, _any_value(item)) for item in value if item is not None)
        return _message_field(5, array)
    return _string_field(1, str(value))


def _tag(number: int, wire_type: int) -> bytes:
    return _varint((number << 3) | wire_type)


def _varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64  # int64 is sent as its 64-bit two's complement
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _varint_field(number: int, value: int) -> bytes:
    return _tag(number, _VARINT) + _varint(value)


def _fixed64_field(number: int, value: int) -> bytes:
    return _tag(number, _FIXED64) + struct.pack("<Q", value)


def _bytes_field(number: int, value: bytes) -> bytes:
    return _tag(number, _LENGTH_DELIMITED) + _varint(len(value)) + value


def _string_field(number: int, value: str) -> bytes:
    return _bytes_field(number, value.encode("utf-8"))


def _message_field(number: int, encoded: bytes) -> bytes:
    return _bytes_field(number, encoded)


def decode(data: bytes) -> list[tuple[int, int, Any]]:
    """Splits one protobuf message into (field number, wire type, raw value) triples - the inverse
    of the writers above, for tests and for debugging what is being sent. Length-delimited values
    come back as bytes, to be decoded again by whoever knows what message they hold."""
    fields: list[tuple[int, int, Any]] = []
    position = 0
    while position < len(data):
        key, position = _read_varint(data, position)
        number, wire_type = key >> 3, key & 0x7
        if wire_type == _VARINT:
            value, position = _read_varint(data, position)
        elif wire_type == _FIXED64:
            value = data[position: position + 8]
            position += 8
        elif wire_type == _LENGTH_DELIMITED:
            length, position = _read_varint(data, position)
            value = data[position: position + length]
            position += length
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        fields.append((number, wire_type, value))
    return fields


def _read_varint(data: bytes, position: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = data[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7
