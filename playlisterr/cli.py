#!/usr/bin/env python3
"""Command line: playlisterr run / test / users / status / config.

The CLI and the web UI (M2) drive the same pipeline functions. Anything the
CLI can do, the UI will be able to do, because neither owns the logic.
"""

import argparse
import json
import os
import sys
import time

from . import __version__, config as config_mod, http, log as log_mod
from .pipeline import publish as publish_mod, run as run_mod
from .providers import LLM, Plex, Tautulli
from .state import State

BANNER = r"""
  |> Playlisterr  %s
     personalized Plex rows, chosen by a local LLM
"""


def _parser():
    parser = argparse.ArgumentParser(
        prog="playlisterr",
        description="Personalized Plex home-screen rows from a local LLM.")
    parser.add_argument("--version", action="version",
                        version=f"playlisterr {__version__}")
    parser.add_argument("--config-dir", default=None,
                        help="where settings.json lives (default: /config in "
                             "a container, ./config from a checkout)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging and full item lists")

    sub = parser.add_subparsers(dest="command")

    run_cmd = sub.add_parser("run", help="build recommendations")
    run_cmd.add_argument("--dry", "--generate", action="store_true",
                         default=True, dest="dry",
                         help="generate picks and print what would change, "
                              "writing nothing (default)")
    run_cmd.add_argument("--publish", dest="dry", action="store_false",
                         help="decide fresh picks and write them to Plex")
    run_cmd.add_argument("--publish-generated", "--publish-preview",
                         dest="publish_preview", action="store_true",
                         help="publish the last generated picks exactly as "
                              "they were, without asking the model again")
    run_cmd.add_argument("--user", action="append", default=[],
                         help="limit to a username, display name or account "
                              "id (repeatable)")
    run_cmd.add_argument("--limit", type=int, default=0,
                         help="only process the first N users")
    run_cmd.add_argument("--record", action="store_true",
                         help="capture every API response to fixtures/")
    run_cmd.add_argument("--offline", action="store_true",
                         help="replay fixtures instead of calling anything")
    run_cmd.add_argument("--fixtures", default=None,
                         help="fixture directory (default ./fixtures)")
    run_cmd.add_argument("--json", action="store_true",
                         help="emit the run summary as JSON on stdout")

    serve_cmd = sub.add_parser(
        "serve", help="run the web UI (this is what the container runs)")
    serve_cmd.add_argument("--host", default=None)
    serve_cmd.add_argument("--port", type=int, default=None)
    serve_cmd.add_argument("--demo", action="store_true",
                           help="run the whole UI against the bundled sample "
                                "data — no Plex, no model, nothing real. For "
                                "previewing and screenshots.")

    sub.add_parser("test", help="check every configured connection")

    models_cmd = sub.add_parser(
        "models", help="list the models an LLM endpoint offers")
    models_cmd.add_argument("--provider", choices=("ollama", "openai"),
                            default=None,
                            help="override llm.provider for this check")
    models_cmd.add_argument("--url", default=None,
                            help="override llm.url for this check")
    models_cmd.add_argument("--api-key", default=None,
                            help="API key for a hosted endpoint")
    models_cmd.add_argument("--json", action="store_true",
                            help="machine-readable output (what the web UI's "
                                 "Detect models button will call)")
    sub.add_parser("users", help="list discovered users and their play counts")
    sub.add_parser("status", help="show the last run")

    cfg_cmd = sub.add_parser("config", help="inspect or seed configuration")
    cfg_cmd.add_argument("--write-example", metavar="PATH", default=None,
                         help="write config.example.json and exit")
    cfg_cmd.add_argument("--init", action="store_true",
                         help="create settings.json in the config dir")
    return parser


def _fixture_setup(args, root):
    if args.record and args.offline:
        raise SystemExit("--record and --offline are mutually exclusive")
    directory = os.path.abspath(
        args.fixtures or os.path.join(root, "fixtures"))
    if args.record:
        http.use_fixtures("record", directory)
        return f"recording fixtures to {directory}"
    if args.offline:
        # Fall back to the anonymized sample set that ships with the repo, so a
        # fresh clone can run --offline with no Plex server and no prior
        # recording — the documented contributor path.
        used_sample = False
        if not _has_fixtures(directory):
            sample = os.path.join(root, "fixtures", "sample")
            if _has_fixtures(sample):
                directory = sample
                used_sample = True
            else:
                raise SystemExit(
                    f"no fixtures in {directory} — run once with --record "
                    f"against a live server first")
        http.use_fixtures("replay", directory)
        return f"replaying fixtures from {directory}", used_sample
    return "", False


def _has_fixtures(directory):
    return os.path.isdir(directory) and any(
        n.endswith(".json") for n in os.listdir(directory))


# The sample fixtures were recorded against this fictional server, so a demo
# run must address it for the fixture URLs to match.
_SAMPLE_OVERRIDES = {
    "plex": {"url": "http://demo.local:32400", "token": "DEMO-TOKEN"},
    "llm": {"url": "http://demo.local:11434", "model": "demo-model:latest",
            "provider": "ollama"},
}


def cmd_run(args, cfg):
    if getattr(args, "publish_preview", False):
        state = State(cfg.state_path)
        payload = state.preview
        if not payload:
            print("nothing generated yet — run without --publish first",
                  file=sys.stderr)
            return 1
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(payload["at"]))
        print(f"publishing what was generated at {when}: "
              f"{len(payload.get('rows') or [])} rows, "
              f"{payload.get('titles', 0)} titles")
        written, errors = run_mod.apply_preview(cfg, payload)
        print(f"wrote {written} changes"
              + (f", {len(errors)} failed" if errors else ""))
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1 if errors else 0

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    note, used_sample = _fixture_setup(args, root)
    if note:
        print(note)
    if used_sample:
        # Point the config at the fictional demo server the sample was
        # recorded against, so a fresh clone with no settings just works.
        for section, leaves in _SAMPLE_OVERRIDES.items():
            cfg.data.setdefault(section, {}).update(leaves)
        print("using the shipped sample data (fictional demo server)")

    problems = cfg.validate()
    if problems:
        for problem in problems:
            print(f"config: {problem}", file=sys.stderr)
        return 2

    result = run_mod.run(cfg, dry=args.dry, only=args.user, limit=args.limit,
                         verbose=args.verbose)

    if args.json:
        json.dump(result.summary(), sys.stdout, indent=2)
        print()
    else:
        _print_report(result, args.verbose)

    state = State(cfg.state_path)
    state.record_run(result.summary())
    if args.dry:
        state.save_preview(run_mod.preview_payload(result, cfg))
        print("\nGenerated and saved. Publish exactly these picks with:\n"
              "  playlisterr run --publish-generated")
    else:
        state.clear_preview()
    state.save()

    if http.fixture_mode() == "record":
        print(f"\nrecorded {http.recorded_count()} responses")
    return 0


def _print_report(result, verbose):
    width = 78
    print()
    print("=" * width)
    print(f" Playlisterr {'generated' if result.dry else 'published'} — "
          f"{len(result.results)} users, "
          f"{len(result.catalog.items)} titles, "
          f"history from {result.history_source}, "
          f"{result.seconds:.0f}s")
    print("=" * width)

    if result.house_picks:
        print()
        print(f"── House Picks  ·  shared fallback row  ·  "
              f"{len(result.house)} viewers below the threshold")
        for index, pick in enumerate(result.house_picks, 1):
            print(f"   {index:>2}.*{pick.item.label:<44} {pick.reason}")
        if result.house_plan:
            for line in publish_mod.render(result.house_plan, result.catalog,
                                           verbose):
                print(line)

    for entry in result.results:
        if entry.mode == "house":
            continue
        print()
        header = f"{entry.name} ({entry.user['username']})"
        tag = {"llm": "personalized", "mixed": "personalized (part fallback)",
               "house": "house picks", "fallback": "rule-based",
               "error": "FAILED"}.get(entry.mode, entry.mode)
        print(f"── {header}  ·  {tag}  ·  "
              f"{entry.profile.plays if entry.profile else 0} plays  ·  "
              f"{entry.candidates} candidates  ·  {entry.seconds}s")
        if entry.error:
            print(f"   error: {entry.error}")
            continue
        if entry.profile and entry.profile.genres and verbose:
            print("   taste: " + ", ".join(
                f"{g} {w}" for g, w in entry.profile.genres[:6]))
        for index, pick in enumerate(entry.picks, 1):
            mark = " " if pick.source == "llm" else "*"
            print(f"   {index:>2}.{mark}{pick.item.label:<44} {pick.reason}")
        if entry.plan:
            for line in publish_mod.render(entry.plan, result.catalog,
                                           verbose):
                print(line)

    print()
    print("-" * width)
    print(f" {len(result.personalized)} personalized · "
          f"{len(result.house)} house picks · "
          f"{sum(1 for r in result.results if r.error)} errors")
    print(" * = rule-based fill, not the model")
    if result.dry:
        print(" nothing was written to Plex")
    print("-" * width)


def cmd_test(args, cfg):
    ok = True
    print(f"config dir: {cfg.dir}")
    problems = cfg.validate()
    for problem in problems:
        print(f"  ! {problem}")
        ok = False

    if cfg["plex"]["url"] and cfg["plex"]["token"]:
        try:
            plex = Plex(cfg["plex"]["url"], cfg["plex"]["token"])
            info = plex.ping()
            sections = plex.sections()
            print(f"  ok  plex        {info['name']} v{info['version']} "
                  f"({len(sections)} libraries)")
            for section in sections:
                print(f"        - [{section.type}] {section.title}")
        except Exception as exc:
            print(f"  !!  plex        {http.redact(str(exc))}")
            ok = False

    try:
        llm = LLM(provider=cfg["llm"]["provider"], url=cfg["llm"]["url"],
                  model=cfg["llm"]["model"], api_key=cfg["llm"]["api_key"])
        info = llm.ping()
        mark = "ok " if info["has_configured_model"] else "!  "
        print(f"  {mark} llm         {cfg['llm']['provider']} at "
              f"{cfg['llm']['url']}, {info['models']} models")
        if not info["has_configured_model"]:
            print(f"        configured model {cfg['llm']['model']!r} is not "
                  f"available; try: {', '.join(info['sample'])}")
            ok = False
    except Exception as exc:
        print(f"  !!  llm         {http.redact(str(exc))}")
        ok = False

    tautulli = Tautulli(cfg["tautulli"]["url"], cfg["tautulli"]["api_key"])
    if tautulli.configured:
        try:
            info = tautulli.ping()
            print(f"  ok  tautulli    {info['users']} users, "
                  f"{info['history_records']} history records")
        except Exception as exc:
            print(f"  !   tautulli    {http.redact(str(exc))} (optional)")
    else:
        print("  -   tautulli    not configured (optional)")

    return 0 if ok else 1


def cmd_serve(args, cfg):
    from .web import serve
    if getattr(args, "demo", False):
        # Run the entire UI against the fictional sample fixtures in a throwaway
        # config dir, so nothing real is contacted and no live state is
        # touched. Used for previewing and for taking screenshots.
        import tempfile
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sample = os.path.join(root, "fixtures", "sample")
        if not _has_fixtures(sample):
            raise SystemExit("no sample fixtures to demo against")
        http.use_fixtures("replay", sample)
        demo_dir = tempfile.mkdtemp(prefix="playlisterr-demo-")
        cfg = config_mod.load(demo_dir, overrides=_SAMPLE_OVERRIDES)
        cfg.data["server"]["schedule_mode"] = "manual"
        cfg.save()
        # Point logging at the throwaway demo dir too, so a demo run's log is
        # readable by the in-app log viewer instead of leaking into the real
        # config dir that logging was first set up against.
        log_mod.setup(cfg["general"]["log_level"], log_dir=cfg.log_dir)
        print("DEMO MODE — fictional data only, nothing real is contacted")
        return serve(cfg, host=args.host, port=args.port, demo=True)
    return serve(cfg, host=args.host, port=args.port)


def cmd_models(args, cfg):
    """Ask an endpoint what it can run.

    Deliberately takes overrides: the point is to answer "is there anything
    local?" *before* the settings are saved, so someone with no GPU can
    discover they have nothing running, paste an OpenAI-compatible URL and
    key, and pick a model from what comes back.
    """
    provider = args.provider or cfg["llm"]["provider"]
    url = args.url or cfg["llm"]["url"]
    api_key = args.api_key or cfg["llm"]["api_key"]
    llm = LLM(provider=provider, url=url, model=cfg["llm"]["model"],
              api_key=api_key)

    try:
        names = llm.models()
        payload = {"ok": True, "provider": provider, "url": url,
                   "models": names, "configured": cfg["llm"]["model"],
                   "configured_available": cfg["llm"]["model"] in names}
    except Exception as exc:
        payload = {"ok": False, "provider": provider, "url": url,
                   "models": [], "error": http.redact(str(exc))}

    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        print()
        return 0 if payload["ok"] else 1

    if not payload["ok"]:
        print(f"no models found at {url} ({provider}): {payload['error']}")
        print("\nIf nothing is running locally, point --url at any "
              "OpenAI-compatible endpoint and pass --api-key:")
        print("  playlisterr models --provider openai "
              "--url https://api.openai.com --api-key sk-...")
        return 1

    print(f"{provider} at {url}: {len(payload['models'])} models")
    for name in payload["models"]:
        mark = "*" if name == payload["configured"] else " "
        print(f"  {mark} {name}")
    if payload["models"] and not payload["configured_available"]:
        print(f"\nllm.model is {payload['configured']!r}, which this endpoint "
              f"does not offer.")
    return 0


def cmd_users(args, cfg):
    from .pipeline import catalog as catalog_mod, history as history_mod, \
        users as users_mod
    plex = Plex(cfg["plex"]["url"], cfg["plex"]["token"])
    tautulli = Tautulli(cfg["tautulli"]["url"], cfg["tautulli"]["api_key"])
    catalog = catalog_mod.build(plex, cfg)
    source, watched = history_mod.collect(plex, tautulli, catalog, cfg)
    discovered = users_mod.discover(plex, cfg, watched)
    min_plays = cfg["recommendations"]["min_plays"]

    print(f"\n{'user':<24} {'account':>11} {'plays':>6} {'titles':>7}  tier")
    print("-" * 62)
    for user in sorted(discovered,
                       key=lambda u: -(watched[u["account_id"]].plays
                                       if u["account_id"] in watched else 0)):
        entry = watched.get(user["account_id"])
        plays = entry.plays if entry else 0
        titles = len(entry.keys) if entry else 0
        tier = "personalized" if plays >= min_plays else "house picks"
        print(f"{user['username'][:24]:<24} {user['account_id']:>11} "
              f"{plays:>6} {titles:>7}  {tier}")
    print(f"\nhistory source: {source}, window "
          f"{cfg['recommendations']['history_days']} days, "
          f"threshold {min_plays} plays")
    return 0


def cmd_status(args, cfg):
    state = State(cfg.state_path)
    last = state.data.get("last_run")
    if not last:
        print("no runs recorded yet")
        return 0
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last["at"]))
    print(f"last run   {when} ({'dry' if last.get('dry') else 'published'})")
    print(f"duration   {last.get('seconds')}s")
    print(f"history    {last.get('history_source')}")
    print(f"users      {last.get('users')} "
          f"({last.get('personalized')} personalized, "
          f"{last.get('house')} house)")
    for error in last.get("errors", []):
        print(f"error      {error}")
    return 0


def cmd_config(args, cfg):
    if args.write_example:
        config_mod.write_example(args.write_example)
        print(f"wrote {args.write_example}")
        return 0
    if args.init:
        if os.path.exists(cfg.settings_path):
            print(f"{cfg.settings_path} already exists")
            return 1
        cfg.save()
        print(f"wrote {cfg.settings_path}")
        return 0
    redacted = {section: {k: ("***" if k in config_mod.SECRET_KEYS and v else v)
                          for k, v in leaves.items()}
                for section, leaves in cfg.data.items()}
    print(f"config dir: {cfg.dir}")
    json.dump(redacted, sys.stdout, indent=2)
    print()
    return 0


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.command:
        print(BANNER % __version__)
        parser.print_help()
        return 0

    try:
        cfg = config_mod.load(args.config_dir)
    except config_mod.ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    for secret in cfg.secrets():
        http.register_secret(secret)
    log_mod.setup("debug" if args.verbose else cfg["general"]["log_level"],
                  log_dir=cfg.log_dir)

    handlers = {"run": cmd_run, "test": cmd_test, "users": cmd_users,
                "status": cmd_status, "config": cmd_config,
                "models": cmd_models, "serve": cmd_serve}
    try:
        return handlers[args.command](args, cfg)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
