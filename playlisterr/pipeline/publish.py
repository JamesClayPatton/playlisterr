#!/usr/bin/env python3
"""Turn picks into Plex mutations — and, in dry-run, into readable English.

**A Plex collection lives inside exactly one library section.** There is no
such thing as a collection spanning Movies and TV Shows, so "For Dalton"
becomes at most two collections: one in a movie library and one in a show
library. Which two is decided per viewer, from where they actually watch — a
household member who lives in Kids Cartoons gets their row there, not in the
biggest library on the server.

Nothing in this module writes anything. It produces a Plan; the runner either
prints it (``--dry``) or, from M3, executes it.
"""

from dataclasses import dataclass, field

from ..log import get
from . import profile as profile_mod

log = get("publish")

# A row with one or two items looks broken on a home screen.
MIN_ITEMS = 3


@dataclass
class Mutation:
    action: str          # create | add | remove | summary | label | delete
    section_key: str
    section_title: str
    collection: str
    detail: str = ""
    items: list = field(default_factory=list)
    section_type: str = "movie"
    owner_row: bool = True


@dataclass
class Plan:
    user: str
    label: str
    mutations: list = field(default_factory=list)
    collections: dict = field(default_factory=dict)  # section_key -> title

    @property
    def empty(self):
        return not self.mutations


def label_for(account_id, cfg):
    """The label that marks a collection as ours. Never reused, never guessed."""
    return f"{cfg['recommendations']['label_prefix']}-u{account_id}"


def house_label(cfg):
    """The fallback row is one shared collection, not one per thin user.

    Labelling it per-user would have fifteen viewers writing conflicting
    labels onto the same collection every night.
    """
    return f"{cfg['recommendations']['label_prefix']}-house"


def watched_sections(watched, catalog, limit=4, min_plays=2):
    """Every library this viewer actually uses, busiest first.

    ``primary_sections`` picks one library per type, which is right for a
    single mixed row. Per-library rows want the whole list: someone who
    watches films, kids films and cartoons should get a row in each of them
    rather than one row in whichever they used most.
    """
    counts = {}
    if watched:
        for key, plays in watched.play_counts.items():
            item = catalog.by_key.get(key)
            if item:
                counts[item.section_key] = counts.get(item.section_key, 0) + plays
    ranked = [key for key, plays in
              sorted(counts.items(), key=lambda kv: -kv[1]) if plays >= min_plays]
    by_key = {s.key: s for s in catalog.sections}
    sections = [by_key[k] for k in ranked if k in by_key][:limit]
    if sections:
        return sections
    # No history worth the name: fall back to the biggest library of each type.
    fallback = primary_sections(watched, catalog)
    return [s for s in fallback.values() if s][:limit]


def primary_sections(watched, catalog):
    """Where this viewer's rows should live.

    Chosen from their own watch distribution, with the largest library of
    each type as the fallback for someone with no history.
    """
    by_type = {"movie": {}, "show": {}}
    if watched:
        for key, count in watched.play_counts.items():
            item = catalog.by_key.get(key)
            if item:
                bucket = by_type[item.type]
                bucket[item.section_key] = bucket.get(item.section_key, 0) + count

    sizes = {}
    for item in catalog.items:
        sizes[item.section_key] = sizes.get(item.section_key, 0) + 1

    chosen = {}
    for kind in ("movie", "show"):
        sections = [s for s in catalog.sections if s.type == kind]
        if not sections:
            chosen[kind] = None
            continue
        counts = by_type[kind]
        if counts:
            best = max(counts.items(), key=lambda kv: kv[1])[0]
            chosen[kind] = next((s for s in sections if s.key == best),
                                None)
        if not chosen.get(kind):
            chosen[kind] = max(sections, key=lambda s: sizes.get(s.key, 0))
    return chosen


def restrict(items, sections):
    """Keep only candidates that live in the viewer's target sections."""
    keys = {s.key for s in sections.values() if s}
    return [i for i in items if i.section_key in keys]


def summary_text(picks, generated_at, profile=None, house=False):
    """The collection blurb — this is where the reasons become visible.

    Opens by saying *why* the row looks like this, in terms of what the
    viewer actually watched, before listing the picks.
    """
    lines = [profile_mod.intro(profile, house=house), ""]
    for pick in picks:
        lines.append(f"• {pick.item.label} — {pick.reason}")
    lines.append("")
    lines.append(f"Picked for you on {generated_at} · refreshed nightly by "
                 f"Playlisterr.")
    return "\n".join(lines)


def plan(user, picks, catalog, cfg, existing, generated_at, title=None,
         label=None, profile=None, house=False):
    """Compute the mutations that would make Plex match these picks.

    ``existing`` maps section_key -> {title: (collection, [child rating keys])}
    so the plan only asks for the differences. ``title`` and ``label``
    override the per-user template and per-user label, which is how the one
    shared fallback row is named "House Picks" and labelled once.
    """
    if title is None:
        title = cfg["recommendations"]["collection_title"].format(
            name=user["display_name"], username=user["username"])
    if label is None:
        label = label_for(user["account_id"], cfg)
    result = Plan(user=user["display_name"], label=label)
    # Account 1 is always the server owner in Plex's own history and account
    # numbering; the shared house row counts as theirs too.
    owner_row = str(user.get("account_id")) in ("1", "house")

    by_section = {}
    for pick in picks:
        by_section.setdefault(pick.item.section_key, []).append(pick)

    section_titles = {s.key: s.title for s in catalog.sections}
    section_types = {s.key: s.type for s in catalog.sections}

    for section_key, section_picks in sorted(by_section.items()):
        if len(section_picks) < MIN_ITEMS:
            log.debug("%s: skipping %s, only %d picks", title,
                      section_titles.get(section_key, section_key),
                      len(section_picks))
            continue
        section_title = section_titles.get(section_key, section_key)
        section_type = section_types.get(section_key, "movie")
        result.collections[section_key] = title
        wanted = [p.item.rating_key for p in section_picks]
        current = existing.get(section_key, {}).get(title)

        if not current:
            result.mutations.append(Mutation(
                action="create", section_key=section_key,
                section_title=section_title, collection=title,
                detail=f"{len(wanted)} items", items=wanted,
                section_type=section_type))
            result.mutations.append(Mutation(
                action="label", section_key=section_key,
                section_title=section_title, collection=title,
                detail=label))
            result.mutations.append(Mutation(
                action="summary", section_key=section_key,
                section_title=section_title, collection=title,
                detail=summary_text(section_picks, generated_at,
                                    profile, house)))
            result.mutations.append(_promotion(cfg, section_key,
                                               section_title, title,
                                               owner_row))
            continue

        collection, children = current
        # Refuse to touch a collection that is not ours, even if the name
        # matches. Someone else's "For Dalton" is not ours to rewrite.
        if not _has_label(collection.labels, label):
            log.warning("%s in %s exists without our label %s — leaving it "
                        "alone", title, section_title, label)
            result.mutations.append(Mutation(
                action="skip", section_key=section_key,
                section_title=section_title, collection=title,
                detail=f"exists without label {label}; not touching it"))
            continue

        add = [k for k in wanted if k not in children]
        drop = [k for k in children if k not in wanted]
        if add:
            result.mutations.append(Mutation(
                action="add", section_key=section_key,
                section_title=section_title, collection=title,
                detail=f"{len(add)} new", items=add))
        if drop:
            result.mutations.append(Mutation(
                action="remove", section_key=section_key,
                section_title=section_title, collection=title,
                detail=f"{len(drop)} stale", items=drop))
        result.mutations.append(Mutation(
            action="summary", section_key=section_key,
            section_title=section_title, collection=title,
            detail=summary_text(section_picks, generated_at,
                                    profile, house)))
        result.mutations.append(_promotion(cfg, section_key, section_title,
                                           title, owner_row))

    return result


def _has_label(labels, wanted):
    """Case-insensitive label match.

    Plex normalises label tags on write: "playlisterr-u1" is stored and returned
    as "Playlisterr-u1". Comparing exactly would make Playlisterr fail to recognise
    the collections it created a day earlier, and refuse to update them.
    """
    return (wanted or "").lower() in {(l or "").lower() for l in (labels or [])}


def _promotion(cfg, section_key, section_title, title, owner_row=True):
    rec = cfg["recommendations"]
    where = [name for name, on in (
        (f"library Recommended ({rec['library_row_position']})",
         rec["promote_to_library"]),
        ("shared home screens", rec["promote_to_home"]),
        ("owner home screen", rec["promote_to_owner_home"])) if on]
    if not owner_row:
        # Someone else's row has no business on the owner's home screen. The
        # owner cannot be library-filtered by Plex at all, so this is the
        # only lever that keeps their home screen about them.
        where = [w for w in where if "owner" not in w]
    return Mutation(action="promote", section_key=section_key,
                    section_title=section_title, collection=title,
                    detail=", ".join(where) or "nowhere (all promotion off)",
                    owner_row=owner_row)


def apply(plex, plan, existing, cfg):
    """Execute a Plan against Plex. Only called when --publish is given.

    Order matters: a collection has to exist before it can be labelled or
    given a summary, and Plex assigns the rating key on creation, so the
    freshly created collection is looked up before the follow-up writes.
    """
    done, failed = 0, []
    created = {}

    for mutation in plan.mutations:
        try:
            if mutation.action == "skip":
                continue

            key = created.get((mutation.section_key, mutation.collection))
            if key is None:
                current = existing.get(mutation.section_key, {}).get(
                    mutation.collection)
                key = current[0].rating_key if current else None

            if mutation.action == "create":
                plex.create_collection(mutation.section_key,
                                       mutation.collection, mutation.items,
                                       section_type=mutation.section_type)
                # Re-read to learn the rating key Plex just assigned.
                for collection in plex.collections(mutation.section_key):
                    if collection.title == mutation.collection:
                        key = collection.rating_key
                        created[(mutation.section_key,
                                 mutation.collection)] = key
                        break
                if not key:
                    raise RuntimeError(
                        f"created {mutation.collection} but could not find it")
            elif key is None:
                raise RuntimeError(
                    f"no collection to modify for {mutation.collection}")
            elif mutation.action == "add":
                plex.add_to_collection(key, mutation.section_key,
                                       mutation.items)
            elif mutation.action == "remove":
                for rating_key in mutation.items:
                    plex.remove_from_collection(key, rating_key)
            elif mutation.action == "label":
                plex.add_collection_label(mutation.section_key, key,
                                          mutation.detail)
            elif mutation.action == "summary":
                plex.set_collection_field(mutation.section_key, key,
                                          "summary", mutation.detail)
            elif mutation.action == "promote":
                rec = cfg["recommendations"]
                plex.promote_collection(
                    mutation.section_key, key,
                    recommended=rec["promote_to_library"],
                    own_home=rec["promote_to_owner_home"]
                    and mutation.owner_row,
                    shared_home=rec["promote_to_home"])
                if rec["promote_to_library"] and                         rec["library_row_position"] == "top":
                    plex.move_hub(
                        mutation.section_key,
                        plex.hub_identifier(mutation.section_key, key))
            done += 1
        except Exception as exc:
            failed.append(f"{mutation.action} {mutation.collection}: {exc}")
            log.error("%s %s in %s failed: %s", mutation.action,
                      mutation.collection, mutation.section_title, exc)

    return done, failed


def render(plan, catalog, verbose=False):
    """The --dry output: exactly what would change, in words."""
    if plan.empty:
        return [f"  (nothing to do for {plan.user})"]
    lines = []
    for mutation in plan.mutations:
        head = (f"  {mutation.action.upper():<8} "
                f"[{mutation.section_title}] \"{mutation.collection}\"")
        if mutation.action == "summary":
            first = mutation.detail.splitlines()[0]
            lines.append(f"{head}  set summary ({len(mutation.detail)} chars, "
                         f"starts {first!r})")
        elif mutation.action in ("create", "add", "remove"):
            lines.append(f"{head}  {mutation.detail}")
            if verbose or mutation.action != "create":
                for key in mutation.items[:20]:
                    item = catalog.by_key.get(key)
                    lines.append(f"           - {item.label if item else key}")
        else:
            lines.append(f"{head}  {mutation.detail}")
    return lines
