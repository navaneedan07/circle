"""Local speech-to-text.

SpeechToTextProvider
    └── LocalWhisperProvider   (faster-whisper when installed, else openai-whisper)

Cloud transcription is never used by default: recordings stay on the machine.
If no local engine is available the provider reports unavailable and the
pipeline marks the recording FAILED with a safe message instead of crashing.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from circle.config import get_settings

log = logging.getLogger("circle.stt")


class SpeechToTextProvider(ABC):
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def transcribe(self, audio_path: Path) -> tuple[str, str]:
        """Returns (text, language). Raises RuntimeError when unavailable."""

    @abstractmethod
    def status(self) -> dict: ...


class UnavailableSTT(SpeechToTextProvider):
    def __init__(self, reason: str = "no local speech-to-text engine installed"):
        self.reason = reason

    def available(self) -> bool:
        return False

    def transcribe(self, audio_path: Path) -> tuple[str, str]:
        raise RuntimeError(f"speech-to-text unavailable: {self.reason}")

    def status(self) -> dict:
        return {"provider": "none", "available": False, "detail": self.reason}


class LocalWhisperProvider(SpeechToTextProvider):
    """Whisper running locally. Prefers faster-whisper (ctranslate2)."""

    def __init__(self):
        self.settings = get_settings()
        self._engine = None
        self._engine_name = ""
        self._failure: Optional[str] = None
        self._detect()

    def _detect(self) -> None:
        if self.settings.stt_provider in ("none", "off"):
            self._failure = "disabled by configuration"
            return
        try:
            import faster_whisper  # noqa: F401
            self._engine_name = "faster-whisper"
            return
        except Exception:
            pass
        try:
            import whisper  # noqa: F401
            self._engine_name = "openai-whisper"
            return
        except Exception:
            pass
        self._failure = ("no local whisper engine (install: pip install faster-whisper)")

    def available(self) -> bool:
        return self._failure is None

    def _load(self):
        if self._engine is not None:
            return self._engine
        model_name = self.settings.whisper_model
        if self._engine_name == "faster-whisper":
            from faster_whisper import WhisperModel
            self._engine = ("faster", WhisperModel(model_name, device="cpu",
                                                   compute_type="int8"))
        elif self._engine_name == "openai-whisper":
            import whisper
            self._engine = ("openai", whisper.load_model(model_name))
        return self._engine

    def transcribe(self, audio_path: Path) -> tuple[str, str]:
        if self._failure:
            raise RuntimeError(f"speech-to-text unavailable: {self._failure}")
        kind, engine = self._load()
        if kind == "faster":
            # Decode with ffmpeg (stable across PyAV versions) and hand
            # Whisper raw 16 kHz mono PCM instead of a file path.
            pcm = _decode_pcm16k(audio_path)
            segments, info = engine.transcribe(pcm, vad_filter=True)
            text = " ".join(s.text for s in segments).strip()
            return text, getattr(info, "language", "") or ""
        if kind == "openai":
            result = engine.transcribe(str(audio_path))
            return (result.get("text") or "").strip(), result.get("language", "") or ""
        raise RuntimeError("unreachable whisper state")

    def status(self) -> dict:
        return {
            "provider": self._engine_name if self.available() else "none",
            "available": self.available(),
            "model": self.settings.whisper_model,
            "detail": self._failure or "local whisper ready",
        }


_provider: Optional[SpeechToTextProvider] = None


def _decode_pcm16k(audio_path: Path):
    """ffmpeg -> 16 kHz mono PCM float32 ndarray (PyAV-independent)."""
    import os
    import subprocess
    import tempfile

    import numpy as np

    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found (required for local transcription)")
    fd, out_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(audio_path),
             "-ac", "1", "-ar", "16000", "-f", "wav", out_path],
            capture_output=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[:200]}")
        import wave
        with wave.open(out_path, "rb") as w:
            frames = w.readframes(w.getnframes())
        arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if arr.size == 0:
            raise RuntimeError("audio decoded to zero samples")
        return arr
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def get_stt() -> SpeechToTextProvider:
    global _provider
    if _provider is None:
        try:
            _provider = LocalWhisperProvider()
        except Exception as e:  # pragma: no cover
            log.warning("STT init failed: %s", e)
            _provider = UnavailableSTT(str(e))
    return _provider


def media_duration_seconds(path: Path) -> Optional[float]:
    """Best-effort duration via ffprobe (ffmpeg is installed on this box)."""
    if not shutil.which("ffprobe"):
        return None
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format",
             str(path)],
            capture_output=True, timeout=30, text=True)
        import json
        info = json.loads(out.stdout or "{}")
        dur = info.get("format", {}).get("duration")
        return float(dur) if dur else None
    except Exception:
        return None
