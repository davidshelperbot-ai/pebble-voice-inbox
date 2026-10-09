# Running it as a service

The ring posts whenever you press it, so the listener has to be always-on.

## macOS (launchd)

Copy the template, replace the placeholders, and load it:

```bash
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__REPO__|$PWD|g" \
    launchd/com.voiceinbox.service.plist.template \
    > ~/Library/LaunchAgents/com.voiceinbox.service.plist

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.voiceinbox.service.plist
launchctl kickstart -k gui/$(id -u)/com.voiceinbox.service   # restart
launchctl bootout   gui/$(id -u)/com.voiceinbox.service      # stop
```

Secrets go in `~/.config/voice-inbox/env`, which the wrapper sources:

```bash
install -m 600 /dev/null ~/.config/voice-inbox/env
cat >> ~/.config/voice-inbox/env <<'VARS'
VOICE_INBOX_TOKEN=...
VOICE_INBOX_RING_TOKEN=...
VARS
```

**launchd gives you a minimal `PATH`** (`/usr/bin:/bin:/usr/sbin:/sbin`), not
your shell's. Anything your handlers shell out to must be on an absolute path
or added in `scripts/run.sh`. This is the single most common way a setup that
works in your terminal fails as a service — and it tends to fail *partially*,
so the service looks healthy while one route quietly does nothing.

Logs: `$VOICE_INBOX_DATA/service.log`.

## Linux (systemd)

No unit file is shipped, but there is nothing macOS-specific about the core.
A `systemd --user` service running `voice-inbox` with an `EnvironmentFile`
works; the `imessage` handler is the only macOS-only piece.
