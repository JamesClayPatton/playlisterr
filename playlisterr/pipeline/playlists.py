#!/usr/bin/env python3
"""Per-user delivery that Plex enforces for us.

A collection is a server-wide object: everyone with access to the library sees
it, owner included. That is not a Playlisterr limitation to work around; it is
how Plex models collections.

A playlist belongs to an *account*. Created with a viewer's own token it is
visible to that viewer and to nobody else, owner included. Verified against a
live server: a playlist made as one shared user was invisible to both the
owner and a second shared user.

The catch, also measured: a playlist cannot hold a show. Adding one expands
it into every episode — a single eight-episode series became eight entries,
and a fifteen-title row would become several hundred. So a recommended show
contributes the episode that viewer should watch next, which is both the
right size and literally what the row claims to be.
"""

from dataclasses import dataclass, field

from ..log import get
from . import profile as profile_mod

log = get("playlists")


@dataclass
class PlaylistPlan:
    user: str
    account_id: str
    title: str
    action: str = "none"          # create | update | none | skip
    detail: str = ""
    add: list = field(default_factory=list)
    remove: list = field(default_factory=list)
    wanted: list = field(default_factory=list)
    summary: str = ""
    existing_key: str = ""
    picks: list = field(default_factory=list)
    considered: int = 0

    @property
    def empty(self):
        return self.action in ("none", "skip")


def resolve_items(plex, picks, token):
    """Turn picks into playlist-able rating keys.

    Movies go in as themselves. Shows resolve to the viewer's next unwatched
    episode, looked up with their own token so "unwatched" means unwatched
    *by them*.
    """
    keys, labels = [], []
    for pick in picks:
        if pick.item.type == "movie":
            keys.append(pick.item.rating_key)
            labels.append(pick.item.label)
            continue
        episode = plex.next_episode(pick.item.rating_key, token=token)
        if episode:
            keys.append(episode)
            labels.append(f"{pick.item.label} (next episode)")
        else:
            log.debug("no episode found for %s", pick.item.label)
    return keys, labels


def summary_text(picks, generated_at, profile=None, house=False):
    """Why this row exists, then each pick and the reason for it."""
    lines = [profile_mod.intro(profile, house=house), ""]
    for pick in picks:
        lines.append(f"• {pick.item.label} — {pick.reason}")
    lines.append("")
    lines.append(f"Picked for you on {generated_at} · refreshed nightly by "
                 f"Playlisterr.")
    return "\n".join(lines)


def plan(plex, user, picks, cfg, generated_at, title=None,
         profile=None, house=False):
    """What would have to change for this viewer's playlist to be right."""
    token = user.get("access_token")
    if title is None:
        title = cfg["recommendations"]["collection_title"].format(
            name=user["display_name"], username=user["username"])
    result = PlaylistPlan(user=user["display_name"],
                          account_id=user["account_id"], title=title)

    if not token:
        result.action = "skip"
        result.detail = "no access token for this user"
        return result

    keys, labels = resolve_items(plex, picks, token)
    result.wanted = keys
    result.summary = summary_text(picks, generated_at, profile, house)
    if not keys:
        result.action = "skip"
        result.detail = "nothing playable resolved"
        return result

    try:
        existing = next((p for p in plex.playlists(token=token)
                         if p["title"] == title), None)
    except Exception as exc:
        result.action = "skip"
        result.detail = f"could not read playlists: {exc}"
        return result

    if not existing:
        result.action = "create"
        result.detail = f"{len(keys)} items"
        return result

    result.existing_key = existing["rating_key"]
    current = plex.playlist_items(existing["rating_key"], token=token)
    if current == keys:
        result.action = "none"
        result.detail = "already correct"
        return result
    result.action = "update"
    result.detail = f"{len(keys)} items (was {len(current)})"
    return result


def apply(plex, user, result):
    """Execute one viewer's playlist plan, as that viewer."""
    token = user.get("access_token")
    if result.action in ("none", "skip"):
        return 0

    if result.action == "update" and result.existing_key:
        # Replacing beats reconciling: playlist item ids are per-playlist
        # rather than per-item, so removing precisely is fiddlier than making
        # the thing again, and the row is regenerated nightly anyway.
        plex.delete_playlist(result.existing_key, token=token)

    key = plex.create_playlist(result.title, result.wanted, token=token)
    if not key:
        raise RuntimeError(f"could not create playlist {result.title!r}")
    try:
        plex.set_playlist_summary(key, result.summary, token=token)
    except Exception as exc:
        log.debug("summary not set on %s: %s", result.title, exc)
    return 1


SIGNATURE = "refreshed nightly by Playlisterr"


def cleanup(plex, user, keep_titles):
    """Delete Playlisterr playlists this viewer should no longer have.

    Rows are identified by the signature Playlisterr writes into every summary,
    not by their title — renaming the scheme (one row per person becoming one
    row per library, say) must not strand the old rows in someone's account
    forever, and a playlist the viewer made themselves must never be touched.
    """
    token = user.get("access_token")
    if not token:
        return 0
    removed = 0
    for playlist in plex.playlists(token=token):
        if playlist["title"] in keep_titles:
            continue
        # Case-insensitive: earlier versions wrote "Refreshed nightly by
        # Playlisterr", and a rename must still be able to clean those up.
        if SIGNATURE.lower() not in (playlist.get("summary") or "").lower():
            continue
        plex.delete_playlist(playlist["rating_key"], token=token)
        log.info("removed stale playlist %r from %s", playlist["title"],
                 user["username"])
        removed += 1
    return removed


def render(result):
    if result.action == "none":
        return [f"  PLAYLIST {result.user:<18} already correct"]
    if result.action == "skip":
        return [f"  SKIP     {result.user:<18} {result.detail}"]
    return [f"  PLAYLIST {result.action.upper():<7} \"{result.title}\" "
            f"for {result.user} — {result.detail}"]
