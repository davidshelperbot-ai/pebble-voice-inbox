import os
from pathlib import Path

import pytest

from voice_inbox import transcriber

MODEL = Path(os.path.expanduser("~/.local/share/voice-inbox/models/ggml-small.en.bin"))
# Not shipped: record your own short clip to run the integration test.
#   say -o hello.m4a --data-format=aac "buy milk"
FIXTURE = Path(__file__).parent / "fixtures" / "hello.m4a"


def test_missing_file_raises(tmp_path):
    with pytest.raises(transcriber.TranscriptionError):
        transcriber.transcribe(tmp_path / "nope.m4a", MODEL)


def test_garbage_audio_raises(tmp_path):
    bad = tmp_path / "bad.m4a"
    bad.write_bytes(b"not audio at all")
    with pytest.raises(transcriber.TranscriptionError):
        transcriber.transcribe(bad, MODEL)


@pytest.mark.integration
@pytest.mark.skipif(not (MODEL.is_file() and FIXTURE.is_file()),
                    reason="needs a whisper model and an audio fixture; "
                           "see docs/audio.md")
def test_real_transcription():
    text = transcriber.transcribe(FIXTURE, MODEL)
    assert "milk" in text.lower()
