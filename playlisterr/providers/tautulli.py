#!/usr/bin/env python3
"""Tautulli — optional history provider.

Plex's own history is enough and is always present, so Tautulli is not
required. It is worth supporting anyway: plenty of installs have years of
Tautulli history against a Plex database that was reset at some point, and
Tautulli knows the *watched percentage* of each play, which distinguishes
"finished it" from "bailed after ten minutes".

``history.provider: auto`` picks whichever source actually has more history
in the window, which is the right answer without asking the user to know.
"""

from .. import http
from ..log import get
from .plex import Play

log = get("tautulli")


class Tautulli:
    def __init__(self, url, api_key, timeout=60):
        self.url = (url or "").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        http.register_secret(api_key)

    @property
    def configured(self):
        return bool(self.url and self.api_key)

    def _cmd(self, command, **params):
        url = http.url_join(self.url, "/api/v2",
                            dict(params, apikey=self.api_key, cmd=command))
        data = http.get_json(url, timeout=self.timeout)
        response = data.get("response", {})
        if response.get("result") != "success":
            raise http.HttpError(
                f"tautulli {command}: {response.get('message', 'failed')}")
        return response.get("data")

    def ping(self):
        users = self._cmd("get_users") or []
        total = (self._cmd("get_history", length=1) or {}).get("recordsTotal", 0)
        return {"users": len(users), "history_records": int(total or 0)}

    def history(self, since_epoch=0, page=1000, max_rows=50000):
        """Plays as plex.Play rows, so both providers are interchangeable."""
        out, start = [], 0
        while start < max_rows:
            data = self._cmd("get_history", length=page, start=start,
                             order_column="date", order_dir="desc")
            rows = (data or {}).get("data", []) or []
            if not rows:
                break
            stop = False
            for row in rows:
                viewed = int(row.get("date") or 0)
                if since_epoch and viewed < since_epoch:
                    stop = True
                    break
                out.append(Play(
                    account_id=str(row.get("user_id", "")),
                    type=row.get("media_type", ""),
                    title=row.get("title", "") or "",
                    show_title=row.get("grandparent_title", "") or "",
                    rating_key=str(row.get("rating_key", "")),
                    section_key=str(row.get("section_id", "")),
                    viewed_at=viewed,
                ))
            if stop or len(rows) < page:
                break
            start += len(rows)
        log.debug("tautulli history: %d plays", len(out))
        return out
