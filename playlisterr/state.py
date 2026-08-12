#!/usr/bin/env python3
"""The small durable file: run history and safety snapshots.

Kept as one json document in the config directory rather than a database,
because it is a few kilobytes and a human should be able to read it when
something looks wrong. What lives here must genuinely survive a restart:

* **run history** — what was published, when, and by which route (model or
  fallback), which is what the UI's History page shows;
* **the saved preview** — the exact picks a dry run decided, so pressing
  publish writes what you just looked at rather than a freshly rolled set.
"""

import json
import os
import time

from .log import get

log = get("state")
VERSION = 1
MAX_RUNS = 60


class State:
    def __init__(self, path):
        self.path = path
        self.data = {"version": VERSION, "runs": [], "last_run": None}
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt state file must not stop a run; the file is a record,
            # not a dependency.
            log.warning("could not read %s (%s) — starting fresh",
                        self.path, exc)

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, self.path)

    # -- runs ------------------------------------------------------------
    def record_run(self, summary):
        summary = dict(summary)
        summary.setdefault("at", int(time.time()))
        self.data["runs"].insert(0, summary)
        del self.data["runs"][MAX_RUNS:]
        self.data["last_run"] = summary
        return summary

    @property
    def runs(self):
        return self.data.get("runs", [])

    # -- saved preview ---------------------------------------------------
    # A dry run decides real picks and then throws them away, which means
    # approving a preview and pressing publish gives you a *different* set —
    # the model is not deterministic. Keeping the preview lets "publish
    # exactly what I just looked at" mean what it says.
    def save_preview(self, payload):
        self.data["preview"] = payload

    @property
    def preview(self):
        return self.data.get("preview")

    def clear_preview(self):
        self.data.pop("preview", None)
