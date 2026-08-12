# Security

## Reporting a vulnerability

Please report security issues privately — open a
[GitHub security advisory](../../security/advisories/new) rather than a public
issue. Include what an attacker could do and how to reproduce it. You will get
an acknowledgement, and a fix or a plan, as quickly as the maintainers can
manage.

## What Playlisterr holds

Playlisterr stores, in `/config/settings.json`, your Plex admin token and any LLM
API key, and it holds each shared user's Plex access token in memory during a
run. Treat the config directory as a secret.

## Threat model and defaults

Playlisterr is built for a trusted LAN, typically behind a reverse proxy that
handles authentication. Given that:

- The read-only dashboard is reachable without the API key by default so the
  app works out of the box. Endpoints that touch a stored secret or reach out
  to a caller-named host (`/api/test`, `/api/models`, `/api/llm/probe`,
  `/api/plex/servers`, `/api/plex/pin`, `/api/notify/test`, `/api/backup`)
  require the API key **always**, and state-changing requests must be
  `application/json` —
  together these block cross-site (CSRF) and server-side-request-forgery
  (SSRF) attempts to exfiltrate a token.
- Set `server.auth_required: true` (Settings → Advanced) to require the API
  key for the whole UI when you expose Playlisterr directly.
- Never pair a request-supplied URL with a stored secret: the Test and model
  endpoints refuse to send a saved credential to a host you just typed in.

## Exposing Playlisterr to the internet

Don't, without a reverse proxy in front doing TLS and authentication. Playlisterr
serves plain HTTP and its own API-key auth is a backstop, not a front door.

## Handling secrets in contributions

- Route every outbound request through `playlisterr/http.py` so tokens are
  redacted from logs, errors and recorded fixtures.
- Register any newly discovered token as a secret (`http.register_secret`) the
  moment it is parsed.
- Never return a token in an API response or write one to a fixture body
  unredacted.
