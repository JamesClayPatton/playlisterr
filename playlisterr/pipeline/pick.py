#!/usr/bin/env python3
"""Choose the titles, with the model on a short leash.

The model never names a title. It is given a numbered candidate list and may
only return numbers from it, which makes hallucination a validation failure
rather than a recommendation for a film nobody owns. Small integers also
survive tokenization far better than Plex rating keys, which models love to
"correct".

Three layers, in order: the model's picks, a re-ask if too few survived
validation, then a rule-based ranking that always produces a usable row. A
nightly job that sometimes publishes nothing is worse than one that
occasionally publishes an obvious pick.
"""

import re
from dataclasses import dataclass

from ..log import get
from ..providers.llm import LLMError
from .catalog import normalize_title

log = get("pick")


def vetoed_titles(cfg):
    """The set of normalized titles the user has said never to recommend."""
    return {normalize_title(t)
            for t in cfg["recommendations"].get("exclude_titles", []) if t}

SYSTEM = (
    "You are a film and television curator for one household's private media "
    "server. You are given one viewer's taste profile and a numbered list of "
    "titles that household already owns and that the viewer has not watched. "
    "Choose the titles this specific viewer is most likely to enjoy next.\n"
    "Rules:\n"
    "- Only ever return numbers that appear in the candidate list.\n"
    "- Never invent a title, and never return a title not in the list.\n"
    "- Prefer variety: do not return several near-identical titles.\n"
    "- Echo the exact title of each pick so your choice can be checked.\n"
    "- Each reason must be ONE sentence under 140 characters, addressed to "
    "the viewer as 'you'.\n"
    "- A reason must say why THIS VIEWER will like it, not what the title is "
    "about. Do not summarise the plot.\n"
    "- You may only cite genres and titles that appear in the profile you are "
    "given. Never claim the viewer watched something that is not listed "
    "there — inventing viewing history is the worst thing you can do here.\n"
    "- Vary the reasons. Repeating one sentence across several picks is a "
    "failed answer.\n"
    "- Respond with JSON only, in exactly this form:\n"
    '{"picks": [{"id": <number>, "title": "<title as written in the list>", '
    '"reason": "<one sentence>"}]}'
)

# The built-in default, exposed so the UI can show it and reset to it.
DEFAULT_SYSTEM = SYSTEM


def system_prompt(cfg):
    """The system prompt to use: the user's override, or the built-in default.

    The override is an escape hatch for people who want to steer the model's
    voice or rules. It is deliberately behind a warning in the UI, because a
    prompt that drops the JSON-contract lines makes every pick fail validation
    and fall back to the rule-based ranking.
    """
    override = (cfg["llm"].get("system_prompt") or "").strip()
    return override or DEFAULT_SYSTEM


MAX_REASON = 165

# Enforced by the server via constrained decoding, not merely requested. This
# is what stops a model returning ids under a key it invented, with no
# reasons attached.
PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "title", "reason"],
            },
        },
    },
    "required": ["picks"],
}


@dataclass
class Pick:
    item: object
    reason: str
    source: str  # "llm" | "fallback"


def _affinity(item, profile):
    """How well one candidate matches a taste profile, 0..1-ish."""
    if not profile.genres:
        return 0.0
    weights = dict(profile.genres)
    top = max(weights.values()) or 1.0
    hit = sum(weights.get(g, 0) for g in item.genres)
    # Normalized by the best possible single-genre score, capped so a
    # five-genre title cannot outrank relevance with breadth alone.
    return min(hit / (top * 2.0), 1.0)


def _decade_bonus(item, profile):
    if not item.year or not profile.decades:
        return 0.0
    favoured = {d for d, _ in profile.decades[:3]}
    return 0.08 if (item.year // 10) * 10 in favoured else 0.0


def score(item, profile):
    """Ranking used both to shortlist candidates and as the fallback order."""
    quality = (item.rating or 5.5) / 10.0
    return (_affinity(item, profile) * 0.62
            + quality * 0.30
            + _decade_bonus(item, profile))


def candidates(catalog, watched_keys, profile, cfg):
    """Shortlist what the model gets to see.

    A 2,000-title library will not fit in a prompt, and showing the model
    everything would waste most of the context on titles nobody would pick.
    The shortlist is ranked by taste affinity and then split between movies
    and TV in roughly the proportion this viewer actually watches, so a
    movies-only viewer is not handed ten shows.
    """
    rec = cfg["recommendations"]
    limit = cfg["llm"]["candidates"]
    excluded = {g.lower() for g in rec["exclude_genres"]}
    vetoed = vetoed_titles(cfg)
    min_rating = rec["min_rating"]

    pool = []
    for item in catalog.items:
        if item.rating_key in watched_keys:
            continue
        if min_rating and item.rating and item.rating < min_rating:
            continue
        if excluded and any(g.lower() in excluded for g in item.genres):
            continue
        if vetoed and normalize_title(item.title) in vetoed:
            continue
        if not item.summary:
            # Without an overview the model has nothing to reason about.
            continue
        pool.append(item)

    pool.sort(key=lambda i: -score(i, profile))

    if not (rec["include_movies"] and rec["include_shows"]):
        return pool[:limit]

    # Keep both types represented even for a lopsided viewer: a floor of 20%
    # means a heavy movie watcher still sees some TV.
    share = min(max(profile.movie_share, 0.2), 0.8)
    want_movies = int(limit * share)
    want_shows = limit - want_movies
    movies = [i for i in pool if i.type == "movie"][:want_movies]
    shows = [i for i in pool if i.type == "show"][:want_shows]
    # Backfill if one type ran out.
    short = limit - len(movies) - len(shows)
    if short > 0:
        chosen = {id(i) for i in movies + shows}
        movies += [i for i in pool if id(i) not in chosen][:short]

    merged = movies + shows
    merged.sort(key=lambda i: -score(i, profile))
    return merged


# Prompt evaluation, not generation, is what costs time: a 200-candidate list
# with 200-character overviews measured ~10k tokens and 60s of prompt eval on
# a 3090. Trimming the overview to a single clause halves that with no
# measurable loss in pick quality — the genre and rating carry most of it.
SUMMARY_CHARS = 130


def render_candidates(items):
    """The numbered block the model reads. One line per title, id first."""
    lines = []
    for index, item in enumerate(items, start=1):
        genres = "/".join(item.genres[:3]) or "—"
        rating = f"{item.rating:.1f}" if item.rating else "n/a"
        kind = "TV" if item.type == "show" else "Film"
        summary = " ".join((item.summary or "").split())[:SUMMARY_CHARS]
        lines.append(f"{index}. [{kind}] {item.title} ({item.year or '?'}) | "
                     f"{genres} | {rating} | {summary}")
    return "\n".join(lines)


def build_prompt(profile, items, count):
    return (
        f"{profile.as_prompt()}\n\n"
        f"Candidate titles they own and have not watched:\n"
        f"{render_candidates(items)}\n\n"
        f"Pick exactly {count} of the numbered candidates for this viewer. "
        f"Return JSON only."
    )


def _rows_from(raw):
    """Find the list of picks however the model chose to wrap it.

    Instructed to return {"picks": [...]}, models variously return a bare
    array, {"recommendations": [...]}, or {"results": [...]}. Rejecting those
    wastes a 60-second generation over a key name, so any list of objects
    carrying an id is accepted.
    """
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        raise LLMError(f"response was a {type(raw).__name__}, not an object")
    for key in ("picks", "recommendations", "results", "items", "selections"):
        if isinstance(raw.get(key), list):
            return raw[key]
    for value in raw.values():
        if isinstance(value, list) and value and isinstance(value[0], dict) \
                and ("id" in value[0] or "title" in value[0]):
            return value
    raise LLMError(f"no array of picks in response (keys: "
                   f"{sorted(raw)[:6]})")


# "You finished three seasons of Bosch", "you loved Breaking Bad" — the shape
# of a claim about the viewer's history, which is the one thing in a reason
# that can be checked against fact.
_CLAIM = re.compile(
    r"\byou(?:'ve| have)?\s+(?:already\s+)?"
    r"(?:finished|watched|binged|enjoyed|loved|rated|completed)\s+"
    r"(?:all\s+|both\s+)?(?:\w+\s+(?:seasons?|episodes?|films?|movies?)\s+of\s+)?"
    r"([A-Z][\w'’&.:!-]*(?:\s+[A-Z0-9][\w'’&.:!-]*){0,5})",
    re.IGNORECASE)


def _cites_real_history(reason, profile, item):
    """Reject reasons that invent something the viewer never watched.

    A model told to ground its reasons in the profile will sometimes ground
    them in a plausible-sounding title instead. A wrong recommendation is
    forgivable; telling someone they finished a show they have never opened
    is not, and it is the one claim cheap enough to verify.
    """
    known = {_title_key(i.title) for i in
             list(profile.favourites) + list(profile.recent)
             + list(profile.binged)}
    known.add(_title_key(item.title))
    for match in _CLAIM.finditer(reason):
        claimed = match.group(1).strip(" .,;:")
        if not claimed:
            continue
        key = _title_key(claimed)
        if not key or key in known:
            continue
        # Allow shrinking the claim word by word: "Bosch and Wednesday" is
        # two claims, and matching either counts.
        words = claimed.split()
        if any(_title_key(" ".join(words[:n])) in known
               for n in range(len(words), 0, -1)):
            continue
        # A bare genre reference ("you watched Comedy") is not a title claim.
        if claimed.lower() in {g.lower() for g in profile.top_genres}:
            continue
        log.debug("dropping invented claim: %r", claimed)
        return False
    return True


def validate(raw, items, count, profile=None):
    """Keep only picks that name a real candidate. Everything else is noise.

    Both the id *and* the echoed title are checked. Models reliably drift
    between the two — returning id 41 with the reason for id 43 — and a row
    whose title does not match its id is a wrong recommendation with a
    convincing explanation attached, which is worse than no recommendation.
    When the title matches a different candidate, that candidate is used;
    when it matches nothing, the row is dropped.
    """
    # The list shows "Title (Year)", so models echo it either way.
    by_title = {}
    for index, item in enumerate(items, start=1):
        by_title.setdefault(_title_key(item.title), index)
        by_title.setdefault(_title_key(item.label), index)

    picks, seen, drifted, dropped, invented = [], set(), 0, 0, 0
    for row in _rows_from(raw):
        if not isinstance(row, dict):
            continue
        try:
            index = int(str(row.get("id")).strip().rstrip("."))
        except (TypeError, ValueError):
            index = 0

        claimed = _claimed_key(row.get("title") or row.get("name") or "")
        if claimed:
            match = by_title.get(claimed)
            if match and match != index:
                # Trust the title: it is what the reason was written about.
                index, drifted = match, drifted + 1
            elif not match and index and 1 <= index <= len(items):
                # A title we do not recognise next to a valid id means the
                # model was talking about something it invented.
                target = items[index - 1]
                if claimed not in (_title_key(target.title),
                                   _title_key(target.label)):
                    dropped += 1
                    continue

        if not 1 <= index <= len(items) or index in seen:
            dropped += 1
            continue
        seen.add(index)
        item = items[index - 1]
        reason = _clamp(row.get("reason"))
        if reason and profile is not None and \
                not _cites_real_history(reason, profile, item):
            invented += 1
            reason = ""
        if not reason:
            reason = f"Fits your taste for {'/'.join(item.genres[:2])}."
        picks.append(Pick(item=item, reason=reason, source="llm"))
        if len(picks) >= count:
            break

    if drifted or dropped or invented:
        log.debug("validation: %d realigned by title, %d dropped, "
                  "%d invented claims replaced", drifted, dropped, invented)
    return picks


def _clamp(reason):
    """One sentence, ending on a word.

    These land in a Plex collection summary where a reason cut off mid-word
    ("known for its gripping narrat") is the most visible possible bug.
    """
    text = " ".join(str(reason or "").split())
    if not text:
        return ""
    if len(text) > MAX_REASON:
        # Prefer ending at the last sentence that fits; failing that, the
        # last whole word.
        window = text[:MAX_REASON]
        stop = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
        if stop > MAX_REASON * 0.5:
            return window[:stop + 1]
        text = window.rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


def _title_key(title):
    return "".join(c for c in (title or "").lower() if c.isalnum())


def _claimed_key(text):
    """Normalize whatever the model put in ``title``.

    Asked to echo the title, models echo the whole candidate line —
    ``"[Film] The Terminator (1984) | Action/Adventure | 9.2 | In a future
    where..."`` — which is a correct answer to a badly-worded request. Strip
    the rendering back off rather than punish it for being literal.
    """
    text = str(text or "").split("|")[0].strip()
    if text.startswith("["):
        text = text.split("]", 1)[-1].strip()
    return _title_key(text)


def balanced(items, count):
    """Take ``count`` items keeping movies and TV both represented.

    The house row is ranked by raw play count, and on a server where one
    person binges television that ranking is all TV — which would publish a
    single TV row and no movie row at all.
    """
    movies = [i for i in items if i.type == "movie"]
    shows = [i for i in items if i.type == "show"]
    if not movies or not shows:
        return items[:count]
    want_shows = count // 2
    want_movies = count - want_shows
    chosen = movies[:want_movies] + shows[:want_shows]
    if len(chosen) < count:
        picked = {i.rating_key for i in chosen}
        chosen += [i for i in items if i.rating_key not in picked][
            :count - len(chosen)]
    return chosen


def fallback(items, profile, count, taken=(), style="taste"):
    """Rule-based picks. Never fails, never empty while candidates exist.

    ``style="house"`` is for the shared fallback row, which is ranked by how
    much the household watched a title — claiming a 2.9-rated film is "well
    rated" because it happens to be popular here reads as a bug, because it
    is one.
    """
    taken_keys = {p.item.rating_key for p in taken}
    out = list(taken)
    for item in items:
        if len(out) >= count:
            break
        if item.rating_key in taken_keys:
            continue
        if style == "house":
            kind = "show" if item.type == "show" else "film"
            reason = f"One of the most-watched {kind}s on this server."
        else:
            shared = [g for g in item.genres if g in profile.top_genres]
            if shared:
                reason = (f"Picked because you watch a lot of "
                          f"{' and '.join(shared[:2])}.")
            elif item.rating and item.rating >= 7.0:
                reason = (f"Well rated at {item.rating:.1f}/10 and still "
                          f"unwatched in your library.")
            else:
                reason = "Unwatched in your library and worth a look."
        out.append(Pick(item=item, reason=reason, source="fallback"))
    return out


def enforce_split(picks, pool, profile, cfg, min_items=3):
    """Make sure each library the viewer uses actually gets a row.

    Candidates are drawn from two libraries — the movie one and the TV one
    the viewer actually watches in — but the model picks freely across both,
    and a run where it happens to choose thirteen shows and two films leaves
    the movie row one item short of publishable. The viewer then opens their
    Movies library and sees rows for everyone except themselves.

    So the split is enforced afterwards rather than requested in the prompt:
    a short side is topped up from its own ranked candidates, paid for by the
    weakest picks on the long side. Only types the viewer actually watches
    are guaranteed — a TV-only viewer still gets no movie row.
    """
    if not picks:
        return picks
    total = len(picks)
    by_type = {"movie": [], "show": []}
    for pick in picks:
        by_type.setdefault(pick.item.type, []).append(pick)

    available = {"movie": [i for i in pool if i.type == "movie"],
                 "show": [i for i in pool if i.type == "show"]}
    watches = {"movie": profile.movie_plays > 0,
               "show": profile.show_plays > 0}

    for kind, other in (("movie", "show"), ("show", "movie")):
        have = len(by_type[kind])
        if have >= min_items or not watches[kind]:
            continue
        if len(available[kind]) < min_items:
            continue
        # Never rob the other side below the same floor.
        spare = len(by_type[other]) - min_items
        need = min(min_items - have, max(0, spare))
        if need <= 0:
            continue
        chosen = {p.item.rating_key for p in picks}
        top_up = [i for i in available[kind] if i.rating_key not in chosen]
        top_up = fallback(top_up[:need], profile, need)
        if not top_up:
            continue
        by_type[other] = by_type[other][:len(by_type[other]) - len(top_up)]
        by_type[kind] += top_up
        log.info("%s: topped up %s row to %d items so it can publish",
                 profile.display_name, kind, len(by_type[kind]))

    merged = by_type["movie"] + by_type["show"]
    return merged[:total]


def choose(llm, profile, items, cfg, stats=None):
    """Full pick sequence for one viewer: model, retry, fallback."""
    count = cfg["recommendations"]["picks"]
    if not items:
        return [], "no-candidates"

    prompt = build_prompt(profile, items, count)
    base_system = system_prompt(cfg)
    system = base_system
    picks, mode = [], "llm"

    for attempt in (1, 2):
        try:
            raw, meta = llm.complete_json(system, prompt,
                                          max_tokens=120 * count,
                                          schema=PICK_SCHEMA)
            if stats is not None:
                stats.append(meta)
            picks = validate(raw, items, count, profile)
        except LLMError as exc:
            log.warning("%s: llm attempt %d failed (%s)",
                        profile.display_name, attempt, exc)
            picks = []
        except Exception as exc:  # network, timeout — same handling
            log.warning("%s: llm attempt %d errored (%s)",
                        profile.display_name, attempt, exc)
            picks = []

        # Two thirds is the line between "the model did the job and dropped a
        # couple" and "the model is not following instructions".
        if len(picks) >= max(1, int(count * 0.67)):
            break
        if attempt == 1:
            log.info("%s: only %d/%d valid picks, re-asking",
                     profile.display_name, len(picks), count)
            system = base_system + (
                "\nYour previous answer contained ids that were not in the "
                "candidate list. Every id MUST be a number shown in the list.")

    if len(picks) < count:
        mode = "fallback" if not picks else "mixed"
        log.info("%s: filling %d picks from the rule-based ranking",
                 profile.display_name, count - len(picks))
        picks = fallback(items, profile, count, taken=picks)

    return picks[:count], mode
