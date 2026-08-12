#!/usr/bin/env python3
"""The nightly run, start to finish.

Shared-work-first ordering, because the expensive things are shared: the
catalog and the history are fetched exactly once and reused for every viewer,
so per-user cost is one LLM call and nothing else.

Steps 1-4 and 6-7 are deterministic. Only step 5 talks to a model, and its
output is validated against the candidate list before it is trusted.
"""

import time
from dataclasses import dataclass, field

from .. import http
from ..log import get
from ..providers import LLM, Plex, Tautulli
from . import catalog as catalog_mod
from . import history as history_mod
from . import pick as pick_mod
from . import profile as profile_mod
from . import publish as publish_mod
from . import users as users_mod
from . import playlists as playlists_mod

log = get("run")


@dataclass
class UserResult:
    user: dict
    profile: object = None
    picks: list = field(default_factory=list)
    plan: object = None
    playlist: object = None
    playlists: list = field(default_factory=list)
    mode: str = ""           # llm | mixed | fallback | house | skipped
    candidates: int = 0
    seconds: float = 0.0
    error: str = ""

    @property
    def name(self):
        return self.user["display_name"]


@dataclass
class RunResult:
    started: float
    catalog: object = None
    history_source: str = ""
    users: list = field(default_factory=list)
    results: list = field(default_factory=list)
    # The shared fallback row, planned once and shown to every thin viewer.
    house_picks: list = field(default_factory=list)
    house_plan: object = None
    disabled_users: list = field(default_factory=list)
    written: int = 0
    write_errors: list = field(default_factory=list)
    dry: bool = True
    seconds: float = 0.0

    @property
    def personalized(self):
        return [r for r in self.results if r.mode in ("llm", "mixed")]

    @property
    def house(self):
        return [r for r in self.results if r.mode == "house"]

    def summary(self):
        return {
            "at": int(self.started),
            "seconds": round(self.seconds, 1),
            "dry": self.dry,
            "history_source": self.history_source,
            "catalog_items": len(self.catalog.items) if self.catalog else 0,
            "users": len(self.results),
            "personalized": len(self.personalized),
            "house": len(self.house),
            "errors": ([f"{r.name}: {r.error}" for r in self.results
                        if r.error] + list(self.write_errors)),
            "written": self.written,
            "picks": {r.name: [
                {"title": p.item.label, "reason": p.reason, "via": p.source}
                for p in r.picks] for r in self.results},
        }


def _house_row(house_ranking, catalog, cfg, existing, generated_at):
    """The shared fallback collection: what this household actually watches.

    Built once per run, not once per thin viewer — it is a single collection
    in Plex and fifteen viewers cannot each own it.
    """
    count = cfg["recommendations"]["picks"]
    sections = publish_mod.primary_sections(None, catalog)
    pool = publish_mod.restrict(house_ranking, sections)
    picks = pick_mod.fallback(pick_mod.balanced(pool, count), None, count,
                              style="house")
    plan = publish_mod.plan(
        {"account_id": "house", "username": "house", "display_name": "House"},
        picks, catalog, cfg, existing, generated_at,
        title=cfg["recommendations"]["fallback_title"],
        label=publish_mod.house_label(cfg), house=True)
    return picks, plan


def _per_library(cfg):
    return bool(cfg["recommendations"]["per_library"])


def _row_profile(user, watched, catalog, cfg, section, account_profile):
    """The taste a single library's row should follow.

    Scoped to that library when there is enough watched there to read one,
    and to the whole account when there is not — a row built from two plays
    is worse than a row built from a taste that is at least real, and the
    blurb says which of the two happened.
    """
    rec = cfg["recommendations"]
    if rec["taste_scope"] != "library":
        return account_profile
    scoped = profile_mod.build(
        user, watched, catalog, min_plays=rec["min_plays"],
        min_titles=rec["min_titles"], section_key=section.key,
        scope=section.title)
    if scoped.plays >= rec["library_min_plays"] and scoped.genres:
        log.debug("%s/%s: taste from %d plays in this library",
                  user["username"], section.title, scoped.plays)
        return scoped
    fallback = profile_mod.build(
        user, watched, catalog, min_plays=rec["min_plays"],
        min_titles=rec["min_titles"], scope=section.title)
    fallback.scope_fallback = True
    log.debug("%s/%s: only %d plays here, using account-wide taste",
              user["username"], section.title, scoped.plays)
    return fallback


def _library_rows(plex, llm, user, profile, watched, house_ranking, catalog,
                  cfg, generated_at, house=False):
    """One playlist per library this viewer uses.

    Each library gets its own model call rather than one call split
    afterwards: a single mixed list leaves some libraries with two picks and
    no row at all, and asking per library is what makes "Kids Movies
    Playlist" reliably full.
    """
    rec = cfg["recommendations"]
    sections = publish_mod.watched_sections(watched, catalog,
                                            limit=rec["max_libraries"])
    watched_keys = watched.keys if watched else set()
    rows = []
    for section in sections:
        if section.type == "movie" and not rec["include_movies"]:
            continue
        if section.type == "show" and not rec["include_shows"]:
            continue
        in_section = [i for i in (house_ranking or catalog.items)
                      if i.section_key == section.key]
        if house:
            pool = [i for i in in_section if i.rating_key not in watched_keys]
            picks = pick_mod.fallback(pool[:rec["picks"]], profile,
                                      rec["picks"], style="house")
            row_profile = profile
        else:
            row_profile = _row_profile(user, watched, catalog, cfg, section,
                                       profile)
            pool = pick_mod.candidates(catalog, watched_keys, row_profile, cfg)
            pool = [i for i in pool if i.section_key == section.key]
            picks, _ = pick_mod.choose(llm, row_profile, pool, cfg)
        if len(picks) < publish_mod.MIN_ITEMS:
            log.debug("%s: skipping %s row, only %d picks",
                      profile.display_name, section.title, len(picks))
            continue
        title = rec["playlist_title"].format(
            library=section.title, name=profile.display_name,
            username=user["username"])
        row = playlists_mod.plan(plex, user, picks, cfg, generated_at,
                                 title=title, profile=row_profile,
                                 house=house)
        row.picks = picks
        row.considered = len(pool)
        rows.append(row)
    return rows


def _as_playlist(user, cfg):
    """Does this viewer get a playlist rather than a collection?

    Playlists are per-account, so Plex shows them only to their owner. In
    ``collection`` mode every viewer gets a server-wide library row instead.
    """
    return cfg["recommendations"]["delivery"] == "playlist"


def preview_payload(result, cfg):
    """Everything needed to publish this run later, without re-deciding it.

    Access tokens are deliberately not stored: they are credentials, and they
    can be fetched again in a second when the preview is actually applied.
    """
    rows = []
    for entry in result.results:
        for row in ([entry.playlist] if entry.playlist else entry.playlists):
            if not row or row.empty:
                continue
            rows.append({"kind": "playlist",
                         "account_id": entry.user["account_id"],
                         "username": entry.user["username"],
                         "title": row.title, "items": list(row.wanted),
                         "summary": row.summary})
    plans = [r.plan for r in result.results if r.plan and not r.plan.empty]
    if result.house_plan and not result.house_plan.empty:
        plans.append(result.house_plan)
    collections = [{
        "kind": "collection", "user": plan.user, "label": plan.label,
        "mutations": [{"action": m.action, "section_key": m.section_key,
                       "section_title": m.section_title,
                       "collection": m.collection, "detail": m.detail,
                       "items": list(m.items), "section_type": m.section_type,
                       "owner_row": m.owner_row} for m in plan.mutations],
    } for plan in plans]
    return {
        "at": int(result.started),
        "delivery": cfg["recommendations"]["delivery"],
        "rows": rows, "collections": collections,
        "users": len(result.results),
        "personalized": len(result.personalized),
        "titles": sum(len(r["items"]) for r in rows)
                  + sum(len(m["items"]) for c in collections
                        for m in c["mutations"]),
    }


def apply_preview(cfg, payload):
    """Publish a saved preview exactly as it was previewed.

    No catalog read, no history read, no model call — the decisions were
    already made. Only the user tokens are fetched again, because those were
    not written to disk.
    """
    plex, _, _ = build_clients(cfg)
    written, errors = 0, []

    rows = payload.get("rows") or []
    if rows:
        users = {u["account_id"]: u
                 for u in users_mod.discover(plex, cfg, None)}
        keep = {}
        for row in rows:
            keep.setdefault(row["account_id"], set()).add(row["title"])
        for row in rows:
            user = users.get(row["account_id"])
            if not user or not user.get("access_token"):
                errors.append(f"{row['username']}: no access token")
                continue
            plan = playlists_mod.PlaylistPlan(
                user=row["username"], account_id=row["account_id"],
                title=row["title"], action="create",
                wanted=row["items"], summary=row["summary"])
            existing = next((p for p in plex.playlists(
                token=user["access_token"]) if p["title"] == row["title"]),
                None)
            if existing:
                plan.action = "update"
                plan.existing_key = existing["rating_key"]
            try:
                written += playlists_mod.apply(plex, user, plan)
            except Exception as exc:
                errors.append(f"{row['username']} / {row['title']}: "
                              f"{http.redact(str(exc))}")
        for account_id, titles in keep.items():
            user = users.get(account_id)
            if user:
                try:
                    playlists_mod.cleanup(plex, user, titles)
                except Exception:
                    pass

    collections = payload.get("collections") or []
    if collections:
        catalog = catalog_mod.build(plex, cfg)
        existing = existing_collections(plex, catalog)
        for saved in collections:
            plan = publish_mod.Plan(user=saved["user"], label=saved["label"])
            plan.mutations = [publish_mod.Mutation(**m)
                              for m in saved["mutations"]]
            done, failed = publish_mod.apply(plex, plan, existing, cfg)
            written += done
            errors.extend(failed)

    log.info("published saved preview: %d writes, %d errors", written,
             len(errors))
    return written, errors


def build_clients(cfg):
    plex = Plex(cfg["plex"]["url"], cfg["plex"]["token"],
                timeout=cfg["plex"]["timeout"])
    llm = LLM(provider=cfg["llm"]["provider"], url=cfg["llm"]["url"],
              model=cfg["llm"]["model"], api_key=cfg["llm"]["api_key"],
              temperature=cfg["llm"]["temperature"],
              keep_alive=cfg["llm"]["keep_alive"],
              timeout=cfg["llm"]["timeout"])
    tautulli = Tautulli(cfg["tautulli"]["url"], cfg["tautulli"]["api_key"],
                        timeout=cfg["tautulli"]["timeout"])
    return plex, llm, tautulli


def existing_collections(plex, catalog, offline=False):
    """Current collection state per section, so the plan is a diff."""
    out = {}
    for section in catalog.sections:
        try:
            found = {}
            for collection in plex.collections(section.key):
                labels = collection.labels or plex.collection_labels(
                    collection.rating_key)
                collection.labels = labels
                children = plex.collection_children(collection.rating_key)
                found[collection.title] = (collection, children)
            out[section.key] = found
        except Exception as exc:
            log.warning("could not read collections in %s (%s)",
                        section.title, exc)
            out[section.key] = {}
    return out


def run(cfg, dry=True, only=None, limit=0, verbose=False,
        state=None):
    """Execute a full pass. ``dry`` never writes; M3 flips the other branch on."""
    started = time.time()
    result = RunResult(started=started, dry=dry)
    plex, llm, tautulli = build_clients(cfg)

    log.info("plex: %s", plex.ping().get("name", "?"))

    # 1-2. Shared, expensive, once.
    result.catalog = catalog_mod.build(plex, cfg)
    source, watched_by_account = history_mod.collect(
        plex, tautulli, result.catalog, cfg)
    result.history_source = source

    # 3. Who.
    all_users = users_mod.discover(plex, cfg, watched_by_account)
    result.users = all_users
    allowed, result.disabled_users = users_mod.enabled(all_users, cfg)
    selected = users_mod.select(allowed, only)
    if limit:
        selected = selected[:limit]

    house_ranking = profile_mod.house_profile(watched_by_account.values(),
                                              result.catalog)
    # The per-title veto applies to the shared house row too — a title on the
    # never-recommend list should not slip back in through House Picks.
    vetoed = pick_mod.vetoed_titles(cfg)
    if vetoed:
        house_ranking = [i for i in house_ranking
                         if catalog_mod.normalize_title(i.title) not in vetoed]
    existing = existing_collections(plex, result.catalog)
    generated_at = time.strftime("%d %b %Y", time.localtime(started))
    min_plays = cfg["recommendations"]["min_plays"]

    # One shared fallback row for everyone below the threshold, computed once.
    result.house_picks, result.house_plan = _house_row(
        house_ranking, result.catalog, cfg, existing, generated_at)
    if cfg["recommendations"]["delivery"] == "playlist":
        # Every viewer gets their own copy as a playlist instead.
        result.house_plan = None

    for user in selected:
        user_started = time.time()
        entry = UserResult(user=user)
        watched = watched_by_account.get(user["account_id"])
        try:
            entry.profile = profile_mod.build(
                user, watched, result.catalog, min_plays=min_plays,
                min_titles=cfg["recommendations"]["min_titles"])
            sections = publish_mod.primary_sections(watched, result.catalog)
            watched_keys = watched.keys if watched else set()

            if entry.profile.thin:
                # No taste to model: point them at the shared house row rather
                # than let a model invent a personality from four plays. The
                # row itself is one collection, already planned above.
                entry.mode = "house"
                entry.candidates = len(house_ranking)
                entry.plan = None
                if _per_library(cfg) and _as_playlist(user, cfg):
                    entry.playlists = _library_rows(
                        plex, llm, user, entry.profile, watched,
                        house_ranking, result.catalog, cfg, generated_at,
                        house=True)
                    entry.picks = [p for pl in entry.playlists
                                   for p in pl.picks]
                else:
                    entry.picks = result.house_picks
                    if _as_playlist(user, cfg):
                        entry.playlist = playlists_mod.plan(
                            plex, user, entry.picks, cfg, generated_at,
                            title=cfg["recommendations"]["fallback_title"],
                            profile=entry.profile, house=True)
            else:
                if _per_library(cfg) and _as_playlist(user, cfg):
                    entry.playlists = _library_rows(
                        plex, llm, user, entry.profile, watched, None,
                        result.catalog, cfg, generated_at)
                    entry.picks = [p for pl in entry.playlists
                                   for p in pl.picks]
                    entry.mode = "llm" if any(
                        p.source == "llm" for p in entry.picks) else "fallback"
                    entry.candidates = sum(pl.considered
                                           for pl in entry.playlists)
                    entry.seconds = round(time.time() - user_started, 1)
                    log.info("%-16s %-8s %2d picks across %d libraries "
                             "in %5.1fs", entry.name, entry.mode,
                             len(entry.picks), len(entry.playlists),
                             entry.seconds)
                    result.results.append(entry)
                    continue
                pool = pick_mod.candidates(result.catalog, watched_keys,
                                           entry.profile, cfg)
                pool = publish_mod.restrict(pool, sections)
                entry.candidates = len(pool)
                entry.picks, entry.mode = pick_mod.choose(
                    llm, entry.profile, pool, cfg)
                # Guarantee a publishable row in each library they use, so a
                # viewer never opens a library and finds everyone's row but
                # their own.
                entry.picks = pick_mod.enforce_split(
                    entry.picks, pool, entry.profile, cfg,
                    min_items=publish_mod.MIN_ITEMS)
                if _as_playlist(user, cfg):
                    entry.playlist = playlists_mod.plan(
                        plex, user, entry.picks, cfg, generated_at,
                        profile=entry.profile)
                else:
                    entry.plan = publish_mod.plan(
                        user, entry.picks, result.catalog, cfg, existing,
                        generated_at, profile=entry.profile)
        except Exception as exc:
            entry.error = http.redact(str(exc)) or type(exc).__name__
            entry.mode = "error"
            log.error("%s: %s", entry.name, entry.error)

        entry.seconds = round(time.time() - user_started, 1)
        log.info("%-16s %-8s %2d picks from %4d candidates in %5.1fs",
                 entry.name, entry.mode, len(entry.picks), entry.candidates,
                 entry.seconds)
        result.results.append(entry)

    # 6. Publish. Every plan is computed first so a failure halfway through
    #    the users cannot leave half a run's worth of collections written
    #    with no record of why.
    if not dry:
        # House first, personal rows after: each promotion moves its row to
        # the front, so whatever is published last ends up highest. A
        # viewer's own row should outrank the shared fallback.
        plans = []
        if result.house_plan and not result.house_plan.empty:
            plans.append(result.house_plan)
        plans += [r.plan for r in result.results
                  if r.plan and not r.plan.empty]
        for plan in plans:
            done, failed = publish_mod.apply(plex, plan, existing, cfg)
            result.written += done
            result.write_errors.extend(failed)
        for user in result.disabled_users:
            try:
                gone = playlists_mod.cleanup(plex, user, set())
                result.written += gone
                if gone:
                    log.info("removed %d row(s) from %s, who is switched off",
                             gone, user["username"])
            except Exception as exc:
                log.debug("cleanup for disabled %s: %s", user["username"], exc)
        for entry in result.results:
            # A user whose generation failed produces no rows this run. Do NOT
            # run cleanup for them: with an empty keep-set it would delete
            # every playlist they already have. Leave their existing rows in
            # place until a run succeeds for them.
            if entry.error:
                continue
            rows = [entry.playlist] if entry.playlist else entry.playlists
            try:
                playlists_mod.cleanup(
                    plex, entry.user, {r.title for r in rows if r})
            except Exception as exc:
                log.debug("playlist cleanup for %s: %s", entry.name, exc)
            for row in rows:
                if not row or row.empty:
                    continue
                try:
                    result.written += playlists_mod.apply(
                        plex, entry.user, row)
                except Exception as exc:
                    result.write_errors.append(
                        f"playlist {row.title} for {entry.name}: "
                        f"{http.redact(str(exc))}")
                    log.error("playlist %s for %s failed: %s", row.title,
                              entry.name, exc)
        log.info("published %d mutations, %d failed", result.written,
                 len(result.write_errors))

    result.seconds = time.time() - started
    return result
