"""Narrows a `SemanticSchema` down to the tables most relevant to a request,
so a schema with hundreds of tables doesn't blow out the prompt's token
budget.

Each table's embedding is computed once, from its ``to_prompt_text()``
(which already carries the business descriptions bundled in
``resources/semantic_schema.json``), and reused for every ``filter`` call -
only the request text is embedded per call. Ranking is a brute-force
cosine-similarity scan over every table, fine at hundreds of tables; swap it
for a vector database lookup once it needs to scale past that.
"""

from __future__ import annotations

import math
from typing import Optional

from .embedding_cache import EmbeddingCache
from .embedding_client import embed_document, embed_query
from .embedding_provider import EmbeddingProvider
from .semantic_schema import SemanticSchema, TableDescription


class EmbeddingSchemaFilter:
    def __init__(
        self,
        schema: SemanticSchema,
        provider: EmbeddingProvider,
        cache: Optional[EmbeddingCache],
        table_embeddings: list[tuple[TableDescription, list[float]]],
    ):
        self._schema = schema
        self._provider = provider
        self._cache = cache
        self._table_embeddings = table_embeddings

    @classmethod
    def for_schema(
        cls, schema: SemanticSchema, provider: EmbeddingProvider, cache: Optional[EmbeddingCache] = None
    ) -> "EmbeddingSchemaFilter":
        """Embeds every table in `schema` up front so later `filter` calls only embed the query."""
        table_embeddings = [
            (table, embed_document(provider, table.to_prompt_text(), cache)) for table in schema.tables
        ]
        return cls(schema, provider, cache, table_embeddings)

    def filter(self, query_text: str, top_k: int = 8) -> SemanticSchema:
        """`schema` narrowed to the `top_k` tables whose embedding is most similar to `query_text`."""
        query_embedding = embed_query(self._provider, query_text, self._cache)
        ranked = sorted(
            self._table_embeddings,
            key=lambda pair: _cosine_similarity(query_embedding, pair[1]),
            reverse=True,
        )
        top_tables = [table for table, _ in ranked[:top_k]]
        return SemanticSchema(
            database_name=self._schema.database_name,
            schema_name=self._schema.schema_name,
            tables=top_tables,
        )


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    denominator = norm_a * norm_b
    return dot / denominator if denominator else 0.0
