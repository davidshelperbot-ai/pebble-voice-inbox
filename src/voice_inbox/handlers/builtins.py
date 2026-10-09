"""Handlers shipped with the project.

Two of these are generic and cover most needs without writing any Python:

* ``command`` runs a shell command with the transcript on stdin;
* ``webhook`` POSTs the transcript as JSON to a URL.

The rest are conveniences for things people actually asked a dictation ring to
do. Each is optional and self-contained: it fails with a clear message when the
thing it talks to is not configured, rather than at import time.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import urllib.error
import urllib.request

from . import Command, Result, handler

TIMEOUT = int(os.environ.get("VOICE_INBOX_HANDLER_TIMEOUT", "300"))


# --------------------------------------------------------------------------
# generic
# --------------------------------------------------------------------------

@handler("command")
def run_command(cmd: Command) -> Result:
    """Run a shell command, transcript on stdin.

    Route config:
        command = ["my-script", "--flag"]    # argv, recommended
        command = "my-script --flag"         # or a string, split with shlex
        shell   = true                       # or run it through a shell

    The transcript arrives on **stdin**, and named groups arrive as
    ``VOICE_GROUP_<NAME>`` environment variables, so nothing dictated is ever
    spliced into a command line. That is deliberate: speech contains quotes and
    semicolons, and string-building a command out of it is a shell-injection
    bug waiting for someone to say the wrong sentence.
    """
    spec = cmd.config.get("command")
    if not spec:
        return Result.failed("route has no `command` configured")
    use_shell = bool(cmd.config.get("shell"))
    if use_shell and not isinstance(spec, str):
        return Result.failed("`shell = true` needs `command` to be a string")

    env = dict(os.environ)
    env["VOICE_TEXT"] = cmd.text
    env["VOICE_SOURCE"] = cmd.source
    env["VOICE_DICTATED_AT"] = cmd.dictated_at or ""
    env["VOICE_AUDIO_PATH"] = cmd.audio_path or ""
    for key, value in cmd.groups.items():
        env[f"VOICE_GROUP_{key.upper()}"] = value or ""

    argv = spec if use_shell else (shlex.split(spec) if isinstance(spec, str) else list(spec))
    try:
        run = subprocess.run(argv, input=cmd.text, capture_output=True, text=True,
                             timeout=cmd.config.get("timeout", TIMEOUT),
                             shell=use_shell, env=env)
    except subprocess.TimeoutExpired:
        return Result.retry("command timed out")
    except OSError as exc:
        return Result.failed(f"could not run command: {exc}")
    if run.returncode != 0:
        detail = (run.stderr or run.stdout or "").strip()[:500]
        # A command that is simply absent will never start working, so there is
        # nothing to gain by retrying it.
        final = run.returncode == 127
        return Result(False, f"exit {run.returncode}: {detail}", final=final)
    return Result.done((run.stdout or "").strip()[:500])


@handler("webhook")
def post_webhook(cmd: Command) -> Result:
    """POST the command to a URL as JSON.

    Route config:
        url     = "https://example.com/hook"
        headers = { Authorization = "Bearer ..." }
        field   = "text"      # key the transcript goes under (default "text")
    """
    url = cmd.config.get("url")
    if not url:
        return Result.failed("route has no `url` configured")
    body = {cmd.config.get("field", "text"): cmd.text,
            "source": cmd.source,
            "dictated_at": cmd.dictated_at,
            "groups": cmd.groups}
    headers = {"Content-Type": "application/json"}
    headers.update(cmd.config.get("headers") or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=cmd.config.get("timeout", 60)) as resp:
            return Result.done(f"HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        # 4xx will not fix itself; 5xx might.
        return Result(False, f"HTTP {exc.code}", final=400 <= exc.code < 500)
    except (urllib.error.URLError, OSError) as exc:
        return Result.retry(f"could not reach {url}: {exc}")


@handler("log")
def log_only(cmd: Command) -> Result:
    """Do nothing but record it. Useful as a fallback while you build up routes,
    and as the honest default for a command nothing claims."""
    print(f"[voice-inbox] unrouted ({cmd.source}): {cmd.text}", flush=True)
    return Result.done("logged")


# --------------------------------------------------------------------------
# optional conveniences
# --------------------------------------------------------------------------

@handler("imessage")
def send_imessage(cmd: Command) -> Result:
    """Send an iMessage on macOS (requires Messages.app signed in).

    Route config:
        contacts = { alex = "+15555550123", mum = "mum@example.com" }
        account  = "<iMessage account UUID>"   # optional; see docs

    Expects named groups `who` and `body` from the route pattern. The recipient
    is looked up in the route's own `contacts` table rather than guessed at from
    the address book: texting the wrong person is the least recoverable thing
    this project can do, so an unknown name is an error, never a near match.

    Handle and body are passed as argv to a fixed AppleScript, never
    interpolated into it -- dictated text contains apostrophes and quotes.
    """
    who = cmd.group("who").strip().lower()
    body = cmd.group("body").strip() or cmd.text
    if not who:
        return Result.failed("route pattern must capture a `who` group")
    contacts = {k.lower(): v for k, v in (cmd.config.get("contacts") or {}).items()}
    handle = contacts.get(who)
    if not handle:
        known = ", ".join(sorted(contacts)) or "(none configured)"
        return Result.failed(f"no contact named {who!r}. Known: {known}")
    if not body:
        return Result.failed("nothing to send")

    script = (
        'on run argv\n'
        '  set h to item 1 of argv\n'
        '  set b to item 2 of argv\n'
        '  tell application "Messages"\n'
        '    set a to %s\n'
        '    send b to participant h of a\n'
        '  end tell\n'
        'end run\n'
    ) % (f'account id "{cmd.config["account"]}"' if cmd.config.get("account")
         else '1st account whose service type = iMessage')
    try:
        run = subprocess.run(["osascript", "-", handle, body],
                             input=script, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return Result.retry(f"osascript failed: {exc}")
    if run.returncode != 0:
        return Result.failed((run.stderr or "").strip()[:300])
    # Messages accepting a message is not proof of delivery; say so.
    return Result.done(f"handed to Messages for {who}")


@handler("notion_page")
def create_notion_page(cmd: Command) -> Result:
    """Create a Notion page under a parent page.

    Route config:
        token  = "secret_..."     # or set NOTION_API_KEY
        parent = "<page id>"
        title_from = "title"      # named group to use as the title
    """
    token = cmd.config.get("token") or os.environ.get("NOTION_API_KEY", "")
    parent = cmd.config.get("parent")
    if not token:
        return Result.failed("no Notion token (set NOTION_API_KEY)")
    if not parent:
        return Result.failed("route has no `parent` page id configured")
    title = cmd.group(cmd.config.get("title_from", "title")) or cmd.text
    body = cmd.group("body")

    children = []
    if body:
        # Notion caps a rich-text run at 2000 characters.
        for chunk in (body[i:i + 1900] for i in range(0, len(body), 1900)):
            children.append({"object": "block", "type": "paragraph",
                             "paragraph": {"rich_text": [
                                 {"type": "text", "text": {"content": chunk}}]}})
    payload = {"parent": {"page_id": parent},
               "properties": {"title": {"title": [
                   {"type": "text", "text": {"content": title[:2000]}}]}},
               "children": children[:100]}
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Notion-Version": "2022-06-28",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            created = json.loads(resp.read() or b"{}")
        return Result.done(created.get("url") or "page created")
    except urllib.error.HTTPError as exc:
        detail = (exc.read() or b"").decode("utf-8", "replace")[:300]
        return Result(False, f"HTTP {exc.code}: {detail}",
                      final=400 <= exc.code < 500 and exc.code != 429)
    except (urllib.error.URLError, OSError) as exc:
        return Result.retry(f"could not reach Notion: {exc}")
