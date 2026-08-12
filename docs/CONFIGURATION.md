# Configuration reference

Everything lives in **one file**, `settings.json`, in the config directory
(`/config` in a container, `./config` from a checkout). Edit it in the web UI
(**Settings**) or by hand. Every value can also be set as an environment
variable, `PLAYLISTERR_<SECTION>_<KEY>` uppercased — e.g. `plex.url` becomes
`PLAYLISTERR_PLEX_URL`.

Write a fully-annotated example with every default:

```bash
python -m playlisterr config --write-example config.example.json
```

---

## `plex` — your server (required)

| Key | Default | Meaning |
|---|---|---|
| `url` | — | e.g. `http://192.168.1.10:32400`. Filled in by Sign in with Plex. |
| `token` | — | Also filled in by signing in. |
| `movie_libraries` | `[]` (all) | Library **names**, as Plex shows them. Empty = every movie library. |
| `show_libraries` | `[]` (all) | Empty = every TV library. |
| `exclude_libraries` | `[]` | Never recommend from these, even if the lists above are empty. |
| `timeout` | `60` | Seconds. |

## `llm` — the model (required)

| Key | Default | Meaning |
|---|---|---|
| `provider` | `ollama` | `ollama` (local, no key), `openai` (hosted, needs a key), or `custom` (any OpenAI-compatible endpoint; key optional). |
| `url` | `http://localhost:11434` | The endpoint. Unused for `openai`. |
| `model` | `qwen2.5:14b-instruct` | Run `playlisterr models` to list what your endpoint offers. |
| `api_key` | — | Required for `openai`; optional for `custom`; unused for `ollama`. |
| `temperature` | `0.4` | Higher = more adventurous. |
| `keep_alive` | `10m` | Ollama only. Keeps the model resident between users (a cold 14B load is ~110s). |
| `timeout` | `600` | Seconds per request. |
| `candidates` | `200` | How many titles the model chooses from. More is slower — prompt evaluation dominates. |
| `system_prompt` | *(blank)* | Override the model's instructions. Blank = the built-in default (recommended). Editable in the UI behind a warning, with a reset. |

Any instruct-tuned model of ~7B+ works. The wizard prefers instruct models that
fit a consumer GPU and skips coder/embedding models (wrong tool) and 70B+
(spills to CPU). No GPU? `playlisterr models --provider openai --url
https://api.openai.com --api-key sk-...`.

## `recommendations` — what gets picked and how it's delivered

| Key | Default | Meaning |
|---|---|---|
| `picks` | `15` | Titles per row. |
| `history_days` | `180` | How far back to read. |
| `min_plays` | `10` | Below this, a viewer gets the shared house row instead of a personal one. |
| `min_titles` | `3` | Distinct titles required too — 30 episodes of one show isn't a taste. |
| `delivery` | `playlist` | `playlist` or `collection`. See [below](#delivery-modes). |
| `per_library` | `true` | One row per library ("Generated Movies") vs. one mixed row per person. |
| `playlist_title` | `Generated {library}` | `{library}`, `{name}`, `{username}` available. |
| `collection_title` | `For {name}` | Used when `per_library` is off. Must contain `{name}`. |
| `fallback_title` | `House Picks` | Name of the shared house row. |
| `taste_scope` | `library` | `library` picks each row from what was watched *in that library*; `account` uses one taste for all rows. |
| `library_min_plays` | `5` | Below this in a library, that row falls back to the account-wide taste (and says so). |
| `max_libraries` | `4` | Cap on how many library rows one person gets. |
| `exclude_genres` | `[]` | Never recommend these. |
| `exclude_titles` | `[]` | Never recommend these (the "never recommend" button appends here). |
| `min_rating` | `0.0` | Drop candidates below this. Plex ratings are /10. `0` disables. |
| `include_movies` / `include_shows` | `true` | Recommend movies / TV. |
| `label_prefix` | `playlisterr` | Playlisterr only ever modifies things carrying this label. |
| `promote_to_library` | `true` | Put a collection on the library's "Recommended" row (collection delivery). |
| `promote_to_home` | `true` | Put a collection on shared users' home screens. |
| `promote_to_owner_home` | `true` | Put the owner's own collection on their home screen. |
| `library_row_position` | `top` | Where a collection sits on the library page: `top` or `bottom`. |

### Delivery modes

A *playlist* belongs to an account; a *collection* is server-wide. That's the
whole trade-off.

- **`playlist`** (default) — each person, owner included, sees only their own.
  Appears under Playlists and on home screens. A recommended show contributes
  the viewer's next unwatched episode, because a playlist can't hold a whole
  series.
- **`collection`** — real "Recommended" library rows, which look better, but a
  collection is shared: everyone with access to the library sees every row.

## `users` — who takes part

| Key | Default | Meaning |
|---|---|---|
| `mode` | `all` | `all` = everyone on the server; `selected` = only the accounts listed. |
| `selected` | `[]` | Plex account ids (the UI writes these). |
| `selected_names` | `[]` | Usernames, stored alongside for readability. |

## `notifications` — webhook on run completion

| Key | Default | Meaning |
|---|---|---|
| `webhook_url` | — | POST a one-line summary here when a run finishes. Blank disables it. |
| `format` | `json` | `json` (generic), `discord`, `slack`, `ntfy`, or `gotify`. |
| `on_success` | `true` | Notify on a successful run. |
| `on_failure` | `true` | Notify when a run fails. |

Works with ntfy, Gotify, Discord/Slack webhooks, or any JSON receiver (Home
Assistant, n8n). Test it with the button in **Settings → Notifications**.

## `history` — where watch history comes from

| Key | Default | Meaning |
|---|---|---|
| `provider` | `auto` | `auto` uses whichever of Plex/Tautulli has more history in the window; `plex` or `tautulli` force it. |

## `tautulli` — optional deeper history

| Key | Default | Meaning |
|---|---|---|
| `url` / `api_key` | — | If set, Tautulli history is used when it's deeper than Plex's. |
| `timeout` | `60` | Seconds. |

## `server` — the web UI and scheduler

| Key | Default | Meaning |
|---|---|---|
| `host` | `0.0.0.0` | Bind address. |
| `port` | `8484` | Web UI port. Takes effect on restart. |
| `api_key` | *(generated)* | For the programmatic API and, when `auth_required`, the browser. |
| `schedule_mode` | `daily` | `daily`, `twice_daily`, `every_6h`, or `manual`. |
| `schedule_time` | `03:30` | Local start time for scheduled runs. |
| `auth_required` | `false` | Require the API key in the browser. Leave off behind a reverse proxy that already handles logins. |

## `general`

| Key | Default | Meaning |
|---|---|---|
| `client_id` | *(generated)* | Identifies this install to plex.tv. Stable once set. |
| `log_level` | `info` | `debug`, `info`, `warning`, `error`. |
| `timezone` | — | Blank = the container/host `TZ`. |
