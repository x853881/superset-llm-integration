"""Which of an interrogated database's tables a request is about, by embedding - for a database too
big to give a prompt whole.

Without this, a database whose tables don't fit ``NLSQL_SCHEMA_MAX_CHARS`` loses every table's
description first, then the column types, then the tables sharing the fewest words with the request
(see ``schema_knowledge.prompt_text``). With ``NLSQL_SCHEMA_EMBEDDINGS`` on, Interrogate DB also
embeds each table - its name, description and columns, under its schema's description - with the
configured embedding model (``NLSQL_EMBEDDING_*``), and a prompt that can't have every table gets
them in order of how close they are to the request, described, until the budget is full. A table
joined to a close one counts as nearly as close (``NEIGHBOUR_WEIGHT``), so the other end of a join
comes along - not every join a request needs, which is what an LLM table filter would be for.

The vectors are kept in one store for the whole Superset, in its key-value store, normalised and
scaled to one byte a dimension (which cosine similarity barely notices). Each is kept under a hash
of the model, its document prefix and the table's text, which the database's knowledge records for
each table, so a table described the same way in two databases is embedded once, and interrogating
again embeds only the tables whose text changed. The store isn't filtered by user: what reaches a
user comes through their database's knowledge, narrowed to the tables they may see.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from operator import mul
from typing import Any, Optional

from flask import current_app

from . import progress, schema_knowledge
from .cancellation import CancelledError, CancelToken
from .embedding_client import embed_documents, embed_query
from .embedding_provider import EmbeddingProvider, resolve_embedding_provider
from .progress import EventStream
from .schema_knowledge import DatabaseKnowledge, SchemaTable
from .semantic_schema import TableDescription

try:
    import numpy
except ImportError:  # outside Superset (tests, tools): plain Python, slower but the same
    numpy = None

logger = logging.getLogger(__name__)

FORMAT_VERSION = 2
#: Tables embedded per call.
BATCH = 32
#: Vectors embedded before they're written to the store, so a long run keeps what it has done.
FLUSH_EVERY = 1024
#: How close a table joined to a close one counts as, relative to that one.
NEIGHBOUR_WEIGHT = 0.9
#: The most vector bytes kept in one bucket. An entry holds at most 16MB, and base64 adds a third.
BUCKET_BYTES = 9_000_000
#: How long a vector found missing isn't looked for again.
RECHECK_SECONDS = 60
WRITE_ATTEMPTS = 3


def enabled() -> bool:
    try:
        return bool(current_app.config.get("NLSQL_SCHEMA_EMBEDDINGS", False))
    except RuntimeError:
        return False


@dataclass
class TableVectors:
    """One database's table embeddings: row `i` of `vectors` (`dimensions` signed bytes) is `keys[i]`."""

    model: str
    dimensions: int
    keys: list[SchemaTable]
    #: Each table's key in the store (`key_for`).
    digests: list[str]
    vectors: bytes
    #: The `version` of the knowledge these are for.
    made_for: float = 0.0
    _matrix: Any = field(default=None, repr=False, compare=False)

    def scores(self, query: list[int]) -> list[int]:
        """Each table's dot product with `query` (also scaled to bytes) - its cosine similarity, scaled."""
        if numpy is not None:
            if self._matrix is None:
                self._matrix = numpy.frombuffer(self.vectors, dtype=numpy.int8).reshape(len(self.keys), self.dimensions)
            q = numpy.asarray(query, dtype=numpy.int32)
            return [int(score) for start in range(0, len(self.keys), 512) for score in self._matrix[start:start + 512].astype(numpy.int32) @ q]
        rows = memoryview(self.vectors).cast("b")
        d = self.dimensions
        return [sum(map(mul, rows[i * d:(i + 1) * d], query)) for i in range(len(self.keys))]


def embedding_text(table: TableDescription, schema_description: Optional[str] = None) -> str:
    """What a table is embedded as: what it's called and what it holds, without the types and sizes
    a request never mentions."""
    lines = [f"{table.schema}.{table.name}"]
    if schema_description:
        lines.append(f"In {table.schema}: {schema_description}")
    if table.description:
        lines.append(table.description)
    if table.synonyms:
        lines.append(f"Also called: {', '.join(table.synonyms)}")
    if table.usage_notes:
        lines.append(table.usage_notes)
    for column in table.columns:
        lines.append(f"{column.name} - {column.description}" if column.description else column.name)
    return "\n".join(lines)


def quantise(vector: list[float]) -> bytes:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return bytes((max(-127, min(127, round(x / norm * 127))) & 0xFF) for x in vector)


def key_for(provider: EmbeddingProvider, text: str) -> str:
    """Where a table's vector is kept: a hash of the model, its document prefix and the text, so the
    same table text embedded the same way is kept once, and a change of model never finds an old
    vector. Short and all the same length; not a secret, since a vector gives away some of its text
    anyway."""
    return hashlib.sha256(f"{provider.model}\0{provider.document_prefix}\0{text}".encode("utf-8")).hexdigest()[:32]


def embed(
    tables: list[TableDescription],
    schema_descriptions: dict[str, str],
    provider: EmbeddingProvider,
    steps: progress.Steps,
    cancel_token: Optional[CancelToken] = None,
) -> EventStream:
    """Makes sure every table has a vector in the shared store, embedding only those it doesn't
    have yet. Returns each table's key and how many were already there."""
    texts = [embedding_text(table, schema_descriptions.get(table.schema)) for table in tables]
    keys = [key_for(provider, text) for text in texts]
    have = lookup(keys, fresh=True)
    todo = sorted({key: index for index, key in enumerate(keys) if key not in have}.values())
    pending: dict[str, bytes] = {}
    try:
        for start in range(0, len(todo), BATCH):
            if cancel_token is not None and cancel_token.cancelled:
                raise CancelledError("The request was cancelled.")
            batch = todo[start:start + BATCH]
            yield steps.update("embed", f"{start + len(batch)} of {len(todo)} tables with {provider.model}")
            for index, vector in zip(batch, embed_documents(provider, [texts[index] for index in batch])):
                pending[keys[index]] = quantise(vector)
            if len(pending) >= FLUSH_EVERY:
                add(pending)
                pending = {}
    finally:
        # Whatever was embedded before a failure or a cancel is kept, so it isn't paid for twice.
        if pending:
            add(pending)
    return {(table.schema, table.name): key for table, key in zip(tables, keys)}, len(tables) - len(todo)


# --- Keeping them --------------------------------------------------------------------------------
#
# One store for the whole Superset: every database's vectors, by key, spread over `BUCKETS` entries
# of the key-value store by the key's first characters. A table described the same way in two
# databases is kept once.


def _bucket(key: str) -> str:
    return key[:2]


def _bucket_name(bucket: str) -> str:
    return f"nltosqlandback:vectors:{bucket}"


_lock = threading.Lock()
#: The vectors this process has read or written, by key - never out of date, since a key is its content.
_vectors: dict[str, bytes] = {}
#: Keys looked for and not found, and when: not looked for again until `RECHECK_SECONDS` later.
_absent: dict[str, float] = {}
#: What each database's knowledge was last turned into, by database and catalog.
_memory: dict[tuple[int, Optional[str]], TableVectors] = {}


def _read_bucket(bucket: str) -> dict[str, str]:
    record = schema_knowledge.store.get_record(_bucket_name(bucket))
    if not record or record.get("version") != FORMAT_VERSION:
        return {}
    return dict(record.get("vectors") or {})


def lookup(keys: list[str], fresh: bool = False) -> dict[str, bytes]:
    """The vectors kept for `keys`, reading the store only for those this process doesn't have.
    `fresh` looks again for keys recently found missing."""
    now = time.monotonic()
    with _lock:
        found = {key: _vectors[key] for key in keys if key in _vectors}
        wanted = {
            key for key in keys
            if key not in found and (fresh or now - _absent.get(key, -math.inf) >= RECHECK_SECONDS)
        }
    by_bucket: dict[str, set[str]] = {}
    for key in wanted:
        by_bucket.setdefault(_bucket(key), set()).add(key)
    for bucket, bucket_keys in by_bucket.items():
        kept = _read_bucket(bucket)
        for key in bucket_keys:
            if key in kept:
                try:
                    found[key] = base64.b64decode(kept[key])
                except (TypeError, ValueError):
                    logger.warning("an unreadable table vector, %s", key)
    with _lock:
        for key in wanted:
            if key in found:
                _vectors[key] = found[key]
                _absent.pop(key, None)
            else:
                _absent[key] = now
    return found


def add(vectors: dict[str, bytes]) -> None:
    """Adds `vectors` to the store. Another process adding to the same bucket at the same moment
    could undo the write, so each bucket is read back and written again until it has them all; a
    vector still missing only means its table ranks last until the next interrogation."""
    by_bucket: dict[str, dict[str, bytes]] = {}
    for key, vector in vectors.items():
        by_bucket.setdefault(_bucket(key), {})[key] = vector
    for bucket, adding in by_bucket.items():
        for _attempt in range(WRITE_ATTEMPTS):
            kept = _read_bucket(bucket)
            missing = {key: vector for key, vector in adding.items() if key not in kept}
            if not missing:
                break
            size = sum(len(value) for value in kept.values()) * 3 // 4
            room = []
            for key, vector in missing.items():
                if size + len(vector) <= BUCKET_BYTES:
                    room.append(key)
                    size += len(vector)
            if len(room) < len(missing):
                logger.warning("the table vector store is full (bucket %s): %d tables not kept", bucket, len(missing) - len(room))
            if not room:
                break
            kept.update({key: base64.b64encode(missing[key]).decode("ascii") for key in room})
            schema_knowledge.store.put_record(_bucket_name(bucket), {"version": FORMAT_VERSION, "vectors": kept})
            adding = {key: missing[key] for key in room}
    with _lock:
        _vectors.update(vectors)
        for key in vectors:
            _absent.pop(key, None)


def for_knowledge(knowledge: DatabaseKnowledge) -> Optional[TableVectors]:
    """The vectors of `knowledge`'s tables, or None when the store has none of them. A table whose
    vector isn't in the store is left out, and so ranks last."""
    if not knowledge.embedding_keys:
        return None
    memory_key = (knowledge.database_id, knowledge.catalog)
    with _lock:
        cached = _memory.get(memory_key)
    if cached is not None and cached.made_for == knowledge.version and len(cached.keys) == len(knowledge.embedding_keys):
        return cached
    tables = [(table, key) for table, key in knowledge.embedding_keys.items()]
    found = lookup([key for _, key in tables])
    present = [(table, key) for table, key in tables if key in found]
    if not present:
        return None
    sizes = {len(found[key]) for _, key in present}
    if len(sizes) > 1:
        logger.warning("table vectors of different sizes (%s) for database %s", sorted(sizes), knowledge.database_id)
        return None
    vectors = TableVectors(
        model=knowledge.embedding_model or "",
        dimensions=sizes.pop() if sizes else 0,
        keys=[table for table, _ in present],
        digests=[key for _, key in present],
        vectors=b"".join(found[key] for _, key in present),
        made_for=knowledge.version,
    )
    with _lock:
        _memory[memory_key] = vectors
    return vectors


# --- Using them ----------------------------------------------------------------------------------


def ranked(knowledge: DatabaseKnowledge, vectors: TableVectors, query: list[float]) -> list[SchemaTable]:
    """Every table of `knowledge`, closest to `query` first - a table joined to a close one counting
    as `NEIGHBOUR_WEIGHT` as close. Tables with no vector come last, in their own order."""
    similarity = dict(zip(vectors.keys, vectors.scores(_signed(quantise(query)))))
    by_name = {f"{schema}.{table}".lower(): (schema, table) for schema, table in knowledge.table_keys}
    neighbours: dict[SchemaTable, set[SchemaTable]] = {}
    for table in knowledge.tables:
        here = (table.schema, table.name)
        for relationship in table.relationships:
            there = by_name.get(relationship.to_table.lower())
            if there is not None and there != here:
                neighbours.setdefault(here, set()).add(there)
                neighbours.setdefault(there, set()).add(here)

    def closeness(key: SchemaTable) -> float:
        own = similarity.get(key)
        near = [similarity[other] for other in neighbours.get(key, ()) if other in similarity]
        candidates = ([own] if own is not None else []) + ([NEIGHBOUR_WEIGHT * max(near)] if near else [])
        return max(candidates) if candidates else -math.inf

    keys = knowledge.table_keys
    return sorted(keys, key=lambda key: -closeness(key))


def _signed(row: bytes) -> list[int]:
    return list(memoryview(row).cast("b"))


def ranking(knowledge: DatabaseKnowledge, query_text: str) -> Optional[list[SchemaTable]]:
    """`knowledge`'s tables in order of closeness to `query_text`, or None when that can't be known:
    embeddings off, the tables not embedded when it was interrogated, a different embedding model
    configured since, or the request couldn't be embedded."""
    if not enabled() or not knowledge.embedding_model:
        return None
    provider = resolve_embedding_provider()
    if provider.model != knowledge.embedding_model:
        logger.warning(
            "table embeddings were made with %s, not %s - interrogate the database again", knowledge.embedding_model, provider.model
        )
        return None
    vectors = for_knowledge(knowledge)
    if vectors is None:
        return None
    try:
        query = embed_query(provider, query_text)
    except Exception as ex:  # noqa: BLE001 - the prompt still gets tables, just not ranked this way
        logger.warning("could not embed the request: %s", ex)
        return None
    return ranked(knowledge, vectors, query)
