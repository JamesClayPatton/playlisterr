#!/usr/bin/env python3
"""Who watched what, per account.

Source selection is deliberate rather than configured-by-default: Plex's own
history is always available, Tautulli's is often deeper — but on a server
where Tautulli was reinstalled last week it is catastrophically shallower.
``auto`` fetches both cheaply and keeps whichever has more plays in the
window, which is the answer the user would pick if they knew to look.
"""

import time
from dataclasses import dataclass, field

from ..log import get
from .catalog import normalize_title

log = get("history")


@dataclass
class Watched:
    """One account's viewing, resolved against the catalog."""
    account_id: str
    plays: int = 0
    # rating_keys of catalog items (shows, not episodes) they have touched
    keys: set = field(default_factory=set)
    # rating_key -> number of plays, so "watched 40 episodes of X" is visible
    play_counts: dict = field(default_factory=dict)
    # rating_key -> last viewedAt
    last_seen: dict = field(default_factory=dict)
    # titles seen in history that are no longer in the library
    unresolved: set = field(default_factory=set)

    def recent_keys(self, limit=20):
        return [k for k, _ in sorted(self.last_seen.items(),
                                     key=lambda kv: -kv[1])][:limit]

    def top_keys(self, limit=10):
        return [k for k, _ in sorted(self.play_counts.items(),
                                     key=lambda kv: -kv[1])][:limit]


def choose_source(plex, tautulli, cfg, since):
    """Return (name, plays). ``auto`` prefers whichever has more history."""
    mode = cfg["history"]["provider"]

    if mode == "plex" or not (tautulli and tautulli.configured):
        return "plex", plex.history(since)

    if mode == "tautulli":
        return "tautulli", tautulli.history(since)

    plex_plays = plex.history(since)
    try:
        taut_plays = tautulli.history(since)
    except Exception as exc:  # optional provider: never fail the run
        log.warning("tautulli unavailable, using plex history (%s)", exc)
        return "plex", plex_plays

    if len(taut_plays) > len(plex_plays):
        log.info("history: tautulli has %d plays vs plex %d — using tautulli",
                 len(taut_plays), len(plex_plays))
        return "tautulli", taut_plays
    log.info("history: plex has %d plays vs tautulli %d — using plex",
             len(plex_plays), len(taut_plays))
    return "plex", plex_plays


def collect(plex, tautulli, catalog, cfg, now=None):
    """Fetch history and fold it into one Watched per account."""
    now = now or int(time.time())
    days = cfg["recommendations"]["history_days"]
    since = now - days * 86400

    source, plays = choose_source(plex, tautulli, cfg, since)

    # Tautulli keys history by plex.tv account id; the local server (and the
    # rest of the pipeline) key the owner as "1". Without bridging the two,
    # the owner's plays land under their plex.tv id, the owner row "1" goes
    # empty and falls back to house picks, and a phantom "Account <id>" user
    # gets the owner's taste published under a nonsense name.
    owner_alias = ""
    if source == "tautulli" and plex.token:
        from ..providers.plextv import PlexTv
        owner_alias = PlexTv(plex.token).owner_account_id()
        if owner_alias:
            log.info("history: aliasing owner plex.tv id %s -> account 1",
                     owner_alias)

    by_account = {}
    unresolved_total = 0
    for play in plays:
        if not play.account_id:
            continue
        if owner_alias and play.account_id == owner_alias:
            play.account_id = "1"
        if play.viewed_at and play.viewed_at < since:
            continue
        watched = by_account.setdefault(play.account_id,
                                        Watched(account_id=play.account_id))
        watched.plays += 1

        key = _resolve(play, catalog)
        if not key:
            unresolved_total += 1
            watched.unresolved.add(play.show_title or play.title)
            continue
        watched.keys.add(key)
        watched.play_counts[key] = watched.play_counts.get(key, 0) + 1
        watched.last_seen[key] = max(watched.last_seen.get(key, 0),
                                     play.viewed_at)

    log.info("history: %d plays from %s across %d accounts over %dd "
             "(%d unresolved)", len(plays), source, len(by_account), days,
             unresolved_total)
    return source, by_account


def _resolve(play, catalog):
    """Map a play to a catalog item.

    Episodes resolve to their *show*: nobody wants a recommendation row that
    says "you watched season 3 episode 7". Plex history rows have a null
    grandparentRatingKey, so the show is found by title.
    """
    if play.type == "episode":
        if play.show_title:
            return catalog.resolve_show(play.show_title)
        return None
    if play.type == "movie":
        # The rating key is authoritative when the item is still in the
        # library; titles are the fallback for re-scanned or moved files.
        if play.rating_key in catalog.by_key:
            return play.rating_key
        return catalog.resolve_movie(play.title)
    # "clip", "track", trailers and the like are not recommendable signal.
    return None


def resolved_titles(watched, catalog, limit=None):
    keys = watched.recent_keys(limit or len(watched.last_seen))
    return [catalog.by_key[k] for k in keys if k in catalog.by_key]


def index_titles(items):
    return {normalize_title(i.title): i for i in items}
