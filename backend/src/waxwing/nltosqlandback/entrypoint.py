"""Entry point for the nltosqlandback extension backend.

Superset auto-discovers this module and imports it on startup. Importing
the API class below triggers the ``@api`` decorator, which registers the
extension's REST endpoints with Superset.
"""

from .api import NlToSqlAndBackAPI  # noqa: F401
