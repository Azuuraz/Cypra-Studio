"""
LLM provider layer: local provider (Local API) or local Ollama on this machine.

Both use OpenAI-compatible chat completions so chat/extract share one path.
Voice (STT/TTS/realtime) stays on local provider when a key is available.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path
import time
from typing import Any

import httpx
import requests
from openai import OpenAI

from engine.auth import LOCAL_API_BASE, resolve_api_key

def _normalize_ollama_base(base: str | None) -> str:
    """Normalize Ollama host values for HTTP clients (bare host:port is valid to Ollama but not requests)."""
    b = str(base or "").strip().rstrip("/")
    if not b:
        b = "http://127.0.0.1:11434"
    if "://" not in b:
        b = "http://" + b
    return b


# The Studio startup process sets OLLAMA_HOST to the private, project-local
# endpoint. Prefer that runtime over any stale saved 11434 setting so pulls and
# chat can never silently fall back to the host Ollama store.
DEFAULT_OLLAMA_BASE = _normalize_ollama_base(os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434")

# Studio exposes one authoritative manual context-size setting. Supported values
# run from 8K through 256K. Runtime callers must use resolve_ollama_context so
# Ollama never receives a context outside this tested allocation ladder.
OLLAMA_CONTEXT_MIN = 8192
OLLAMA_CONTEXT_MAX = 262144
OLLAMA_CONTEXT_CHOICES = (8192, 16384, 32768, 65536, 131072, 262144)


def normalize_ollama_context(value: Any, *, allow_auto: bool = False) -> int:
    raw = str(value if value is not None else "").strip().lower()
    # Keep the compatibility flag for plugins/older callers, but Studio itself no
    # longer stores AUTO. Legacy 0/auto values migrate to the 8K minimum.
    if allow_auto and raw in {"", "auto", "0", "-1"}:
        return 0
    if raw in {"", "auto", "0", "-1"}:
        return OLLAMA_CONTEXT_MIN
    try:
        requested = int(float(raw))
    except (TypeError, ValueError):
        return OLLAMA_CONTEXT_MIN
    if requested <= OLLAMA_CONTEXT_MIN:
        return OLLAMA_CONTEXT_MIN
    if requested >= OLLAMA_CONTEXT_MAX:
        return OLLAMA_CONTEXT_MAX
    if requested in OLLAMA_CONTEXT_CHOICES:
        return requested
    # For imported/custom values, round down to the largest supported allocation
    # so normalization never consumes more memory than the requested value.
    return max(choice for choice in OLLAMA_CONTEXT_CHOICES if choice <= requested)


def resolve_ollama_context(settings: dict[str, Any] | None = None) -> int:
    configured = normalize_ollama_context((settings or {}).get("ollama_num_ctx", OLLAMA_CONTEXT_MIN))
    return configured if configured >= OLLAMA_CONTEXT_MIN else OLLAMA_CONTEXT_MIN


def ollama_v1(base: str | None = None) -> str:
    # Prefer the private process-local endpoint whenever the launcher supplied it.
    chosen = os.environ.get("OLLAMA_HOST") or base or DEFAULT_OLLAMA_BASE
    b = _normalize_ollama_base(chosen)
    if b.endswith("/v1"):
        return b
    return b + "/v1"


def ollama_root(base: str | None = None) -> str:
    # Never allow a stale saved http://127.0.0.1:11434 setting to override the
    # private project runtime started by START.ps1.
    chosen = os.environ.get("OLLAMA_HOST") or base or DEFAULT_OLLAMA_BASE
    b = _normalize_ollama_base(chosen)
    if b.endswith("/v1"):
        return b[:-3]
    return b


def local_ollama_store() -> str:
    """Return the model store selected for the running Studio process."""
    env_store = os.environ.get("OLLAMA_MODELS")
    if env_store:
        return str(Path(env_store)).replace("\\", "/")
    root = Path(__file__).resolve().parents[1]
    return str(root / "OllamaModels").replace("\\", "/")


def get_provider(settings: dict[str, Any] | None = None) -> str:
    """Local Ollama is the only supported provider."""
    return "ollama"


def provider_for(settings: dict[str, Any] | None, purpose: str = "chat") -> str:
    """Resolve the local project provider."""
    return "ollama"


def make_client(
    settings: dict[str, Any] | None = None,
    *,
    purpose: str = "chat",
    timeout_seconds: float | None = None,
) -> tuple[OpenAI, str, str]:
    """Return the project-local Ollama OpenAI-compatible client."""
    s = settings or {}
    base = ollama_v1(s.get("ollama_base_url") or DEFAULT_OLLAMA_BASE)
    # Short connect timeout so a down Ollama fails in seconds, not a frozen window.
    client = OpenAI(
        api_key=s.get("ollama_api_key") or "ollama",
        base_url=base,
        timeout=httpx.Timeout(float(timeout_seconds if timeout_seconds is not None else 300.0), connect=3.0),
    )
    return client, "ollama", base


def resolve_chat_model(settings: dict[str, Any] | None = None) -> str:
    s = settings or {}
    if provider_for(s, "chat") == "ollama":
        return (
            s.get("ollama_chat_model")
            or s.get("chat_model")
            or "llama3.2:3b"
        )
    return s.get("chat_model") or "local-4.5"


def resolve_extract_model(settings: dict[str, Any] | None = None) -> str:
    s = settings or {}
    if provider_for(s, "extract") == "ollama":
        return (
            s.get("ollama_extract_model")
            or s.get("ollama_chat_model")
            or s.get("extract_model")
            or "llama3.2:3b"
        )
    return s.get("extract_model") or "local-4.3"


# Short TTL cache so UI status + chat don't hammer Ollama /api/tags
_OLLAMA_CACHE: dict[str, Any] = {"ts": 0.0, "root": "", "ok": False, "models": [], "err": ""}
_OLLAMA_CACHE_TTL = 12.0  # seconds
# Per-model /api/show capability cache. This keeps model discovery accurate
# without turning the settings page into an N+1 latency trap on every refresh.
_OLLAMA_CAP_CACHE: dict[str, dict[str, Any]] = {}

# Warm state is deliberately separate from request/response generation.
# Loading a local model can take a while on 6GB-class GPUs and should never
# block the BV settings request or make the UI look frozen.
_WARM_LOCK = threading.RLock()
_WARM_STATE: dict[str, Any] = {
    "running": False,
    "model": None,
    "purpose": None,
    "started_at": None,
    "finished_at": None,
    "ok": None,
    "error": None,
    "num_ctx": None,
    "done_reason": None,
    "stage": "idle",
}
_OLLAMA_CAP_TTL = 60.0


def _ollama_ok(settings: dict[str, Any], *, force: bool = False) -> tuple[bool, list[str], str, str]:
    import time

    root = ollama_root(settings.get("ollama_base_url"))
    now = time.time()
    if (
        not force
        and _OLLAMA_CACHE.get("root") == root
        and (now - float(_OLLAMA_CACHE.get("ts") or 0)) < _OLLAMA_CACHE_TTL
    ):
        return (
            bool(_OLLAMA_CACHE["ok"]),
            list(_OLLAMA_CACHE["models"]),
            root,
            str(_OLLAMA_CACHE.get("err") or ""),
        )

    models: list[str] = []
    err = ""
    ok = False
    try:
        r = requests.get(f"{root}/api/tags", timeout=2.5)
        if r.ok:
            ok = True
            models = [
                m.get("name") or m.get("model")
                for m in (r.json().get("models") or [])
                if m.get("name") or m.get("model")
            ]
        else:
            err = f"Ollama HTTP {r.status_code}"
    except Exception as e:
        err = str(e)
    _OLLAMA_CACHE.update({"ts": now, "root": root, "ok": ok, "models": models, "err": err})
    return ok, models, root, err


def _xai_ok(settings: dict[str, Any]) -> tuple[bool, bool, str]:
    key, source = resolve_api_key(settings, validate=False)
    has = bool(key)
    ok = False
    if key:
        try:
            r = requests.get(
                f"{LOCAL_API_BASE}/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=8,
            )
            ok = r.status_code == 200
        except Exception:
            ok = False
    return ok if has else False, has, source if has else "none"


def provider_status(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    s = settings or {}
    provider = get_provider(s)
    chat_p = provider_for(s, "chat")
    extract_p = provider_for(s, "extract")

    if provider == "ollama":
        ok, models, root, err = _ollama_ok(s)
        return {
            "provider": "ollama",
            "ok": ok,
            "label": "Local Ollama",
            "base_url": root,
            "models": models,
            "chat_provider": "ollama",
            "extract_provider": "ollama",
            "chat_model": resolve_chat_model(s),
            "extract_model": resolve_extract_model(s),
            "hint": (
                f"Ollama · {len(models)} model(s)"
                if ok
                else f"Ollama not reachable at {root}. Start Ollama, then refresh."
            ),
            "error": err or None,
            "voice_note": "Voice STT/TTS still needs an local provider key (optional).",
        }

    if provider == "hybrid":
        o_ok, models, root, o_err = _ollama_ok(s)
        x_ok, has, source = _xai_ok(s)
        need_o = chat_p == "ollama" or extract_p == "ollama"
        need_x = chat_p == "legacy_cloud" or extract_p == "legacy_cloud"
        ok = (o_ok or not need_o) and (x_ok or not need_x)
        parts = []
        parts.append(f"chat={chat_p}:{resolve_chat_model(s)}")
        parts.append(f"extract={extract_p}:{resolve_extract_model(s)}")
        return {
            "provider": "hybrid",
            "ok": ok,
            "label": "Hybrid (local + API)",
            "chat_provider": chat_p,
            "extract_provider": extract_p,
            "chat_model": resolve_chat_model(s),
            "extract_model": resolve_extract_model(s),
            "models": models,
            "base_url": root,
            "has_key": has,
            "source": source,
            "hint": "Hybrid · " + " · ".join(parts) + ("" if ok else " · check Ollama/local provider"),
            "error": o_err if need_o and not o_ok else None,
            "voice_note": "Voice uses local provider when a key is present.",
        }

    ok, has, source = _xai_ok(s)
    return {
        "provider": "legacy_cloud",
        "ok": ok if has else False,
        "label": "local provider Local API",
        "source": source,
        "has_key": has,
        "chat_provider": "legacy_cloud",
        "extract_provider": "legacy_cloud",
        "chat_model": resolve_chat_model(s),
        "extract_model": resolve_extract_model(s),
        "hint": (
            "Connected to local provider / Local"
            if ok
            else (
                "API key found but validation failed"
                if has
                else "No local provider key — set key or switch to Local / Hybrid"
            )
        ),
        "models": [
            "local-4.5",
            "local-4.3",
            "local-4.20-0309-reasoning",
            "local-4.20-0309-non-reasoning",
        ],
    }


# ── Local model catalog (new + older installs) ──────────────────────
# Roles: chat, extract, embed, vision, code, thinking
# Tiers: nano (≤2B), small (~3B), mid (~7–9B), heavy (slow / large VRAM)

def _cat(
    label: str,
    *,
    tier: str,
    roles: list[str],
    speed: int,
    quality: int,
    group: str,
    note: str = "",
) -> dict[str, Any]:
    return {
        "label": label,
        "tier": tier,
        "roles": roles,
        "speed": speed,  # 1–5 higher = faster
        "quality": quality,  # 1–5 higher = smarter
        "group": group,
        "note": note,
        "vision": "vision" in roles,
        "embedding": "embed" in roles,
        "thinking": "thinking" in roles,
        "code": "code" in roles,
    }


# Known models on this machine + common Ollama tags (order = display preference within group)
LOCAL_MODEL_CATALOG: dict[str, dict[str, Any]] = {
    # ── Fast / nano ──
    "qwen3:0.6b": _cat("Qwen3 0.6B", tier="nano", roles=["chat"], speed=5, quality=1, group="Fast", note="Tiny text"),
    "qwen3.5:0.8b": _cat("Qwen3.5 0.8B", tier="nano", roles=["chat", "extract", "vision"], speed=5, quality=2, group="Fast"),
    "qwen2.5:1.5b": _cat("Qwen2.5 1.5B", tier="nano", roles=["chat", "extract"], speed=5, quality=2, group="Fast"),
    "llama3.2:1b": _cat("Llama 3.2 1B", tier="nano", roles=["chat", "extract"], speed=5, quality=2, group="Fast"),
    "deepseek-r1:1.5b": _cat("DeepSeek R1 1.5B", tier="nano", roles=["chat", "thinking"], speed=4, quality=2, group="Thinking", note="Tiny reasoner"),
    "qwen3:1.7b": _cat("Qwen3 1.7B", tier="nano", roles=["chat", "extract", "thinking"], speed=4, quality=2, group="Fast"),
    "gemma2:2b": _cat("Gemma2 2B", tier="nano", roles=["chat"], speed=4, quality=2, group="Fast"),
    "qwen3-vl:2b-instruct": _cat("Qwen3-VL 2B Instruct", tier="nano", roles=["chat", "vision"], speed=4, quality=2, group="Vision"),
    "qwen3-vl:2b-thinking": _cat("Qwen3-VL 2B Thinking", tier="nano", roles=["chat", "vision", "thinking"], speed=3, quality=2, group="Vision"),
    # ── Balanced / small ──
    "qwen2.5:3b": _cat("Qwen2.5 3B", tier="small", roles=["chat", "extract"], speed=4, quality=3, group="Balanced", note="Best speed/quality default"),
    "llama3.2:3b": _cat("Llama 3.2 3B", tier="small", roles=["chat", "extract"], speed=4, quality=3, group="Balanced", note="App default"),
    "qwen3.5:2b": _cat("Qwen3.5 2B", tier="small", roles=["chat", "extract", "vision"], speed=4, quality=3, group="Balanced"),
    "phi3:mini": _cat("Phi-3 Mini", tier="small", roles=["chat", "extract"], speed=3, quality=3, group="Balanced"),
    "qwen3:4b": _cat("Qwen3 4B", tier="small", roles=["chat", "extract", "thinking"], speed=3, quality=4, group="Balanced"),
    "qwen3.5:4b": _cat("Qwen3.5 4B", tier="small", roles=["chat", "extract", "vision"], speed=3, quality=4, group="Quality", note="Strong all-round local"),
    "qwen3-vl:4b-instruct": _cat("Qwen3-VL 4B Instruct", tier="small", roles=["chat", "vision"], speed=3, quality=4, group="Vision", note="Best vision on 6GB"),
    "qwen3-vl:4b-thinking": _cat("Qwen3-VL 4B Thinking", tier="small", roles=["chat", "vision", "thinking"], speed=2, quality=4, group="Vision"),
    # ── Mid / quality ──
    "mistral:7b": _cat("Mistral 7B", tier="mid", roles=["chat", "extract"], speed=2, quality=4, group="Quality"),
    "codellama:7b": _cat("Code Llama 7B", tier="mid", roles=["chat", "code"], speed=2, quality=3, group="Code"),
    "llama3.1:8b": _cat("Llama 3.1 8B", tier="mid", roles=["chat", "extract"], speed=2, quality=4, group="Quality", note="Tight on 6GB VRAM"),
    # ── Heavy ──
    "qwen3.6:latest": _cat("Qwen3.6 (large)", tier="heavy", roles=["chat", "extract", "vision", "thinking"], speed=1, quality=5, group="Heavy", note="Slow · large VRAM/RAM"),
    "gemma4:latest": _cat("Gemma 4", tier="heavy", roles=["chat", "thinking", "vision"], speed=1, quality=5, group="Heavy", note="Thinking + multimodal; often 9GB+"),
    "gemma4:e2b": _cat("Gemma 4 E2B", tier="mid", roles=["chat", "thinking", "vision"], speed=2, quality=4, group="Thinking"),
    "gemma4:e4b": _cat("Gemma 4 E4B", tier="mid", roles=["chat", "thinking", "vision"], speed=2, quality=4, group="Thinking"),
    "gemma4:12b": _cat("Gemma 4 12B", tier="mid", roles=["chat", "thinking", "vision"], speed=1, quality=5, group="Heavy"),
    "gemma4:26b": _cat("Gemma 4 26B", tier="heavy", roles=["chat", "thinking", "vision"], speed=1, quality=5, group="Heavy", note="Does not fit 6GB"),
    "gemma4:31b": _cat("Gemma 4 31B", tier="heavy", roles=["chat", "thinking", "vision"], speed=1, quality=5, group="Heavy", note="Does not fit 6GB"),
        "huihui_ai/gemma-4-abliterated:latest": _cat(
        "Gemma 4 4B abliterated",
        tier="small",
        roles=["chat", "extract", "thinking"],
        speed=3,
        quality=4,
        group="Thinking",
        note="Solid Matrix fleet base + think on 6GB (~3.2GB)",
    ),
    "huihui_ai/gemma-4-abliterated:e4b": _cat(
        "Gemma 4 E4B abliterated",
        tier="mid",
        roles=["chat", "extract", "thinking"],
        speed=2,
        quality=4,
        group="Thinking",
        note="Check size — some E4B tags are 9GB+",
    ),
    "huihui_ai/deepseek-r1-abliterated:1.5b": _cat(
        "DeepSeek-R1 1.5B abliterated",
        tier="nano",
        roles=["chat", "thinking"],
        speed=5,
        quality=2,
        group="Thinking",
        note="Fast uncensored reasoner (~1.1GB)",
    ),
    "huihui_ai/deepseek-r1-abliterated:7b": _cat(
        "DeepSeek-R1 7B abliterated",
        tier="mid",
        roles=["chat", "thinking", "code"],
        speed=2,
        quality=4,
        group="Thinking",
        note="Best 6GB reasoning pick (~4.7GB)",
    ),
    # ── Embed ──
    "nomic-embed-text:latest": _cat("Nomic Embed Text", tier="nano", roles=["embed"], speed=5, quality=3, group="Embed"),
    "nomic-embed-text": _cat("Nomic Embed Text", tier="nano", roles=["embed"], speed=5, quality=3, group="Embed"),
}

# Preferred order when applying a usage preset (first installed wins)
LOCAL_PRESETS: dict[str, dict[str, Any]] = {
    "fast": {
        "id": "fast",
        "label": "Fast",
        "description": "Snappy Qwen replies (≤2B · sparse-friendly)",
        "chat": [
            "qwen3.5:2b",
            "qwen2.5:1.5b",
            "qwen3:1.7b",
            "qwen3.5:0.8b",
            "qwen3:0.6b",
            "llama3.2:1b",
            "gemma2:2b",
        ],
        # extract forced = chat at apply time; list kept for inventory display
        "extract": [
            "qwen3.5:2b",
            "qwen2.5:1.5b",
            "qwen3:1.7b",
            "qwen3.5:0.8b",
            "qwen3:0.6b",
        ],
        "embed": ["nomic-embed-text:latest", "nomic-embed-text"],
        # Short ctx conserves VRAM on 6GB cards
        "ollama_num_ctx": 8192,
        "ollama_chat_tokens": 768,
        "ollama_extract_tokens": 512,
    },
    "balanced": {
        "id": "balanced",
        "label": "Balanced",
        "description": "Daily Qwen driver (~3B · one model in VRAM)",
        "chat": [
            "qwen2.5:3b",
            "qwen3.5:2b",
            "qwen3:4b",
            "qwen3.5:4b",
            "llama3.2:3b",
            "phi3:mini",
        ],
        "extract": [
            "qwen2.5:3b",
            "qwen3.5:2b",
            "qwen3:4b",
            "qwen3.5:4b",
            "llama3.2:3b",
        ],
        "embed": ["nomic-embed-text:latest", "nomic-embed-text"],
        "ollama_num_ctx": 8192,
        "ollama_chat_tokens": 1024,
        "ollama_extract_tokens": 768,
    },
    "quality": {
        "id": "quality",
        "label": "Quality",
        "description": "Best small Qwen on 6GB (4B · still lean ctx)",
        "chat": [
            "qwen3.5:4b",
            "qwen3:4b",
            "qwen2.5:3b",
            "mistral:7b",
            "llama3.1:8b",
        ],
        "extract": [
            "qwen3.5:4b",
            "qwen3:4b",
            "qwen2.5:3b",
            "mistral:7b",
            "llama3.1:8b",
        ],
        "embed": ["nomic-embed-text:latest", "nomic-embed-text"],
        # Keep lean on 6GB — avoid thrashing KV cache
        "ollama_num_ctx": 8192,
        "ollama_chat_tokens": 1536,
        "ollama_extract_tokens": 1024,
    },
    "vision": {
        "id": "vision",
        "label": "Vision",
        "description": "Multimodal chat + solid text extract",
        "chat": [
            "qwen3-vl:4b-instruct",
            "qwen3.5:4b",
            "qwen3-vl:2b-instruct",
            "qwen3.5:2b",
            "qwen3-vl:4b-thinking",
        ],
        "extract": [
            "qwen3.5:4b",
            "qwen3:4b",
            "qwen2.5:3b",
            "llama3.2:3b",
            "qwen3.5:2b",
        ],
        "embed": ["nomic-embed-text:latest", "nomic-embed-text"],
        "ollama_num_ctx": 8192,
        "ollama_chat_tokens": 1024,
        "ollama_extract_tokens": 768,
    },
    "code": {
        "id": "code",
        "label": "Code",
        "description": "Code-focused chat",
        "chat": [
            "codellama:7b",
            "qwen3.5:4b",
            "qwen3:4b",
            "llama3.1:8b",
            "mistral:7b",
            "qwen2.5:3b",
        ],
        "extract": [
            "qwen3.5:4b",
            "qwen3:4b",
            "qwen2.5:3b",
            "llama3.2:3b",
        ],
        "embed": ["nomic-embed-text:latest", "nomic-embed-text"],
        "ollama_num_ctx": 8192,
        "ollama_chat_tokens": 1536,
        "ollama_extract_tokens": 768,
    },
    "thinking": {
        "id": "thinking",
        "label": "Thinking",
        "description": "Reasoning / thinking models",
        "chat": [
            "qwen3:4b",
            "qwen3-vl:4b-thinking",
            "deepseek-r1:1.5b",
            "qwen3-vl:2b-thinking",
            "qwen3:1.7b",
        ],
        "extract": [
            "qwen3.5:4b",
            "qwen3:4b",
            "qwen2.5:3b",
            "llama3.2:3b",
        ],
        "embed": ["nomic-embed-text:latest", "nomic-embed-text"],
        "ollama_num_ctx": 8192,
        "ollama_chat_tokens": 1536,
        "ollama_extract_tokens": 768,
    },
}

_GROUP_ORDER = ["Fast", "Balanced", "Quality", "Vision", "Code", "Thinking", "Heavy", "Embed", "Other"]


def catalog_entry(model_id: str) -> dict[str, Any] | None:
    """Lookup catalog by exact id or base name (tag-insensitive where possible)."""
    if not model_id:
        return None
    mid = model_id.strip()
    if mid in LOCAL_MODEL_CATALOG:
        return LOCAL_MODEL_CATALOG[mid]
    # nomic-embed-text:latest ↔ nomic-embed-text
    base = mid.split(":")[0]
    if mid.endswith(":latest") and base in LOCAL_MODEL_CATALOG:
        return LOCAL_MODEL_CATALOG[base]
    if f"{base}:latest" in LOCAL_MODEL_CATALOG:
        return LOCAL_MODEL_CATALOG[f"{base}:latest"]
    return None


def _infer_meta(name: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fallback metadata for models not in the catalog."""
    d = details or {}
    low = name.lower()
    embedding = "embed" in low or d.get("family") == "nomic-bert"
    vision = any(x in low for x in ("-vl", "vision", "llava", "moondream"))
    # Name-based fallback only. Authoritative thinking support is filled from
    # Ollama /api/show capabilities when available (see _apply_runtime_capabilities).
    thinking = (
        "thinking" in low
        or "deepseek-r1" in low
        or re.search(r"(^|[/:\-_.])(r1|qwen3|qwen3\.5|qwen3\.6|gemma4|gemma-4)([:./\-_]|$)", low) is not None
    )
    code = "code" in low or "coder" in low
    roles = []
    if embedding:
        roles = ["embed"]
        group = "Embed"
    else:
        roles = ["chat", "extract"]
        if vision:
            roles.append("vision")
        if thinking:
            roles.append("thinking")
        if code:
            roles.append("code")
        group = "Vision" if vision else ("Code" if code else ("Thinking" if thinking else "Other"))
    return {
        "label": name,
        "tier": "unknown",
        "roles": roles,
        "speed": 3,
        "quality": 3,
        "group": group,
        "note": "",
        "vision": vision,
        "embedding": embedding,
        "thinking": thinking,
        "code": code,
    }


def _enrich_model(
    name: str,
    *,
    size: Any = None,
    family: str | None = None,
    parameter_size: str | None = None,
    details: dict[str, Any] | None = None,
    installed: bool = True,
) -> dict[str, Any]:
    cat = catalog_entry(name)
    meta = dict(cat) if cat else _infer_meta(name, details)
    embedding = bool(meta.get("embedding")) or "embed" in name.lower()
    if details and details.get("family") == "nomic-bert":
        embedding = True
    return {
        "id": name,
        "name": name,
        "label": meta.get("label") or name,
        "size": size,
        "family": family or (details or {}).get("family"),
        "parameter_size": parameter_size or (details or {}).get("parameter_size"),
        "quantization_level": (details or {}).get("quantization_level"),
        "embedding": embedding,
        "vision": bool(meta.get("vision")),
        "thinking": bool(meta.get("thinking")),
        "code": bool(meta.get("code")),
        "roles": list(meta.get("roles") or []),
        "tier": meta.get("tier") or "unknown",
        "group": meta.get("group") or "Other",
        "speed": int(meta.get("speed") or 3),
        "quality": int(meta.get("quality") or 3),
        "note": meta.get("note") or "",
        "installed": installed,
        "display": _format_model_display(name, meta, parameter_size),
    }


def _format_model_display(name: str, meta: dict[str, Any], parameter_size: str | None) -> str:
    label = meta.get("label") or name
    bits: list[str] = []
    if parameter_size:
        bits.append(str(parameter_size))
    elif meta.get("tier") and meta["tier"] != "unknown":
        bits.append(str(meta["tier"]))
    tags = []
    if meta.get("vision"):
        tags.append("vision")
    if meta.get("thinking"):
        tags.append("think")
    if meta.get("code"):
        tags.append("code")
    if meta.get("embedding"):
        tags.append("embed")
    if tags:
        bits.append("+".join(tags))
    if meta.get("note"):
        bits.append(str(meta["note"]))
    if bits:
        return f"{label} · {name} ({', '.join(bits)})"
    return f"{label} · {name}"


def _sort_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gidx = {g: i for i, g in enumerate(_GROUP_ORDER)}

    def key(m: dict[str, Any]) -> tuple:
        return (
            gidx.get(m.get("group") or "Other", 99),
            -int(m.get("speed") or 0),
            -int(m.get("quality") or 0),
            m.get("name") or m.get("id") or "",
        )

    return sorted(models, key=key)


def pick_installed(candidates: list[str], installed: list[str] | set[str]) -> str | None:
    """First candidate that is installed (exact, then :latest alias)."""
    inst = set(installed)
    inst_lower = {x.lower(): x for x in inst}
    for c in candidates:
        if c in inst:
            return c
        if c.lower() in inst_lower:
            return inst_lower[c.lower()]
        # tag aliases
        base = c.split(":")[0]
        if f"{base}:latest" in inst:
            return f"{base}:latest"
        if base in inst:
            return base
        for name in inst:
            if name.split(":")[0] == base and c.endswith(name.split(":")[-1] if ":" in name else ""):
                return name
    return None


def resolve_local_preset(
    preset_id: str,
    settings: dict[str, Any] | None = None,
    *,
    installed: list[str] | None = None,
) -> dict[str, Any]:
    """
    Resolve a named local usage preset to concrete model ids + optional knobs.
    Never breaks functionality: falls back to current settings / defaults.
    """
    s = settings or {}
    preset = LOCAL_PRESETS.get((preset_id or "").strip().lower())
    if not preset:
        return {
            "ok": False,
            "error": f"Unknown preset: {preset_id}",
            "presets": list(LOCAL_PRESETS.keys()),
        }
    if installed is None:
        _, names, _, _ = _ollama_ok(s, force=True)
        installed = names
    chat = pick_installed(list(preset.get("chat") or []), installed)
    extract = pick_installed(list(preset.get("extract") or []), installed) or chat
    embed = pick_installed(list(preset.get("embed") or []), installed) or s.get("embed_model") or "nomic-embed-text"
    if not chat:
        chat = s.get("ollama_chat_model") or "llama3.2:3b"
    if not extract:
        extract = s.get("ollama_extract_model") or chat
    return {
        "ok": True,
        "preset": preset["id"],
        "label": preset["label"],
        "description": preset.get("description") or "",
        "ollama_chat_model": chat,
        "ollama_extract_model": extract,
        "embed_model": embed,
        "ollama_num_ctx": preset.get("ollama_num_ctx"),
        "ollama_chat_tokens": preset.get("ollama_chat_tokens"),
        "ollama_extract_tokens": preset.get("ollama_extract_tokens"),
        "installed_count": len(installed),
        "missing_preferred": not bool(pick_installed(list(preset.get("chat") or []), installed)),
    }


def list_local_presets(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    s = settings or {}
    _, names, _, _ = _ollama_ok(s, force=False)
    root = ollama_root(s.get("ollama_base_url"))
    out = []
    for pid, p in LOCAL_PRESETS.items():
        chat = pick_installed(list(p.get("chat") or []), names)
        # The Thinking preset must follow the live Ollama capability inventory,
        # not a fixed shortlist. This makes newer families such as Gemma 4
        # available automatically as soon as they are installed.
        if pid == "thinking" and not chat:
            runtime_thinking: list[str] = []
            seen_runtime: set[str] = set()
            for name in names:
                if not name:
                    continue
                candidates = [name]
                if ":" not in name:
                    candidates.append(f"{name}:latest")
                cap = None
                chosen = name
                for cand in candidates:
                    cap = _OLLAMA_CAP_CACHE.get(f"{root}|{cand}")
                    if cap:
                        chosen = cand
                        break
                if cap and (cap.get("thinking_supported") or cap.get("thinking_detected")) and chosen not in seen_runtime:
                    runtime_thinking.append(chosen)
                    seen_runtime.add(chosen)
            if runtime_thinking:
                # Prefer the smallest/faster thinking model by known catalog
                # score, then fall back to lexical ordering for unknown models.
                def score(mid: str):
                    meta = catalog_entry(mid) or _infer_meta(mid)
                    return (-int(meta.get("speed") or 0), -int(meta.get("quality") or 0), mid)
                runtime_thinking.sort(key=score)
                chat = runtime_thinking[0]
        extract = pick_installed(list(p.get("extract") or []), names) or chat
        out.append(
            {
                "id": pid,
                "label": p["label"],
                "description": p.get("description") or "",
                "available": bool(chat),
                "would_set": {
                    "ollama_chat_model": chat,
                    "ollama_extract_model": extract,
                    "embed_model": pick_installed(list(p.get("embed") or []), names),
                },
            }
        )
    return out


def _ollama_model_capabilities(
    root: str, model_name: str, *, force: bool = False
) -> dict[str, Any]:
    """Read authoritative per-model capabilities from Ollama /api/show.

    Ollama's /api/tags payload has historically omitted or differed in capability
    details, so model selectors should not infer thinking support from the name
    alone. Results are cached briefly to keep refreshes cheap.
    """
    key = f"{root}|{model_name}"
    now = time.time()
    cached = _OLLAMA_CAP_CACHE.get(key)
    if cached and not force and (now - float(cached.get("ts") or 0.0)) < _OLLAMA_CAP_TTL:
        return dict(cached)
    out: dict[str, Any] = {"ts": now, "capabilities": [], "thinking_supported": False, "thinking_detected": False}
    try:
        r = requests.post(
            f"{root}/api/show",
            json={"model": model_name},
            timeout=3,
        )
        if r.ok:
            payload = r.json() or {}
            caps = [str(x).strip().lower() for x in (payload.get("capabilities") or [])]
            template = str(payload.get("template") or "").lower()
            out["capabilities"] = caps
            out["thinking_supported"] = "thinking" in caps
            # Some imported GGUFs can emit a parsed thinking field even when
            # /api/show does not advertise the capability. Treat those as
            # thinking-detected, but do not force think:true on them.
            out["thinking_detected"] = (
                "thinking" in caps
                or "<think>" in template
                or "<|think|>" in template
                or "channel>thought" in template
                or "reasoning_content" in template
            )
            out["template_thinking"] = bool(out["thinking_detected"] and "thinking" not in caps)
    except Exception:
        pass
    _OLLAMA_CAP_CACHE[key] = dict(out)
    return out



def ollama_model_thinking_support(
    settings: dict[str, Any] | None,
    model_name: str,
) -> tuple[bool, bool]:
    """Return (thinking_supported, thinking_detected) for the given model.

    Authoritative source is Ollama /api/show capabilities. Falls back to catalog
    and name heuristics so Gemma-4 abliterated and similar keep Think toggle live
    when the runtime has not yet been queried.
    """
    s = settings or {}
    name = str(model_name or "").strip()
    if not name:
        return False, False
    root = ollama_root(s.get("ollama_base_url"))
    try:
        cap = _ollama_model_capabilities(root, name)
        supported = bool(cap.get("thinking_supported"))
        detected = bool(cap.get("thinking_detected"))
        if supported or detected:
            return supported, detected
    except Exception:
        pass
    # Catalog / name fallback
    try:
        meta = catalog_entry(name) or _infer_meta(name)
        thinking = bool(meta.get("thinking"))
        return thinking, thinking
    except Exception:
        return False, False


def _apply_runtime_capabilities(
    root: str, models: list[dict[str, Any]], *, force: bool = False
) -> list[dict[str, Any]]:
    """Augment installed model metadata with runtime capabilities."""
    for m in models:
        name = str(m.get("id") or m.get("name") or "")
        if not name:
            continue
        cap = _ollama_model_capabilities(root, name, force=force)
        caps = list(cap.get("capabilities") or [])
        if caps:
            m["capabilities"] = caps
        m["thinking_supported"] = bool(cap.get("thinking_supported"))
        m["thinking_detected"] = bool(cap.get("thinking_detected"))
        # Runtime truth wins over catalog/name heuristics when available.
        if caps:
            m["thinking"] = bool(cap.get("thinking_supported"))
        elif cap.get("thinking_detected"):
            m["thinking"] = True
        if m.get("thinking"):
            roles = list(m.get("roles") or [])
            if "thinking" not in roles:
                roles.append("thinking")
            m["roles"] = roles
            if not m.get("group") or m.get("group") == "Other":
                m["group"] = "Thinking"
            # Rebuild the display tag after runtime detection.
            meta = {
                "label": m.get("label") or m.get("name"),
                "tier": m.get("tier") or "unknown",
                "vision": bool(m.get("vision")),
                "thinking": True,
                "code": bool(m.get("code")),
                "embedding": bool(m.get("embedding")),
                "note": m.get("note") or "",
            }
            m["display"] = _format_model_display(
                str(m.get("id") or m.get("name")),
                meta,
                m.get("parameter_size"),
            )
    return models


def list_ollama_models(
    settings: dict[str, Any] | None = None,
    *,
    force: bool = False,
) -> list[dict[str, Any]]:
    """
    All installed Ollama models, enriched with catalog roles for versatile local use.
    Chat-capable models include new Qwen3 / VL / 3.5 tags and older Llama/Mistral/etc.
    """
    s = settings or {}
    root = ollama_root(s.get("ollama_base_url"))
    ok, names, _, _ = _ollama_ok(s, force=force)
    if not ok and not names:
        ok, names, _, _ = _ollama_ok(s, force=True)

    out: list[dict[str, Any]] = []
    try:
        r = requests.get(f"{root}/api/tags", timeout=4)
        if r.ok:
            for m in r.json().get("models") or []:
                name = m.get("name") or m.get("model")
                if not name:
                    continue
                details = m.get("details") or {}
                out.append(
                    _enrich_model(
                        name,
                        size=m.get("size"),
                        family=details.get("family"),
                        parameter_size=details.get("parameter_size"),
                        details=details,
                        installed=True,
                    )
                )
        else:
            for n in names:
                out.append(_enrich_model(n, installed=True))
    except Exception:
        for n in names:
            out.append(_enrich_model(n, installed=True))

    # Ensure every installed name appears even if tags payload was partial
    seen = {m["id"] for m in out}
    for n in names:
        if n not in seen:
            out.append(_enrich_model(n, installed=True))
            seen.add(n)

    # Runtime capability discovery makes arbitrary installed models first-class,
    # including newer thinking families such as Gemma 4 that may not be in the
    # static catalog yet. The short cache keeps this cheap after the first load.
    out = _apply_runtime_capabilities(root, out, force=False)
    return _sort_models(out)


def _disk_local_model_inventory() -> list[dict[str, Any]]:
    """Discover models cached in the project-local Ollama store even when the runtime API is stale/offline.

    Ollama stores model manifests below <OLLAMA_MODELS>/manifests. We intentionally
    read only manifest metadata here; this never starts Ollama or changes the store.
    """
    store = Path(local_ollama_store())
    manifests = store / "manifests"
    if not manifests.exists() or not manifests.is_dir():
        return []
    found: dict[str, dict[str, Any]] = {}
    try:
        for mf in manifests.rglob("*"):
            if not mf.is_file() or mf.name.startswith("."):
                continue
            rel = mf.relative_to(manifests).parts
            if not rel:
                continue
            # Standard Ollama layout: registry.ollama.ai/library/<model>/<tag>
            parts = list(rel)
            if len(parts) >= 2 and parts[0] in {"registry.ollama.ai", "registry.ollama.ai.v2"}:
                parts = parts[1:]
            if parts and parts[0] == "library":
                parts = parts[1:]
            if not parts:
                continue
            if len(parts) == 1:
                model_name = parts[0]
            else:
                model_name = "/".join(parts[:-1]) + ":" + parts[-1]
            model_name = model_name.replace("\\", "/").strip()
            if not model_name:
                continue
            if ":" not in model_name.rsplit("/", 1)[-1]:
                model_name += ":latest"
            size = None
            try:
                payload = json.loads(mf.read_text(encoding="utf-8", errors="replace"))
                total = 0
                for layer in payload.get("layers") or []:
                    try:
                        total += int(layer.get("size") or 0)
                    except Exception:
                        pass
                cfg = payload.get("config") or {}
                try:
                    total += int(cfg.get("size") or 0)
                except Exception:
                    pass
                size = total or None
            except Exception:
                pass
            if model_name.lower() not in found:
                found[model_name.lower()] = {
                    "id": model_name,
                    "name": model_name,
                    "size": size,
                    "installed": False,
                    "local_cache": True,
                    "source": "project-local-disk",
                }
    except Exception:
        return []
    return sorted(found.values(), key=lambda x: str(x.get("name") or "").lower())


def local_model_inventory(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full local inventory for Settings UI: live runtime + project-local disk cache + catalog."""
    models = list_ollama_models(settings, force=True)
    live_ids = {str(m.get("id") or "").lower() for m in models}
    for cached in _disk_local_model_inventory():
        cid = str(cached.get("id") or "").lower()
        if cid and cid not in live_ids:
            models.append(_enrich_model(cached["id"], size=cached.get("size"), installed=False))
            models[-1]["local_cache"] = True
            models[-1]["source"] = "project-local-disk"
    chat = [m for m in models if not m.get("embedding")]
    embed = [m for m in models if m.get("embedding")]
    installed_ids = [m["id"] for m in models]
    # Catalog entries not installed (optional “known but missing” for pull hints)
    missing = []
    for mid, meta in LOCAL_MODEL_CATALOG.items():
        if mid in installed_ids:
            continue
        # skip alias duplicates
        if mid == "nomic-embed-text" and "nomic-embed-text:latest" in installed_ids:
            continue
        if f"{mid}:latest" in installed_ids:
            continue
        missing.append(_enrich_model(mid, installed=False))
    missing = _sort_models(missing)
    return {
        "models": models,
        "chat": chat,
        "embed": embed,
        "vision": [m for m in chat if m.get("vision")],
        "code": [m for m in chat if m.get("code")],
        "thinking": [m for m in chat if m.get("thinking")],
        "catalog_size": len(LOCAL_MODEL_CATALOG),
        "installed_count": len([m for m in models if m.get("installed") is not False or not m.get("local_cache")]),
        "runtime_count": len([m for m in models if not m.get("local_cache")]),
        "local_cache_count": len([m for m in models if m.get("local_cache")]),
        "missing_catalog": missing,
        "presets": list_local_presets(settings),
        "groups": _GROUP_ORDER,
    }


def warm_ollama_model(
    settings: dict[str, Any] | None = None,
    purpose: str = "chat",
) -> dict[str, Any]:
    """Warm/pin an Ollama model without competing with another loaded model.

    The previous implementation waited synchronously for a single 90s HTTP
    request. On a 6GB GPU, switching between base models can legitimately take
    longer than that while Ollama unloads an older model and maps the new one.
    This version:
      * fast-paths an already-loaded model via /api/ps;
      * unloads other running models first to prevent VRAM contention;
      * uses a 1-token no-op generation to trigger the load with minimal work;
      * runs safely behind a process-local warm lock;
      * records timing/status so callers can poll instead of blocking the UI.
    """
    s = settings or {}
    if provider_for(s, purpose) != "ollama":
        return {"ok": False, "model": None, "error": f"provider is not ollama for {purpose}"}

    root = ollama_root(s.get("ollama_base_url"))
    model = resolve_chat_model(s) if purpose == "chat" else resolve_extract_model(s)
    keep = s.get("ollama_keep_alive")
    if keep is None or keep == "":
        keep = -1
    if str(keep) == "-1":
        keep = -1
    num_ctx = resolve_ollama_context(s)

    with _WARM_LOCK:
        if _WARM_STATE.get("running"):
            if _WARM_STATE.get("model") == model:
                return {
                    "ok": None,
                    "loading": True,
                    "model": model,
                    "num_ctx": num_ctx,
                    "stage": _WARM_STATE.get("stage") or "loading",
                }
            return {
                "ok": False,
                "model": model,
                "num_ctx": num_ctx,
                "error": f"Another model is warming: {_WARM_STATE.get('model')}",
            }

    try:
        try:
            ping = requests.get(f"{root}/api/tags", timeout=3)
            if not ping.ok:
                return {"ok": False, "model": model, "num_ctx": num_ctx,
                        "error": f"Ollama tags HTTP {ping.status_code} at {root}"}
        except requests.RequestException as e:
            return {"ok": False, "model": model, "num_ctx": num_ctx,
                    "error": f"Ollama not reachable at {root}: {e}"}

        # Do not reload a model that Ollama already has resident.
        try:
            ps = requests.get(f"{root}/api/ps", timeout=3)
            if ps.ok:
                running = ps.json().get("models") or []
                loaded_names = {str(x.get("name") or x.get("model") or "") for x in running}
                if any(_same_ollama_model(model, n) for n in loaded_names):
                    return {"ok": True, "loading": False, "already_warm": True,
                            "model": model, "num_ctx": num_ctx, "stage": "ready"}
        except requests.RequestException:
            running = []

        with _WARM_LOCK:
            _WARM_STATE.update({
                "running": True, "model": model, "purpose": purpose,
                "started_at": time.time(), "finished_at": None,
                "ok": None, "error": None, "num_ctx": num_ctx,
                "done_reason": None, "stage": "preparing",
            })

        # Free VRAM held by another loaded model before the target load. This is
        # the main protection against the "GPU busy or model still loading" case.
        for row in (running or []):
            name = str(row.get("name") or row.get("model") or "").strip()
            if not name or _same_ollama_model(name, model):
                continue
            try:
                requests.post(
                    f"{root}/api/generate",
                    json={"model": name, "prompt": "", "stream": False, "keep_alive": 0},
                    timeout=10,
                )
            except requests.RequestException:
                pass

        with _WARM_LOCK:
            _WARM_STATE["stage"] = "loading"

        # A single token is enough to force model mapping and KV allocation. The
        # response includes load_duration, so diagnostics can distinguish a
        # real load from slow generation later.
        r = requests.post(
            f"{root}/api/generate",
            json={
                "model": model,
                "prompt": " ",
                "stream": False,
                "keep_alive": keep,
                "options": {
                    "num_ctx": num_ctx,
                    # Match live-chat prompt ingestion so warming allocates the
                    # same runner shape and the first real turn does not reload.
                    "num_batch": recommended_ollama_num_batch(s) if num_ctx <= 8192 else 256,
                    "num_predict": 1,
                },
            },
            # Model mapping on a 6GB GPU can legitimately exceed four minutes
            # when Ollama is unloading another resident model. Keep the wait
            # inside the background worker so the BV UI remains non-blocking.
            timeout=600,
        )
        payload = {}
        try:
            payload = r.json() or {}
        except Exception:
            payload = {}
        if not r.ok:
            detail = (r.text or "")[:240]
            raise RuntimeError(f"Ollama generate HTTP {r.status_code}: {detail}")

        with _WARM_LOCK:
            _WARM_STATE.update({
                "running": False, "finished_at": time.time(), "ok": True,
                "stage": "ready", "done_reason": payload.get("done_reason"),
            })
        return {
            "ok": True, "loading": False, "model": model, "num_ctx": num_ctx,
            "done_reason": payload.get("done_reason"),
            "load_duration_ns": payload.get("load_duration"),
            "total_duration_ns": payload.get("total_duration"),
            "stage": "ready",
        }
    except requests.Timeout:
        msg = f"Timed out after 600s loading {model} (Ollama/GPU did not finish the load)"
        with _WARM_LOCK:
            _WARM_STATE.update({"running": False, "finished_at": time.time(), "ok": False,
                                "error": msg, "stage": "failed"})
        return {"ok": False, "loading": False, "model": model, "num_ctx": num_ctx, "error": msg}
    except Exception as e:
        with _WARM_LOCK:
            _WARM_STATE.update({"running": False, "finished_at": time.time(), "ok": False,
                                "error": str(e), "stage": "failed"})
        return {"ok": False, "loading": False, "model": model, "num_ctx": num_ctx, "error": str(e)}


def warm_status() -> dict[str, Any]:
    with _WARM_LOCK:
        out = dict(_WARM_STATE)
    out["elapsed_s"] = round(max(0.0, time.time() - float(out.get("started_at") or time.time())), 2) if out.get("running") else None
    return out


_PS_CACHE: dict[str, Any] = {"ts": 0.0, "root": "", "loaded": []}
_PS_CACHE_TTL = 4.0


def loaded_ollama_models(settings: dict[str, Any] | None = None) -> list[str]:
    """Cached /api/ps names. Never used as the reachability signal."""
    s = settings or {}
    root = ollama_root(s.get("ollama_base_url"))
    now = time.time()
    if (
        _PS_CACHE.get("root") == root
        and (now - float(_PS_CACHE.get("ts") or 0)) < _PS_CACHE_TTL
    ):
        return list(_PS_CACHE.get("loaded") or [])
    loaded: list[str] = []
    try:
        r = requests.get(f"{root}/api/ps", timeout=1.5)
        if r.ok:
            for m in (r.json() or {}).get("models") or []:
                name = str((m or {}).get("name") or (m or {}).get("model") or "").strip()
                if name:
                    loaded.append(name)
    except Exception:
        pass
    _PS_CACHE.update({"ts": now, "root": root, "loaded": loaded})
    return list(loaded)


def honesty_snapshot(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Live 6GB-class honesty: Ollama up, resident model, VRAM, warm, too-heavy.

    Does not change context size or reply length. Warns only.
    """
    s = settings or {}
    hw = probe_hardware()
    ok, _models, root, err = _ollama_ok(s)
    warm = warm_status()
    chat = resolve_chat_model(s)
    vram = int(hw.get("vram_mb") or 6144)
    used = hw.get("vram_used_mb")
    gpu = hw.get("gpu") or "GPU"
    loaded = loaded_ollama_models(s) if ok else []
    resident = loaded[0] if loaded else ""
    quantization = None
    parameter_size = None
    try:
        tags = requests.get(f"{root}/api/tags", timeout=1.5)
        if tags.ok:
            target = (resident or chat).lower().removesuffix(":latest")
            for item in (tags.json() or {}).get("models") or []:
                candidate = str(item.get("name") or item.get("model") or "").lower().removesuffix(":latest")
                if candidate == target:
                    details = item.get("details") or {}
                    quantization = details.get("quantization_level")
                    parameter_size = details.get("parameter_size")
                    break
    except Exception:
        pass
    batch = recommended_ollama_num_batch(s, hw)
    too_heavy = bool(ok and chat) and _model_too_heavy(chat, None, vram)
    tight = False
    try:
        if used is not None and vram:
            tight = (float(used) / float(vram)) >= 0.88
    except (TypeError, ValueError, ZeroDivisionError):
        tight = False
    warming = bool(warm.get("running"))
    level = "ok"
    line = f"{gpu} · {used if used is not None else '—'}/{vram} MB"
    if resident:
        line += f" · {resident} resident"
    elif chat:
        line += f" · {chat} idle"
    if not ok:
        level = "bad"
        line = err or f"Ollama is not running at {root}."
    elif warming:
        level = "warn"
        elapsed = warm.get("elapsed_s")
        wait = f"{elapsed:.0f}s" if isinstance(elapsed, (int, float)) else "…"
        line = (
            f"Loading {warm.get('model') or chat} · {warm.get('stage') or 'warming'} · {wait}. "
            "Wait — switching models on 6GB VRAM will stall."
        )
    elif too_heavy:
        level = "bad"
        line = (
            f"{chat} is too large for {gpu} ({vram} MB). "
            "Chat may hang. Stay on a 6GB kit. Context/reply limits are unchanged."
        )
    elif tight:
        level = "warn"
        line = (
            f"VRAM tight · {used}/{vram} MB on {gpu}. "
            "Full replies still on; do not load a second model."
        )
    return {
        "ok": ok,
        "level": level,
        "line": line,
        "gpu": gpu,
        "vram_mb": vram,
        "vram_used_mb": used,
        "chat_model": chat,
        "resident_model": resident or None,
        "warming": warming,
        "warm_stage": warm.get("stage"),
        "warm_model": warm.get("model"),
        "too_heavy": too_heavy,
        "tight": tight,
        "quantization": quantization,
        "parameter_size": parameter_size,
        "kv_cache_quantization": os.environ.get("OLLAMA_KV_CACHE_TYPE", "q8_0"),
        "flash_attention": str(os.environ.get("OLLAMA_FLASH_ATTENTION", "1")).lower() not in {"0", "false", "off"},
        "max_loaded_models": int(os.environ.get("OLLAMA_MAX_LOADED_MODELS", "1")) if str(os.environ.get("OLLAMA_MAX_LOADED_MODELS", "1")).isdigit() else 1,
        "num_parallel": int(os.environ.get("OLLAMA_NUM_PARALLEL", "1")) if str(os.environ.get("OLLAMA_NUM_PARALLEL", "1")).isdigit() else 1,
        "num_batch": batch,
        "tuning_mode": "manual" if s.get("ollama_num_batch") is not None else "plan-b-auto",
        "error": err or None,
    }


def _same_ollama_model(a: str, b: str) -> bool:
    def norm(x: str) -> str:
        s = str(x or "").strip().lower()
        if s.endswith(":latest"):
            s = s[:-7]
        return s
    return bool(norm(a)) and norm(a) == norm(b)


def pin_resident_keep_alive(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Refresh keep_alive on the loaded chat model. Never unloads anything."""
    s = settings or {}
    root = ollama_root(s.get("ollama_base_url"))
    model = resolve_chat_model(s)
    keep = s.get("ollama_keep_alive")
    if keep is None or keep == "" or str(keep) == "-1":
        keep = -1
    try:
        ps = requests.get(f"{root}/api/ps", timeout=3)
        names = {str(x.get("name") or x.get("model") or "") for x in ((ps.json() or {}).get("models") or [])} if ps.ok else set()
    except requests.RequestException as e:
        return {"ok": False, "error": str(e)}
    if not any(_same_ollama_model(model, n) for n in names):
        return {"ok": True, "pinned": False, "model": model}
    resident = next((n for n in names if _same_ollama_model(model, n)), model)
    return {"ok": True, "pinned": True, "model": resident}


def unload_resident_models(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Drop Ollama residents from VRAM. Only used by Kill localhost."""
    s = settings or {}
    root = ollama_root(s.get("ollama_base_url"))
    unloaded: list[str] = []
    try:
        ps = requests.get(f"{root}/api/ps", timeout=3)
        rows = (ps.json() or {}).get("models") or [] if ps.ok else []
    except requests.RequestException as e:
        return {"ok": False, "unloaded": [], "error": str(e)}
    for row in rows:
        name = str((row or {}).get("name") or (row or {}).get("model") or "").strip()
        if not name:
            continue
        try:
            requests.post(
                f"{root}/api/generate",
                json={"model": name, "prompt": "", "stream": False, "keep_alive": 0},
                timeout=12,
            )
            unloaded.append(name)
        except requests.RequestException:
            pass
    return {"ok": True, "unloaded": unloaded}


def kill_local_runtime(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Launch the project Kill Localhost script (same as launch.bat)."""
    root = Path(__file__).resolve().parents[1]
    script = root / "kill-localhost.ps1"
    bat = root / "launch.bat"
    if script.is_file():
        args = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File",
            str(script),
        ]
        launched = str(script)
    elif bat.is_file():
        args = ["cmd.exe", "/c", str(bat)]
        launched = str(bat)
    else:
        return {"ok": False, "error": "kill-localhost.ps1 / launch.bat not found"}
    try:
        subprocess.Popen(
            args,
            cwd=str(root),
            close_fds=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        return {"ok": False, "error": str(e), "launched": launched}
    return {"ok": True, "launched": launched}


def start_background_warm(settings: dict[str, Any] | None = None, purpose: str = "chat") -> dict[str, Any]:
    """Start warming in the background; never block the caller on model load."""
    s = dict(settings or {})
    model = resolve_chat_model(s) if purpose == "chat" else resolve_extract_model(s)
    with _WARM_LOCK:
        if _WARM_STATE.get("running"):
            return {"ok": None, "loading": True, "model": _WARM_STATE.get("model"),
                    "purpose": _WARM_STATE.get("purpose"), "stage": _WARM_STATE.get("stage")}
    def _runner() -> None:
        # warm_ollama_model will reuse the lock/state and perform the real work.
        warm_ollama_model(s, purpose)
    threading.Thread(target=_runner, name="bv-ollama-warm", daemon=True).start()
    return {"ok": None, "loading": True, "model": model, "purpose": purpose, "stage": "queued"}


# ── Hardware + recommend / install / update ─────────────────────────

_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,120}$")

# Curated for this machine class (GTX 1060 6GB / 16GB RAM). Sizes are
# typical Ollama Q4 weights, not theoretical FP16.
RECOMMENDED_BASES: list[dict[str, Any]] = [
    {
        "id": "huihui_ai/deepseek-r1-abliterated:7b",
        "label": "DeepSeek-R1 7B abliterated",
        "role": "thinking",
        "kit": "quality",
        "size_gb": 4.7,
        "min_vram_mb": 6000,
        "star": True,
        "note": "6GB pick — reasoning + uncensored. Fits at ctx 1024–2048.",
    },
    {
        "id": "huihui_ai/deepseek-r1-abliterated:1.5b",
        "label": "DeepSeek-R1 1.5B abliterated",
        "role": "fast",
        "kit": "fast",
        "size_gb": 1.1,
        "min_vram_mb": 2000,
        "star": False,
        "note": "Tiny reasoner. Fast on 4–6GB.",
    },
    {
        "id": "huihui_ai/gemma-4-abliterated:latest",
        "label": "Gemma 4 4B abliterated",
        "role": "fleet",
        "kit": "fleet",
        "size_gb": 3.2,
        "min_vram_mb": 4000,
        "star": False,
        "note": "Solid Matrix fleet base. Do not pull :26b.",
    },
    {
        "id": "llama3.2:3b",
        "label": "Llama 3.2 3B",
        "role": "daily",
        "kit": "daily",
        "size_gb": 2.0,
        "min_vram_mb": 3000,
        "star": False,
        "note": "Reliable daily chat + extract on 6GB.",
    },
    {
        "id": "qwen2.5:3b",
        "label": "Qwen 2.5 3B",
        "role": "daily",
        "kit": "daily",
        "size_gb": 1.9,
        "min_vram_mb": 3000,
        "star": False,
        "note": "Strong small Qwen daily driver.",
    },
    {
        "id": "nomic-embed-text",
        "label": "Nomic Embed Text",
        "role": "embed",
        "kit": "embed",
        "size_gb": 0.27,
        "min_vram_mb": 0,
        "star": False,
        "note": "Semantic memory. Always install this.",
    },
]

RECOMMENDED_KITS: dict[str, dict[str, Any]] = {
    "daily": {
        "id": "daily",
        "label": "Daily 6GB",
        "description": "Llama 3.2 3B + embeddings. Safe default for chat + extract.",
        "models": ["llama3.2:3b", "nomic-embed-text"],
        "preset": "balanced",
        "apply_chat": "llama3.2:3b",
        "apply_embed": "nomic-embed-text",
    },
    "fast": {
        "id": "fast",
        "label": "Fast / tiny",
        "description": "1.5B reasoner + embeddings. Snappy on this GPU.",
        "models": ["huihui_ai/deepseek-r1-abliterated:1.5b", "nomic-embed-text"],
        "preset": "fast",
        "apply_chat": "huihui_ai/deepseek-r1-abliterated:1.5b",
        "apply_embed": "nomic-embed-text",
    },
    "quality": {
        "id": "quality",
        "label": "Reasoning 6GB",
        "description": "DeepSeek-R1 7B abliterated — the 6GB quality pick.",
        "models": ["huihui_ai/deepseek-r1-abliterated:7b", "nomic-embed-text"],
        "preset": "thinking",
        "apply_chat": "huihui_ai/deepseek-r1-abliterated:7b",
        "apply_embed": "nomic-embed-text",
    },
    "fleet": {
        "id": "fleet",
        "label": "Agents fleet base",
        "description": "Gemma 4 4B abliterated — use this as the Matrix FROM base, not 26B.",
        "models": ["huihui_ai/gemma-4-abliterated:latest"],
        "preset": "",
        "apply_chat": "huihui_ai/gemma-4-abliterated:latest",
        "apply_embed": "",
    },
}

_HEAVY_NAME_HINTS = (
    ":26b",
    ":27b",
    ":12b",
    ":13b",
    ":14b",
    ":32b",
    ":70b",
    "qwen3.6",
    "gemma4:latest",
)


_PULL_LOCK = threading.Lock()
_PULL: dict[str, Any] = {
    "running": False,
    "models": [],
    "current": None,
    "status": "",
    "completed": [],
    "failed": [],
    "log": [],
    "ok": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
    "bytes_done": 0,
    "bytes_total": 0,
    "percent": 0,
    "stage": "idle",
    "apply": False,
    "cancel": False,
    "apply_pending": None,
}


def safe_model_name(name: str) -> str:
    raw = (name or "").strip()
    if not raw or not _MODEL_NAME_RE.match(raw):
        raise ValueError(f"Invalid model name: {name!r}")
    return raw


def probe_hardware() -> dict[str, Any]:
    """Best-effort NVIDIA probe. Falls back to known 6GB laptop if nvidia-smi missing."""
    info: dict[str, Any] = {
        "gpu": None,
        "vram_mb": None,
        "vram_used_mb": None,
        "vram_free_mb": None,
        "source": "fallback",
        "class": "6gb",
    }
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode == 0 and r.stdout.strip():
            line = r.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                info["gpu"] = parts[0]
                info["vram_mb"] = int(float(parts[1]))
                info["vram_used_mb"] = int(float(parts[2]))
                info["vram_free_mb"] = int(float(parts[3]))
                info["source"] = "nvidia-smi"
    except Exception:
        pass
    vram = info.get("vram_mb")
    if not vram:
        info["gpu"] = info["gpu"] or "NVIDIA GeForce GTX 1060"
        info["vram_mb"] = 6144
        info["source"] = "fallback"
        vram = 6144
    if vram <= 4500:
        info["class"] = "4gb"
    elif vram <= 7000:
        info["class"] = "6gb"
    elif vram <= 10000:
        info["class"] = "8gb"
    else:
        info["class"] = "12gb+"
    return info


def recommended_ollama_num_batch(
    settings: dict[str, Any] | None = None,
    hardware: dict[str, Any] | None = None,
) -> int:
    """Plan B: choose prompt batching from detected VRAM, with an expert override."""
    s = settings or {}
    raw = s.get("ollama_num_batch")
    if raw is not None:
        try:
            return max(32, min(2048, int(raw)))
        except (TypeError, ValueError):
            pass
    hw = hardware or probe_hardware()
    if hw.get("source") == "fallback":
        return 512
    vram = int(hw.get("vram_mb") or 0)
    if vram <= 0:
        return 256
    if vram <= 4500:
        return 512
    if vram <= 7000:
        return 1024
    return 1024


def _installed_name_set(settings: dict[str, Any] | None = None) -> set[str]:
    names: set[str] = set()
    for m in list_ollama_models(settings, force=True):
        mid = str(m.get("id") or "")
        if not mid:
            continue
        names.add(mid)
        names.add(mid.split(":")[0])
        if ":" not in mid:
            names.add(f"{mid}:latest")
        else:
            names.add(mid.rsplit(":", 1)[0])
    return names


def _is_installed(model_id: str, installed: set[str]) -> bool:
    if model_id in installed:
        return True
    if f"{model_id}:latest" in installed:
        return True
    if model_id.endswith(":latest") and model_id[: -len(":latest")] in installed:
        return True
    return False


def _model_too_heavy(model_id: str, size_bytes: Any, vram_mb: int) -> bool:
    mid = (model_id or "").lower()
    if any(h in mid for h in _HEAVY_NAME_HINTS):
        return True
    try:
        if size_bytes and int(size_bytes) > int(vram_mb) * 1024 * 1024 * 1.15:
            return True
    except (TypeError, ValueError):
        pass
    return False


def model_fit_report(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Whether the *currently selected* local chat model can load on this GPU."""
    s = settings or {}
    hw = probe_hardware()
    vram = int(hw.get("vram_mb") or 6144)
    chat = resolve_chat_model(s)
    extract = resolve_extract_model(s)
    embed = s.get("embed_model") or "nomic-embed-text"
    local_chat = provider_for(s, "chat") == "ollama"
    installed = list_ollama_models(s, force=True)
    by_id = {m["id"]: m for m in installed}
    chat_row = by_id.get(chat) or by_id.get(f"{chat}:latest")
    size = (chat_row or {}).get("size")
    too_heavy = bool(local_chat and chat) and _model_too_heavy(chat, size, vram)
    missing = local_chat and chat not in by_id and f"{chat}:latest" not in by_id
    inst_names = _installed_name_set(s)
    warning = ""
    if too_heavy:
        warning = (
            f"{chat} is too large for {hw.get('gpu') or 'this GPU'} "
            f"({vram} MB). Chat will hang or crawl. Install a 6GB kit below."
        )
    elif missing and chat:
        warning = f"{chat} is not installed in Ollama."
    return {
        "hardware": hw,
        "chat_model": chat,
        "extract_model": extract,
        "embed_model": embed,
        "chat_installed": (not missing) if local_chat else True,
        "chat_size": size,
        "too_heavy": too_heavy,
        "embed_installed": _is_installed(str(embed), inst_names),
        "warning": warning,
    }


def recommend_base_models(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rank curated base models for this GPU and show what is already in."""
    s = settings or {}
    hw = probe_hardware()
    vram = int(hw.get("vram_mb") or 6144)
    installed = _installed_name_set(s)
    live = list_ollama_models(s, force=True)
    models: list[dict[str, Any]] = []
    for rec in RECOMMENDED_BASES:
        row = dict(rec)
        size_gb = float(rec.get("size_gb") or 0)
        # ~1.2x weight vs VRAM is the offload cliff
        fits = (size_gb * 1024) <= (vram * 0.92) or rec.get("role") == "embed"
        tight = fits and (size_gb * 1024) > (vram * 0.70) and rec.get("role") != "embed"
        row["installed"] = _is_installed(rec["id"], installed)
        row["fits"] = bool(fits)
        row["tight"] = bool(tight)
        row["fit"] = "installed" if row["installed"] else ("tight" if tight else ("good" if fits else "no"))
        models.append(row)

    kits = []
    for kid, kit in RECOMMENDED_KITS.items():
        names = list(kit["models"])
        kits.append(
            {
                **kit,
                "installed": all(_is_installed(n, installed) for n in names),
                "missing": [n for n in names if not _is_installed(n, installed)],
            }
        )

    avoid = []
    for m in live:
        if _model_too_heavy(m.get("id") or "", m.get("size"), vram):
            avoid.append(
                {
                    "id": m.get("id"),
                    "label": m.get("label") or m.get("id"),
                    "size": m.get("size"),
                    "reason": "Larger than this GPU can hold in VRAM",
                }
            )

    fit = model_fit_report(s)
    return {
        "ok": True,
        "hardware": hw,
        "models": models,
        "kits": kits,
        "avoid": avoid,
        "fit": fit,
        "installed_count": len(live),
        "hint": (
            f"{hw.get('gpu') or 'GPU'} · {vram} MB VRAM. "
            "Install Daily or Reasoning — do not load Gemma 4 26B / 9GB+ tags."
        ),
    }


def pull_status() -> dict[str, Any]:
    with _PULL_LOCK:
        return {k: v for k, v in _PULL.items() if k not in ("resp",)}


def consume_apply_pending() -> dict[str, Any] | None:
    with _PULL_LOCK:
        pending = _PULL.get("apply_pending")
        _PULL["apply_pending"] = None
        return dict(pending) if isinstance(pending, dict) and pending else None


def _pull_log(msg: str, *, stage: str | None = None) -> None:
    line = f"{time.strftime('%H:%M:%S')}  {msg}"
    with _PULL_LOCK:
        log = list(_PULL.get("log") or [])
        log.append(line)
        _PULL["log"] = log[-80:]
        _PULL["status"] = msg
        if stage:
            _PULL["stage"] = stage


def _run_pull_job(names: list[str], settings: dict[str, Any], apply: bool) -> None:
    root = ollama_root((settings or {}).get("ollama_base_url"))
    completed: list[str] = []
    failed: list[dict[str, str]] = []
    try:
        for name in names:
            with _PULL_LOCK:
                _PULL["current"] = name
                _PULL["bytes_done"] = 0
                _PULL["bytes_total"] = 0
                _PULL["percent"] = 0
                _PULL["stage"] = "starting"
            _pull_log(f"Starting {name}…", stage="starting")
            layer_totals: dict[str, int] = {}
            layer_done: dict[str, int] = {}
            try:
                with _PULL_LOCK:
                    if _PULL.get("cancel"):
                        raise RuntimeError("cancelled")
                with requests.post(
                    f"{root}/api/pull",
                    json={"name": name, "stream": True},
                    stream=True,
                    timeout=3600,
                ) as r:
                    with _PULL_LOCK:
                        _PULL["resp"] = r
                    if r.status_code >= 400:
                        detail = (r.text or "")[:240]
                        failed.append({"id": name, "error": f"HTTP {r.status_code}: {detail}"})
                        _pull_log(f"Failed {name}: HTTP {r.status_code}", stage="failed")
                        continue
                    last_err = ""
                    cancelled = False
                    try:
                        for raw in r.iter_lines(decode_unicode=True):
                            with _PULL_LOCK:
                                if _PULL.get("cancel"):
                                    cancelled = True
                            if cancelled:
                                break
                            if not raw:
                                continue
                            try:
                                ev = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if ev.get("error"):
                                last_err = str(ev["error"])
                                break
                            st = str(ev.get("status") or "").strip()
                            digest = str(ev.get("digest") or "") or "_"
                            done = int(ev.get("completed") or 0)
                            total = int(ev.get("total") or 0)
                            if total:
                                layer_totals[digest] = total
                            if done:
                                layer_done[digest] = done
                            overall_total = sum(layer_totals.values())
                            overall_done = sum(layer_done.get(d, 0) for d in layer_totals)
                            percent = round((overall_done / overall_total) * 100, 1) if overall_total else 0
                            low = st.lower()
                            stage = "downloading" if "download" in low else ("verifying" if "verify" in low else ("pulling" if st else "working"))
                            label = f"{name}: {st or stage.title()}"
                            if overall_total:
                                label += f" · {percent:.1f}%"
                            with _PULL_LOCK:
                                if overall_done:
                                    _PULL["bytes_done"] = overall_done
                                if overall_total:
                                    _PULL["bytes_total"] = overall_total
                                _PULL["percent"] = percent
                                _PULL["stage"] = stage
                                if st:
                                    _PULL["status"] = label
                    except (AttributeError, OSError, requests.RequestException) as e:
                        with _PULL_LOCK:
                            if _PULL.get("cancel") or "read" in str(e).lower():
                                cancelled = True
                            else:
                                last_err = str(e)
                    if cancelled:
                        failed.append({"id": name, "error": "cancelled"})
                        _pull_log(f"Cancelled {name}", stage="cancelled")
                        try:
                            requests.post(f"{root}/api/delete", json={"model": name, "name": name}, timeout=8)
                            _pull_log(f"Removed partial {name}", stage="cancelled")
                        except Exception:
                            pass
                        break
                    if last_err:
                        failed.append({"id": name, "error": last_err})
                        _pull_log(f"Failed {name}: {last_err}", stage="failed")
                    else:
                        completed.append(name)
                        _pull_log(f"Ready {name}", stage="ready")
            except requests.RequestException as e:
                with _PULL_LOCK:
                    was_cancel = bool(_PULL.get("cancel"))
                if was_cancel:
                    failed.append({"id": name, "error": "cancelled"})
                    _pull_log(f"Cancelled {name}", stage="cancelled")
                    try:
                        requests.post(f"{root}/api/delete", json={"model": name, "name": name}, timeout=8)
                    except Exception:
                        pass
                    break
                failed.append({"id": name, "error": str(e)})
                _pull_log(f"Failed {name}: {e}", stage="failed")
            except RuntimeError as e:
                if "cancel" in str(e).lower():
                    failed.append({"id": name, "error": "cancelled"})
                    _pull_log(f"Cancelled {name}", stage="cancelled")
                    break
                raise
            finally:
                with _PULL_LOCK:
                    _PULL["resp"] = None
        cancelled_all = any((f.get("error") == "cancelled") for f in failed)
        ok = not failed
        with _PULL_LOCK:
            _PULL["ok"] = ok
            _PULL["completed"] = completed
            _PULL["failed"] = failed
            _PULL["error"] = None if ok else (failed[0].get("error") if failed else "pull failed")
            _PULL["finished_at"] = time.time()
            _PULL["current"] = None
            _PULL["running"] = False
            _PULL["status"] = "cancelled" if cancelled_all else ("done" if ok else "finished with errors")
            _PULL["stage"] = "cancelled" if cancelled_all else ("ready" if ok else "failed")
            if cancelled_all:
                _PULL["percent"] = 0
                _PULL["bytes_done"] = 0
                _PULL["bytes_total"] = 0
                _PULL["error"] = None
            elif ok:
                _PULL["percent"] = 100
        if apply and completed:
            pending = _pending_apply_patch(completed)
            with _PULL_LOCK:
                _PULL["apply_pending"] = pending
    except Exception as e:
        with _PULL_LOCK:
            was_cancel = bool(_PULL.get("cancel")) or "read" in str(e).lower()
            _PULL["ok"] = False if not was_cancel else False
            _PULL["error"] = None if was_cancel else str(e)
            _PULL["running"] = False
            _PULL["finished_at"] = time.time()
            _PULL["percent"] = 0 if was_cancel else _PULL.get("percent") or 0
            _PULL["bytes_done"] = 0 if was_cancel else _PULL.get("bytes_done") or 0
            _PULL["status"] = "cancelled" if was_cancel else f"error: {e}"
            _PULL["stage"] = "cancelled" if was_cancel else "failed"


def _pending_apply_patch(completed: list[str]) -> dict[str, Any]:
    """Settings keys to apply after a successful pull (server consumes this)."""
    chat_pick = None
    embed_pick = None
    for name in completed:
        low = name.lower()
        if "embed" in low or name.startswith("nomic-embed"):
            embed_pick = name
        elif not chat_pick:
            chat_pick = name
    patch: dict[str, Any] = {}
    if chat_pick:
        patch["ollama_chat_model"] = chat_pick
        patch["ollama_extract_model"] = chat_pick
        patch["llm_provider"] = "ollama"
    if embed_pick:
        patch["embed_model"] = embed_pick
    return patch


def _installed_ollama_models(settings: dict[str, Any] | None = None) -> tuple[list[str], str]:
    """Return the exact model tags currently installed on the active local Ollama runtime."""
    root = ollama_root((settings or {}).get("ollama_base_url"))
    try:
        r = requests.get(f"{root}/api/tags", timeout=3)
        r.raise_for_status()
        data = r.json() or {}
        models = []
        for m in data.get("models") or []:
            name = str(m.get("name") or m.get("model") or "").strip()
            if name:
                models.append(name)
        return models, root
    except Exception:
        return [], root


def start_ollama_pull(
    names: list[str],
    settings: dict[str, Any] | None = None,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    cleaned: list[str] = []
    for n in names:
        cleaned.append(safe_model_name(n))
    if not cleaned:
        raise ValueError("No models to pull")

    # If the exact requested tag is already installed on the active project-local
    # Ollama runtime, do not re-download it. Mark it READY immediately and, when
    # requested, make it the active Studio model.
    installed, _root = _installed_ollama_models(settings)
    installed_map = {m.lower(): m for m in installed}
    already = [installed_map[n.lower()] for n in cleaned if n.lower() in installed_map]
    missing = [n for n in cleaned if n.lower() not in installed_map]
    if already and not missing:
        with _PULL_LOCK:
            _PULL.update({
                "running": False,
                "models": cleaned,
                "current": None,
                "status": f"Ready {already[0]}",
                "completed": already,
                "failed": [],
                "log": [f"{time.strftime('%H:%M:%S')}  Already installed · {already[0]}"],
                "ok": True,
                "error": None,
                "started_at": time.time(),
                "finished_at": time.time(),
                "bytes_done": 0,
                "bytes_total": 0,
                "percent": 100,
                "stage": "ready",
                "apply": bool(apply),
                "apply_pending": _pending_apply_patch(already) if apply else None,
            })
        return {"ok": True, "ready": True, "already_installed": already, **pull_status()}

    with _PULL_LOCK:
        if _PULL.get("running"):
            if _PULL.get("cancel"):
                return {"ok": False, "error": "Cancelling the previous pull…", **pull_status()}
            return {"ok": False, "error": "A pull is already running", **pull_status()}
        _PULL.update(
            {
                "running": True,
                "models": cleaned,
                "current": cleaned[0],
                "status": "starting",
                "completed": [],
                "failed": [],
                "log": [],
                "ok": None,
                "error": None,
                "started_at": time.time(),
                "finished_at": None,
                "bytes_done": 0,
                "bytes_total": 0,
                "percent": 0,
                "stage": "queued",
                "apply": bool(apply),
                "apply_pending": None,
                "cancel": False,
                "resp": None,
            }
        )
    t = threading.Thread(
        target=_run_pull_job,
        args=(cleaned, settings or {}, apply),
        name="ollama-pull",
        daemon=True,
    )
    t.start()
    return {"ok": True, **pull_status()}


def cancel_ollama_pull() -> dict[str, Any]:
    """Stop an in-flight Ollama pull and try to drop the partial model."""
    with _PULL_LOCK:
        if not _PULL.get("running"):
            return {"ok": False, "error": "No model pull is running."}
        _PULL["cancel"] = True
        name = _PULL.get("current")
        resp = _PULL.get("resp")
        _PULL["status"] = "cancelling — dropping partial download"
        _PULL["stage"] = "cancelling"
        _PULL["percent"] = 0
        _PULL["bytes_done"] = 0
        _PULL["bytes_total"] = 0
        _PULL["error"] = None
    if resp is not None:
        try:
            resp.close()
        except Exception:
            pass
    return {"ok": True, "cancelling": True, "model": name}


def start_kit_pull(
    kit_id: str,
    settings: dict[str, Any] | None = None,
    *,
    apply: bool = True,
) -> dict[str, Any]:
    kit = RECOMMENDED_KITS.get((kit_id or "").strip().lower())
    if not kit:
        raise ValueError(f"Unknown kit: {kit_id}")
    rec = recommend_base_models(settings)
    missing = []
    for k in rec.get("kits") or []:
        if k.get("id") == kit["id"]:
            missing = list(k.get("missing") or [])
            break
    names = missing or list(kit["models"])
    if not missing:
        # Everything present — still allow a refresh pull of the kit
        names = list(kit["models"])
    return start_ollama_pull(names, settings, apply=apply)


def start_ollama_update(
    names: list[str] | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-pull installed models (or a given list) so tags refresh."""
    if names:
        targets = [safe_model_name(n) for n in names]
    else:
        live = list_ollama_models(settings, force=True)
        targets = [m["id"] for m in live if m.get("id") and not str(m["id"]).endswith("-pro")]
        if not targets:
            raise ValueError("No installed models to update")
    return start_ollama_pull(targets, settings, apply=False)
