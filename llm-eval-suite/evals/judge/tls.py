"""How the judge proves who it is, and whom it trusts, over HTTPS.

For a model server that wants a client certificate (mutual TLS), and one whose
own certificate comes from a CA the system doesn't know: an in-house gateway,
typically both. The settings mean what the plugin's NLSQL_LLM_* ones do (its
tls.py), and are paths to files the suite can read:

    JUDGE_CLIENT_CERT          the client certificate (PEM), perhaps with its key
    JUDGE_CLIENT_KEY           its key, when it is a file of its own
    JUDGE_CLIENT_KEY_PASSWORD  only for an encrypted key
    JUDGE_CA_BUNDLE            trust only these CAs for the server's certificate

Nothing set, nothing changes: the system's trust store, no client certificate.

JUDGE_HEADERS adds headers to every judge request, as Name=value pairs
separated by commas. The plugin can tell its model server which Superset user
a call is for (NLSQL_LLM_USER_HEADER); a server that expects that header gets
it from the judge with a fixed value:

    JUDGE_HEADERS=X-Superset-User-Name=llm-eval-suite

Only the openai provider uses these (judge/model.py).
"""

import logging
import re
import ssl
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple
from urllib.parse import quote

logger = logging.getLogger(__name__)

PREFIX = "JUDGE"

# What HTTP allows in a header name (RFC 9110's `token`).
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
# What the calls set themselves.
_RESERVED_HEADERS = {
    "authorization",
    "content-type",
    "content-length",
    "accept",
    "host",
    "transfer-encoding",
}
# Every printable ASCII character but `%`, left as it is in a header value.
_HEADER_SAFE = "".join(chr(code) for code in range(0x21, 0x7F) if chr(code) != "%")


@dataclass(frozen=True)
class TlsSettings:
    #: Path to the client certificate (PEM), which may hold its key too.
    client_cert: str = ""
    #: Path to the client certificate's key, when it isn't in `client_cert`.
    client_key: str = ""
    #: The key's password, for an encrypted key.
    key_password: str = ""
    #: Path to the CAs to trust for the server's certificate, in place of the
    #: system's.
    ca_bundle: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.client_cert or self.client_key or self.ca_bundle)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "TlsSettings":
        def setting(name: str) -> str:
            return (env.get(f"{PREFIX}_{name}") or "").strip()

        return cls(
            client_cert=setting("CLIENT_CERT"),
            client_key=setting("CLIENT_KEY"),
            # A password may start or end with a space.
            key_password=env.get(f"{PREFIX}_CLIENT_KEY_PASSWORD") or "",
            ca_bundle=setting("CA_BUNDLE"),
        )


def ssl_context(settings: TlsSettings) -> Optional[ssl.SSLContext]:
    """The context to make HTTPS calls with, or None when nothing is set, for
    the default one. Raises `ValueError`, naming the setting, for a missing
    file, a key with no certificate, or a certificate and key that don't load."""
    if not settings.configured:
        return None
    if settings.client_key and not settings.client_cert:
        raise ValueError(f"{PREFIX}_CLIENT_KEY is set but {PREFIX}_CLIENT_CERT isn't")
    for name, path in (
        ("CLIENT_CERT", settings.client_cert),
        ("CLIENT_KEY", settings.client_key),
        ("CA_BUNDLE", settings.ca_bundle),
    ):
        if not path:
            continue
        try:
            with open(path, "rb"):
                pass
        except OSError as ex:
            raise ValueError(
                f"{PREFIX}_{name}: can't read {path} ({ex.strerror})"
            ) from ex

    # A CA bundle replaces the system's trust store instead of adding to it:
    # an in-house server's certificate should be trusted for being signed by
    # the in-house CA, not by anyone the system trusts.
    try:
        context = ssl.create_default_context(cafile=settings.ca_bundle or None)
    except (OSError, ssl.SSLError) as ex:
        raise ValueError(
            f"{PREFIX}_CA_BUNDLE: {settings.ca_bundle} isn't a PEM file of "
            f"certificates ({ex})"
        ) from ex
    if settings.client_cert:
        try:
            context.load_cert_chain(
                settings.client_cert,
                settings.client_key or None,
                settings.key_password or None,
            )
        except (OSError, ssl.SSLError) as ex:
            raise ValueError(
                f"{PREFIX}_CLIENT_CERT: couldn't load the client certificate and "
                f"key. It needs a PEM certificate, its key in it or in "
                f"{PREFIX}_CLIENT_KEY, and {PREFIX}_CLIENT_KEY_PASSWORD if the key "
                f"is encrypted ({ex})"
            ) from ex
    return context


def warn_if_unused(settings: TlsSettings, base_url: Optional[str]) -> None:
    """Certificates do nothing for a server that isn't reached over HTTPS."""
    if settings.configured and not (base_url or "").lower().startswith("https:"):
        logger.warning(
            "%s_CLIENT_CERT / _CA_BUNDLE are set but %s_BASE_URL isn't https; "
            "they aren't used",
            PREFIX,
            PREFIX,
        )


def header_value(text: str) -> str:
    """`text` as an HTTP header value: percent-encoded UTF-8 for anything but
    printable ASCII, and `%` encoded too, as the plugin sends usernames."""
    return quote(text, safe=_HEADER_SAFE)


def parse_headers(text: str) -> Tuple[Tuple[str, str], ...]:
    """JUDGE_HEADERS as (name, value) pairs, values encoded for sending.
    Raises `ValueError` for a pair without `=`, a name HTTP doesn't allow, or
    a header the calls already send."""
    headers: Dict[str, str] = {}
    for pair in (text or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, equals, value = pair.partition("=")
        name = name.strip()
        if not equals or not name:
            raise ValueError(
                f"{PREFIX}_HEADERS: {pair!r} isn't Name=value "
                "(separate several with commas)"
            )
        if not _HEADER_NAME.match(name):
            raise ValueError(f"{PREFIX}_HEADERS: {name!r} is not a valid HTTP header name")
        if name.lower() in _RESERVED_HEADERS:
            raise ValueError(
                f"{PREFIX}_HEADERS: {name!r} is a header the judge's calls already send"
            )
        headers[name] = header_value(value.strip())
    return tuple(headers.items())
