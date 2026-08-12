# Installing Playlisterr

Playlisterr needs **Plex** and **an LLM endpoint** (a local Ollama, or anything
OpenAI-compatible). It's a single service on **port 8484**, and its whole state
lives in one `/config` directory. Python 3.11+ with **no third-party packages**.

- [Docker](#docker) (recommended)
- [Docker Compose](#docker-compose)
- [From source](#from-source)
- [Bare metal / systemd](#bare-metal--systemd)
- [First run](#first-run)
- [Environment variables](#environment-variables)

## Docker

There's no image on a registry — you build it from the repo (one command,
no dependencies to fetch):

```bash
git clone https://github.com/JamesClayPatton/playlisterr && cd playlisterr
docker build -t playlisterr .

docker run -d \
  --name playlisterr \
  -p 8484:8484 \
  -e PUID=1000 -e PGID=1000 -e TZ=Etc/UTC \
  -v /path/to/config:/config \
  --restart unless-stopped \
  playlisterr
```

`PUID`/`PGID` set the user that owns the `/config` bind mount — the same
convention every \*arr image uses. `/config` is the only writable path and
holds `settings.json`, `state.json`, and the logs.

## Docker Compose

The bundled `docker-compose.yml` builds from the checkout, so the whole
install is `git clone` then `docker compose up -d`:

```yaml
services:
  playlisterr:
    build: .                     # builds from this checkout, nothing is pulled
    container_name: playlisterr
    restart: unless-stopped
    ports:
      - 8484:8484
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
    volumes:
      - ./config:/config
```

`docker compose up -d`, then open `http://<host>:8484`.

## From source

No build step, no dependencies to install:

```bash
git clone https://github.com/JamesClayPatton/playlisterr
cd playlisterr
python -m playlisterr serve            # http://localhost:8484
```

Config lands in `./config` by default (override with `--config-dir` or
`PLAYLISTERR_CONFIG_DIR`).

**Preview it with no Plex server:**

```bash
python -m playlisterr serve --demo     # the full UI on bundled sample data
```

## Bare metal / systemd

Because it's dependency-free, running it as a service is trivial. A minimal
unit:

```ini
# /etc/systemd/system/playlisterr.service
[Unit]
Description=Playlisterr
After=network-online.target

[Service]
User=media
Environment=PLAYLISTERR_CONFIG_DIR=/var/lib/playlisterr
ExecStart=/usr/bin/python3 -m playlisterr serve
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now playlisterr
```

On Windows, `python -m playlisterr serve` runs the same way; point
`PLAYLISTERR_CONFIG_DIR` at a folder you control.

## First run

1. Open `http://<host>:8484`.
2. **Sign in with Plex** — approve Playlisterr on plex.tv (no password, no
   token hunting). Pick your server from the list of addresses that answered.
3. **Find my model** — it probes your network for a running model and
   preselects a good one. No GPU? Switch to a hosted API and paste a key.
4. Press **Save**. That's it — everything else has a working default.

Then, from the Home page:

- **Generate** — works everything out, writes nothing. Review the picks.
- **Publish these picks** — writes exactly what you're looking at.
- **Generate & publish** — a fresh set, written straight away.

A nightly run (default 03:30) keeps everyone's rows fresh. Change the cadence
in **Settings → Schedule**.

## Environment variables

Every setting can be set as `PLAYLISTERR_<SECTION>_<KEY>` (uppercase), which
overrides `settings.json`. Handy for Docker:

```yaml
environment:
  - PLAYLISTERR_PLEX_URL=http://192.168.1.10:32400
  - PLAYLISTERR_PLEX_TOKEN=xxxxxxxxxxxx
  - PLAYLISTERR_LLM_PROVIDER=ollama
  - PLAYLISTERR_LLM_URL=http://192.168.1.20:11434
  - PLAYLISTERR_LLM_MODEL=qwen2.5:14b-instruct
```

Container-specific:

| Variable | Purpose |
|---|---|
| `PUID` / `PGID` | user/group that owns `/config` |
| `TZ` | timezone for the schedule and logs |
| `PLAYLISTERR_CONFIG_DIR` | where `settings.json` lives (default `/config`) |

The full list of settings is in [CONFIGURATION.md](CONFIGURATION.md).
