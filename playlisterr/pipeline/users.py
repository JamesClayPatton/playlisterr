#!/usr/bin/env python3
"""Who to build rows for.

Three sources, merged, because no single one is complete:

* ``/accounts`` on the server knows the owner and any managed/home users, and
  the owner's plays are recorded under ``accountID`` **1**;
* plex.tv's shared-servers list is the only place the 20-odd shared users
  appear, with the account ids their plays are recorded under;
* the history itself, which occasionally contains an account that has since
  been removed from the share — worth naming rather than silently dropping.

plex.tv is a cloud call, so a failure there degrades to server-only discovery
instead of failing the run.
"""

from ..log import get
from ..providers.plextv import PlexTv, SharedUser

log = get("users")


def _display(username):
    return SharedUser(account_id="", username=username).display_name


def discover(plex, cfg, watched_by_account=None, allow_cloud=True):
    """Return [{account_id, username, display_name, source, shared}]."""
    users, seen = [], set()

    # The owner. Their plays are account 1 regardless of their plex.tv id.
    owner_name = "Owner"
    try:
        owner_name = plex.server_name() or "Owner"
    except Exception as exc:
        log.debug("could not read server name: %s", exc)
    try:
        for account in plex.accounts():
            if account["account_id"] == "1":
                owner_name = account["username"] or owner_name
    except Exception as exc:
        log.debug("could not read /accounts: %s", exc)

    users.append({"account_id": "1", "username": owner_name,
                  "display_name": _display(owner_name), "source": "owner",
                  "shared": False,
                  # The admin token *is* the owner's own token, which is what
                  # lets the owner have a playlist like everybody else.
                  "access_token": plex.token})
    seen.add("1")

    if allow_cloud and plex.token:
        try:
            machine = plex.machine_identifier()
            for shared in PlexTv(plex.token).shared_users(machine):
                if not shared.account_id or shared.account_id in seen:
                    continue
                users.append({"account_id": shared.account_id,
                              "username": shared.username,
                              "display_name": shared.display_name,
                              "source": "plex.tv", "shared": True,
                              "access_token": shared.access_token,
                              "share_id": shared.share_id})
                seen.add(shared.account_id)
        except Exception as exc:
            log.warning("plex.tv user list unavailable (%s) — falling back to "
                        "accounts seen in history", exc)

    # Managed/home users that plex.tv does not list as shares.
    try:
        for account in plex.accounts():
            if account["account_id"] in seen:
                continue
            users.append({"account_id": account["account_id"],
                          "username": account["username"],
                          "display_name": _display(account["username"]),
                          "source": "accounts", "shared": False})
            seen.add(account["account_id"])
    except Exception:
        pass

    for account_id in sorted(watched_by_account or {}):
        if account_id in seen:
            continue
        log.info("account %s appears in history but is not a known user",
                 account_id)
        users.append({"account_id": account_id,
                      "username": f"account-{account_id}",
                      "display_name": f"Account {account_id}",
                      "source": "history", "shared": False})
        seen.add(account_id)

    log.info("users: %d (%d shared)", len(users),
             sum(1 for u in users if u["shared"]))
    return users


def enabled(users, cfg):
    """Narrow the discovered list to the accounts this install is meant for.

    Sharing a server does not mean everyone asked for a recommendations row
    in their account, so the owner picks. "all" stays the default because it
    is what someone setting this up for their household expects.
    """
    if cfg["users"]["mode"] != "selected":
        return users, []
    wanted = {str(a) for a in cfg["users"]["selected"]}
    on = [u for u in users if str(u["account_id"]) in wanted]
    off = [u for u in users if str(u["account_id"]) not in wanted]
    log.info("users: %d enabled, %d skipped by configuration",
             len(on), len(off))
    return on, off


def select(users, names):
    """Filter to the --user values given on the command line."""
    if not names:
        return users
    wanted = {n.lower() for n in names}
    picked = [u for u in users
              if u["username"].lower() in wanted
              or u["display_name"].lower() in wanted
              or u["account_id"] in wanted]
    missing = wanted - {u["username"].lower() for u in picked} \
        - {u["display_name"].lower() for u in picked} \
        - {u["account_id"] for u in picked}
    for name in sorted(missing):
        log.warning("no such user: %s", name)
    return picked
