#!/usr/bin/env python3
"""The web UI and its API — the \\*arr shell around the pipeline.

Deliberately the same shape as the apps this sits next to: one port, settings
edited in the browser and stored in ``/config``, a Test button beside every
connection, an API key for programmatic access, and a System page with the
run history and the log.

It owns no logic. Every endpoint calls the same pipeline functions the CLI
calls, which is why the two can never drift apart.
"""

import hmac
import json
import mimetypes
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import __version__, config as config_mod, http as http_mod, \
    notify as notify_mod
from ..log import get
from ..pipeline import catalog as catalog_mod, history as history_mod, \
    run as run_mod, users as users_mod
from ..providers import LLM, Plex, PlexAuth, PlexTv, Tautulli
from ..providers import llm as llm_mod
from ..state import State

log = get("web")

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Windows registry mime tables get these wrong often enough to be worth
# stating outright.
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("image/x-icon", ".ico")

# Endpoints reachable without an API key: the health probe a container
# healthcheck uses, and the UI shell itself.
OPEN_PATHS = {"/ping", "/", "/index.html", "/favicon.ico"}

# Endpoints that touch a saved secret or reach out to a caller-named host.
# These require the API key EVEN WHEN auth_required is off, because the app
# defaults to open on the LAN and these are the CSRF/SSRF-reachable surfaces.
# The read-only UI (status, settings-with-secrets-masked, users, runs, logs)
# stays open so the dashboard works out of the box; these do not.
SENSITIVE_PATHS = {"/api/test", "/api/models", "/api/llm/probe",
                   "/api/plex/servers", "/api/plex/pin", "/api/plex/reset",
                   "/api/notify/test", "/api/backup"}


def _reachable(uri, token, timeout=3):
    """Does this Plex address actually answer for us?"""
    try:
        http_mod.get_json(http_mod.url_join(uri, "/identity",
                                            {"X-Plex-Token": token}),
                          timeout=timeout, retries=0)
        return True
    except Exception:
        return False


class Runner:
    """Owns the one run allowed at a time, and the log of what it did."""

    def __init__(self, cfg_holder):
        self._cfg = cfg_holder
        self.lock = threading.Lock()
        self.thread = None
        self.started = 0
        self.dry = True
        self.progress = ""
        self.result = None
        self.error = ""

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, dry=True, only=None, preview=False):
        with self.lock:
            if self.running:
                return False
            self.error = ""
            self.result = None
            self.dry = dry
            self.started = int(time.time())
            self.progress = "starting"
            target = self._publish_preview if preview else self._run
            args = () if preview else (dry, only)
            self.thread = threading.Thread(target=target, args=args,
                                           daemon=True)
            self.thread.start()
            return True

    def _publish_preview(self):
        """Write out exactly what the last dry run decided."""
        cfg = self._cfg()
        state = State(cfg.state_path)
        payload = state.preview
        if not payload:
            self.error = "no saved preview to publish"
            self.progress = "failed"
            return
        try:
            self.progress = "publishing the saved preview"
            written, errors = run_mod.apply_preview(cfg, payload)
            summary = {"at": int(time.time()), "seconds": 0, "dry": False,
                       "from_preview": True,
                       "history_source": "saved preview",
                       "catalog_items": 0, "users": payload.get("users", 0),
                       "personalized": payload.get("personalized", 0),
                       "house": 0, "written": written, "errors": errors,
                       "picks": {}}
            state.record_run(summary)
            state.save()
            self.progress = "done"
            notify_mod.notify_run(cfg, summary)
        except Exception as exc:
            self.error = http_mod.redact(str(exc)) or type(exc).__name__
            self.progress = "failed"
            log.error("publishing preview failed: %s", self.error)
            notify_mod.notify_run(cfg, {"failed": True, "dry": False,
                                        "errors": [self.error]})

    def _run(self, dry, only):
        cfg = self._cfg()
        state = State(cfg.state_path)
        try:
            self.progress = "reading plex"
            result = run_mod.run(cfg, dry=dry, only=only, state=state)
            self.result = result
            summary = result.summary()
            state.record_run(summary)
            if dry:
                # Keep what this preview decided so it can be published
                # verbatim instead of being re-decided by the model.
                state.save_preview(run_mod.preview_payload(result, cfg))
            else:
                state.clear_preview()
            state.save()
            self.progress = "done"
            # Notify only on a real publish; a generate writes nothing worth
            # pinging a phone about.
            if not dry:
                notify_mod.notify_run(cfg, summary)
        except Exception as exc:
            self.error = http_mod.redact(str(exc)) or type(exc).__name__
            self.progress = "failed"
            log.error("run failed: %s", self.error)
            # Record the failure so it appears in run history instead of
            # vanishing — a silent gap in the history of an unattended nightly
            # job is exactly what you do not want.
            failure = {
                "at": int(time.time()), "seconds": 0, "dry": dry,
                "failed": True, "history_source": "-", "catalog_items": 0,
                "users": 0, "personalized": 0, "house": 0, "written": 0,
                "errors": [self.error], "picks": {}}
            try:
                state.record_run(failure)
                state.save()
            except Exception:
                pass
            notify_mod.notify_run(cfg, failure)

    def status(self):
        out = {"running": self.running, "dry": self.dry,
               "started": self.started, "progress": self.progress,
               "error": self.error}
        if self.result:
            out["summary"] = self.result.summary()
        return out


class Handler(BaseHTTPRequestHandler):
    server_version = f"Playlisterr/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------
    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status, body=b"", content_type="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status=200):
        self._send(status, json.dumps(payload, default=str))

    def _error(self, status, message):
        self._json({"error": http_mod.redact(message)}, status)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _is_json_post(self):
        """A real UI/API request sends application/json. A cross-site HTML
        form can only send urlencoded/plain/multipart, so requiring JSON on
        state-changing POSTs is a second CSRF barrier behind the API key."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        return ctype == "application/json"

    def _api_key_ok(self):
        cfg = self.server.cfg()
        given = (self.headers.get("X-Api-Key")
                 or urllib.parse.parse_qs(
                     urllib.parse.urlparse(self.path).query
                 ).get("apikey", [""])[0])
        real = cfg["server"]["api_key"] or ""
        return bool(given) and hmac.compare_digest(str(given), str(real))

    def _authorized(self, path):
        """Who may reach a path.

        The static shell and read-only dashboard are open by default so the
        app works out of the box on a trusted LAN. But endpoints that touch a
        saved secret or fetch a caller-named host require the API key ALWAYS,
        regardless of auth_required — that requirement is what stops a
        cross-site page (which cannot read the key or set the header) from
        driving them via CSRF. When auth_required is on, everything but the
        static shell needs the key.
        """
        if path in OPEN_PATHS or path.startswith("/static/"):
            return True
        if path in SENSITIVE_PATHS:
            return self._api_key_ok()
        if self.server.cfg()["server"].get("auth_required"):
            return self._api_key_ok()
        return True

    # -- routing ---------------------------------------------------------
    def do_GET(self):
        try:
            return self._route_get()
        except Exception as exc:  # never drop the connection with no response
            log.error("GET %s failed: %s", self.path, exc)
            return self._error(500, http_mod.redact(str(exc)) or "error")

    def _route_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if not self._authorized(path):
            return self._error(401, "api key required")

        if path == "/ping":
            return self._json({"status": "ok", "version": __version__})
        if path in ("/", "/index.html"):
            return self._index()
        if path.startswith("/static/"):
            return self._file(os.path.join(STATIC, os.path.basename(path)))
        if path == "/favicon.ico":
            return self._file(os.path.join(STATIC, "favicon.ico"))

        if path == "/api/status":
            return self._json(self._status())
        if path == "/api/health":
            return self._json(self.server.health())
        if path == "/api/settings":
            return self._json(self._settings())
        if path == "/api/models":
            return self._json(self._models(query))
        if path == "/api/users":
            return self._json(self._users())
        if path == "/api/plex/pin":
            return self._json(self._pin_check(query))
        if path == "/api/plex/servers":
            return self._json(self._plex_servers(query))
        if path == "/api/libraries":
            return self._json(self._libraries())
        if path == "/api/llm/default-prompt":
            from ..pipeline.pick import DEFAULT_SYSTEM
            return self._json({"prompt": DEFAULT_SYSTEM})
        if path == "/api/llm/probe":
            return self._json(self._llm_probe())
        if path == "/api/run":
            return self._json(self.server.runner.status())
        if path == "/api/runs":
            state = State(self.server.cfg().state_path)
            # The history table wants counts, not every pick of every run —
            # sending those was a quarter-megabyte response for a six-column
            # table.
            runs = [{k: v for k, v in run.items() if k != "picks"}
                    for run in state.runs[:20]]
            return self._json({"runs": runs})
        if path == "/api/logs":
            try:
                lines = int(query.get("lines", ["300"])[0])
            except (TypeError, ValueError):
                lines = 300
            return self._json({"lines": self._logs(max(1, min(lines, 5000)))})
        if path == "/api/logs/download":
            return self._download_logs()
        if path == "/api/backup":
            return self._backup()
        return self._error(404, f"no such endpoint: {path}")

    def do_POST(self):
        try:
            return self._route_post()
        except Exception as exc:
            log.error("POST %s failed: %s", self.path, exc)
            return self._error(500, http_mod.redact(str(exc)) or "error")

    def _route_post(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._authorized(path):
            return self._error(401, "api key required")
        # State-changing POSTs must be JSON — blocks cross-site form CSRF.
        if not self._is_json_post():
            return self._error(415, "expected application/json")
        body = self._body()

        if path == "/api/settings":
            return self._json(self._save_settings(body))
        if path == "/api/test":
            return self._json(self._test(body))
        if path == "/api/notify/test":
            ok, message = notify_mod.send_test(
                self.server.cfg(), url=(body.get("url") or "").strip() or None,
                fmt=body.get("format"))
            return self._json({"ok": ok, "message":
                               "Test notification sent." if ok
                               else f"Could not send: {message}"})
        if path == "/api/plex/pin":
            return self._json(self._pin_create())
        if path == "/api/plex/reset":
            return self._json(self._plex_reset())
        if path == "/api/users":
            return self._json(self._save_users(body))
        if path == "/api/exclude":
            return self._json(self._exclude_title(body))
        if path == "/api/run":
            started = self.server.runner.start(
                dry=bool(body.get("dry", True)),
                only=body.get("users") or None,
                preview=bool(body.get("publish_preview")))
            if not started:
                return self._error(409, "a run is already in progress")
            return self._json({"started": True})
        return self._error(404, f"no such endpoint: {path}")

    def do_HEAD(self):
        self.do_GET()

    # -- handlers --------------------------------------------------------
    def _index(self):
        """Serve the shell with the API key injected, so the frontend can
        authenticate itself. Same-origin JS can read this; a cross-origin page
        cannot (same-origin policy), which is what makes it a usable CSRF
        token as well as a login credential."""
        path = os.path.join(STATIC, "index.html")
        if not os.path.isfile(path):
            return self._error(404, "not found")
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        key = self.server.cfg()["server"]["api_key"] or ""
        demo = "true" if getattr(self.server, "demo", False) else "false"
        inject = (f'<script>window.PLAYLISTERR_KEY={json.dumps(key)};'
                  f'window.PLAYLISTERR_DEMO={demo};</script>')
        html = html.replace("</head>", inject + "</head>", 1)
        self._send(200, html, "text/html; charset=utf-8")

    def _file(self, path):
        # Serve only real files from STATIC, by basename — no traversal.
        real = os.path.realpath(path)
        if os.path.dirname(real) != os.path.realpath(STATIC) \
                or not os.path.isfile(real):
            return self._error(404, "not found")
        ctype = mimetypes.guess_type(real)[0] or "application/octet-stream"
        with open(real, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def _status(self):
        cfg = self.server.cfg()
        state = State(cfg.state_path)
        problems = cfg.validate()
        return {
            "version": __version__,
            # In demo mode show the container path a real deploy uses rather
            # than the throwaway temp dir the demo happens to run from.
            "config_dir": "/config" if getattr(self.server, "demo", False)
            else cfg.dir,
            "configured": not problems,
            "problems": problems,
            "last_run": state.data.get("last_run"),
            "preview": ({"at": state.preview["at"],
                         "rows": len(state.preview.get("rows") or [])
                                 + len(state.preview.get("collections") or []),
                         "titles": state.preview.get("titles", 0),
                         "users": state.preview.get("users", 0)}
                        if state.preview else None),
            "run": self.server.runner.status(),
            "schedule": cfg["server"]["schedule_mode"],
            "schedule_time": cfg["server"]["schedule_time"],
            "next_run": self.server.scheduler.next_run_text(),
        }

    def _settings(self):
        cfg = self.server.cfg()
        data = {}
        for section, leaves in cfg.data.items():
            data[section] = {}
            for key, value in leaves.items():
                # Never send a secret to the browser. The UI shows a filled
                # placeholder and only sends a value back when it is changed.
                if key in config_mod.SECRET_KEYS and value:
                    data[section][key] = "__set__"
                else:
                    data[section][key] = value
        return {"settings": data, "config_dir": cfg.dir}

    def _save_settings(self, body):
        cfg = self.server.cfg()
        incoming = body.get("settings") or {}

        # Resolve a wizard-chosen server's opaque token ref back into the real
        # token, server-side. The browser never held the token itself.
        plex_in = incoming.get("plex") or {}
        ref = plex_in.pop("token_ref", None)
        if ref and ref in self.server.server_tokens:
            plex_in["token"] = self.server.server_tokens[ref]
        elif ref and self.server.pending_token:
            plex_in["token"] = self.server.pending_token

        for section, leaves in incoming.items():
            if section not in config_mod.DEFAULTS:
                continue
            for key, value in (leaves or {}).items():
                if key not in config_mod.DEFAULTS[section]:
                    continue
                # "__set__" means "unchanged" — do not overwrite the real
                # secret with the placeholder the browser was shown.
                if key in config_mod.SECRET_KEYS and value == "__set__":
                    continue
                default = config_mod.DEFAULTS[section][key]
                try:
                    # A string value ("false", "3", "a, b") is coerced the same
                    # way an env var would be, so POSTing "false" cannot save
                    # True. Non-string values (real JSON bools/ints) pass
                    # through the type checks below.
                    if isinstance(value, str) and not isinstance(default, str):
                        value = config_mod._coerce(default, value)
                    elif isinstance(default, bool):
                        value = bool(value)
                    elif isinstance(default, int) \
                            and not isinstance(value, bool):
                        value = int(value)
                    elif isinstance(default, float):
                        value = float(value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    return {"ok": False,
                            "error": f"{section}.{key}: bad value {value!r}"}
                cfg.data[section][key] = value
        cfg.save()
        self.server.reload()
        cfg = self.server.cfg()
        for secret in cfg.secrets():
            http_mod.register_secret(secret)
        self.server.scheduler.reschedule()
        return {"ok": True, "problems": cfg.validate()}

    def _plex_reset(self):
        """Clear the saved Plex connection — server address, token, and both
        library lists — so the connect-from-scratch flow can be tested or a
        different server linked. Nothing outside the ``plex`` section is
        touched, and any in-memory token from a half-finished sign-in is
        dropped too."""
        cfg = self.server.cfg()
        cfg.data["plex"]["url"] = ""
        cfg.data["plex"]["token"] = ""
        cfg.data["plex"]["movie_libraries"] = []
        cfg.data["plex"]["show_libraries"] = []
        cfg.data["plex"]["exclude_libraries"] = []
        cfg.save()
        self.server.server_tokens.clear()
        self.server.pending_token = ""
        self.server.reload()
        return {"ok": True}

    def _test(self, body):
        """Test a connection using the values on screen, not the saved ones.

        The point of a Test button is to check credentials *before* saving
        them, so anything supplied in the request wins over config.

        Security: a request-supplied URL is NEVER paired with a stored secret.
        If the caller sends a different URL than the saved one, they must send
        the matching secret too — otherwise the test is refused. Without this,
        an unauthenticated POST could name an attacker URL and no token, and
        the handler would obligingly send the real saved Plex/Tautulli
        credential there in the outbound query string.
        """
        cfg = self.server.cfg()
        target = body.get("target", "")
        given = body.get("values") or {}

        # A just-completed "Sign in with Plex" hands the browser an opaque
        # token ref, never the token itself. Resolve it to the real token here
        # so Test — and the library list it returns — work before Save. Only a
        # ref the server itself minted resolves, so this leaks nothing.
        ref = given.get("token_ref") if target == "plex" else None
        if ref:
            token = self.server.server_tokens.get(ref) or \
                self.server.pending_token
            if token:
                given = dict(given, token=token)

        def supplied(key):
            v = given.get(key)
            return v if v and v != "__set__" else None

        url_keys = {"plex": "url", "llm": "url", "tautulli": "url"}
        secret_keys = {"plex": "token", "llm": "api_key",
                       "tautulli": "api_key"}
        url_key = url_keys.get(target)
        secret_key = secret_keys.get(target)
        if url_key and supplied(url_key) is not None:
            saved_url = cfg.get(target, url_key, "")
            if supplied(url_key) != saved_url and secret_key \
                    and supplied(secret_key) is None \
                    and cfg.get(target, secret_key, ""):
                return {"ok": False, "message":
                        "To test a different address, enter its credential "
                        "too — Playlisterr will not send a saved secret to a "
                        "server you just typed in."}

        def value(section, key):
            got = supplied(key)
            return got if got is not None else cfg[section][key]

        try:
            if target == "plex":
                plex = Plex(value("plex", "url"), value("plex", "token"))
                info = plex.ping()
                sections = plex.sections()
                return {"ok": True, "message":
                        f"{info['name']} v{info['version']}",
                        "detail": [f"{s.type}: {s.title}" for s in sections],
                        # Let the UI fill in the library checkboxes straight
                        # from a successful test, before anything is saved.
                        "libraries": [{"title": s.title, "type": s.type}
                                      for s in sections
                                      if s.type in ("movie", "show")]}
            if target == "llm":
                llm = LLM(provider=value("llm", "provider"),
                          url=value("llm", "url"),
                          model=value("llm", "model"),
                          api_key=value("llm", "api_key"))
                names = llm.models()
                configured = value("llm", "model")
                return {"ok": configured in names,
                        "message": (f"{len(names)} models available"
                                    if configured in names else
                                    f"connected, but {configured!r} is not "
                                    f"one of them"),
                        "detail": names}
            if target == "tautulli":
                taut = Tautulli(value("tautulli", "url"),
                                value("tautulli", "api_key"))
                if not taut.configured:
                    return {"ok": False, "message": "not configured"}
                info = taut.ping()
                return {"ok": True, "message":
                        f"{info['users']} users, "
                        f"{info['history_records']} history records"}
        except Exception as exc:
            return {"ok": False, "message": http_mod.redact(str(exc))}
        return {"ok": False, "message": f"unknown target {target!r}"}

    # -- guided setup ----------------------------------------------------
    def _auth(self):
        cfg = self.server.cfg()
        return PlexAuth(cfg["general"]["client_id"])

    def _pin_create(self):
        try:
            return dict(self._auth().create_pin(), ok=True)
        except Exception as exc:
            return {"ok": False, "message": http_mod.redact(str(exc))}

    def _pin_check(self, query):
        pin_id = (query.get("id") or [""])[0]
        if not pin_id:
            return {"ok": False, "message": "missing pin id"}
        try:
            token = self._auth().check_pin(pin_id)
        except Exception as exc:
            return {"ok": False, "message": http_mod.redact(str(exc))}
        if not token:
            return {"ok": True, "waiting": True}
        # Hold the token in memory only until a server is chosen; it is
        # written to settings.json as part of that choice, not before.
        self.server.pending_token = token
        return {"ok": True, "waiting": False, "authenticated": True}

    def _plex_servers(self, query):
        token = self.server.pending_token or self.server.cfg()["plex"]["token"]
        if not token:
            return {"ok": False, "message": "not signed in yet"}
        try:
            servers = self._auth().servers(token)
        except Exception as exc:
            return {"ok": False, "message": http_mod.redact(str(exc))}

        # The per-server access tokens NEVER go to the browser — that would
        # hand a working Plex token to any unauthenticated caller. Instead
        # each server keeps its token server-side under an opaque ref, and the
        # UI sends that ref back when it saves; _save_settings resolves it.
        out = []
        self.server.server_tokens.clear()
        for i, server in enumerate(servers):
            ref = f"srv{i}"
            self.server.server_tokens[ref] = server["access_token"]
            for conn in server["connections"]:
                conn["reachable"] = _reachable(conn["uri"],
                                               server["access_token"])
            best = next((c["uri"] for c in server["connections"]
                         if c["reachable"]), "")
            out.append({"name": server["name"], "owned": server["owned"],
                        "product": server.get("product", ""),
                        "version": server.get("version", ""),
                        "connections": server["connections"], "best": best,
                        "token_ref": ref})
        return {"ok": True, "servers": out}

    def _libraries(self):
        """Real libraries, so the UI can offer checkboxes instead of asking
        someone to type library names exactly right."""
        cfg = self.server.cfg()
        if not cfg["plex"]["url"] or not cfg["plex"]["token"]:
            return {"ok": False, "libraries": [],
                    "message": "connect Plex first"}
        try:
            sections = Plex(cfg["plex"]["url"], cfg["plex"]["token"]).sections()
        except Exception as exc:
            return {"ok": False, "libraries": [],
                    "message": http_mod.redact(str(exc))}
        return {"ok": True, "libraries": [
            {"title": s.title, "type": s.type} for s in sections
            if s.type in ("movie", "show")]}

    def _llm_probe(self):
        """Look for a model server without asking anyone for a URL."""
        cfg = self.server.cfg()
        hosts = ["127.0.0.1", "host.docker.internal"]
        # Whatever is already configured, and the box running Plex — in most
        # homelabs that is also the box with the GPU.
        for url in (cfg["llm"]["url"], cfg["plex"]["url"]):
            if not url:
                continue
            host = urllib.parse.urlparse(url).hostname
            if host and host not in hosts:
                hosts.append(host)
        found = llm_mod.probe(hosts)
        return {"ok": bool(found), "found": found, "hosts": hosts,
                "message": "" if found else
                           "nothing found on the usual ports — if your model "
                           "runs elsewhere, enter its URL; if you have no GPU, "
                           "switch the provider to an OpenAI-compatible API."}

    def _models(self, query):
        cfg = self.server.cfg()
        provider = (query.get("provider") or [cfg["llm"]["provider"]])[0]
        url = (query.get("url") or [cfg["llm"]["url"]])[0]
        api_key = (query.get("api_key") or [""])[0]
        if api_key == "__set__":
            api_key = ""
        # Never send the saved API key to a URL the caller typed. The stored
        # key is used only when probing the *saved* url; a different url must
        # carry its own key or none at all. (Same class of bug as _test.)
        if not api_key and url == cfg["llm"]["url"]:
            api_key = cfg["llm"]["api_key"]
        try:
            names = LLM(provider=provider, url=url, model=cfg["llm"]["model"],
                        api_key=api_key).models()
            return {"ok": True, "models": names, "provider": provider,
                    "url": url}
        except Exception as exc:
            return {"ok": False, "models": [],
                    "message": http_mod.redact(str(exc))}

    def _users(self):
        cfg = self.server.cfg()
        if cfg.validate():
            return {"users": [], "error": "not configured yet"}
        plex, _, tautulli = run_mod.build_clients(cfg)
        catalog = catalog_mod.build(plex, cfg)
        source, watched = history_mod.collect(plex, tautulli, catalog, cfg)
        discovered = users_mod.discover(plex, cfg, watched)
        min_plays = cfg["recommendations"]["min_plays"]
        min_titles = cfg["recommendations"]["min_titles"]
        rows = []
        for user in discovered:
            entry = watched.get(user["account_id"])
            plays = entry.plays if entry else 0
            titles = len(entry.keys) if entry else 0
            rows.append({
                "username": user["username"],
                "display_name": user["display_name"],
                "account_id": user["account_id"],
                "shared": user["shared"],
                "plays": plays,
                "titles": titles,
                "tier": ("personalized"
                         if plays >= min_plays and titles >= min_titles
                         else "house"),
            })
        mode = cfg["users"]["mode"]
        chosen = {str(a) for a in cfg["users"]["selected"]}
        for row in rows:
            row["enabled"] = (mode == "all"
                              or row["account_id"] in chosen)
        rows.sort(key=lambda r: -r["plays"])
        return {"users": rows, "history_source": source,
                "catalog": len(catalog.items), "mode": mode,
                "window_days": cfg["recommendations"]["history_days"]}

    def _exclude_title(self, body):
        """Add a title to the never-recommend list (the per-pick button)."""
        title = (body.get("title") or "").strip()
        if not title:
            return {"ok": False, "error": "no title"}
        cfg = self.server.cfg()
        current = list(cfg["recommendations"].get("exclude_titles", []))
        # Strip a trailing " (year)" so the stored veto matches the catalog
        # title, which is what the pipeline normalizes against.
        base = title.rsplit(" (", 1)[0].strip() if title.endswith(")") \
            else title
        if base.lower() not in [t.lower() for t in current]:
            current.append(base)
            cfg.data["recommendations"]["exclude_titles"] = current
            cfg.save()
            self.server.reload()
        return {"ok": True, "title": base, "count": len(current)}

    def _save_users(self, body):
        """Store who Playlisterr builds rows for."""
        cfg = self.server.cfg()
        mode = body.get("mode", "all")
        if mode not in ("all", "selected"):
            return {"ok": False, "error": "mode must be all or selected"}
        cfg.data["users"]["mode"] = mode
        cfg.data["users"]["selected"] = [str(a) for a in
                                         (body.get("selected") or [])]
        cfg.data["users"]["selected_names"] = list(body.get("names") or [])
        cfg.save()
        self.server.reload()
        return {"ok": True, "mode": mode,
                "count": len(cfg.data["users"]["selected"])}

    def _logs(self, lines):
        path = os.path.join(self.server.cfg().log_dir, "playlisterr.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [line.rstrip() for line in fh.readlines()[-lines:]]

    def _attach(self, data, filename, ctype):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _download_logs(self):
        """The whole current log file, for attaching to a bug report.

        Already redacted at write time, so this ships no tokens."""
        path = os.path.join(self.server.cfg().log_dir, "playlisterr.log")
        text = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        self._attach(text or "(no log yet)\n", "playlisterr.log",
                     "text/plain; charset=utf-8")

    def _backup(self):
        """A zip of settings.json + state.json.

        settings.json is your whole configuration and state.json is the run
        history — the two files worth keeping if the /config volume is lost.
        Built in memory with the stdlib.
        """
        import io
        import zipfile
        cfg = self.server.cfg()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, path in (("settings.json", cfg.settings_path),
                               ("state.json", cfg.state_path)):
                if os.path.exists(path):
                    zf.write(path, name)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        self._attach(buf.getvalue(), f"playlisterr-backup-{stamp}.zip",
                     "application/zip")


class Scheduler(threading.Thread):
    """The nightly job, in the shape *arr users expect from System → Tasks."""

    def __init__(self, server):
        super().__init__(daemon=True)
        self.server = server
        self.wake = threading.Event()
        self._next = 0
        self.reschedule()

    def reschedule(self):
        """Work out the next run from the mode and the anchor time."""
        cfg = self.server.cfg()
        mode = cfg["server"]["schedule_mode"]
        if mode == "manual":
            self._next = 0
            self.wake.set()
            return
        text = (cfg["server"]["schedule_time"] or "03:30").strip()
        try:
            hour, minute = (int(x) for x in text.split(":", 1))
        except ValueError:
            log.warning("bad schedule_time %r — expected HH:MM", text)
            hour, minute = 3, 30

        interval = {"daily": 86400, "twice_daily": 43200,
                    "every_6h": 21600}.get(mode, 86400)
        now = time.localtime()
        stamp = time.mktime(time.struct_time(
            (now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0, 0, 0, -1)))
        # Step forward from the anchor so every mode keeps the chosen minute
        # rather than drifting from whenever the container happened to start.
        while stamp <= time.time():
            stamp += interval
        while stamp - interval > time.time():
            stamp -= interval
        self._next = stamp
        self.wake.set()

    def next_run_text(self):
        if not self._next:
            return "disabled"
        return time.strftime("%a %d %b %H:%M", time.localtime(self._next))

    def run(self):
        while True:
            self.wake.clear()
            delay = max(5, self._next - time.time()) if self._next else 3600
            if self.wake.wait(timeout=delay):
                continue          # rescheduled; recompute
            if not self._next or time.time() < self._next:
                continue
            cfg = self.server.cfg()
            if cfg.validate():
                log.warning("scheduled run skipped: configuration incomplete")
                self.reschedule()
            else:
                log.info("scheduled run starting")
                # Scheduled runs publish. A schedule that only ever dry-runs
                # would be a cron job that does nothing.
                if self.server.runner.start(dry=False):
                    self.reschedule()
                else:
                    # A run is already going (a long cold-model run can overrun
                    # into the next slot). Retry in a few minutes rather than
                    # silently skipping until tomorrow.
                    log.info("a run is already in progress; retrying in 5 min")
                    self._next = time.time() + 300


class PlaylisterrServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, config_dir):
        super().__init__(address, Handler)
        self.config_dir = config_dir
        self._cfg = config_mod.load(config_dir)
        self.runner = Runner(self.cfg)
        # Held between "signed in" and "server chosen"; never persisted here.
        self.pending_token = ""
        # Opaque-ref -> real Plex token for the servers offered to the wizard,
        # so a working token never travels to the browser. Cleared each time
        # the server list is re-fetched.
        self.server_tokens = {}
        self.demo = False
        self._health = None
        self._health_at = 0
        self._health_lock = threading.Lock()
        self.scheduler = Scheduler(self)
        self.scheduler.start()

    def cfg(self):
        return self._cfg

    def reload(self):
        self._cfg = config_mod.load(self.config_dir)
        # Config changed — the next health check should re-probe, not serve a
        # cached result from the old settings.
        self._health = None

    def health(self, ttl=300):
        """Reachability of each configured service, cached for a few minutes.

        "configured" is not "working": a dead Ollama or an expired Plex token
        only shows up at 03:30 without this. Probing is a couple of network
        round-trips, so it is cached and computed under a lock so a burst of
        page loads triggers one probe, not one per request.
        """
        with self._health_lock:
            if self._health and time.time() - self._health_at < ttl:
                return self._health
            self._health = self._probe_health()
            self._health_at = int(time.time())
            return self._health

    def _probe_health(self):
        cfg = self._cfg
        checks = []

        def check(name, required, fn):
            try:
                checks.append({"name": name, "ok": True, "required": required,
                               "detail": fn()})
            except Exception as exc:
                checks.append({"name": name, "ok": False, "required": required,
                               "detail": http_mod.redact(str(exc))})

        if cfg["plex"]["url"] and cfg["plex"]["token"]:
            check("Plex", True, lambda: Plex(
                cfg["plex"]["url"], cfg["plex"]["token"]).ping().get("name",
                                                                     "ok"))
        if cfg["llm"]["url"] and cfg["llm"]["model"]:
            def llm_ok():
                info = LLM(provider=cfg["llm"]["provider"],
                           url=cfg["llm"]["url"], model=cfg["llm"]["model"],
                           api_key=cfg["llm"]["api_key"]).ping()
                if not info["has_configured_model"]:
                    raise RuntimeError(
                        f"{cfg['llm']['model']} not offered by the endpoint")
                return f"{info['models']} models"
            check("LLM", True, llm_ok)
        taut = Tautulli(cfg["tautulli"]["url"], cfg["tautulli"]["api_key"])
        if taut.configured:
            check("Tautulli", False,
                  lambda: f"{taut.ping()['history_records']} records")

        healthy = all(c["ok"] for c in checks if c["required"])
        problems = [f"{c['name']}: {c['detail']}"
                    for c in checks if not c["ok"] and c["required"]]
        return {"healthy": healthy, "checks": checks, "problems": problems,
                "at": int(time.time())}


def serve(cfg, host=None, port=None, demo=False):
    host = host or cfg["server"]["host"]
    port = int(port or cfg["server"]["port"])
    server = PlaylisterrServer((host, port), cfg.dir)
    server.demo = demo
    shown = "localhost" if host in ("0.0.0.0", "") else host
    print(f"Playlisterr {__version__} — http://{shown}:{port}")
    print(f"config: {cfg.settings_path}")
    if cfg.validate():
        print("not configured yet — the browser will open the setup wizard")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0
