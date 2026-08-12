#!/usr/bin/env python3
"""Logging that cannot leak a token.

Everything routes through the http module's redact(), because the most likely
way a Plex token escapes is an exception message containing the URL that
carried it.
"""

import logging
import logging.handlers
import os
import sys

from . import http

LEVELS = {"debug": logging.DEBUG, "info": logging.INFO,
          "warning": logging.WARNING, "error": logging.ERROR}


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return http.redact(super().format(record))


def setup(level="info", log_dir=None, quiet=False):
    root = logging.getLogger("playlisterr")
    root.setLevel(LEVELS.get(str(level).lower(), logging.INFO))
    root.handlers.clear()
    root.propagate = False

    fmt = RedactingFormatter("%(asctime)s %(levelname)-7s %(message)s",
                             datefmt="%H:%M:%S")
    if not quiet:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        root.addHandler(stream)

    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        # Rotate so the log cannot grow without bound on a long-running
        # container: five 1 MB files, ~5 MB total.
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "playlisterr.log"), maxBytes=1_000_000,
            backupCount=5, encoding="utf-8")
        fh.setFormatter(RedactingFormatter(
            "%(asctime)s %(levelname)-7s %(name)s %(message)s"))
        root.addHandler(fh)
    return root


def get(name="playlisterr"):
    return logging.getLogger(name if name.startswith("playlisterr")
                             else f"playlisterr.{name}")
