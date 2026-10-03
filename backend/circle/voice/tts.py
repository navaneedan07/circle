"""Optional voice output via ElevenLabs (spec §56).

ONLY the final generated answer text is sent for speech synthesis -- never
conversation history or retrieved evidence. The feature is fully optional:
without an API key the endpoint reports unavailable and the UI hides it.

    private data -> local retrieval -> local Gemma -> answer
                  -> (optional) ElevenLabs TTS -> audio playback
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from circle.config import get_settings

log = logging.getLogger("circle.tts")

API_BASE = "https://api.elevenlabs.io/v1"


def tts_enabled() -> bool:
    return bool(get_settings().elevenlabs_api_key)


def tts_status() -> dict:
    enabled = tts_enabled()
    return {
        "provider": "elevenlabs",
        "enabled": enabled,
        "available": enabled,
        "detail": "configured — read answers aloud" if enabled
                  else "not configured (optional)",
    }


def synthesize(answer_text: str) -> Optional[bytes]:
    """Returns MP3 bytes for the answer text, or None when unavailable.

    Sends only `answer_text` (the generated answer) -- no history, no evidence.
    """
    settings = get_settings()
    if not settings.elevenlabs_api_key:
        return None
    text = (answer_text or "").strip()
    if not text:
        return None
    if len(text) > 3000:
        text = text[:3000]      # keep TTS payloads small
    voice_id = settings.elevenlabs_voice_id or "21m00Tcm4TlvDq8ikWAM"
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                f"{API_BASE}/text-to-speech/{voice_id}",
                headers={"xi-api-key": settings.elevenlabs_api_key,
                         "accept": "audio/mpeg"},
                json={"text": text, "model_id": "eleven_multilingual_v2"},
            )
            if r.status_code != 200:
                log.warning("ElevenLabs TTS failed: HTTP %s", r.status_code)
                return None
            return r.content
    except Exception as e:
        log.warning("ElevenLabs TTS error: %s", e)
        return None
