#!/usr/bin/env python3
"""plex.tv cloud API — the shared-users list.

The shared-users list does not exist on the local server; only plex.tv knows
who a server is shared with. This is the one call that leaves the LAN, so it
is used sparingly and its results are cached in state between runs.
"""

import json
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .. import http
from ..log import get

log = get("plextv")

BASE = "https://plex.tv"


@dataclass
class SharedUser:
    account_id: str
    username: str
    email: str = ""
    share_id: str = ""
    # This user's token for this server. Present in the owner's own
    # shared_servers response; it is how a playlist gets created as them.
    access_token: str = ""
    all_libraries: bool = False

    @property
    def display_name(self):
        """A first name if we can get one, else the username.

        "alexdoe" -> "Alex" reads better on a home screen row than the raw
        handle does, but anything with digits or separators is left alone
        rather than mangled.
        """
        name = (self.username or "").strip()
        if not name:
            return "there"
        head = name.split(".")[0].split("_")[0].split("-")[0]
        if head.isalpha() and len(head) >= 3:
            return head[:1].upper() + head[1:].lower()
        return name


class PlexAuth:
    """"Sign in with Plex" — the PIN flow, so nobody has to hunt for a token.

    Asking a self-hoster to open Plex web, find an item, open Get Info, click
    View XML and copy a query parameter out of the address bar is the single
    most common place these setups stall. Plex publishes the same OAuth-ish
    PIN flow that Overseerr and Tautulli use: request a four-character code,
    send the person to plex.tv to approve it, poll until a token appears.

    No password ever touches this application.
    """

    def __init__(self, client_id, product="Playlisterr", timeout=30):
        self.client_id = client_id
        self.product = product
        self.timeout = timeout

    def _headers(self):
        return {"Accept": "application/json",
                "X-Plex-Product": self.product,
                "X-Plex-Version": "1.0",
                "X-Plex-Client-Identifier": self.client_id,
                "X-Plex-Platform": "Web",
                "X-Plex-Device": "Playlisterr"}

    def create_pin(self):
        """Start a sign-in. Returns the pin id, the code, and where to send them."""
        raw = http.request(
            http.url_join(BASE, "/api/v2/pins", {"strong": "true"}),
            method="POST", headers=self._headers(), timeout=self.timeout,
            retries=1)
        data = json.loads(raw)
        params = urllib.parse.urlencode({
            "clientID": self.client_id,
            "code": data["code"],
            "context[device][product]": self.product,
        })
        return {"id": data["id"], "code": data["code"],
                "url": f"https://app.plex.tv/auth#?{params}"}

    def check_pin(self, pin_id):
        """Poll a pin. Returns the token once approved, else None."""
        data = http.get_json(
            http.url_join(BASE, f"/api/v2/pins/{pin_id}"),
            headers=self._headers(), timeout=self.timeout, retries=0)
        token = data.get("authToken")
        if token:
            http.register_secret(token)
        return token

    def servers(self, token):
        """Every Plex server this account can reach, with usable URLs.

        Connections are returned local-first: a LAN address is faster and
        does not depend on plex.tv being up, and it is what a self-hoster
        wants even when a remote URI also works.
        """
        headers = dict(self._headers(), **{"X-Plex-Token": token})
        data = http.get_json(
            http.url_join(BASE, "/api/v2/resources",
                          {"includeHttps": "1", "includeRelay": "0"}),
            headers=headers, timeout=self.timeout)
        out = []
        for resource in data:
            if "server" not in (resource.get("provides") or ""):
                continue
            connections = []
            for conn in resource.get("connections") or []:
                if conn.get("relay"):
                    continue
                connections.append({"uri": conn.get("uri", ""),
                                    "address": conn.get("address", ""),
                                    "port": conn.get("port"),
                                    "local": bool(conn.get("local")),
                                    "protocol": conn.get("protocol", "")})
            connections.sort(key=lambda c: (not c["local"],
                                            c["protocol"] != "http"))
            out.append({
                "name": resource.get("name", "Plex"),
                "owned": bool(resource.get("owned")),
                "product": resource.get("product", ""),
                "version": resource.get("productVersion", ""),
                "client_identifier": resource.get("clientIdentifier", ""),
                "access_token": resource.get("accessToken") or token,
                "connections": connections,
            })
        out.sort(key=lambda s: not s["owned"])
        return out


class PlexTv:
    def __init__(self, token, timeout=30):
        self.token = token
        self.timeout = timeout
        http.register_secret(token)

    def _xml(self, path, params=None):
        url = http.url_join(BASE, path,
                            dict(params or {}, **{"X-Plex-Token": self.token}))
        text = http.get_text(url, headers={"Accept": "application/xml"},
                             timeout=self.timeout)
        try:
            return ET.fromstring(text)
        except ET.ParseError as exc:
            raise http.HttpError(f"bad XML from plex.tv{path}: {exc}") from exc

    def owner_account_id(self):
        """This token's own plex.tv account id.

        On the local server the owner is always account 1, but Tautulli keys
        history by plex.tv account id, so the two id spaces have to be bridged
        to stop the owner turning into a phantom user. Returns "" on failure —
        the caller treats that as "cannot alias" rather than an error.
        """
        try:
            data = http.get_json(
                "https://plex.tv/api/v2/user",
                headers={"Accept": "application/json",
                         "X-Plex-Token": self.token,
                         "X-Plex-Client-Identifier": "Playlisterr"},
                timeout=self.timeout, retries=1)
            return str(data.get("id") or "")
        except Exception as exc:
            log.debug("could not resolve owner plex.tv id: %s", exc)
            return ""

    def shared_users(self, machine_id):
        """Everyone this server is shared with."""
        root = self._xml(f"/api/servers/{machine_id}/shared_servers")
        out = []
        for node in root.iter("SharedServer"):
            token = node.get("accessToken", "") or ""
            # Each of these grants that user's full access to the server, so
            # they are secrets in their own right — register them the moment
            # they are parsed so redaction covers them everywhere.
            http.register_secret(token)
            out.append(SharedUser(
                account_id=node.get("userID", ""),
                username=node.get("username", "") or node.get("name", ""),
                email=node.get("email", "") or "",
                share_id=node.get("id", ""),
                access_token=token,
                all_libraries=node.get("allLibraries", "0") == "1",
            ))
        log.debug("plex.tv: %d shared users", len(out))
        return out
