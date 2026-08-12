#!/usr/bin/env python3
"""A taste profile, computed without an LLM.

This is deliberately cheap arithmetic. The model's job is matching a taste to
a catalog, not inferring the taste — handing it a summary instead of 500 raw
play rows keeps the prompt small, keeps the run fast, and means a bad model
can only pick badly, not hallucinate a personality.
"""

from dataclasses import dataclass, field

from ..log import get

log = get("profile")

# A show watched this many times is a commitment, not a sample.
BINGE_PLAYS = 5


@dataclass
class Profile:
    account_id: str
    username: str
    display_name: str
    plays: int = 0
    distinct: int = 0
    genres: list = field(default_factory=list)      # [(genre, weight)]
    decades: list = field(default_factory=list)     # [(decade, count)]
    movie_plays: int = 0
    show_plays: int = 0
    avg_rating: float = 0.0
    favourites: list = field(default_factory=list)  # Items, most-played first
    recent: list = field(default_factory=list)      # Items, newest first
    binged: list = field(default_factory=list)      # show Items
    thin: bool = False
    # Set when this profile describes one library rather than the whole
    # account, which is how a Kids Movies row gets picked from kids viewing
    # instead of from whatever the grown-ups watch.
    scope: str = ""
    scope_fallback: bool = False

    @property
    def movie_share(self):
        total = self.movie_plays + self.show_plays
        return (self.movie_plays / total) if total else 0.5

    @property
    def top_genres(self):
        return [g for g, _ in self.genres[:6]]

    def as_prompt(self):
        """The human-readable block handed to the model."""
        lines = [f"Viewer: {self.display_name}",
                 f"Plays in window: {self.plays} across {self.distinct} "
                 f"distinct titles "
                 f"({self.movie_plays} movie / {self.show_plays} TV)"]
        if self.genres:
            top = ", ".join(f"{g} ({w})" for g, w in self.genres[:8])
            lines.append(f"Favourite genres by watch weight: {top}")
        if self.decades:
            dec = ", ".join(f"{d}s ({c})" for d, c in self.decades[:4])
            lines.append(f"Eras watched: {dec}")
        if self.avg_rating:
            lines.append(f"Average critic rating of what they watch: "
                         f"{self.avg_rating:.1f}/10")
        if self.binged:
            lines.append("Shows they stuck with: " +
                         ", ".join(i.label for i in self.binged[:8]))
        if self.favourites:
            lines.append("Most-played titles: " +
                         ", ".join(i.label for i in self.favourites[:10]))
        if self.recent:
            lines.append("Watched most recently: " +
                         ", ".join(i.label for i in self.recent[:10]))
        return "\n".join(lines)


def build(user, watched, catalog, min_plays=10, min_titles=3,
          section_key=None, scope=""):
    """Fold one account's watch history into a Profile.

    ``section_key`` narrows it to a single library. One Plex account is very
    often several people — a parent's login with the kids watching on it —
    so a profile built from everything produces a Kids Movies row chosen by
    grown-up taste. Scoping the analysis to the library the row lives in
    fixes that without needing to know who is really holding the remote.
    """
    profile = Profile(
        account_id=user["account_id"],
        username=user["username"],
        display_name=user["display_name"],
        plays=watched.plays if watched else 0,
        distinct=len(watched.keys) if watched else 0,
    )
    if not watched or not watched.keys:
        profile.thin = True
        return profile

    profile.scope = scope
    genre_weight, decade_count = {}, {}
    ratings = []
    counted = 0
    for key, count in watched.play_counts.items():
        item = catalog.by_key.get(key)
        if not item:
            continue
        if section_key and item.section_key != section_key:
            continue
        counted += count
        if item.type == "movie":
            profile.movie_plays += count
        else:
            profile.show_plays += count
        # Weight by plays but sub-linearly: forty episodes of one show should
        # not drown out every movie the person watched.
        weight = 1 + min(count - 1, 9) * 0.3
        for genre in item.genres:
            genre_weight[genre] = genre_weight.get(genre, 0) + weight
        if item.year:
            decade = (item.year // 10) * 10
            decade_count[decade] = decade_count.get(decade, 0) + 1
        if item.rating:
            ratings.append(item.rating)

    profile.genres = sorted(((g, round(w, 1)) for g, w in genre_weight.items()),
                            key=lambda gw: -gw[1])
    profile.decades = sorted(decade_count.items(), key=lambda dc: -dc[1])
    profile.avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else 0

    def here(key):
        item = catalog.by_key.get(key)
        return item and (not section_key or item.section_key == section_key)

    profile.favourites = [catalog.by_key[k] for k in watched.top_keys(60)
                          if here(k)][:12]
    profile.recent = [catalog.by_key[k] for k in watched.recent_keys(60)
                      if here(k)][:12]
    profile.binged = [catalog.by_key[k] for k, c in
                      sorted(watched.play_counts.items(), key=lambda kv: -kv[1])
                      if c >= BINGE_PLAYS and here(k)
                      and catalog.by_key[k].type == "show"][:10]

    if section_key:
        # Only what happened in this library counts towards the threshold.
        profile.plays = counted
        profile.distinct = len([k for k in watched.keys if here(k)])
    profile.thin = (profile.plays < min_plays
                    or profile.distinct < min_titles)
    return profile


def _join(names):
    """"a, b and c" — the way a person would write a list."""
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def intro(profile, house=False):
    """One or two sentences saying why this row looks the way it does.

    A row of titles with no explanation reads like an advert. Naming the
    shows someone actually finished makes it obvious the list came from
    *their* viewing and not from a generic popularity chart — and it is the
    difference between "here are some films" and "we were paying attention".

    Everything cited here is real: the titles come from their own play
    counts, so this can never claim they watched something they did not.
    """
    # Keyed on actually being the house row, not on the profile being thin.
    # A library-scoped profile is often "thin" by the account-wide threshold
    # and still perfectly real — seven plays in Kids Movies is a taste for
    # kids' films, and telling that viewer they have not watched enough while
    # handing them a personalised row is simply untrue.
    if house or profile is None:
        return ("You haven't watched quite enough yet for us to know your "
                "taste, so this row is what everyone on this server has been "
                "watching most. Once you've seen a few more things, it will "
                "turn into a list picked just for you.")

    # Prefer shows they stuck with, then what they play most, then what they
    # watched last — strongest evidence first.
    seeds = [item.title for item in
             (profile.binged or profile.favourites or profile.recent)][:3]
    genres = [g.lower() for g in profile.top_genres[:2]]

    if profile.scope and profile.scope_fallback:
        tail = (f" — {_join(seeds)}" if seeds else "")
        return (f"There is not much watched in {profile.scope} yet, so this "
                f"row follows your taste across the whole server{tail}. "
                f"As more gets watched in {profile.scope}, the row will "
                f"start following that instead.")

    parts = []
    if seeds:
        where = f" in {profile.scope}" if profile.scope else ""
        parts.append(f"Because{where} you've been watching {_join(seeds)}")
    if genres:
        lead = "and you tend to go for" if parts else "Because you tend to go for"
        parts.append(f"{lead} {_join(genres)}")
    if not parts:
        return ("Picked from what you've been watching lately — all of it "
                "unwatched and already on this server.")
    return (", ".join(parts)
            + ", we thought these might be a good match. Everything here is "
              "already on the server and you haven't seen it yet.")


def house_profile(all_watched, catalog, limit=400):
    """Server-wide popularity, for users too new to have a taste.

    Everybody's plays pooled, so the fallback row is "what this house
    watches" rather than "what some algorithm thinks is popular".
    """
    counts = {}
    for watched in all_watched:
        for key, count in watched.play_counts.items():
            counts[key] = counts.get(key, 0) + count
    ranked = [catalog.by_key[k] for k, _ in
              sorted(counts.items(), key=lambda kv: -kv[1])
              if k in catalog.by_key]
    log.debug("house profile: %d titles with plays", len(ranked))
    return ranked[:limit]
