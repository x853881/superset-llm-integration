"""Resolves which LLM backend to talk to, and how to reach it.

Always speaks the OpenAI-compatible chat-completions API. Swap models or
providers by setting Flask config in ``superset_config.py`` - no code change
needed.

    # Default: local Ollama running gemma3:4b via its OpenAI-compatible API
    NLSQL_LLM_BASE_URL = "http://host.docker.internal:11434/v1"
    NLSQL_LLM_MODEL = "gemma3:4b"

    # Point at any other OpenAI-compatible endpoint instead
    NLSQL_LLM_BASE_URL = "https://openrouter.ai/api/v1"
    NLSQL_LLM_MODEL = "google/gemma-4-31b-it"
    NLSQL_LLM_API_KEY = "sk-or-..."  # or set NLSQL_LLM_API_KEY in the environment

    # How long to wait for a completion. Hosted providers answer a schema-grounded
    # request in seconds; a local model on consumer hardware can take minutes, so
    # this is a config key rather than a constant.
    NLSQL_LLM_TIMEOUT_SECONDS = 600

    # Ask for token usage on streamed calls (`stream_options.include_usage`), which is what puts
    # token counts in the call log. On by default; turn off for a server that rejects the option.
    NLSQL_LLM_REQUEST_USAGE = True

    # Tell the LLM endpoint which Superset user each call is for, as this HTTP header - for a
    # gateway that tracks usage or applies limits per user. Sent on embedding calls too. Off
    # unless set. The value is the username, percent-encoded (see `request_context.header_value`).
    NLSQL_LLM_USER_HEADER = "X-Superset-User-Name"

    # A client certificate for an endpoint that wants one, and the CAs to trust for its own - see
    # `tls.py` for all four settings.
    NLSQL_LLM_CLIENT_CERT = "/app/certs/client.pem"
    NLSQL_LLM_CA_BUNDLE = "/app/certs/ca.pem"
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Mapping

from flask import current_app

from .tls import TlsSettings, tls_settings, warn_if_unused

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemma3:4b"
# host.docker.internal, not localhost: this code runs inside Superset's own container, and
# "localhost" there means the container itself, not the host machine Ollama is presumably running
# on. superset-r2's docker-compose.yml already wires up host.docker.internal:host-gateway for
# every service via extra_hosts - the same mechanism Superset itself uses to reach the host.
# The /v1 suffix is Ollama's OpenAI-compatible API mount, as opposed to its native /api/generate.
_DEFAULT_BASE_URL = "http://host.docker.internal:11434/v1"
# Generous because the floor is set by local inference, not by the network: a 12B
# model answering a ~4k-token schema-grounded prompt on an M-series laptop runs to
# several minutes, and a timeout shorter than that reads as a failed request when
# the model was in fact still working.
_DEFAULT_TIMEOUT_SECONDS = 600

# What HTTP allows in a header name (RFC 9110's `token`).
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
# What the calls set themselves, which a username must never stand in for.
_RESERVED_HEADERS = {"authorization", "content-type", "content-length", "accept", "host", "transfer-encoding"}
_warned_header_names: set[str] = set()


@dataclass(frozen=True)
class LlmProvider:
    model: str
    base_url: str
    api_key: str = ""
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS
    request_usage: bool = True
    #: The header the Superset user is sent as - empty for none (see `NLSQL_LLM_USER_HEADER`).
    user_header: str = ""
    #: The client certificate and CAs for an HTTPS endpoint (see `tls.py`).
    tls: TlsSettings = TlsSettings()


def resolve_llm_provider() -> LlmProvider:
    config = current_app.config
    model = config.get("NLSQL_LLM_MODEL", _DEFAULT_MODEL)
    base_url = config.get("NLSQL_LLM_BASE_URL", _DEFAULT_BASE_URL)
    api_key = config.get("NLSQL_LLM_API_KEY", os.environ.get("NLSQL_LLM_API_KEY", ""))
    timeout_seconds = config.get("NLSQL_LLM_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS)
    tls = tls_settings(config, "NLSQL_LLM")
    warn_if_unused(tls, base_url)
    return LlmProvider(
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        request_usage=bool(config.get("NLSQL_LLM_REQUEST_USAGE", True)),
        user_header=user_header_name(config),
        tls=tls,
    )


def user_header_name(config: Mapping) -> str:
    """`NLSQL_LLM_USER_HEADER`, or "" when it is unset. Also "" - logged once, and left off rather
    than sent - for a name HTTP doesn't allow, which would fail every call made with it, and for one
    the calls set themselves (`Authorization`, say), which a username must never stand in for."""
    name = config.get("NLSQL_LLM_USER_HEADER") or ""
    if not isinstance(name, str):
        name = ""
    name = name.strip()
    if not name:
        return ""
    problem = (
        "is not a valid HTTP header name"
        if not _HEADER_NAME.match(name)
        else "is a header the calls already send"
        if name.lower() in _RESERVED_HEADERS
        else None
    )
    if problem is None:
        return name
    if name not in _warned_header_names:
        _warned_header_names.add(name)
        logger.warning("NLSQL_LLM_USER_HEADER %r %s; the user is not being sent", name, problem)
    return ""
