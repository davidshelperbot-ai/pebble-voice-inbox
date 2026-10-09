# Handlers

A handler takes one dictated command and does something with it. Adding a new
destination for your speech should never mean editing this project, so there
are three ways to add one.

## 1. No Python at all

The `command` and `webhook` handlers cover most integrations from the config
file alone.

```toml
[[route]]
name = "log my weight"
match = '^(?:weight|weigh in)\s+(?P<value>[\d.]+)$'
handler = "webhook"
url = "https://api.example.com/weight"
headers = { Authorization = "Bearer ..." }
```

`webhook` POSTs:

```json
{"text": "weight 182", "source": "ring",
 "dictated_at": "2026-10-09T08:15:00+00:00", "groups": {"value": "182"}}
```

`command` runs a program. The transcript arrives on **stdin**; named groups
arrive as `VOICE_GROUP_<NAME>` environment variables, along with `VOICE_TEXT`,
`VOICE_SOURCE`, `VOICE_DICTATED_AT` and `VOICE_AUDIO_PATH`.

```toml
[[route]]
name = "add a task"
match = '^(?:add|todo)\s+(?P<body>.+)$'
handler = "command"
command = ["/usr/local/bin/task", "add"]
```

Dictated text is **never** placed on a command line. Speech contains
apostrophes, quotes and semicolons; building a shell command out of it by
string concatenation is an injection bug waiting for a particular sentence.
With `shell = true` the *command* is a shell string you wrote, and the
dictation still only reaches it through the environment and stdin.

## 2. One file, no packaging

Drop a `.py` file in `~/.config/voice-inbox/handlers/`:

```python
from voice_inbox.handlers import Command, Result, handler

@handler("lights")
def lights(cmd: Command) -> Result:
    room = cmd.group("room", "living room")
    ...
    return Result.done(f"turned on the {room} lights")
```

Then reference it:

```toml
[[route]]
match = '^lights (?:on )?in (?:the )?(?P<room>.+)$'
handler = "lights"
```

Files beginning with `_` are skipped. A plugin that raises on import is logged
and skipped — a half-written experiment should not take the service down with
it.

## 3. Your own package

Advertise the `voice_inbox.handlers` entry-point group:

```toml
[project.entry-points."voice_inbox.handlers"]
lights = "my_package:lights_handler"
```

Entry points load before local plugins, and local plugins can override them by
name, so you can shadow a handler without forking it.

## The contract

```python
Command(
    text: str,                 # the transcript
    source: str,               # "ring", or whatever the client sent
    dictated_at: str | None,   # ISO-8601, when it was actually spoken
    audio_path: str | None,    # original clip, if the client sent audio
    groups: dict,              # named groups from the route's pattern
    config: dict,              # the route's own extra keys
)
```

Return one of:

| | meaning |
|---|---|
| `Result.done(detail)` | Worked. The command is marked dispatched. |
| `Result.retry(detail)` | Try again later — backoff, up to 3 attempts. |
| `Result.failed(detail)` | Will never work. Stop. |

Raising is fine and is treated as retryable; handlers are not expected to be
careful about exceptions.

**Make handlers idempotent.** Delivery is at-least-once: a phone that was
offline flushes its outbox when it reconnects, and the same command can arrive
twice. If double-running yours would be harmful — sending a message, charging
something, logging a transaction — key it on something stable and ignore a
repeat.

**Prefer asking over guessing.** Where being wrong is expensive, fail with the
candidates in `detail` rather than picking one. Sending a message to the wrong
person is not recoverable by a retry.

## Bundled handler reference

### `command`
| key | |
|---|---|
| `command` | argv list (recommended), or a string |
| `shell` | `true` to run the string through a shell |
| `timeout` | seconds, default 300 |

Exit 0 succeeds. Exit 127 (not found) is final; any other non-zero retries.

### `webhook`
| key | |
|---|---|
| `url` | required |
| `headers` | table of extra headers |
| `field` | key the transcript goes under, default `text` |
| `timeout` | seconds, default 60 |

4xx is final, 5xx and connection errors retry.

### `imessage` (macOS)
| key | |
|---|---|
| `contacts` | table of spoken name → phone/email, **required** |
| `account` | iMessage account UUID, optional |

Needs named groups `who` and `body`. Recipients come only from `contacts`: an
unknown name is an error, never a near match. To find your account UUID:

```bash
osascript -e 'tell application "Messages" to get id of every account'
```

Success means Messages *accepted* the message, which is not a delivery
receipt — reading those requires Full Disk Access and is not implemented here.

### `notion_page`
| key | |
|---|---|
| `parent` | page id, required |
| `token` | integration token, or set `NOTION_API_KEY` |
| `title_from` | named group to use as the title, default `title` |

The parent page must be shared with your integration. A `body` group becomes
paragraph blocks, chunked to Notion's 2000-character limit.

### `log`
No config. Prints the command. A good fallback while you build up routes.
