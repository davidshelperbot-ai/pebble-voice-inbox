"""Match a dictated command to a handler.

Routing is configuration, not code. A route is a regex plus a handler name,
read from a TOML file, so adding "log my weight 182" -> an HTTP endpoint is a
few lines in a file rather than a patch to this project.

Why regex and not an LLM: the pattern only has to decide *which* handler gets
the command, which is cheap and deterministic. Pulling the interesting parts
out of loose speech is the handler's problem, and a handler is free to call a
model if it wants one. Keeping the router dumb means a misheard word cannot
silently reroute speech to the wrong integration.

Patterns are matched in file order, case-insensitively, against the whole
transcript, and named groups are passed through to the handler. The first
match wins; if nothing matches, the `[fallback]` handler gets it, and if there
is no fallback the command is left for a human to look at rather than guessed
at.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path(
    __import__("os").environ.get("VOICE_INBOX_ROUTES",
                                 Path.home() / ".config" / "voice-inbox" / "routes.toml"))


@dataclass(frozen=True)
class Route:
    name: str
    handler: str
    pattern: re.Pattern | None = None
    config: dict = field(default_factory=dict)

    def match(self, text: str):
        """Returns the named groups if this route claims `text`, else None."""
        if self.pattern is None:
            return None
        m = self.pattern.search(text.strip())
        return {k: v for k, v in (m.groupdict() or {}).items()} if m else None


@dataclass(frozen=True)
class Routing:
    routes: tuple[Route, ...] = ()
    fallback: Route | None = None

    def resolve(self, text: str) -> tuple[Route, dict] | None:
        for route in self.routes:
            groups = route.match(text)
            if groups is not None:
                return route, groups
        return (self.fallback, {}) if self.fallback else None


class ConfigError(Exception):
    """The routes file is unusable. Raised early, with the route named."""


def parse(data: dict) -> Routing:
    routes = []
    for i, raw in enumerate(data.get("route", [])):
        name = raw.get("name") or f"route-{i + 1}"
        if "handler" not in raw:
            raise ConfigError(f"route {name!r} has no handler")
        if "match" not in raw:
            raise ConfigError(
                f"route {name!r} has no match pattern "
                f"(use [fallback] for the catch-all)")
        try:
            pattern = re.compile(raw["match"], re.IGNORECASE | re.DOTALL)
        except re.error as exc:
            raise ConfigError(f"route {name!r} has an invalid pattern: {exc}") from exc
        known = {"name", "handler", "match"}
        routes.append(Route(name=name, handler=raw["handler"], pattern=pattern,
                            config={k: v for k, v in raw.items() if k not in known}))

    fb = data.get("fallback")
    fallback = None
    if fb:
        if "handler" not in fb:
            raise ConfigError("[fallback] has no handler")
        fallback = Route(name="fallback", handler=fb["handler"],
                         config={k: v for k, v in fb.items() if k != "handler"})
    return Routing(tuple(routes), fallback)


def load(path: Path | None = None) -> Routing:
    path = Path(DEFAULT_CONFIG if path is None else path)
    if not path.is_file():
        # No config is a legitimate state: the service still accepts and queues
        # dictation, it just has nowhere to send it yet. Losing speech because a
        # file is missing would be far worse than holding it.
        return Routing()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return parse(data)
