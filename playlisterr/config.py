#!/usr/bin/env python3
"""Layered configuration.

Precedence, lowest to highest: built-in defaults -> settings.json in the
config directory -> PLAYLISTERR_* environment variables -> command-line flags.

Settings live in ONE json file in the config directory, Overseerr-style, so
the web UI (M2) can rewrite it without a yaml dependency. Nothing
environment-specific is ever hardcoded anywhere else in the codebase: if a
value differs between two homelabs, it belongs in DEFAULTS below.
"""

import json
import os
import secrets
import sys
import uuid

# The whole configuration surface. Every leaf is settable from settings.json,
# from an environment variable, and (for the ones the CLI exposes) a flag.
#
# Env var names are derived mechanically: PLAYLISTERR_<SECTION>_<KEY>, uppercased.
# PLAYLISTERR_PLEX_TOKEN, PLAYLISTERR_LLM_MODEL, PLAYLISTERR_RECOMMENDATIONS_MIN_PLAYS...
DEFAULTS = {
    "plex": {
        # http://host:32400 — the only genuinely required setting, along with
        # the token. Everything else has a working default.
        "url": "",
        "token": "",
        # Library titles to draw candidates from. Empty means "every movie and
        # show library on the server". Section *keys* are deliberately not
        # settable: they differ per server and mean nothing to a human.
        "movie_libraries": [],
        "show_libraries": [],
        # Libraries never to recommend from even when the lists above are empty.
        "exclude_libraries": [],
        "timeout": 60,
    },
    "llm": {
        # "ollama"  — a local Ollama server. Never needs an API key.
        # "openai"  — OpenAI's hosted API. Always needs a key.
        # "custom"  — anything else that speaks the OpenAI API: LM Studio,
        #             llama.cpp, vLLM, OpenRouter, or Ollama's compatibility
        #             endpoint on another machine. Key optional.
        "provider": "ollama",
        "url": "http://localhost:11434",
        "model": "qwen2.5:14b-instruct",
        "api_key": "",
        "temperature": 0.4,
        # Cold-loading a 14B model costs ~110s. Keeping it resident across the
        # per-user loop is the difference between a 4-minute and a 40-minute run.
        "keep_alive": "10m",
        "timeout": 600,
        # How many candidate titles to show the model. 200 lands around 4k
        # tokens, which measured 31s/user on an RTX 3090 with a 14B model.
        "candidates": 200,
        # Override the model's system prompt. Blank uses the built-in default,
        # which is what almost everyone should do. Editing it is an escape
        # hatch behind a warning in the UI: drop the JSON-contract lines and
        # every pick fails validation and falls back to the rule-based list.
        "system_prompt": "",
    },
    "recommendations": {
        "picks": 15,
        "history_days": 180,
        # Below this many plays in the window a user gets the fallback row
        # instead of a personalized one — the LLM cannot infer a taste from
        # three plays, it can only invent one.
        "min_plays": 10,
        # Plays alone are not enough: someone who watched 21 episodes of one
        # show has a large play count and a taste profile of exactly one
        # title, and the model responds by writing about that show's plot
        # instead of about them. Distinct titles is the honest signal.
        "min_titles": 3,
        "collection_title": "For {name}",
        # One row per library rather than one row per person. Playlists are
        # per-account, so every viewer can have their own "Movies Playlist"
        # without the names colliding — and a viewer who watches films, kids
        # films and TV gets a row in each instead of one mixed bag.
        # {library} is the Plex library name; {name} and {username} also work.
        "per_library": True,
        "playlist_title": "Generated {library}",
        # Whose taste a per-library row follows.
        #   "library" — only what was watched in that library. One Plex
        #               account is often several people, so the kids' films
        #               row should follow the kids' viewing, not yours.
        #   "account" — one taste for every row, from everything watched.
        "taste_scope": "library",
        # Below this many plays in a library there is not enough there to
        # read a taste from, so that row falls back to the whole account.
        "library_min_plays": 5,
        # Don't build rows for every library someone has ever opened.
        "max_libraries": 4,
        "fallback_title": "House Picks",
        # Collections Playlisterr owns carry "<prefix>-<accountId>". Nothing
        # without this prefix is ever modified or deleted.
        "label_prefix": "playlisterr",
        # Drop candidates rated below this (0 disables). Plex ratings are /10.
        "min_rating": 0.0,
        "exclude_genres": [],
        # Titles never to recommend, however well they match. Matched on a
        # normalized title (case/punctuation/leading-article insensitive), so
        # the same veto works across a re-scan or a slightly different edition.
        # The UI's "never recommend" button appends here.
        "exclude_titles": [],
        # Recommend movies, shows, or both.
        "include_movies": True,
        "include_shows": True,
        # Where the published collection actually shows up. Creating a
        # collection only files it under the library's Collections tab;
        # these three flags are what put it on a row people will see.
        "promote_to_library": True,   # "Recommended" category in the library
        "promote_to_home": True,      # shared users' home screens
        "promote_to_owner_home": True,
        # Where the row sits on the library page. Plex appends new rows at
        # the bottom, under ten built-in ones — far enough down that a
        # working feature looks broken. "top" moves it just under Continue
        # Watching; "bottom" leaves Plex's default.
        "library_row_position": "top",
        # How each viewer's row reaches them.
        #   "playlist"   — per-account playlists (default). Plex ties a
        #                  playlist to an account, so each person sees only
        #                  their own, owner included. Shows are represented by
        #                  their next unwatched episode, because a playlist
        #                  expands a series into every episode it has.
        #   "collection" — library category rows. These look better, but a
        #                  collection is server-wide: everyone with access to
        #                  the library sees every user's row, owner included.
        "delivery": "playlist",
    },
    "users": {
        # "all" builds rows for everyone with a Plex account on this server.
        # "selected" builds them only for the accounts listed below, which is
        # what you want when some people share the server but did not ask for
        # this.
        "mode": "all",
        # Plex account ids. The UI writes these; they are opaque numbers, so
        # it also stores the usernames alongside for readability.
        "selected": [],
        "selected_names": [],
    },
    "notifications": {
        # Where to POST a one-line summary when a run finishes. Works with any
        # webhook that accepts a POST: ntfy, Gotify, Discord, Slack, or a
        # generic JSON receiver (Home Assistant, n8n...). Blank disables it.
        "webhook_url": "",
        # How to shape the body for the receiver:
        #   json    — {"title","message","success","summary"} (generic)
        #   discord — {"content"}          (Discord / Slack-compatible)
        #   slack   — {"text"}
        #   ntfy    — plain text body, Title header
        #   gotify  — {"title","message"}
        "format": "json",
        "on_success": True,
        "on_failure": True,
    },
    "history": {
        # "auto" prefers Tautulli when it is configured and actually has more
        # history than Plex, otherwise uses Plex. "plex" and "tautulli" force it.
        "provider": "auto",
    },
    "tautulli": {"url": "", "api_key": "", "timeout": 60},
    "server": {
        "host": "0.0.0.0",
        "port": 8484,
        # Generated on first run and shown in Settings -> General, like every
        # other *arr app.
        "api_key": "",
        # How often to rebuild everyone's rows.
        #   daily / twice_daily / every_6h / manual
        "schedule_mode": "daily",
        # Anchor time, local, for daily and twice_daily.
        "schedule_time": "03:30",
        # Off by default: this is a LAN tool that normally sits behind a
        # reverse proxy with its own SSO, and a login wall in front of the
        # setup wizard is how first-run experiences die. Turn it on for a
        # directly exposed instance; the API key then guards every endpoint.
        "auth_required": False,
    },
    "general": {
        # Identifies this Playlisterr install to plex.tv, for sign-in and for
        # filter writes. Generated once and then stable, like every Plex
        # client. Changing it makes plex.tv treat you as a new device.
        "client_id": "",
        "log_level": "info",
        # Blank means "whatever the container/host says". TZ env wins.
        "timezone": "",
    },
}

# Leaves whose values must never reach a log line or an HTTP echo.
SECRET_KEYS = {"token", "api_key", "password"}


def default_config_dir():
    """Where settings.json lives.

    /config in a container (the *arr convention), ./config when running from
    a checkout. PLAYLISTERR_CONFIG_DIR overrides both.
    """
    env = os.environ.get("PLAYLISTERR_CONFIG_DIR")
    if env:
        return os.path.abspath(env)
    if os.path.isdir("/config") and os.access("/config", os.W_OK):
        return "/config"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "config")


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _coerce(default, raw):
    """Env vars are strings; settings.json leaves are typed. Follow the default."""
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(float(raw))
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, list):
        raw = raw.strip()
        if raw.startswith("["):
            return json.loads(raw)
        return [x.strip() for x in raw.split(",") if x.strip()]
    return raw


def _from_env(cfg):
    """Overlay PLAYLISTERR_<SECTION>_<KEY> variables."""
    for section, leaves in DEFAULTS.items():
        for key, default in leaves.items():
            name = f"PLAYLISTERR_{section}_{key}".upper()
            if name in os.environ:
                try:
                    cfg[section][key] = _coerce(default, os.environ[name])
                except (ValueError, json.JSONDecodeError) as exc:
                    raise ConfigError(f"{name}: {exc}") from exc
    return cfg


class ConfigError(Exception):
    pass


class Config:
    """Resolved settings plus the paths that hang off the config directory."""

    def __init__(self, data, config_dir):
        self.data = data
        self.dir = config_dir

    # -- access ----------------------------------------------------------
    def __getitem__(self, section):
        return self.data[section]

    def get(self, section, key, fallback=None):
        return self.data.get(section, {}).get(key, fallback)

    @property
    def settings_path(self):
        return os.path.join(self.dir, "settings.json")

    @property
    def state_path(self):
        return os.path.join(self.dir, "state.json")

    @property
    def log_dir(self):
        return os.path.join(self.dir, "logs")

    def secrets(self):
        """Every secret value currently in play, for log redaction."""
        out = set()
        for leaves in self.data.values():
            if not isinstance(leaves, dict):
                continue
            for key, val in leaves.items():
                if key in SECRET_KEYS and isinstance(val, str) and len(val) > 6:
                    out.add(val)
        return out

    # -- persistence -----------------------------------------------------
    def save(self):
        """Write settings.json. Used by the CLI's setup path and the web UI."""
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.settings_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, self.settings_path)

    # -- validation ------------------------------------------------------
    def validate(self):
        """Return a list of human-readable problems, empty when runnable."""
        problems = []
        if not self["plex"]["url"]:
            problems.append(
                "plex.url is not set (e.g. http://192.168.1.10:32400). "
                "Set it in settings.json or PLAYLISTERR_PLEX_URL.")
        elif not self["plex"]["url"].startswith(("http://", "https://")):
            problems.append("plex.url must start with http:// or https://")
        if not self["plex"]["token"]:
            problems.append(
                "plex.token is not set. Find it via Plex web: any item -> "
                "... -> Get Info -> View XML, then copy X-Plex-Token from "
                "the address bar. Set PLAYLISTERR_PLEX_TOKEN.")
        if self["llm"]["provider"] not in ("ollama", "openai", "custom"):
            problems.append(
                "llm.provider must be 'ollama', 'openai' or 'custom'")
        if self["llm"]["provider"] == "openai" and not self["llm"]["api_key"]:
            problems.append(
                "llm.api_key is required when the provider is OpenAI")
        if self["server"]["schedule_mode"] not in (
                "daily", "twice_daily", "every_6h", "manual"):
            problems.append("server.schedule_mode must be daily, "
                            "twice_daily, every_6h or manual")
        if not self["llm"]["url"]:
            problems.append("llm.url is not set")
        if not self["llm"]["model"]:
            problems.append("llm.model is not set")
        rec = self["recommendations"]
        if rec["picks"] < 1:
            problems.append("recommendations.picks must be at least 1")
        if self["llm"]["candidates"] < rec["picks"]:
            problems.append(
                "llm.candidates must be at least recommendations.picks")
        if not rec["include_movies"] and not rec["include_shows"]:
            problems.append(
                "recommendations: at least one of include_movies / "
                "include_shows must be true")
        if "{name}" not in rec["collection_title"]:
            problems.append(
                "recommendations.collection_title must contain {name}")
        if self["history"]["provider"] not in ("auto", "plex", "tautulli"):
            problems.append(
                "history.provider must be 'auto', 'plex' or 'tautulli'")
        if self["history"]["provider"] == "tautulli" and \
                not self["tautulli"]["url"]:
            problems.append(
                "history.provider is 'tautulli' but tautulli.url is not set")
        if self["notifications"]["webhook_url"] and \
                self["notifications"]["format"] not in (
                    "json", "discord", "slack", "ntfy", "gotify"):
            problems.append("notifications.format must be json, discord, "
                            "slack, ntfy or gotify")
        return problems


def load(config_dir=None, overrides=None):
    """Build the effective configuration."""
    config_dir = os.path.abspath(config_dir or default_config_dir())
    data = {k: dict(v) for k, v in DEFAULTS.items()}

    path = os.path.join(config_dir, "settings.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = _deep_merge(data, json.load(fh))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

    data = _from_env(data)

    for section, leaves in (overrides or {}).items():
        for key, val in leaves.items():
            if val is not None:
                data.setdefault(section, {})[key] = val

    # An API key the user never chose is better than an unprotected UI, and
    # generating it here means the container comes up usable.
    if not data["server"]["api_key"]:
        data["server"]["api_key"] = secrets.token_hex(16)
    if not data["general"]["client_id"]:
        data["general"]["client_id"] = str(uuid.uuid4())

    return Config(data, config_dir)


def write_example(path):
    """Emit config.example.json from DEFAULTS so the two can never drift."""
    example = {k: dict(v) for k, v in DEFAULTS.items()}
    example["plex"]["url"] = "http://192.168.1.10:32400"
    example["plex"]["token"] = "YOUR_PLEX_TOKEN"
    example["llm"]["url"] = "http://192.168.1.20:11434"
    example["server"]["api_key"] = ""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(example, fh, indent=2)
        fh.write("\n")


if __name__ == "__main__":  # python -m playlisterr.config > effective settings
    cfg = load()
    redacted = {s: {k: ("***" if k in SECRET_KEYS and v else v)
                    for k, v in leaves.items()}
                for s, leaves in cfg.data.items()}
    json.dump({"config_dir": cfg.dir, "settings": redacted}, sys.stdout,
              indent=2)
    print()
