"""How the model calls prove who they are, and whom they trust, over HTTPS.

For an endpoint that wants a client certificate (mutual TLS), and one whose own certificate comes
from a CA the system doesn't know - an in-house LLM gateway, typically both. Set in
``superset_config.py``, as paths to files the Superset process can read:

    NLSQL_LLM_CLIENT_CERT = "/app/certs/client.pem"   # the client certificate (PEM), perhaps with its key
    NLSQL_LLM_CLIENT_KEY = "/app/certs/client.key"    # its key, when it is a file of its own
    NLSQL_LLM_CLIENT_KEY_PASSWORD = "..."             # only for an encrypted key; or set it in the environment
    NLSQL_LLM_CA_BUNDLE = "/app/certs/ca.pem"         # trust only these CAs for the endpoint's certificate

The embedding calls take ``NLSQL_EMBEDDING_CLIENT_CERT`` and so on, each falling back to the LLM
setting, since the two are usually the same gateway. Nothing set, nothing changes: the system's
trust store, no client certificate.

The files are read when a call is made, and again whenever one of them changes, so a certificate
rotated in place (a Kubernetes secret, say) is picked up without a restart.
"""

from __future__ import annotations

import logging
import os
import ssl
from dataclasses import dataclass
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

_warned: set[str] = set()


@dataclass(frozen=True)
class TlsSettings:
    #: Path to the client certificate (PEM), which may hold its key too - empty for none.
    client_cert: str = ""
    #: Path to the client certificate's key, when it isn't in `client_cert`.
    client_key: str = ""
    #: The key's password, for an encrypted key.
    key_password: str = ""
    #: Path to the CAs to trust for the endpoint's certificate, in place of the system's - empty for the system's.
    ca_bundle: str = ""
    #: The settings' prefix (`NLSQL_LLM`, `NLSQL_EMBEDDING`), to name them in errors.
    prefix: str = "NLSQL_LLM"

    @property
    def configured(self) -> bool:
        return bool(self.client_cert or self.client_key or self.ca_bundle)


def tls_settings(config: Mapping, prefix: str, fallback: Optional[TlsSettings] = None) -> TlsSettings:
    """The `{prefix}_CLIENT_CERT`, `_CLIENT_KEY`, `_CLIENT_KEY_PASSWORD` and `_CA_BUNDLE` settings; each
    unset one is `fallback`'s. The password can also come from the environment, so it needn't sit in a file."""

    def setting(name: str, inherited: str) -> str:
        value = config.get(f"{prefix}_{name}")
        if value is None:
            value = os.environ.get(f"{prefix}_{name}") if name == "CLIENT_KEY_PASSWORD" else None
        return str(value).strip() if value else inherited

    base = fallback or TlsSettings()
    return TlsSettings(
        client_cert=setting("CLIENT_CERT", base.client_cert),
        client_key=setting("CLIENT_KEY", base.client_key),
        key_password=setting("CLIENT_KEY_PASSWORD", base.key_password),
        ca_bundle=setting("CA_BUNDLE", base.ca_bundle),
        prefix=prefix,
    )


_contexts: dict[tuple, ssl.SSLContext] = {}


def ssl_context(settings: TlsSettings) -> Optional[ssl.SSLContext]:
    """The context to make an HTTPS call with - None when nothing is set, for the default one.
    Made afresh when a file changes; raises `ValueError`, naming the setting, for a missing file, a
    key with no certificate, or a certificate and key that don't load."""
    if not settings.configured:
        return None
    if settings.client_key and not settings.client_cert:
        raise ValueError(f"{settings.prefix}_CLIENT_KEY is set but {settings.prefix}_CLIENT_CERT isn't")
    files = [
        (f"{settings.prefix}_{name}", path)
        for name, path in (("CLIENT_CERT", settings.client_cert), ("CLIENT_KEY", settings.client_key), ("CA_BUNDLE", settings.ca_bundle))
        if path
    ]
    stamps = []
    for name, path in files:
        try:
            stamps.append(os.stat(path).st_mtime_ns)
        except OSError as ex:
            raise ValueError(f"{name}: can't read {path} ({ex.strerror})") from ex
    key = (settings, tuple(stamps))
    context = _contexts.get(key)
    if context is not None:
        return context

    # A CA bundle replaces the system's trust store rather than adding to it: an in-house endpoint's
    # certificate should be trusted for being signed by the in-house CA, not by anyone the system trusts.
    try:
        context = ssl.create_default_context(cafile=settings.ca_bundle or None)
    except (OSError, ssl.SSLError) as ex:
        raise ValueError(f"{settings.prefix}_CA_BUNDLE: {settings.ca_bundle} isn't a PEM file of certificates ({ex})") from ex
    if settings.client_cert:
        try:
            context.load_cert_chain(settings.client_cert, settings.client_key or None, settings.key_password or None)
        except (OSError, ssl.SSLError) as ex:
            raise ValueError(
                f"{settings.prefix}_CLIENT_CERT: couldn't load the client certificate and key"
                f" - a PEM certificate, its key in it or in {settings.prefix}_CLIENT_KEY, and"
                f" {settings.prefix}_CLIENT_KEY_PASSWORD if the key is encrypted ({ex})"
            ) from ex
    # Keep only the current one per setting, so a rotated certificate's old context goes.
    for old in [k for k in _contexts if k[0] == settings]:
        del _contexts[old]
    _contexts[key] = context
    return context


def warn_if_unused(settings: TlsSettings, base_url: str) -> None:
    """Logs once when certificates are set for an endpoint that isn't HTTPS, where they do nothing."""
    if settings.configured and not base_url.lower().startswith("https:") and settings.prefix not in _warned:
        _warned.add(settings.prefix)
        logger.warning("%s_CLIENT_CERT / _CA_BUNDLE are set but %s_BASE_URL isn't https; they aren't used", settings.prefix, settings.prefix)
