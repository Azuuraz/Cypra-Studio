"""STT, TTS, and realtime helpers.

TTS backends:
- local provider Local TTS (cloud, high quality) when an API key is available
- Browser Speech Synthesis (local, free) handled client-side — works with Ollama
"""

from __future__ import annotations

from typing import Any

import requests

from engine.auth import LOCAL_API_BASE

VOICES = ["eve", "ara", "leo", "rex", "sal"]
TTS_PROVIDERS = ["off", "local", "edge", "browser", "legacy_cloud", "auto"]


def resolve_tts_provider(settings: dict[str, Any] | None, *, has_xai_key: bool) -> str:
    """Resolve presentation audio without loading the lazy local CPU engine."""
    s = settings or {}
    if not bool(s.get("voice_output_enabled", False)):
        return "off"
    p = (s.get("tts_provider") or "local").strip().lower()
    if p not in TTS_PROVIDERS:
        p = "auto"
    if p == "auto":
        return "local"
    if p == "legacy_cloud" and not has_xai_key:
        return "browser"
    return p


def speech_to_text(api_key: str, audio_bytes: bytes, filename: str = "audio.webm") -> str:
    """Transcribe audio via Local STT."""
    # Guess content type
    lower = filename.lower()
    if lower.endswith(".wav"):
        ctype = "audio/wav"
    elif lower.endswith(".mp3"):
        ctype = "audio/mpeg"
    elif lower.endswith(".ogg"):
        ctype = "audio/ogg"
    elif lower.endswith(".m4a"):
        ctype = "audio/mp4"
    else:
        ctype = "audio/webm"

    r = requests.post(
        f"{LOCAL_API_BASE}/stt",
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (filename, audio_bytes, ctype)},
        timeout=120,
    )
    if not r.ok:
        raise RuntimeError(f"STT failed ({r.status_code}): {r.text[:500]}")
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if isinstance(data, dict):
        text = data.get("text") or data.get("transcript") or ""
        if text:
            return text.strip()
    # some APIs return plain text
    return (r.text or "").strip()


def text_to_speech(
    api_key: str,
    text: str,
    *,
    voice_id: str = "eve",
    language: str = "en",
) -> bytes:
    """Synthesize speech via Local TTS. Returns audio bytes (mp3)."""
    r = requests.post(
        f"{LOCAL_API_BASE}/tts",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "text": text[:5000],
            "voice_id": voice_id or "eve",
            "language": language,
        },
        timeout=120,
    )
    if not r.ok:
        raise RuntimeError(f"TTS failed ({r.status_code}): {r.text[:500]}")
    return r.content


def create_realtime_client_secret(
    api_key: str,
    *,
    expires_seconds: int = 300,
) -> dict[str, Any]:
    """Mint an ephemeral token for browser ↔ local provider realtime WebSocket."""
    r = requests.post(
        f"{LOCAL_API_BASE}/realtime/client_secrets",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"expires_after": {"seconds": expires_seconds}},
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Realtime session failed ({r.status_code}): {r.text[:500]}")
    data = r.json()
    # Normalize shapes
    if "client_secret" in data:
        return data
    return {
        "client_secret": {
            "value": data.get("value") or data.get("secret") or data.get("token"),
            "expires_at": data.get("expires_at"),
        },
        "raw": data,
    }
