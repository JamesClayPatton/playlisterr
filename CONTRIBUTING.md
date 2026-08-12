# Contributing to Playlisterr

Thanks for helping. Two ground rules shape everything here, and PRs that break
them will be asked to change:

1. **Standard library only, for the core.** No third-party Python packages —
   not requests, not flask, nothing. If you reach for a dependency, there is
   almost always a stdlib way (`urllib.request`, `http.server`, `json`,
   `xml.etree`). This is what keeps Playlisterr a single-file-simple thing to
   self-host.
2. **No build step for the web UI.** It is vanilla HTML/CSS/JS served straight
   from `playlisterr/web/static/`. No npm, no bundler, no framework.

## Getting set up

```bash
git clone https://github.com/JamesClayPatton/playlisterr
cd playlisterr
python -m playlisterr serve        # http://localhost:8484
```

You need Python 3.11+. That's the whole toolchain.

### Developing without a Plex server

Record fixtures once against a real server, then replay them forever:

```bash
python -m playlisterr run --record     # captures API responses to fixtures/
python -m playlisterr run --offline    # replays them — no Plex, no GPU needed
```

Recorded fixtures are redacted at write time, but they still contain your
library's contents. **Never commit fixtures from your own server.** Only an
intentionally anonymized set belongs in `fixtures/sample/`.

## Before you open a PR

```bash
python -m compileall playlisterr       # everything imports / parses
python -m playlisterr config --write-example /tmp/ex.json   # config example stays in sync
```

If you changed any setting in `playlisterr/config.py`, regenerate
`config.example.json` and mention the new setting in the README table if it is
user-facing.

## Style

- Match the surrounding code. Comments explain *why*, not *what*.
- Keep the CLI and the web UI in step: both drive the same functions in
  `playlisterr/pipeline/`; neither should hold logic the other can't reach.
- Security-sensitive code (anything touching tokens, the filter writes, or the
  web endpoints) gets extra scrutiny — see [SECURITY.md](SECURITY.md).

## Where things live

```
playlisterr/
  cli.py            command line
  config.py         the single source of truth for every setting (DEFAULTS)
  http.py           the one HTTP client: retries, redaction, record/replay
  providers/        everything that talks to Plex / plex.tv / an LLM / Tautulli
  pipeline/         users → history → catalog → profile → pick → publish
  web/              the server and its static UI
docs/DESIGN.md      why it is built this way
```
