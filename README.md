# voice-inbox

Press the button on a [Pebble Index 01](https://repebble.com/index) dictation
ring, say something, and have it *do* something — send a text, file a note, hit
your own API, start a script.

The ring already posts your speech to any URL you give it. This is the other
half: a small service that accepts that POST, stores it durably, and routes it
to whatever you want, based on rules you write in a config file.

```
ring ──► POST /webhook ──► SQLite queue ──► router ──► handler
                             (durable)      (regex)    (your thing)
```

Unofficial and unaffiliated with Core Devices. Python 3.11+, **no runtime
dependencies**, MIT licensed. Primarily tested on macOS; the core is portable,
and a couple of the bundled handlers are macOS-only.

## Why it is shaped like this

**Speech is unrepeatable, so it is stored before it is understood.** Every
command lands in SQLite and an append-only `transcripts.jsonl` *before* any
route is consulted. If your handler is broken, your API is down, or your regex
is wrong, the command is still there — retried with backoff, and readable by
you. Nothing is parsed in the request path.

**Routing is dumb on purpose.** A regex decides only *which* handler gets the
command. Pulling the interesting parts out of loose speech is the handler's
job, and a handler can call an LLM if it wants one. Keeping the router
deterministic means a misheard word cannot quietly send your speech to the
wrong integration.

**It asks rather than guesses.** Where a wrong answer is expensive — which
contact, which page — the bundled handlers fail with the candidates instead of
picking one.

## Install

```bash
git clone https://github.com/davidshelperbot-ai/pebble-voice-inbox
cd pebble-voice-inbox
python3 -m venv .venv && .venv/bin/pip install -e .

export VOICE_INBOX_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
.venv/bin/voice-inbox
```

It refuses to start without a token rather than listening unauthenticated.

Then in the Pebble app → **Index** tab → **Advanced → Webhook**:

| Field | Value |
|---|---|
| URL | `https://your-host/webhook` |
| Header name | `Authorization` |
| Header value | `Bearer <your token>` |

The word `Bearer` belongs at the front of the *value*, not in the name box.
That mistake returns 401 and the log will tell you so explicitly.

## Routing

Copy `routes.example.toml` to `~/.config/voice-inbox/routes.toml`:

```toml
[[route]]
name = "text someone"
match = '^(?:text|message)\s+(?P<who>\S+)\s+(?P<body>.+)$'
handler = "imessage"
contacts = { alex = "+15555550123" }

[[route]]
name = "note to my own API"
match = '^(?:note|remember)\s+(?P<body>.+)$'
handler = "webhook"
url = "https://example.com/notes"

[fallback]
handler = "log"
```

Check it without saying a word:

```bash
voice-inbox-routes --list
voice-inbox-routes --test "text alex running late"
voice-inbox-routes --test "text alex running late" --run
```

## Bundled handlers

| name | what it does |
|---|---|
| `command` | Runs a program. Transcript on **stdin**, named groups as `VOICE_GROUP_<NAME>`. |
| `webhook` | POSTs `{text, source, dictated_at, groups}` to a URL. |
| `log` | Prints it. A sane fallback while you build up routes. |
| `imessage` | Sends an iMessage (macOS, Messages.app signed in). |
| `notion_page` | Creates a Notion page under a parent you choose. |

`command` and `webhook` between them cover most integrations with no Python at
all. Dictated text is **never** interpolated into a command line — speech
contains apostrophes and semicolons, and string-building a shell command out of
it is an injection bug waiting for someone to say the wrong sentence.

## Adding your own

Drop a `.py` file in `~/.config/voice-inbox/handlers/`:

```python
from voice_inbox.handlers import Command, Result, handler

@handler("shout")
def shout(cmd: Command) -> Result:
    print(cmd.text.upper())
    return Result.done("shouted")
```

Then `handler = "shout"` in a route. No packaging, no fork. Return
`Result.retry()` to leave it queued for another attempt; raise, and it is
treated as retryable. A broken plugin logs and is skipped — it cannot take the
service down. Handlers can also ship in their own package via the
`voice_inbox.handlers` entry-point group.

Because delivery is at-least-once (a phone that was offline flushes its outbox
later), **a handler must be safe to call twice** for the same command.

See [docs/handlers.md](docs/handlers.md).

## Audio

If you configure the ring to send audio instead of (or as well as) its
on-device transcription, clips are transcribed locally with
[whisper.cpp](https://github.com/ggerganov/whisper.cpp) — nothing leaves your
machine. When both are sent, the ring's transcription wins and the audio is
kept alongside the row. See [docs/audio.md](docs/audio.md).

## Running it for real

- **As a service:** [docs/service.md](docs/service.md) (launchd on macOS).
- **Reaching it from outside:** the ring posts from your phone, so the service
  needs a public HTTPS URL. [docs/exposing.md](docs/exposing.md) covers
  Tailscale Funnel and putting a custom domain in front of it.

## Endpoints

| | |
|---|---|
| `POST /webhook` | `multipart/form-data` as the ring sends it (`transcription`, `audio`, `recordedAt`, `client`). Also accepts JSON for hand-testing. |
| `POST /command` | `{"text", "source", "dictated_at"}`, or a raw audio body. For iOS Shortcuts and other clients. |
| `GET /health` | Liveness and queue depth. No auth. |

Both POSTs need `Authorization: Bearer <token>`. `VOICE_INBOX_RING_TOKEN` sets
a second token accepted only on `/webhook`, so the credential you paste into a
device can be rotated without touching your other clients.

`recordedAt` is converted to ISO-8601 and stored as `dictated_at`, so a clip
delivered late — phone offline, flushed hours later — reaches your handler with
the time it was actually spoken.

## Operating it

```bash
curl -s localhost:8790/health

sqlite3 ~/.local/share/voice-inbox/queue.db \
  "SELECT id,status,attempts,source,substr(transcript,1,60) FROM commands ORDER BY id DESC LIMIT 10"

tail -f ~/.local/share/voice-inbox/transcripts.jsonl
```

`transcripts.jsonl` records every command on arrival and again when audio is
transcribed. It is hooked into the queue's single entry point, so no client and
no routing decision can bypass it — which is the property a debug log needs.

## Configuration

| variable | meaning | default |
|---|---|---|
| `VOICE_INBOX_TOKEN` | bearer token for both POST endpoints | **required** |
| `VOICE_INBOX_RING_TOKEN` | extra token, `/webhook` only | unset |
| `VOICE_INBOX_PORT` | port | `8790` |
| `VOICE_INBOX_HOST` | bind address | `127.0.0.1` |
| `VOICE_INBOX_DATA` | queue, audio, logs | `~/.local/share/voice-inbox` |
| `VOICE_INBOX_ROUTES` | routes file | `~/.config/voice-inbox/routes.toml` |
| `VOICE_INBOX_HANDLER_DIR` | plugin directory | `~/.config/voice-inbox/handlers` |
| `VOICE_INBOX_WHISPER_MODEL` | ggml model path | `$VOICE_INBOX_DATA/models/ggml-small.en.bin` |

## Tests

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

Nothing in the suite sends a message, writes to an external service, or needs
the network.

## Contributing

Handlers are the interesting surface. If you build one for a service other
people use, a PR adding it to `handlers/builtins.py` — or a link to your own
package — is welcome. Please include tests that don't touch the network.
