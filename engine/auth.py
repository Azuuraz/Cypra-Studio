"""Resolve local provider API credentials from env, settings, or Local CLI login."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

LOCAL_AUTH_PATH = Path.home() / ".local" / "auth.json"
REFRESH_BUFFER = timedelta(minutes=5)
LOCAL_API_BASE = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

_AUTH_CACHE: dict[str, Any] = {"at": 0.0, "result": None}
_AUTH_CACHE_TTL = 45.0


def _parse_expiry(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _load_auth_entries() -> list[tuple[str, dict[str, Any]]]:
    if not LOCAL_AUTH_PATH.exists():
        return []
    try:
        with open(LOCAL_AUTH_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return [(k, v) for k, v in data.items() if isinstance(v, dict)]


def _save_auth_entry(entry_key: str, entry: dict[str, Any]) -> None:
    data: dict[str, Any] = {}
    if LOCAL_AUTH_PATH.exists():
        try:
            with open(LOCAL_AUTH_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}
    data[entry_key] = entry
    LOCAL_AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCAL_AUTH_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def refresh_local_token() -> str | None:
    for entry_key, entry in _load_auth_entries():
        refresh_token = (entry.get("refresh_token") or "").strip()
        client_id = (entry.get("oidc_client_id") or "").strip()
        if not refresh_token or not client_id:
            continue
        try:
            r = requests.post(
                "https://auth.x.ai/oauth2/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
                timeout=15,
            )
            if not r.ok:
                continue
            payload = r.json()
            access = (payload.get("access_token") or "").strip()
            if not access:
                continue
            expires_in = int(payload.get("expires_in", 3600))
            entry["key"] = access
            entry["expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            if payload.get("refresh_token"):
                entry["refresh_token"] = payload["refresh_token"]
            _save_auth_entry(entry_key, entry)
            return access
        except Exception:
            continue
    return None


def load_local_token(*, allow_refresh: bool = True) -> str | None:
    now = datetime.now(timezone.utc)
    for _, entry in _load_auth_entries():
        token = (entry.get("key") or "").strip()
        if not token:
            continue
        expires = _parse_expiry(entry.get("expires_at", ""))
        if expires and now >= expires - REFRESH_BUFFER:
            if allow_refresh:
                refreshed = refresh_local_token()
                if refreshed:
                    return refreshed
            if expires and now >= expires:
                continue
        return token
    if allow_refresh:
        return refresh_local_token()
    return None


def list_key_candidates(settings: dict[str, Any] | None = None) -> list[tuple[str, str]]:
    settings = settings or {}
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(key: str | None, source: str) -> None:
        if key and key not in seen:
            seen.add(key)
            candidates.append((key, source))

    add(os.environ.get("LOCAL_API_KEY", "").strip(), "env")
    add((settings.get("legacy_cloud_key") or "").strip(), "settings")
    add(load_local_token(), "grok_cli")
    return candidates


def validate_api_key(api_key: str) -> bool:
    try:
        r = requests.get(
            f"{LOCAL_API_BASE}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=12,
        )
        return r.status_code == 200
    except Exception:
        return False


def resolve_api_key(
    settings: dict[str, Any] | None = None, *, validate: bool = True
) -> tuple[str | None, str]:
    candidates = list_key_candidates(settings)
    if not candidates:
        return None, "none"
    if validate:
        for key, source in candidates:
            if validate_api_key(key):
                return key, source
    return candidates[0][0], candidates[0][1]


def auth_status(settings: dict[str, Any] | None = None, *, fast: bool = False) -> dict[str, Any]:
    now = time.time()
    if fast and _AUTH_CACHE["result"] and now - float(_AUTH_CACHE["at"]) < _AUTH_CACHE_TTL:
        return _AUTH_CACHE["result"]

    key, source = resolve_api_key(settings, validate=not fast)
    if not key:
        result = {
            "ok": False,
            "source": "none",
            "hint": "Set LOCAL_API_KEY, save a key in Settings, or run `local login`",
            "has_key": False,
        }
        _AUTH_CACHE.update(at=now, result=result)
        return result

    if fast:
        result = {
            "ok": True,
            "source": source,
            "hint": "local provider key detected",
            "has_key": True,
            "pending_validation": True,
        }
        _AUTH_CACHE.update(at=now, result=result)
        return result

    ok = validate_api_key(key)
    result = {
        "ok": ok,
        "source": source,
        "hint": "Connected to local provider / Local" if ok else "API key found but validation failed",
        "has_key": True,
    }
    _AUTH_CACHE.update(at=now, result=result)
    return result
