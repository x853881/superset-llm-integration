"""One line saying which of the plugin's settings are in effect, logged when Superset loads the
backend. On a deployment it answers "is our config being read at all?" without a shell in the
container. It names hosts and models, never keys or other secrets.
"""

from __future__ import annotations

from typing import Any, Callable, Optional
from urllib.parse import urlparse

from flask import current_app

from . import data_dictionary, table_selection
from .embedding_provider import resolve_embedding_provider
from .llm_provider import resolve_llm_provider


def _host(url: str) -> str:
    return urlparse(url or "").hostname or url or "none"


def _part(name: str, read: Callable[[], Any]) -> str:
    try:
        return f"{name} {read()}"
    except Exception as ex:  # noqa: BLE001 - a bad setting is reported, not raised at start-up
        return f"{name} unreadable ({type(ex).__name__}: {str(ex)[:120]})"


def _model() -> str:
    llm = resolve_llm_provider()
    return f"{llm.model} at {_host(llm.base_url)}, key {'set' if llm.api_key else 'not set'}"


def _embeddings() -> str:
    embedding = resolve_embedding_provider()
    return f"{embedding.model} at {_host(embedding.base_url)}"


def _dictionary() -> str:
    found: Optional[data_dictionary.Config] = data_dictionary.configured()
    return ", ".join(found.tables) if found else "none"


def _on(name: str, default: bool) -> str:
    return "on" if current_app.config.get(name, default) else "off"


def summary() -> str:
    return "; ".join([
        _part("model", _model),
        _part("embeddings", _embeddings),
        _part("database info", lambda: _on("NLSQL_DB_INFO", True)),
        _part("structure read", lambda: current_app.config.get("NLSQL_STRUCTURE_READ", "per_table")),
        _part("table embeddings", lambda: _on("NLSQL_SCHEMA_EMBEDDINGS", False)),
        _part("dictionary", _dictionary),
        _part("table selection", lambda: f"{table_selection.choice().name}, allowed {', '.join(table_selection.allowed())}"),
    ])
