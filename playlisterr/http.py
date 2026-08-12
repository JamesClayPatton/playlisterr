#!/usr/bin/env python3
"""One tiny HTTP client for every provider.

Three things live here rather than in each provider, because getting them
wrong once is enough:

* **Redaction.** Tokens appear in Plex query strings. Register them once and
  no exception message, log line or fixture filename can leak them.
* **Retries.** Homelab services restart. A transient 502 should not kill a
  nightly run.
* **Record / replay.** Every request can be captured to a fixture and replayed
  offline, which is what lets a contributor with no Plex server develop
  against a real payload.
"""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Playlisterr/0.1 (+https://github.com/playlisterr/playlisterr)"

# Values registered via register_secret(), longest first so overlapping
# secrets redact predictably.
_SECRETS = []

# Query parameters whose values are secrets no matter what is registered.
_SECRET_PARAMS = ("X-Plex-Token", "apikey", "api_key", "apiKey", "token")

# Parameters that change on every run without changing what is being asked
# for. The history window is derived from "now", so leaving it in the fixture
# key would mean a recorded fixture could never be replayed — not tomorrow,
# not one second later.
_VOLATILE_PARAMS = ("viewedAt>", "viewedAt<", "viewedAt>>", "_", "t")

# Fixture mode: None (live), "record" or "replay".
_MODE = None
_FIXTURE_DIR = None
_RECORDED = 0


class HttpError(Exception):
    def __init__(self, message, status=None, url=None, body=None):
        super().__init__(redact(message))
        self.status = status
        self.url = redact(url or "")
        self.body = redact((body or "")[:2000])


def register_secret(value):
    if value and isinstance(value, str) and len(value) > 6:
        if value not in _SECRETS:
            _SECRETS.append(value)
            _SECRETS.sort(key=len, reverse=True)


# Token-shaped fields that appear *inside response bodies* — Plex XML
# attributes and JSON keys that carry per-user credentials. These are scrubbed
# structurally, regardless of whether the value was ever registered, because a
# recorded fixture body is the one place a secret arrives that register_secret
# never saw first.
_BODY_SECRET_KEYS = ("accessToken", "authToken", "token", "apikey",
                     "api_key", "email")
_BODY_ATTR_RE = re.compile(
    r'(\b(?:' + "|".join(_BODY_SECRET_KEYS) + r')="?)([^"&\s<>]+)',
    re.IGNORECASE)
_BODY_JSON_RE = re.compile(
    r'("(?:' + "|".join(_BODY_SECRET_KEYS) + r')"\s*:\s*")([^"]+)',
    re.IGNORECASE)


def redact(text):
    """Replace every known secret, plus anything that looks like one."""
    if not text:
        return text
    text = str(text)
    for secret in _SECRETS:
        text = text.replace(secret, "***")
    for param in _SECRET_PARAMS:
        text = re.sub(rf"({re.escape(param)}=)[^&\s\"']+", r"\1***", text)
    return text


def redact_body(text):
    """Redact a response body before it is written to a fixture.

    Everything ``redact`` does, plus structural scrubbing of token-shaped
    attributes and JSON keys — because the secrets in a body (per-user
    ``accessToken``, plex.tv ``authToken``, emails) arrive *in* the response
    and so were never registered as secrets first. Without this, --record is a
    credential-publishing machine, which is the opposite of its documented
    purpose.
    """
    text = redact(text)
    text = _BODY_ATTR_RE.sub(r"\1***", text)
    text = _BODY_JSON_RE.sub(r"\1***", text)
    return text


def use_fixtures(mode, directory):
    """Switch the whole client into record or replay mode."""
    global _MODE, _FIXTURE_DIR
    if mode not in (None, "record", "replay"):
        raise ValueError(f"bad fixture mode {mode!r}")
    _MODE = mode
    _FIXTURE_DIR = directory
    if mode == "record":
        os.makedirs(directory, exist_ok=True)


def fixture_mode():
    return _MODE


def recorded_count():
    return _RECORDED


def _fixture_path(method, url, body):
    """A stable, secret-free filename for a request.

    The hash covers the sorted query minus secret params, so the same logical
    request maps to the same fixture even when the token is rotated.
    """
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query)
             if k not in _SECRET_PARAMS and k not in _VOLATILE_PARAMS]
    query.sort()
    canonical = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path,
         urllib.parse.urlencode(query), ""))
    # The hash covers the netloc so two servers never collide; the visible
    # slug is built from the PATH only, so a fixture filename never carries a
    # LAN IP or the plex.tv machine identifier (the docstring promises a
    # secret-free filename, and a hostname in the clear is a leak).
    digest = hashlib.sha256(
        f"{method} {parts.netloc} {canonical} {body or ''}".encode()
    ).hexdigest()[:10]
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", parts.path)
    slug = slug.strip("-").lower()[:70] or "request"
    return os.path.join(_FIXTURE_DIR, f"{method.lower()}-{slug}-{digest}.json")


def _fixture_fallback(method, url):
    """A body-independent fixture name for the same path.

    A POST's exact fixture hash includes its body, so a canned response can
    never match a freshly built prompt. The shipped sample set uses this
    path-only name (suffix ``-any``) so one canned LLM reply serves any
    prompt, which is what makes an offline demo work on a fresh clone.
    """
    parts = urllib.parse.urlsplit(url)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", parts.path).strip("-").lower()[:70]
    return os.path.join(_FIXTURE_DIR, f"{method.lower()}-{slug or 'request'}"
                        f"-any.json")


def request(url, method="GET", headers=None, data=None, timeout=30,
            retries=2, backoff=1.5):
    """Perform a request, honouring the fixture mode. Returns bytes."""
    global _RECORDED
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)
    body = data.decode("utf-8", "replace") if isinstance(data, bytes) else data

    if _MODE == "replay":
        path = _fixture_path(method, url, body)
        if not os.path.exists(path):
            # Fall back to a body-independent fixture (the sample set's canned
            # LLM reply), so an offline demo works without a hash-exact match.
            fallback = _fixture_fallback(method, url)
            if os.path.exists(fallback):
                path = fallback
            else:
                raise HttpError(
                    f"no fixture for {method} {redact(url)} (expected "
                    f"{os.path.basename(path)}). Re-run with --record against "
                    f"a live server to capture it.", url=url)
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)["body"].encode("utf-8")

    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
            # 4xx means we asked wrong; retrying just asks wrong again.
            if exc.code < 500 and exc.code != 429:
                raise HttpError(f"HTTP {exc.code} for {redact(url)}",
                                status=exc.code, url=url, body=detail) from exc
            last = HttpError(f"HTTP {exc.code} for {redact(url)}",
                             status=exc.code, url=url, body=detail)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = HttpError(f"{type(exc).__name__} for {redact(url)}: {exc}",
                             url=url)
        if attempt < retries:
            time.sleep(backoff * (2 ** attempt))
    else:
        raise last

    if _MODE == "record":
        path = _fixture_path(method, url, body)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "method": method,
                "url": redact(url),
                "recorded_at": int(time.time()),
                "body": redact_body(payload.decode("utf-8", "replace")),
            }, fh, indent=1)
        _RECORDED += 1

    return payload


def get_json(url, headers=None, timeout=30, retries=2):
    headers = dict(headers or {})
    headers.setdefault("Accept", "application/json")
    raw = request(url, headers=headers, timeout=timeout, retries=retries)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HttpError(f"expected JSON from {redact(url)}: {exc}",
                        url=url, body=raw[:500].decode("utf-8", "replace")
                        ) from exc


def post_json(url, payload, headers=None, timeout=60, retries=1):
    headers = dict(headers or {})
    headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "application/json")
    raw = request(url, method="POST", headers=headers, timeout=timeout,
                  retries=retries, data=json.dumps(payload).encode("utf-8"))
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HttpError(f"expected JSON from {redact(url)}: {exc}",
                        url=url, body=raw[:500].decode("utf-8", "replace")
                        ) from exc


def get_text(url, headers=None, timeout=30, retries=2):
    raw = request(url, headers=headers, timeout=timeout, retries=retries)
    return raw.decode("utf-8", "replace")


def url_join(base, path, params=None):
    url = base.rstrip("/") + "/" + path.lstrip("/")
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
    return url
