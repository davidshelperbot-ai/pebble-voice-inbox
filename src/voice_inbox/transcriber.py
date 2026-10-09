"""Local transcription: afconvert (macOS built-in) -> 16kHz mono WAV -> whisper.cpp."""
import subprocess
import tempfile
from pathlib import Path

WHISPER_BIN = "whisper-cli"
AFCONVERT = "/usr/bin/afconvert"


class TranscriptionError(Exception):
    pass


def transcribe(audio_path: Path, model_path: Path) -> str:
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise TranscriptionError(f"audio file missing: {audio_path}")
    if not Path(model_path).exists():
        raise TranscriptionError(f"whisper model missing: {model_path}")
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio.wav"
        conv = subprocess.run(
            [AFCONVERT, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
             str(audio_path), str(wav)],
            capture_output=True, text=True,
        )
        if conv.returncode != 0 or not wav.exists():
            raise TranscriptionError(f"afconvert failed: {conv.stderr.strip()[:500]}")
        run = subprocess.run(
            [WHISPER_BIN, "-m", str(model_path), "-f", str(wav),
             "--no-timestamps", "--no-prints"],
            capture_output=True, text=True, timeout=600,
        )
        if run.returncode != 0:
            raise TranscriptionError(f"whisper failed: {run.stderr.strip()[:500]}")
        text = run.stdout.strip()
        if not text:
            raise TranscriptionError("whisper produced empty transcript")
        return text
