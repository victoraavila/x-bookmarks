"""x-bookmarks: local searchable archive of your X bookmarks."""

import os

# Keep twscrape quiet and opt out of its anonymous operation-name telemetry.
# `setdefault` so an explicit value in the environment always wins.
os.environ.setdefault("TWS_LOG_LEVEL", "WARNING")
os.environ.setdefault("TWS_TELEMETRY", "0")

__version__ = "0.1.0"
