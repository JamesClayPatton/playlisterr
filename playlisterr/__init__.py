"""Playlisterr — personalized Plex recommendation rows from a local LLM."""

import os

# The base version lives in pyproject.toml; kept in sync here for the many
# call sites that read it. A build can stamp the exact release/commit via the
# PLAYLISTERR_VERSION environment variable (the Docker image does this), so an
# installed container reports what it actually is rather than a static string.
__version__ = os.environ.get("PLAYLISTERR_VERSION") or "0.1.0"
