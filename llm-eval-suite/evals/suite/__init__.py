"""The suite's own setup: settings, goldens and schema metadata.

Importing any of it first loads .env.eval at the root of the suite (or
EVAL_ENV_FILE) when present, so modules that read a setting when they are
imported see it. Variables already set in the environment win.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(
    os.getenv(
        "EVAL_ENV_FILE", str(Path(__file__).resolve().parents[2] / ".env.eval")
    ),
    override=False,
)
