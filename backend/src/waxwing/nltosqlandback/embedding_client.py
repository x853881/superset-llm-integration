"""Turns text into an embedding vector via the OpenAI-compatible embeddings shape, which Ollama,
OpenAI, and OpenRouter all serve.

When `cache` is supplied, every embed call checks it first and records a
miss's result back into it - see ``embedding_cache.py`` for what "recording"
means (in-memory always, on-disk best-effort).
"""

from __future__ import annotations

import logging
from typing import Optional

import ssl

import requests
from requests.adapters import HTTPAdapter

from . import request_context, telemetry, tls
from .embedding_cache import EmbeddingCache
from .embedding_provider import EmbeddingProvider

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 60


def embed_document(provider: EmbeddingProvider, text: str, cache: Optional[EmbeddingCache] = None) -> list[float]:
    """Embeds `text` as a document being indexed (e.g. a table's description), applying the
    provider's document prefix."""
    return _embed(provider, provider.document_prefix + text, cache)


def embed_query(provider: EmbeddingProvider, text: str, cache: Optional[EmbeddingCache] = None) -> list[float]:
    """Embeds `text` as a search query, applying the provider's query prefix."""
    return _embed(provider, provider.query_prefix + text, cache)


def _embed(provider: EmbeddingProvider, text: str, cache: Optional[EmbeddingCache]) -> list[float]:
    if cache is not None:
        cached = cache.get(text, provider.model)
        if cached is not None:
            logger.info("embedding_cache_hit model=%s input_chars=%d", provider.model, len(text))
            return cached

    start_ns = telemetry.now_ns()
    usage: dict = {}
    try:
        vector, usage = _request(provider, text)
        _record(provider, text, start_ns, "ok", usage)
        if cache is not None:
            cache.put(text, provider.model, vector)
        return vector
    except Exception as ex:
        _record(provider, text, start_ns, "error", usage, str(ex))
        raise


def _record(
    provider: EmbeddingProvider, text: str, start_ns: int, outcome: str, usage: dict, error: Optional[str] = None
) -> None:
    """Logs the call (see `telemetry`) - an embedding call costs tokens like any other."""
    input_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    telemetry.record_llm_call(telemetry.LlmCallRecord(
        operation="embeddings",
        request_model=provider.model,
        base_url=provider.base_url,
        start_ns=start_ns,
        end_ns=telemetry.now_ns(),
        outcome=outcome,
        purpose="embed",
        stream=False,
        input_tokens=int(input_tokens) if isinstance(input_tokens, (int, float)) else None,
        total_tokens=usage.get("total_tokens") if isinstance(usage, dict) else None,
        prompt_chars=len(text),
        error=error,
        prompt=text,
    ))


def _request(provider: EmbeddingProvider, text: str) -> tuple[list[float], dict]:
    # Authorization only when there is a key to send - see the note in llm_client._request.
    headers = request_context.user_headers(provider.user_header)
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    url = f"{provider.base_url.rstrip('/')}/embeddings"
    call = dict(headers=headers, json={"model": provider.model, "input": text}, timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS))
    context = tls.ssl_context(provider.tls) if url.lower().startswith("https:") else None
    if context is None:
        response = requests.post(url, **call)
    else:
        # requests' own `cert=` can't take a key's password, so the call goes with the same context
        # as the LLM's. `verify` still names the bundle, or requests would add its own CAs to it.
        with requests.Session() as session:
            session.mount("https://", _ContextAdapter(context))
            response = session.post(url, verify=provider.tls.ca_bundle or True, **call)
    response.raise_for_status()
    body = response.json()
    # A router (OpenRouter among them) can report an upstream failure this way even on an
    # HTTP-success response - surface its actual message rather than returning an empty vector.
    error = body.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise RuntimeError(message or "The embedding backend returned an error.")
    usage = body.get("usage") or {}
    data = body.get("data") or []
    if not data:
        return [], usage
    return data[0].get("embedding", []), usage


class _ContextAdapter(HTTPAdapter):
    """Makes every HTTPS connection - through a proxy too - with `context`."""

    def __init__(self, context: ssl.SSLContext):
        self._context = context
        super().__init__()

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs["ssl_context"] = self._context
        return super().proxy_manager_for(proxy, **proxy_kwargs)
