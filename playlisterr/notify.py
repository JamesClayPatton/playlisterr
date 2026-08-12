#!/usr/bin/env python3
"""Fire-and-forget webhook notifications when a run finishes.

The whole product is an unattended job that runs while everyone sleeps, so
"did it work?" needs an answer that arrives without anyone opening the UI. One
POST to a webhook covers ntfy, Gotify, Discord, Slack and any generic JSON
receiver — no per-service SDKs, which would be dependencies this project does
not take.

Notifying never affects the run: any failure here is logged and swallowed.
"""

import json

from . import http
from .log import get

log = get("notify")


def _summary_line(summary):
    """One human sentence from a run summary dict."""
    if summary.get("failed"):
        errs = summary.get("errors") or ["unknown error"]
        return f"Run failed: {errs[0]}"
    users = summary.get("users", 0)
    personal = summary.get("personalized", 0)
    written = summary.get("written", 0)
    verb = "published" if not summary.get("dry") else "generated"
    errs = summary.get("errors") or []
    tail = f", {len(errs)} error(s)" if errs else ""
    return (f"{verb.capitalize()} rows for {users} users "
            f"({personal} personalized, {written} writes){tail}.")


def _payload(fmt, title, message, summary, success):
    """Shape the body for the configured receiver."""
    if fmt == "discord":
        return {"content": f"**{title}**\n{message}"}, "application/json"
    if fmt == "slack":
        return {"text": f"*{title}*\n{message}"}, "application/json"
    if fmt == "gotify":
        return ({"title": title, "message": message,
                 "priority": 5 if not success else 3}), "application/json"
    if fmt == "ntfy":
        # ntfy takes a plain-text body; title/priority ride in headers.
        return message, "text/plain"
    return ({"title": title, "message": message, "success": success,
             "summary": summary}), "application/json"


def notify_run(cfg, summary):
    """Send a run-summary notification if configured for this outcome."""
    conf = cfg["notifications"]
    url = (conf.get("webhook_url") or "").strip()
    if not url:
        return False
    success = not summary.get("failed") and not (summary.get("errors") or [])
    if success and not conf.get("on_success", True):
        return False
    if not success and not conf.get("on_failure", True):
        return False

    title = "Playlisterr"
    message = _summary_line(summary)
    return _send(url, conf.get("format", "json"), title, message, summary,
                 success)


def send_test(cfg, url=None, fmt=None):
    """Send a test notification (used by the Settings Test button)."""
    conf = cfg["notifications"]
    url = (url or conf.get("webhook_url") or "").strip()
    if not url:
        return False, "no webhook URL set"
    fmt = fmt or conf.get("format", "json")
    ok = _send(url, fmt, "Playlisterr",
               "Test notification — your webhook is wired up.",
               {"test": True}, True, raise_errors=True)
    return ok, "sent" if ok else "failed"


def _send(url, fmt, title, message, summary, success, raise_errors=False):
    body, ctype = _payload(fmt, title, message, summary, success)
    headers = {"Content-Type": ctype}
    if fmt == "ntfy":
        headers["Title"] = title
        headers["Priority"] = "default" if success else "high"
        data = body.encode("utf-8")
    else:
        data = json.dumps(body).encode("utf-8")
    try:
        http.request(url, method="POST", headers=headers, data=data,
                     timeout=15, retries=1)
        log.info("notification sent (%s)", fmt)
        return True
    except Exception as exc:
        log.warning("notification failed: %s", http.redact(str(exc)))
        if raise_errors:
            return False
        return False
