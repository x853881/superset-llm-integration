"""Resolves which embedding backend to talk to, and how to reach it.

Mirrors ``llm_provider.py``'s shape: always speaks the OpenAI-compatible
embeddings API. Swap models or providers via Flask config:

    # Default: local Ollama running nomic-embed-text via its OpenAI-compatible API
    NLSQL_EMBEDDING_BASE_URL = "http://host.docker.internal:11434/v1"
    NLSQL_EMBEDDING_MODEL = "nomic-embed-text"

    # Point at any other OpenAI-compatible endpoint instead
    NLSQL_EMBEDDING_BASE_URL = "https://openrouter.ai/api/v1"
    NLSQL_EMBEDDING_MODEL = "qwen/qwen3-embedding-8b"
    NLSQL_EMBEDDING_API_KEY = "sk-or-..."  # or set NLSQL_EMBEDDING_API_KEY in the environment

The Superset user goes with embedding calls too when ``NLSQL_LLM_USER_HEADER`` is set (see
``llm_provider.py``) - there is no separate setting, since the two are usually the same gateway.
For the same reason the client certificate and CA settings (``NLSQL_EMBEDDING_CLIENT_CERT`` and so
on, see ``tls.py``) each fall back to the LLM's.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from flask import current_app

from .llm_provider import user_header_name
from .tls import TlsSettings, tls_settings, warn_if_unused

_DEFAULT_MODEL = "nomic-embed-text"
# host.docker.internal, not localhost: this code runs inside Superset's own container, and
# "localhost" there means the container itself, not the host machine Ollama is presumably running
# on. superset-r2's docker-compose.yml already wires up host.docker.internal:host-gateway for
# every service via extra_hosts - the same mechanism Superset itself uses to reach the host.
# The /v1 suffix is Ollama's OpenAI-compatible API mount, as opposed to its native /api/embeddings.
_DEFAULT_BASE_URL = "http://host.docker.internal:11434/v1"
# nomic-embed-text was trained with these instruction prefixes and ranks poorly without them; most
# other embedding models don't need either, so leave these blank when switching models.
_DEFAULT_DOCUMENT_PREFIX = "search_document: "
_DEFAULT_QUERY_PREFIX = "search_query: "


@dataclass(frozen=True)
class EmbeddingProvider:
    model: str
    base_url: str
    api_key: str = ""
    document_prefix: str = ""
    query_prefix: str = ""
    #: The header the Superset user is sent as - empty for none (see `NLSQL_LLM_USER_HEADER`).
    user_header: str = ""
    #: The client certificate and CAs for an HTTPS endpoint (see `tls.py`).
    tls: TlsSettings = TlsSettings()


def resolve_embedding_provider() -> EmbeddingProvider:
    config = current_app.config
    model = config.get("NLSQL_EMBEDDING_MODEL", _DEFAULT_MODEL)
    base_url = config.get("NLSQL_EMBEDDING_BASE_URL", _DEFAULT_BASE_URL)
    api_key = config.get("NLSQL_EMBEDDING_API_KEY", os.environ.get("NLSQL_EMBEDDING_API_KEY", ""))
    llm_tls = tls_settings(config, "NLSQL_LLM")
    own = tls_settings(config, "NLSQL_EMBEDDING")
    # The LLM's, unless any of the embedding's own are set - so an error names what was actually set.
    tls = tls_settings(config, "NLSQL_EMBEDDING", fallback=llm_tls) if own.configured or own.key_password else llm_tls
    if own.configured:
        warn_if_unused(tls, base_url)
    return EmbeddingProvider(
        model=model,
        base_url=base_url,
        api_key=api_key,
        document_prefix=config.get("NLSQL_EMBEDDING_DOCUMENT_PREFIX", _DEFAULT_DOCUMENT_PREFIX),
        query_prefix=config.get("NLSQL_EMBEDDING_QUERY_PREFIX", _DEFAULT_QUERY_PREFIX),
        user_header=user_header_name(config),
        tls=tls,
    )
