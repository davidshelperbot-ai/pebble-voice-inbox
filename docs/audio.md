# Audio

The ring can send its **on-device transcription**, the **raw audio**, or both.

- Transcription only (recommended): fastest, nothing to transcribe locally.
- Audio only: clips are transcribed on your machine with whisper.cpp. Nothing
  is uploaded anywhere.
- Both: the ring's transcription wins, and the clip is kept alongside the queue
  row so you can check what was actually said.

Clips are stored under `$VOICE_INBOX_DATA/audio/`. Nothing deletes them for
you — if you send audio routinely, prune that directory on a schedule.

## Setting up local transcription

Install whisper.cpp and a model:

```bash
brew install whisper-cpp          # or build from source

mkdir -p ~/.local/share/voice-inbox/models
curl -L -o ~/.local/share/voice-inbox/models/ggml-small.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin
```

Point at a different model with `VOICE_INBOX_WHISPER_MODEL`. `small.en` is a
reasonable default for short dictation on Apple silicon; `base.en` is faster
and noticeably worse at names.

`afconvert` (macOS, built in) converts the ring's m4a to the 16 kHz WAV
whisper.cpp wants. On Linux, substitute ffmpeg — see `transcriber.py`, which is
a single short module.

A transcription failure is retried with backoff like any other failure, and the
clip stays on disk, so a missing model is recoverable rather than lossy: install
it and the queued audio drains.
