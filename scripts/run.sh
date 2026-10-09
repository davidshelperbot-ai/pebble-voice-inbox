#!/bin/bash
# launchd wrapper: load config, fix PATH, exec the service.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# launchd's PATH is minimal. Add anything your handlers shell out to.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

ENV_FILE="${VOICE_INBOX_ENV:-$HOME/.config/voice-inbox/env}"
if [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
fi

mkdir -p "${VOICE_INBOX_DATA:-$HOME/.local/share/voice-inbox}"
exec "$REPO/.venv/bin/voice-inbox"
