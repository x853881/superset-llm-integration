"""Entry point for the nltosqlandback extension backend.

Superset auto-discovers this module and imports it on startup. Importing
the API class below triggers the ``@api`` decorator, which registers the
extension's REST endpoints with Superset. It also logs the settings in effect
(see ``settings_report``) and starts the scheduled read of every database, when a system user is
configured (see ``system_reads``).
"""

import logging

from . import settings_report, system_reads
from .api import NlToSqlAndBackAPI  # noqa: F401

logging.getLogger(__name__).info("AI Assistant backend loaded: %s", settings_report.summary())
system_reads.start_timer()
