#!/usr/bin/env python3
"""Plex Media Server — the only required provider.

Everything Playlisterr needs comes from the server itself:

* the catalog, with genres, ratings and overviews, from bulk section listings
  (a 1,560-item movie library answers in ~2 seconds);
* per-user watch history from ``/status/sessions/history/all``, which carries
  ``accountID`` on every row — no Tautulli required;
* collections and labels, the publishing surface.

One wrinkle worth knowing: history rows for episodes have a null
``grandparentRatingKey``, so an episode play cannot be mapped to its show by
key. ``grandparentTitle`` is present, which is why the pipeline builds a
title index over the show libraries.
"""

import json
import re
from dataclasses import dataclass, field

from .. import http
from ..log import get

log = get("plex")

# Plex's own type numbers, used when filtering a section listing.
TYPE_MOVIE = 1
TYPE_SHOW = 2


@dataclass
class Section:
    key: str
    type: str          # "movie" | "show" | "artist" | ...
    title: str


@dataclass
class Item:
    """One recommendable thing: a movie, or a show as a whole."""
    rating_key: str
    type: str
    title: str
    year: int = 0
    genres: list = field(default_factory=list)
    rating: float = 0.0
    summary: str = ""
    section_key: str = ""
    section_title: str = ""
    content_rating: str = ""
    duration_min: int = 0
    added_at: int = 0

    @property
    def label(self):
        return f"{self.title} ({self.year})" if self.year else self.title


@dataclass
class Play:
    account_id: str
    type: str            # "movie" | "episode"
    title: str
    show_title: str
    rating_key: str
    section_key: str
    viewed_at: int


@dataclass
class Collection:
    rating_key: str
    title: str
    section_key: str
    child_count: int = 0
    labels: list = field(default_factory=list)
    summary: str = ""


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class Plex:
    def __init__(self, url, token, timeout=60):
        if not url:
            raise ValueError("plex url is required")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        http.register_secret(token)
        self._machine_id = None

    # -- plumbing --------------------------------------------------------
    def _get(self, path, params=None, timeout=None, token=None):
        url = http.url_join(self.url, path,
                            dict(params or {},
                                 **{"X-Plex-Token": token or self.token}))
        return http.get_json(url, timeout=timeout or self.timeout)

    def _text(self, path, params=None):
        url = http.url_join(self.url, path,
                            dict(params or {}, **{"X-Plex-Token": self.token}))
        return http.get_text(url, timeout=self.timeout)

    def _write(self, path, method, params=None, token=None):
        url = http.url_join(self.url, path,
                            dict(params or {},
                                 **{"X-Plex-Token": token or self.token}))
        return http.request(url, method=method, timeout=self.timeout,
                            retries=1)

    def _container(self, path, params=None, page=1000, timeout=None):
        """Walk a paged MediaContainer, yielding Metadata dicts."""
        start, total = 0, None
        while True:
            page_params = dict(params or {})
            page_params["X-Plex-Container-Start"] = start
            page_params["X-Plex-Container-Size"] = page
            data = self._get(path, page_params, timeout=timeout)
            container = data.get("MediaContainer", {})
            batch = container.get("Metadata", []) or []
            if total is None:
                total = int(container.get("totalSize",
                                          container.get("size", len(batch))))
            for row in batch:
                yield row
            start += len(batch)
            if not batch or start >= total:
                return

    # -- identity --------------------------------------------------------
    def machine_identifier(self):
        if self._machine_id is None:
            xml = self._text("/identity")
            match = re.search(r'machineIdentifier="([^"]+)"', xml)
            if not match:
                raise http.HttpError("no machineIdentifier in /identity")
            self._machine_id = match.group(1)
        return self._machine_id

    def server_name(self):
        data = self._get("/")
        return data.get("MediaContainer", {}).get("friendlyName", "Plex")

    def ping(self):
        """Cheap reachability + auth check for the Test button."""
        data = self._get("/")
        mc = data.get("MediaContainer", {})
        return {"name": mc.get("friendlyName", "Plex"),
                "version": mc.get("version", "?"),
                "platform": mc.get("platform", "?")}

    def accounts(self):
        """Home/managed accounts known to the server, including the owner."""
        data = self._get("/accounts")
        out = []
        for row in data.get("MediaContainer", {}).get("Account", []) or []:
            if str(row.get("id")) == "0" or not row.get("name"):
                continue
            out.append({"account_id": str(row.get("id")),
                        "username": row.get("name", "")})
        return out

    # -- catalog ---------------------------------------------------------
    def sections(self):
        data = self._get("/library/sections")
        return [Section(key=str(d["key"]), type=d.get("type", ""),
                        title=d.get("title", ""))
                for d in data.get("MediaContainer", {}).get("Directory", [])]

    def items(self, section, summary_max=400):
        """Every movie or show in a section, as recommendable Items."""
        want = TYPE_MOVIE if section.type == "movie" else TYPE_SHOW
        out = []
        for row in self._container(f"/library/sections/{section.key}/all",
                                   {"type": want}, timeout=180):
            summary = (row.get("summary") or "").strip()
            if len(summary) > summary_max:
                summary = summary[:summary_max].rsplit(" ", 1)[0] + "…"
            # Plex exposes both a critic rating and an audience rating; either
            # is a usable quality signal and many items only carry one.
            rating = _num(row.get("rating")) or _num(row.get("audienceRating"))
            out.append(Item(
                rating_key=str(row.get("ratingKey")),
                type="movie" if section.type == "movie" else "show",
                title=row.get("title", ""),
                year=int(row.get("year") or 0),
                genres=[g.get("tag", "") for g in row.get("Genre", []) or []
                        if g.get("tag")],
                rating=rating,
                summary=summary,
                section_key=section.key,
                section_title=section.title,
                content_rating=row.get("contentRating", "") or "",
                duration_min=int((row.get("duration") or 0) / 60000),
                added_at=int(row.get("addedAt") or 0),
            ))
        log.debug("section %s (%s): %d items", section.title, section.key,
                  len(out))
        return out

    # -- history ---------------------------------------------------------
    def history(self, since_epoch=0):
        """Every play on the server, newest first, one row per view.

        ``accountID`` is what makes this per-user. The owner is account 1.
        """
        out = []
        params = {"sort": "viewedAt:desc"}
        if since_epoch:
            # Plex accepts a viewedAt filter, but silently ignores it on some
            # versions — so filter client-side too and stop paging early.
            params["viewedAt>"] = int(since_epoch)
        for row in self._container("/status/sessions/history/all", params,
                                   page=1000, timeout=180):
            viewed = int(row.get("viewedAt") or 0)
            if since_epoch and viewed < since_epoch:
                break
            out.append(Play(
                account_id=str(row.get("accountID", "")),
                type=row.get("type", ""),
                title=row.get("title", "") or "",
                show_title=row.get("grandparentTitle", "") or "",
                rating_key=str(row.get("ratingKey", "")),
                section_key=str(row.get("librarySectionID", "")),
                viewed_at=viewed,
            ))
        log.debug("history: %d plays", len(out))
        return out

    # -- collections (read) ----------------------------------------------
    def collections(self, section_key):
        data = self._get(f"/library/sections/{section_key}/collections")
        out = []
        for row in data.get("MediaContainer", {}).get("Metadata", []) or []:
            out.append(Collection(
                rating_key=str(row.get("ratingKey")),
                title=row.get("title", ""),
                section_key=str(section_key),
                child_count=int(row.get("childCount") or 0),
                summary=row.get("summary", "") or "",
                labels=[la.get("tag", "")
                        for la in row.get("Label", []) or []],
            ))
        return out

    def collection_children(self, rating_key):
        return [str(r.get("ratingKey"))
                for r in self._container(
                    f"/library/collections/{rating_key}/children")]

    def collection_labels(self, rating_key):
        """Labels are not always inlined in the section listing."""
        data = self._get(f"/library/metadata/{rating_key}")
        rows = data.get("MediaContainer", {}).get("Metadata", []) or []
        if not rows:
            return []
        return [la.get("tag", "") for la in rows[0].get("Label", []) or []]

    # -- collections (write) ---------------------------------------------
    # Nothing here is called before M3; the pipeline plans mutations and the
    # dry-run printer shows them. Kept beside the reads so the API surface
    # lives in one file.
    def create_collection(self, section_key, title, rating_keys,
                          section_type="movie"):
        """Create a collection.

        ``type`` must match the library: a movie-typed collection created in
        a show library is accepted and then silently contains nothing, which
        is a much harder failure to spot than an error would have been.
        """
        machine = self.machine_identifier()
        uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (
            machine, ",".join(rating_keys))
        self._write("/library/collections", "POST", {
            "type": TYPE_MOVIE if section_type == "movie" else TYPE_SHOW,
            "title": title,
            "smart": 0, "sectionId": section_key, "uri": uri})

    def add_to_collection(self, collection_key, section_key, rating_keys):
        machine = self.machine_identifier()
        uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (
            machine, ",".join(rating_keys))
        self._write(f"/library/collections/{collection_key}/items", "PUT",
                    {"uri": uri})

    def remove_from_collection(self, collection_key, rating_key):
        self._write(f"/library/collections/{collection_key}/children/"
                    f"{rating_key}", "DELETE")

    def delete_collection(self, collection_key):
        self._write(f"/library/collections/{collection_key}", "DELETE")

    def set_collection_field(self, section_key, collection_key, field_name,
                             value):
        self._write("/library/sections/%s/all" % section_key, "PUT", {
            "type": 18,  # collection
            "id": collection_key,
            f"{field_name}.value": value,
            f"{field_name}.locked": 1})

    # -- playlists --------------------------------------------------------
    # Playlists belong to an *account*, which collections do not. That single
    # difference is what makes them the only way to give one viewer a row
    # nobody else — including the server owner — can see. Every call here
    # takes the viewer's own access token and acts as them.
    def playlists(self, token=None):
        data = self._get("/playlists", token=token)
        out = []
        for row in data.get("MediaContainer", {}).get("Metadata", []) or []:
            out.append({"rating_key": str(row.get("ratingKey")),
                        "title": row.get("title", ""),
                        "items": int(row.get("leafCount") or 0),
                        "summary": row.get("summary", "") or ""})
        return out

    def playlist_items(self, rating_key, token=None):
        data = self._get(f"/playlists/{rating_key}/items", token=token)
        return [str(r.get("ratingKey"))
                for r in data.get("MediaContainer", {}).get("Metadata", [])
                or []]

    def create_playlist(self, title, rating_keys, token=None):
        machine = self.machine_identifier()
        uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (
            machine, ",".join(rating_keys))
        data = self._write("/playlists", "POST",
                           {"type": "video", "title": title, "smart": 0,
                            "uri": uri}, token=token)
        try:
            payload = json.loads(data)
            return str(payload["MediaContainer"]["Metadata"][0]["ratingKey"])
        except (ValueError, KeyError, IndexError):
            for playlist in self.playlists(token=token):
                if playlist["title"] == title:
                    return playlist["rating_key"]
        return ""

    def delete_playlist(self, rating_key, token=None):
        self._write(f"/playlists/{rating_key}", "DELETE", token=token)

    def set_playlist_summary(self, rating_key, summary, token=None):
        self._write(f"/playlists/{rating_key}", "PUT", {"summary": summary},
                    token=token)

    def next_episode(self, show_rating_key, token=None):
        """The episode this viewer should watch next in a show.

        A playlist cannot hold a show — adding one expands it into every
        episode it has, which turns a fifteen-title row into several hundred
        entries. One episode per recommendation keeps the row the right size
        and makes it literally a watch-next list.
        """
        try:
            data = self._get(f"/library/metadata/{show_rating_key}/allLeaves",
                             token=token)
        except http.HttpError:
            return None
        episodes = data.get("MediaContainer", {}).get("Metadata", []) or []
        for episode in episodes:
            if not int(episode.get("viewCount") or 0):
                return str(episode.get("ratingKey"))
        return str(episodes[0].get("ratingKey")) if episodes else None

    def collection_visibility(self, section_key, rating_key):
        """Which hubs a collection is currently promoted to."""
        data = self._get(f"/hubs/sections/{section_key}/manage",
                         {"metadataItemId": rating_key})
        hubs = data.get("MediaContainer", {}).get("Hub", []) or []
        if not hubs:
            return {}
        hub = hubs[0]
        return {"recommended": bool(hub.get("promotedToRecommended")),
                "own_home": bool(hub.get("promotedToOwnHome")),
                "shared_home": bool(hub.get("promotedToSharedHome"))}

    def promote_collection(self, section_key, rating_key, recommended=True,
                           own_home=True, shared_home=True):
        """Put the collection on the library and home rows.

        Creating a collection only files it under the library's Collections
        tab, where nobody looks. Appearing as a category row in the library,
        or on a home screen, is a separate piece of state Plex calls hub
        promotion — and the reason a freshly published collection otherwise
        seems not to exist.
        """
        params = {
            "metadataItemId": rating_key,
            "promotedToRecommended": int(bool(recommended)),
            "promotedToOwnHome": int(bool(own_home)),
            "promotedToSharedHome": int(bool(shared_home))}
        # POST creates the hub record; PUT only edits one that already
        # exists and 404s otherwise. For a collection Playlisterr just made there
        # is never an existing record, so POST first.
        try:
            self._write(f"/hubs/sections/{section_key}/manage", "POST", params)
        except http.HttpError:
            self._write(f"/hubs/sections/{section_key}/manage", "PUT", params)

    def hub_identifier(self, section_key, rating_key):
        """How Plex names the row a collection produces on the library page."""
        return f"custom.collection.{section_key}.{rating_key}"

    def move_hub(self, section_key, identifier, after=None):
        """Reorder a row on the library's Recommended page.

        A promoted collection lands at the *bottom*, below ten built-in rows
        like "Recently Watched" — far enough down that nobody scrolls to it,
        which makes a working feature look broken. Omitting ``after`` moves
        the row to the front, which in practice means directly beneath
        Continue Watching (that hub is not user-manageable and always leads).
        """
        params = {}
        if after:
            params["after"] = after
        self._write(f"/hubs/sections/{section_key}/manage/{identifier}/move",
                    "PUT", params)

    def managed_hubs(self, section_key):
        """Rows on this library page that can be reordered or hidden."""
        data = self._get(f"/hubs/sections/{section_key}/manage")
        return [{"title": h.get("title", ""),
                 "identifier": h.get("identifier", ""),
                 "promoted": bool(h.get("promotedToRecommended"))}
                for h in data.get("MediaContainer", {}).get("Hub", []) or []]

    def add_collection_label(self, section_key, collection_key, label):
        self._write("/library/sections/%s/all" % section_key, "PUT", {
            "type": 18, "id": collection_key,
            "label[0].tag.tag": label, "label.locked": 1})
