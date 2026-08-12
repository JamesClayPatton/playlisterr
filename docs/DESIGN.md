# Playlisterr — design notes

How Playlisterr works and why it is built the way it is. This is background for
contributors; the [README](../README.md) is the place to start if you just
want to run it.

## What it does

Every scheduled run, for each Plex account with enough watch history, Playlisterr:

1. reads that account's watch history (from Plex directly, or Tautulli),
2. builds a taste profile — top genres, eras, favourite and binged titles —
   with plain arithmetic, no model involved,
3. asks a language model to pick ~15 owned, unwatched titles from a numbered
   candidate list drawn from the library,
4. validates every pick against that list (a model can never introduce a
   title the library does not own), and
5. publishes the result as a per-account Plex playlist (or a collection),
   one row per library, named "Generated Movies", "Generated TV Shows", …

Accounts with too little history to read a taste from get a shared "house"
row of what the server watches most, and the row's blurb says so.

## Principles

- **Standard library only, for the core.** No third-party Python packages,
  ever. Dependencies are a tax on everyone who self-hosts; the reference
  implementation pays none. The web UI is framework-free vanilla JS with no
  build step for the same reason.
- **Playlisterr only arranges what already exists.** It never downloads, never
  requests, never writes to Radarr/Sonarr. Its only writes are the playlists
  and collections it owns — everything it creates carries a `playlisterr-`
  label, and nothing without that label is ever touched.
- **The model is on a short leash.** It sees a profile and a numbered list and
  returns numbers. Output is schema-constrained where the endpoint supports
  it and validated against the candidate list regardless, so a
  hallucination is a dropped pick rather than a recommendation for something
  nobody owns. Reasons are checked for invented viewing history.
- **Plex is the only hard requirement**, plus any LLM endpoint (a local
  Ollama, or anything OpenAI-compatible). Tautulli and the \*arrs are optional.

## Pipeline

```
users     → who to build rows for (owner + shared users, minus anyone
            switched off)
history   → per-account watch history, episodes mapped to their show
catalog   → every owned title, once, shared across all users
profile   → per user, per library: genres, eras, favourites — no model
pick      → the model chooses from a numbered candidate shortlist; validated
publish   → create/update the playlist or collection, set the blurb
report    → run log, visible in the UI
```

Only the `pick` step talks to a model. Everything else is deterministic
Python, which is what keeps a bad model from doing more than picking poorly.

## Delivery: playlists vs collections

Plex offers no per-user *collection* — a collection is a server-wide object
that everyone with library access sees. A *playlist*, by contrast, belongs to
an account: created with a viewer's own token it is visible to that viewer and
nobody else, owner included. So the default delivery is playlists, with
collections available for anyone who prefers real library rows and doesn't mind
that they're shared. One measured constraint drove this: a playlist cannot hold
a whole show (it expands to every episode), so a recommended show contributes
the viewer's next unwatched episode.

## Per-library taste

One Plex login is often several people — a parent's account with the kids
watching on it. So the taste analysis is scoped per library by default: the
"Generated Kids Movies" row follows what was watched in Kids Movies, not the
account's overall taste. Below a small play threshold a library has too little
to read, and that row falls back to the account-wide profile (and says so).

## Offline development

The HTTP layer can record every API response to a fixture and replay it, so
the pipeline — including the model calls — runs with no Plex server and no
GPU. Recorded fixtures are redacted at write time (tokens and emails are
scrubbed from response bodies), but treat any fixtures you record against your
own server as private until you have confirmed they are clean; only commit an
intentionally anonymized set.
