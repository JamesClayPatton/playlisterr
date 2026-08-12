#!/usr/bin/env python3
"""The candidate universe: everything on the server, once per run.

Pulled once and shared by every user, because a 2,000-title library takes two
seconds to fetch and 22 users would otherwise refetch it 22 times.

Also home to the title index that repairs Plex's history data: history rows
for episodes carry ``grandparentTitle`` but a null ``grandparentRatingKey``,
so "they watched an episode of Dark" can only become "they have watched Dark"
by matching on a normalized title.
"""

import re
from dataclasses import dataclass, field

from ..log import get

log = get("catalog")

_ARTICLES = ("the ", "a ", "an ")
_PUNCT = re.compile(r"[^a-z0-9]+")


def normalize_title(title):
    """A key that survives punctuation, case and a leading article."""
    text = _PUNCT.sub(" ", (title or "").lower()).strip()
    for article in _ARTICLES:
        if text.startswith(article):
            text = text[len(article):]
            break
    return " ".join(text.split())


@dataclass
class Catalog:
    items: list = field(default_factory=list)
    by_key: dict = field(default_factory=dict)
    # normalized title -> rating_key, for episode->show repair
    show_index: dict = field(default_factory=dict)
    movie_index: dict = field(default_factory=dict)
    sections: list = field(default_factory=list)
    skipped_sections: list = field(default_factory=list)

    @property
    def movies(self):
        return [i for i in self.items if i.type == "movie"]

    @property
    def shows(self):
        return [i for i in self.items if i.type == "show"]

    def resolve_show(self, title):
        return self.show_index.get(normalize_title(title))

    def resolve_movie(self, title):
        return self.movie_index.get(normalize_title(title))


def select_sections(sections, cfg):
    """Which libraries to draw from, chosen by title — never by section key.

    Section keys differ on every server and mean nothing to a human, so the
    config names libraries the way Plex's own UI does. An empty list means
    "all libraries of that type", which is what a fresh install wants.
    """
    rec = cfg["recommendations"]
    plex_cfg = cfg["plex"]
    wanted_movie = {t.lower() for t in plex_cfg["movie_libraries"]}
    wanted_show = {t.lower() for t in plex_cfg["show_libraries"]}
    excluded = {t.lower() for t in plex_cfg["exclude_libraries"]}

    chosen, skipped = [], []
    for section in sections:
        title = section.title.lower()
        if section.type not in ("movie", "show"):
            skipped.append((section, "not a movie or show library"))
            continue
        if title in excluded:
            skipped.append((section, "excluded in config"))
            continue
        if section.type == "movie":
            if not rec["include_movies"]:
                skipped.append((section, "movies disabled"))
                continue
            if wanted_movie and title not in wanted_movie:
                skipped.append((section, "not in plex.movie_libraries"))
                continue
        else:
            if not rec["include_shows"]:
                skipped.append((section, "shows disabled"))
                continue
            if wanted_show and title not in wanted_show:
                skipped.append((section, "not in plex.show_libraries"))
                continue
        chosen.append(section)

    missing = (wanted_movie | wanted_show) - {s.title.lower() for s in sections}
    for name in sorted(missing):
        log.warning("configured library %r does not exist on this server",
                    name)
    return chosen, skipped


def build(plex, cfg):
    """Fetch every selected library and index it."""
    sections, skipped = select_sections(plex.sections(), cfg)
    if not sections:
        raise RuntimeError(
            "no usable libraries — check plex.movie_libraries / "
            "plex.show_libraries in settings.json")

    catalog = Catalog(sections=sections, skipped_sections=skipped)
    for section in sections:
        items = plex.items(section)
        catalog.items.extend(items)
        for item in items:
            catalog.by_key[item.rating_key] = item
            index = catalog.show_index if item.type == "show" \
                else catalog.movie_index
            # First writer wins: duplicates across libraries (the same show in
            # "TV Shows" and "Old Shows") should resolve to one of them, and
            # which one does not matter for "have they watched it".
            index.setdefault(normalize_title(item.title), item.rating_key)
        log.info("library %-24s %5d %s", section.title, len(items),
                 section.type + "s")

    log.info("catalog: %d movies, %d shows across %d libraries",
             len(catalog.movies), len(catalog.shows), len(sections))
    return catalog
