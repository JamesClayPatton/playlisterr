# FAQ

### Do my users have to sign in or install anything?

No. Only **you** sign in, once, as the server owner. Plex hands the owner an
access token for each shared user, and Playlisterr uses that to create the row
in their account. They just find a "Generated Movies" row in their Plex one
day. They never see Playlisterr.

*(Managed/home sub-profiles that aren't full shared accounts have no
retrievable token and are skipped in playlist mode; collection delivery still
reaches them, since a collection is server-wide.)*

### Will people see each other's recommendations?

In the default **playlist** mode, no — a Plex playlist belongs to an account,
so each person (owner included) sees only their own. In **collection** mode a
collection is server-wide, so everyone with library access sees every row. See
[delivery modes](CONFIGURATION.md#delivery-modes).

### Does it need Plex Pass?

No. Sharing a library and creating per-account playlists or server-wide
collections all work without Plex Pass.

### Does my watch history leave my server?

Only if you choose a **hosted** LLM (`openai` or a remote `custom` endpoint) —
then a candidate list and a short taste summary go to that provider per run. A
local **Ollama** keeps everything on your LAN. Playlisterr reads *every*
account's history, not just yours; use `users.mode: selected` to limit it.

### Where do the rows actually appear?

Playlists show under **Playlists** and on home screens. Collections appear as a
**Recommended** category row inside a library (and can be pushed to the top).
It's per-library — "Generated Movies", "Generated TV Shows", etc.

### The picks are bland / generic for some users. Bug?

No — it's honest behavior. Below `min_plays`/`min_titles` a person hasn't
watched enough for the model to read a taste, so they get the shared **House
Picks** row. A per-library row with little history in *that* library falls back
to the account-wide taste and says so. More history = sharper picks.

### A recommendation is wrong / I never want to see it again.

Click the ✕ on any pick, or add it to **Never recommend these titles** in
Settings. It's gone from the next run onward.

### Can I see what it would do before it touches Plex?

Yes. **Generate** runs the whole pipeline and writes nothing — you review the
exact picks. **Publish these picks** then writes that same set, unchanged.
`playlisterr run` (no `--publish`) is the CLI equivalent.

### It didn't run / a dependency was down.

The **Home** page and header show a health pill; a dead Plex or model surfaces
there, and failed runs are recorded in **System → Recent runs** with the error.
Turn on notifications to get pinged when a run fails.

### Does it delete or change my other collections/playlists?

No. It only ever touches things carrying its own `playlisterr-` label (or, for
playlists, ones it made — identified by a signature in the summary). Anything
else with a matching name is left alone.

### How do I move it to a new machine?

**System → Back up config** downloads a zip of `settings.json` + `state.json`
(your configuration and run history). Restore it into the new `/config`.

### Can I develop / preview without a Plex server?

Yes. `python -m playlisterr serve --demo` runs the whole UI on bundled
fictional data, and `python -m playlisterr run --offline` runs the pipeline the
same way. Nothing real is contacted. See [CONTRIBUTING.md](../CONTRIBUTING.md).

### Is there an API?

Every UI action is a JSON endpoint under `/api/*`, authenticated with the API
key from **Settings → Advanced** (header `X-Api-Key` or `?apikey=`). Handy for
Home Assistant or scripts.

### Is this affiliated with Plex?

No. It's an independent open-source project that talks to Plex's API.
