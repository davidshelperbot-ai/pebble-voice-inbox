"""Handler registry — the extension point of this project.

A *handler* takes one dictated command and does something with it. Adding a new
place to route speech should never mean patching the server, so handlers are
looked up by name from three sources, in order:

1. **Entry points** under the ``voice_inbox.handlers`` group, so a handler can
   live in its own pip-installable package.
2. **A local plugin directory** (``$VOICE_INBOX_HANDLER_DIR``, default
   ``~/.config/voice-inbox/handlers``), so a single ``.py`` file dropped in a
   folder is enough — no packaging, no fork.
3. **The built-ins** shipped here.

Later sources never shadow earlier ones, so you can override a built-in by
name without editing it.

Writing one
-----------

A handler is any callable taking a :class:`Command` and returning a
:class:`Result`::

    from voice_inbox.handlers import Command, Result, handler

    @handler("shout")
    def shout(cmd: Command) -> Result:
        print(cmd.text.upper())
        return Result.done(f"shouted {len(cmd.text)} chars")

Return ``Result.done()`` and the command is marked dispatched. Return
``Result.retry()`` and it stays queued for the worker's backoff to pick up
again. Raise, and it is treated as a retryable failure — handlers are not
expected to be careful about exceptions.

The queue is the source of truth, so a handler must be safe to call more than
once for the same command: delivery is at-least-once, and a phone that was
offline will re-send.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HANDLER_GROUP = "voice_inbox.handlers"
DEFAULT_PLUGIN_DIR = Path(
    os.environ.get("VOICE_INBOX_HANDLER_DIR",
                   Path.home() / ".config" / "voice-inbox" / "handlers"))


@dataclass(frozen=True)
class Command:
    """One dictated command, as handed to a handler."""

    text: str
    #: Which client sent it — "ring" for the Pebble Index, or whatever a
    #: custom client puts in the `client` field / X-Source header.
    source: str = "unknown"
    #: When it was actually spoken, ISO-8601. Differs from arrival time when a
    #: phone was offline and flushed its outbox later.
    dictated_at: str | None = None
    #: Path to the original audio, when the client sent audio.
    audio_path: str | None = None
    #: Named groups captured by the route's pattern, e.g. {"who": "alex"}.
    groups: dict = field(default_factory=dict)
    #: Per-route settings from the config file.
    config: dict = field(default_factory=dict)

    def group(self, name: str, default: str = "") -> str:
        return self.groups.get(name) or default


@dataclass(frozen=True)
class Result:
    ok: bool
    detail: str = ""
    #: False asks the worker to try again later rather than giving up.
    final: bool = True

    @classmethod
    def done(cls, detail: str = "") -> "Result":
        return cls(True, detail)

    @classmethod
    def failed(cls, detail: str = "") -> "Result":
        return cls(False, detail, final=True)

    @classmethod
    def retry(cls, detail: str = "") -> "Result":
        return cls(False, detail, final=False)


Handler = Callable[[Command], Result]

_REGISTRY: dict[str, Handler] = {}


def handler(name: str):
    """Decorator registering a handler under `name`."""
    def register(fn: Handler) -> Handler:
        _REGISTRY[name] = fn
        return fn
    return register


def register(name: str, fn: Handler, *, overwrite: bool = False) -> None:
    if name in _REGISTRY and not overwrite:
        raise ValueError(f"handler {name!r} is already registered")
    _REGISTRY[name] = fn


def available() -> dict[str, Handler]:
    return dict(_REGISTRY)


def get(name: str) -> Handler:
    if name not in _REGISTRY:
        raise KeyError(
            f"no handler named {name!r}. Available: "
            f"{', '.join(sorted(_REGISTRY)) or '(none)'}. "
            f"Drop a .py file in {DEFAULT_PLUGIN_DIR} to add one.")
    return _REGISTRY[name]


def _load_entry_points() -> list[str]:
    from importlib.metadata import entry_points
    loaded = []
    try:
        points = entry_points(group=HANDLER_GROUP)
    except Exception:
        return loaded
    for ep in points:
        try:
            register(ep.name, ep.load(), overwrite=True)
            loaded.append(ep.name)
        except Exception as exc:
            print(f"[voice-inbox] handler {ep.name!r} failed to load: {exc!r}",
                  flush=True)
    return loaded


def _load_plugin_dir(directory: Path) -> list[str]:
    """Import every .py in `directory`; they self-register via @handler.

    A broken plugin must not take the service down -- it is someone's
    half-written experiment, and the rest of the routes still need to work.
    """
    loaded = []
    if not directory.is_dir():
        return loaded
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(
            f"voice_inbox_plugin_{path.stem}", path)
        if not spec or not spec.loader:
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded.append(path.name)
        except Exception as exc:
            print(f"[voice-inbox] plugin {path.name} failed to load: {exc!r}",
                  flush=True)
    return loaded


def load_all(plugin_dir: Path | None = None) -> dict[str, Handler]:
    """Populate the registry from all three sources. Idempotent."""
    from . import builtins  # noqa: F401  (registers the shipped handlers)
    _load_entry_points()
    _load_plugin_dir(DEFAULT_PLUGIN_DIR if plugin_dir is None else Path(plugin_dir))
    return available()
