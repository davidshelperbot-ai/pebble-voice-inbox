"""`voice-inbox-routes` — inspect and test routing without saying anything aloud.

Dictation is awkward to iterate on: you have to speak, wait, and read a log to
find out a regex did not match. This does the same resolution the worker does,
against text you type.

    voice-inbox-routes --list
    voice-inbox-routes --test "text alex running late"
    voice-inbox-routes --test "..." --run     # actually invoke the handler
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import router
from .handlers import Command, Result, available, get, load_all


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="voice-inbox-routes")
    ap.add_argument("--routes", type=Path, default=None,
                    help="routes.toml (default: $VOICE_INBOX_ROUTES)")
    ap.add_argument("--list", action="store_true",
                    help="show the configured routes and available handlers")
    ap.add_argument("--test", metavar="TEXT",
                    help="show which route and handler would claim TEXT")
    ap.add_argument("--run", action="store_true",
                    help="with --test, actually invoke the handler")
    ap.add_argument("--json", dest="as_json", action="store_true")
    args = ap.parse_args(argv)

    handlers = load_all()
    try:
        routing = router.load(args.routes)
    except router.ConfigError as exc:
        print(f"bad routes config: {exc}", file=sys.stderr)
        return 2

    if args.list or not args.test:
        if args.as_json:
            print(json.dumps({
                "handlers": sorted(handlers),
                "routes": [{"name": r.name, "handler": r.handler,
                            "match": r.pattern.pattern} for r in routing.routes],
                "fallback": routing.fallback.handler if routing.fallback else None,
            }, indent=2))
        else:
            print(f"handlers: {', '.join(sorted(handlers)) or '(none)'}\n")
            if not routing.routes:
                print("no routes configured "
                      "(see routes.example.toml to get started)")
            for r in routing.routes:
                print(f"  {r.name}\n    match   {r.pattern.pattern}\n"
                      f"    handler {r.handler}")
            print(f"  fallback: "
                  f"{routing.fallback.handler if routing.fallback else '(none)'}")
        return 0

    resolved = routing.resolve(args.test)
    if resolved is None:
        print("no route matched, and no fallback is configured.")
        return 1
    route, groups = resolved
    if args.as_json:
        print(json.dumps({"route": route.name, "handler": route.handler,
                          "groups": groups}, indent=2))
    else:
        print(f"route   {route.name}\nhandler {route.handler}\ngroups  {groups}")

    if not args.run:
        return 0
    try:
        result = get(route.handler)(Command(text=args.test, source="cli",
                                            groups=groups, config=route.config))
    except KeyError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    if not isinstance(result, Result):
        result = Result.done(str(result or ""))
    print(f"\n{'ok' if result.ok else 'failed'}: {result.detail}")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
