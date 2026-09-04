"""Cypra Matrix Studio local chat/runtime."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import traceback
import uuid
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from engine.auth import auth_status, resolve_api_key
from engine.conversation import suggest_followups, temperature_for_style
from engine.local_chat import (
    apply_extract_to_vault,
    apply_extract_to_vault_iter,
    build_chat_messages,
    chat_completion,
    chat_stream,
    extract_from_exchange,
    extract_knowledge,
)
from engine.analytics import analyze_vault
from engine.embeddings import EmbeddingStore
from engine.extract_fallback import heuristic_extract
from engine.inbox_watch import InboxWatcher
from engine.hygiene import prune_junk_notes, vault_health
from engine.plugins import PluginManager
from engine.llm import (
    get_provider,
    list_local_presets,
    list_ollama_models,
    local_model_inventory,
    model_fit_report,
    provider_for,
    provider_status,
    consume_apply_pending,
    pull_status,
    recommend_base_models,
    resolve_chat_model,
    resolve_extract_model,
    resolve_local_preset,
    start_kit_pull,
    start_ollama_pull,
    cancel_ollama_pull,
    start_ollama_update,
    warm_ollama_model,
    start_background_warm,
    pin_resident_keep_alive,
    unload_resident_models,
    kill_local_runtime,
    warm_status,
    honesty_snapshot,
    local_ollama_store,
    normalize_ollama_context,
    ollama_model_thinking_support,
    ollama_root,
    resolve_ollama_context,
)
from engine.memory import MemoryIndex
from engine.rag import MAX_FILE_BYTES, RAGStore
from engine.ops import OpsLog
from engine.operational_state import (
    analyze_workforce,
    record_chat_feedback,
    record_chat_interaction,
    set_evolution_proposal_status,
    snapshot as operational_snapshot,
)
_RECOVERED_OPERATIONS = operational_snapshot(mark_interrupted=True)
from engine.quality import (
    collect_allowed_titles,
    quality_summary,
    sanitize_assistant_reply,
    sanitize_extract,
)
from engine.matrix import (
    core_models,
    get_agent,
    public_agent,
    resolve_chat_agent,
    resolve_matrix_root,
    sanitize_matrix_root_setting,
    search_agents,
    status as matrix_status,
)
from engine.vault import (
    DEFAULT_SETTINGS,
    RETIRED_SETTING_KEYS,
    SETTINGS_SECTIONS,
    Vault,
    load_settings,
    migrate_settings,
    reset_settings_section,
    save_settings,
)
from engine.vault_manager import VaultManager
from engine.voice import (
    TTS_PROVIDERS,
    VOICES,
    create_realtime_client_secret,
    resolve_tts_provider,
    speech_to_text,
    text_to_speech,
)
from tts import LocalTTSService
from tts.service import TTSCancelled

ROOT = Path(__file__).resolve().parent
# Keep direct `python server.py` launches consistent with START.bat/START.ps1.
os.environ.setdefault("OLLAMA_FLASH_ATTENTION", "1")
APP_ID = "cypra-local-bv-chat"
INSTANCE_ID = os.environ.get("CYPRA_INSTANCE_ID", "")
DATA = ROOT / "data"
SETTINGS_PATH = DATA / "settings.json"
SESSIONS_DIR = DATA / "sessions"
BG_DIR = DATA / "backgrounds"
LOCAL_TTS = LocalTTSService(ROOT)
BG_FILE = BG_DIR / "chat"
BG_META = BG_DIR / "chat.json"
OPS_PATH = DATA / "ops.json"
PLUGINS_DIR = DATA / "plugins"
BUNDLED_PLUGINS = ROOT / "plugins"
RAG_ROOT = ROOT / "MatrixFiles" / "RAG"

for p in (DATA, SESSIONS_DIR, BG_DIR, PLUGINS_DIR):
    p.mkdir(parents=True, exist_ok=True)

APP_VERSION = "1.0.0-point2"
BUILD_ID = "1.1.15-files-consent-hardening-20260904"
app = FastAPI(title="Cypra Matrix Studio", version=APP_VERSION)
# Local-only request hardening. Cypra Studio is intended to be a desktop/local app.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

@app.middleware("http")
async def _local_request_guard(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":", 1)[0].strip().lower()
    origin = (request.headers.get("origin") or "").strip().lower()
    if host and host not in _LOOPBACK_HOSTS:
        return Response("Cypra Studio is local-only.", status_code=403, media_type="text/plain")
    if origin and origin != "null":
        try:
            origin_host = urlparse(origin).hostname
        except Exception:
            origin_host = None
        if origin_host and origin_host.lower() not in _LOOPBACK_HOSTS:
            return Response("Cross-origin requests are not allowed.", status_code=403, media_type="text/plain")
    return await call_next(request)


app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

_settings = load_settings(SETTINGS_PATH)
# Context is Studio-wide. Sanitize legacy/custom values at startup while
# preserving AUTO (0) and every supported explicit tier.
_normalized_ctx = normalize_ollama_context(_settings.get("ollama_num_ctx", 8192))
if _settings.get("ollama_num_ctx") != _normalized_ctx:
    _settings["ollama_num_ctx"] = _normalized_ctx
    save_settings(SETTINGS_PATH, _settings)
# Response-length cap sliders are removed from the UI; generation is always
# unlimited (num_predict=-1) now, so no stale per-response token cap should
# silently reappear from an old settings.json.
_settings["ollama_chat_tokens"] = -1
# Startup launches a private Ollama endpoint and exports OLLAMA_HOST / OLLAMA_MODELS.
# Always bind Studio to that runtime so a stale saved 11434 setting cannot pull into
# the host Ollama store.
_env_ollama_host = os.environ.get("OLLAMA_HOST")
if _env_ollama_host:
    _settings["ollama_base_url"] = ollama_root(_env_ollama_host)
_settings["ollama_models_dir"] = local_ollama_store()
_sessions: dict[str, dict[str, Any]] = {}
rag_store = RAGStore(RAG_ROOT)
vault_mgr = VaultManager(DATA)
ops_log = OpsLog(OPS_PATH)
inbox_watch = InboxWatcher(DATA / "inbox_seen.json")
plugin_mgr = PluginManager(PLUGINS_DIR)

# Active vault + memory (rebound on vault switch)
vault: Vault
memory: MemoryIndex
embed_store: EmbeddingStore


def _bind_vault(path: Path | None = None) -> None:
    global vault, memory, embed_store
    root = Path(path) if path else vault_mgr.active_path()
    root.mkdir(parents=True, exist_ok=True)
    mem_root = DATA / "memory" / vault_mgr.active_id()
    mem_root.mkdir(parents=True, exist_ok=True)
    vault = Vault(root)
    memory = MemoryIndex(mem_root)
    embed_store = EmbeddingStore(mem_root)
    # Legacy long-term memory is intentionally dormant in this clean baseline; do not rebuild it on boot.
    # Drop index/embedding ghosts that no longer resolve on disk
    prune_shared_memory(force=True)


# Throttle automatic GC so chat paths stay snappy
_LAST_MEMORY_PRUNE: float = 0.0
_MEMORY_PRUNE_TTL = 90.0  # seconds between opportunistic full prunes


def prune_shared_memory(
    *,
    force: bool = False,
    scrub_links: bool = True,
) -> dict[str, Any]:
    """
    Automatically clear shared-memory data that is not retrievable:
    missing wiki notes, unreadable files, orphan usage, orphan embeddings,
    and (optionally) dead [[wikilinks]] that create unopenable stale references.
    """
    global _LAST_MEMORY_PRUNE
    import time

    now = time.time()
    if not force and (now - _LAST_MEMORY_PRUNE) < _MEMORY_PRUNE_TTL:
        return {"ok": True, "skipped": True, "reason": "throttled"}

    link_stats: dict[str, int] = {}
    if scrub_links:
        try:
            link_stats = vault.scrub_dead_wikilinks()
            if link_stats.get("links_removed"):
                memory.rebuild_from_vault(vault)
        except Exception:
            link_stats = {"error": 1}

    mem_stats = memory.prune_missing(vault, save=True, also_unreadable=True)
    live = memory.live_note_ids(vault)
    emb_stats = embed_store.prune_missing(live, drop_empty=True)

    # Drop pinned / grown refs in live sessions that no longer resolve
    sessions_cleaned = 0
    try:
        for sid, sess in list(_sessions.items()):
            changed = False
            pinned = [p for p in (sess.get("pinned") or []) if vault.read_note(p)]
            if pinned != list(sess.get("pinned") or []):
                sess["pinned"] = pinned
                changed = True
            grown = [
                g
                for g in (sess.get("grown_notes") or [])
                if vault.read_note(g if isinstance(g, str) else (g or {}).get("id") or "")
            ]
            # grown may be list of ids
            raw_grown = sess.get("grown_notes") or []
            if isinstance(raw_grown, list) and raw_grown and isinstance(raw_grown[0], str):
                new_grown = [g for g in raw_grown if vault.read_note(g)]
                if new_grown != raw_grown:
                    sess["grown_notes"] = new_grown
                    changed = True
            if changed:
                persist_session(sid, sess)
                sessions_cleaned += 1
    except Exception:
        pass

    _LAST_MEMORY_PRUNE = now
    result = {
        "ok": True,
        "skipped": False,
        "memory": mem_stats,
        "embeddings": emb_stats,
        "links": link_stats,
        "sessions_cleaned": sessions_cleaned,
        "memory_stats": memory.stats(),
        "embedding_stats": embed_store.stats(),
    }
    try:
        # Only log when something was actually cleaned (avoid noise every boot)
        if (
            mem_stats.get("docs_removed")
            or mem_stats.get("usage_removed")
            or emb_stats.get("embeddings_removed")
            or link_stats.get("links_removed")
        ):
            ops_log.record(
                "memory_prune",
                note_ids=[],
                meta={
                    "docs_removed": mem_stats.get("docs_removed", 0),
                    "usage_removed": mem_stats.get("usage_removed", 0),
                    "embeddings_removed": emb_stats.get("embeddings_removed", 0),
                    "links_removed": link_stats.get("links_removed", 0),
                },
            )
    except Exception:
        pass
    return result


def forget_missing_note(note_id: str) -> dict[str, Any]:
    """Drop one unretrievable id from index + embeddings (file already gone)."""
    nid = (note_id or "").strip()
    if not nid:
        return {"ok": False, "error": "empty id"}
    if vault.read_note(nid):
        return {"ok": True, "exists": True, "id": nid}
    memory.remove_doc(nid, save=True)
    embed_store.drop(nid)
    return {"ok": True, "exists": False, "forgotten": nid}


_bind_vault()


def bootstrap_bundled_plugins() -> dict[str, Any]:
    """Install missing bundled plugins from ROOT/plugins into data/plugins."""
    installed: list[str] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []
    if not BUNDLED_PLUGINS.is_dir():
        return {"installed": installed, "skipped": skipped, "errors": errors}
    for path in sorted(BUNDLED_PLUGINS.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        if not (path / "plugin.json").is_file():
            continue
        try:
            plugin_mgr.install_from_folder(path, force=False)
            installed.append(path.name)
        except FileExistsError:
            skipped.append(path.name)
        except Exception as e:
            errors.append({"id": path.name, "error": str(e)})
    return {"installed": installed, "skipped": skipped, "errors": errors}


# Auto-install bundled plugin pack, then load enabled plugins.
try:
    _plugin_bootstrap = bootstrap_bundled_plugins()
except Exception:
    _plugin_bootstrap = {"installed": [], "skipped": [], "errors": [{"error": "bootstrap failed"}]}

# Load enabled Python plugins (hooks). Failures are non-fatal.
try:
    _plugin_boot = plugin_mgr.load_enabled(
        {
            "settings": _settings,
            "data": DATA,
            "root": ROOT,
            "vault_mgr": vault_mgr,
        }
    )
    plugin_mgr.emit("startup")
except Exception:
    _plugin_boot = {"loaded": [], "errors": [{"error": "plugin boot failed"}]}


def require_key() -> str:
    """local provider key only (voice STT/TTS/realtime)."""
    key, _ = resolve_api_key(_settings, validate=False)
    if not key:
        raise HTTPException(
            status_code=401,
            detail="No local provider API key. Set LOCAL_API_KEY, save one in Settings, or run `local login`.",
        )
    return key


def ensure_llm_ready() -> None:
    """Chat/extract: needs local provider key or reachable Ollama (or hybrid parts)."""
    # Auto hybrid: prefer local chat if Ollama up, else local provider; persist so UI stays honest
    if _settings.get("hybrid_auto"):
        st_local = provider_status({**_settings, "llm_provider": "ollama"})
        prev = get_provider(_settings)
        if st_local.get("ok"):
            _settings["llm_provider"] = "hybrid"
            _settings.setdefault("hybrid_chat", "ollama")
            _settings.setdefault("hybrid_extract", "legacy_cloud")
        else:
            # fall back to local provider if key exists, else stay ollama (will error with hint)
            key, _ = resolve_api_key(_settings, validate=False)
            _settings["llm_provider"] = "legacy_cloud" if key else "ollama"
        if get_provider(_settings) != prev:
            try:
                save_settings(SETTINGS_PATH, _settings)
            except OSError:
                pass
    st = provider_status(_settings)
    if st.get("ok"):
        return
    mode = get_provider(_settings)
    if mode in ("ollama", "hybrid"):
        raise HTTPException(
            status_code=503,
            detail=st.get("hint")
            or "LLM backend not ready. Check Ollama and/or local provider key.",
        )
    raise HTTPException(
        status_code=401,
        detail=st.get("hint")
        or "No local provider API key. Add a key in Settings or switch LLM provider to Local (Ollama).",
    )


def _snapshot_notes(note_ids: list[str]) -> list[dict[str, Any]]:
    snaps = []
    for nid in note_ids:
        n = vault.read_note(nid)
        if n:
            snaps.append(
                {
                    "id": n["id"],
                    "title": n.get("title"),
                    "content": n.get("content") or n.get("body"),
                    "type": n.get("type"),
                    "tags": n.get("tags") or [],
                    "links": n.get("links") or [],
                    "existed": True,
                }
            )
        else:
            snaps.append({"id": nid, "existed": False})
    return snaps


def get_session(sid: str | None) -> tuple[str, dict[str, Any]]:
    if sid and sid in _sessions:
        return sid, _sessions[sid]
    if sid:
        path = SESSIONS_DIR / f"{sid}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                _sessions[sid] = data
                return sid, data
            except (OSError, json.JSONDecodeError):
                pass
    new_id = sid or uuid.uuid4().hex[:12]
    data = {
        "id": new_id,
        "messages": [],
        "title": "New chat",
        "pinned": [],  # legacy memory note ids pinned into this chat
    }
    _sessions[new_id] = data
    return new_id, data


def persist_session(sid: str, data: dict[str, Any]) -> None:
    # This rewrites the FULL transcript on every single turn, and the file only
    # grows as the chat continues — so on a USB/removable drive this is both the
    # most frequent and most write-heavy disk operation in the app. Two changes
    # cut wear and corruption risk without touching what's stored:
    #  - compact JSON (no indent) instead of pretty-printed: same data, fewer
    #    bytes written every turn as history grows.
    #  - atomic write (temp file + replace) so a turn interrupted by unplugging
    #    the drive can't leave a half-written, corrupt session file.
    path = SESSIONS_DIR / f"{sid}.json"
    tmp = SESSIONS_DIR / f"{sid}.tmp"
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp.replace(path)


def _chat_message_id(sid: str, index: int, message: dict[str, Any]) -> str:
    """Stable identity for legacy replies; new replies persist their UUID directly."""
    existing = str(message.get("message_id") or "").strip()
    if existing:
        return existing
    seed = f"cypra-chat:{sid}:{index}:{message.get('matrix_agent') or ''}:{message.get('content') or ''}"
    return uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:20]


def _public_session(sid: str, session: dict[str, Any]) -> dict[str, Any]:
    public = dict(session)
    messages: list[dict[str, Any]] = []
    for index, raw in enumerate(session.get("messages") or []):
        message = dict(raw) if isinstance(raw, dict) else {"role": "system", "content": str(raw)}
        if message.get("role") == "assistant":
            message["message_id"] = _chat_message_id(sid, index, message)
            message["feedback"] = int(message.get("feedback") or 0)
        messages.append(message)
    public["messages"] = messages
    return public


def existing_titles() -> list[str]:
    return [n["title"] for n in vault.list_notes()]


def reindex_notes(notes: list[dict[str, Any]], *, save: bool = True) -> None:
    """Update the in-memory search index; optionally persist once per batch."""
    touched = False
    for n in notes:
        if n:
            memory.upsert_note(n)
            touched = True
    if touched and save:
        memory.save()


def memory_snapshot_stats() -> dict[str, int]:
    """Cheap local memory counts without constructing visualization payloads."""
    notes = vault.list_notes()
    relationships = 0
    for note in notes:
        try:
            relationships += int(note.get("link_count") or len(note.get("links") or []))
        except Exception:
            pass
    return {"notes": len(notes), "relationships": relationships}


def strongest_memory_titles(limit: int = 30) -> list[str]:
    """Return the strongest stored note titles without visualizer metadata."""
    ranked = []
    for meta in vault.list_notes():
        did = str(meta.get("id") or "")
        usage = memory.usage.get(did) or {}
        score = float(usage.get("strength") or 0.0) + 0.05 * int(usage.get("hits") or 0)
        ranked.append((score, str(meta.get("title") or did)))
    ranked.sort(key=lambda item: (-item[0], item[1].lower()))
    return [title for _score, title in ranked[: max(0, int(limit))] if title]


def _written_public(written: list) -> list[dict[str, Any]]:
    """Compact public shape for legacy memory-note maintenance endpoints."""
    out: list[dict[str, Any]] = []
    for w in written or []:
        if not w:
            continue
        out.append(
            {
                "id": w.get("id"),
                "title": w.get("title"),
                "type": w.get("type") or "concept",
                "tags": w.get("tags") or [],
                "links": w.get("links") or [],
                "description": w.get("description") or w.get("summary") or w.get("preview") or "",
                "preview": (w.get("preview") or w.get("description") or "")[:200],
                "body": (w.get("body") or "")[:400],
            }
        )
    return out


# ── models ──────────────────────────────────────────────────────────


class WorkplaceBody(BaseModel):
    slug: str = "cypra"
    path: str = ""
    content: str = ""


class ChatRequest(BaseModel):
    message: str = ""
    session_id: str | None = None
    use_memory: bool = False
    use_rag: bool | None = None
    think: bool | None = None  # legacy compatibility: True=standard, False=off
    think_mode: str | None = None  # per-turn override: off | standard | deep; null uses Settings
    plain: bool | None = None
    auto_extract: bool | None = None
    stream: bool = True
    pinned: list[str] = Field(default_factory=list)
    turn_file_name: str = ""
    turn_file_text: str = ""
    turn_file_path: str = ""
    review_context: str = ""
    review_context_name: str = ""
    talk: bool = False
    files: bool = False


class IngestRequest(BaseModel):
    text: str
    title: str | None = None
    save_source: bool = True


class NoteWrite(BaseModel):
    title: str
    content: str
    type: str = "concept"
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    merge: bool = True


class SettingsUpdate(BaseModel):
    """Accept known and forward-compatible settings; persistence is filtered to defaults."""

    model_config = ConfigDict(extra="allow")


    # Models / AI
    llm_provider: str | None = None  # legacy_cloud | ollama | hybrid
    hybrid_chat: str | None = None
    hybrid_extract: str | None = None
    hybrid_auto: bool | None = None
    chat_model: str | None = None
    extract_model: str | None = None
    ollama_base_url: str | None = None
    ollama_chat_model: str | None = None
    ollama_extract_model: str | None = None
    ollama_api_key: str | None = None
    ollama_local_preset: str | None = None  # fast | balanced | quality | vision | code | thinking
    ollama_num_ctx: int | None = None
    ollama_keep_alive: str | None = None
    ollama_num_batch: int | None = None
    ollama_chat_tokens: int | None = None
    show_generation_stats: bool | None = None
    ollama_extract_tokens: int | None = None
    ollama_history_turns: int | None = None
    ollama_memory_chars: int | None = None
    ollama_max_notes: int | None = None
    extract_growth: str | None = None  # sparse | balanced | dense
    extract_fallback: bool | None = None
    use_embeddings: bool | None = None
    embed_model: str | None = None
    # RAG v2 — explicit external knowledge store (separate from Memory v1).
    rag_enabled: bool | None = None
    rag_top_k: int | None = None
    rag_context_chars: int | None = None
    rag_chunk_chars: int | None = None
    rag_chunk_overlap: int | None = None
    rag_min_score: float | None = None
    theme_preset: str | None = None
    ui_mode: str | None = None  # classic | modern
    ui_colors: dict[str, Any] | None = None
    reduce_motion: bool | None = None
    onboarding_done: bool | None = None
    voice_id: str | None = None
    voice_model: str | None = None
    auto_extract: bool | None = None
    speak_replies: bool | None = None
    voice_output_enabled: bool | None = None
    conversation_flow: bool | None = None
    conversation_style: str | None = None
    error_reduction: bool | None = None
    matrix_enabled: bool | None = None
    matrix_agent: str | None = None
    matrix_root: str | None = None
    matrix_handoff: bool | None = None
    matrix_history_mode: str | None = None
    matrix_history_turns: int | None = None
    tts_provider: str | None = None
    tts_local_voice: str | None = None
    tts_allow_online: bool | None = None
    tts_edge_voice: str | None = None
    tts_online_fallback: str | None = None
    tts_rate: float | None = None
    tts_pitch: float | None = None
    tts_speak_director: bool | None = None
    tts_speak_system: bool | None = None
    tts_skip_code: bool | None = None
    tts_skip_urls: bool | None = None
    tts_max_chars: int | None = None
    tts_stop_previous: bool | None = None
    tts_cpu_threads: int | None = None
    legacy_cloud_key: str | None = None
    port: int | None = None
    window_width: int | None = None
    window_height: int | None = None
    ui_density: str | None = None
    settings_schema: int | None = None
    # QoL
    chat_temperature: float | None = None
    memory_context_limit: int | None = None
    auto_open_new_notes: bool | None = None
    sticky_pins: list[str] | None = None  # always-in-context note titles/ids
    confirm_destructive: bool | None = None
    ui_font_scale: float | None = None
    chat_font_scale: float | None = None
    chat_bg_strength: float | None = None
    show_model_thinking: bool | None = None
    think_mode: str | None = None
    think_budget_tokens: int | None = None
    plain_chat: bool | None = None


class SettingsResetRequest(BaseModel):
    section: str = "all"  # ai | visuals | ui | all


class TTSRequest(BaseModel):
    text: str
    voice_id: str | None = None
    provider: str | None = None  # local | browser | legacy_cloud
    replace: bool | None = None
    preview: bool = False


class TTSStopRequest(BaseModel):
    release: bool = False


class QueryRequest(BaseModel):
    question: str


class RAGTextRequest(BaseModel):
    name: str = "knowledge.txt"
    text: str


class RAGContentRequest(BaseModel):
    name: str = "knowledge.txt"
    path: str = ""
    text: str | None = None
    content_b64: str | None = None


class RAGChatKnowledgeRequest(BaseModel):
    text: str
    role: str = "user"
    label: str = ""


class RAGSearchRequest(BaseModel):
    query: str
    limit: int = 4
    min_score: float | None = None


class RAGSourceUpdateRequest(BaseModel):
    name: str | None = None
    label: str | None = None
    group: str | None = None
    tags: list[str] | str | None = None
    enabled: bool | None = None
    pinned: bool | None = None


class RAGBundleImportRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    format: str
    version: int
    sources: list[dict[str, Any]] = Field(default_factory=list)


class PinRequest(BaseModel):
    session_id: str | None = None
    node_id: str
    pinned: bool = True


class TouchRequest(BaseModel):
    node_ids: list[str]


# ── pages / state ───────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
@app.get("/cypra/director", response_class=HTMLResponse)
def index() -> HTMLResponse:
    # Keep MatrixFiles/boot.txt authoritative, but embed it in the initial
    # document so the boot screen can never hang on a second HTTP fetch.
    index_html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    boot_path = ROOT / "MatrixFiles" / "boot.txt"
    if not boot_path.is_file():
        boot_path = ROOT / "static" / "boot.txt"
    boot_text = ""
    try:
        if boot_path.is_file():
            boot_text = boot_path.read_text(encoding="utf-8")
    except Exception:
        boot_text = ""
    marker = '<pre id="boot-art" class="boot-art" aria-label="Cypra boot art"></pre>'
    if marker in index_html and boot_text:
        index_html = index_html.replace(
            marker,
            '<pre id="boot-art" class="boot-art" aria-label="Cypra boot art">'
            + html_escape(boot_text)
            + '</pre>',
            1,
        )
    return HTMLResponse(
        index_html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "app_id": APP_ID,
        "instance_id": INSTANCE_ID,
        "app": "Cypra Studio",
        "version": APP_VERSION,
        "build_id": BUILD_ID,
        "data_root": str(DATA),
        "memory": memory.stats(),
        "rag": {**rag_store.stats(), "enabled": bool(_settings.get("rag_enabled", True))},
    }


@app.get("/api/auth")
def api_auth() -> dict[str, Any]:
    return auth_status(_settings, fast=False)


def _public_settings() -> dict[str, Any]:
    s = {k: _settings.get(k, DEFAULT_SETTINGS.get(k)) for k in DEFAULT_SETTINGS if k not in RETIRED_SETTING_KEYS}
    s["has_saved_key"] = bool(_settings.get("legacy_cloud_key"))
    s["voices"] = VOICES
    s["tts_providers"] = TTS_PROVIDERS
    key, _ = resolve_api_key(_settings, validate=False)
    s["tts_provider_active"] = resolve_tts_provider(_settings, has_xai_key=bool(key))
    s["llm_provider"] = get_provider(_settings)
    s["chat_model_active"] = resolve_chat_model(_settings)
    s["extract_model_active"] = resolve_extract_model(_settings)
    s["ollama_runtime_endpoint"] = ollama_root(_settings.get("ollama_base_url"))
    s["ollama_models_dir"] = local_ollama_store()
    s["ollama_runtime_scope"] = "PROJECT-LOCAL" if os.environ.get("OLLAMA_MODELS") else "HOST"
    try:
        s["matrix"] = matrix_status(_settings, root=ROOT)
    except Exception:
        s["matrix"] = {"ok": False, "enabled": False, "count": 0}
    if s.get("legacy_cloud_key"):
        s["legacy_cloud_key"] = "••••" + str(s["legacy_cloud_key"])[-4:]
    else:
        s["legacy_cloud_key"] = ""
    return s


def _polish_reply(
    reply: str,
    *,
    memory_context: str = "",
    pinned_titles: list[str] | None = None,
) -> dict[str, Any]:
    """Error-reduction pass over assistant text."""
    try:
        vault_titles = [n.get("title") or n.get("id") for n in vault.list_notes()]
    except Exception:
        vault_titles = []
    allowed = collect_allowed_titles(
        vault_titles=vault_titles,
        memory_context=memory_context,
        pinned_titles=pinned_titles,
    )
    return sanitize_assistant_reply(
        reply,
        allowed_titles=allowed,
        memory_context=memory_context,
        settings=_settings,
    )


def _memory_recall_public(
    used_ids: list[str] | None = None,
    recall_meta: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """Map retrieved note ids to {id, title, why?} for the chat UI recall strip."""
    if recall_meta:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for m in recall_meta:
            if not isinstance(m, dict):
                continue
            nid = m.get("id") or m.get("title")
            if not nid or nid in seen:
                continue
            seen.add(str(nid))
            title = m.get("title") or nid
            item: dict[str, Any] = {"id": str(nid), "title": str(title)}
            if m.get("why"):
                item["why"] = str(m["why"])
            if m.get("score") is not None:
                item["score"] = m["score"]
            out.append(item)
        return out
    out = []
    seen = set()
    for nid in used_ids or []:
        if not nid or nid in seen:
            continue
        seen.add(nid)
        n = vault.read_note(nid)
        title = (n.get("title") if n else None) or nid
        out.append({"id": nid, "title": str(title)})
    return out


def _followups_for(
    user_text: str,
    reply: str,
    used_ids: list[str] | None = None,
) -> list[str]:
    titles: list[str] = []
    for nid in used_ids or []:
        n = vault.read_note(nid)
        if n and n.get("title"):
            titles.append(n["title"])
    return suggest_followups(user_text, reply, memory_titles=titles, limit=3)


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    llm = provider_status(_settings)
    try:
        llm["honesty"] = honesty_snapshot(_settings)
    except Exception:
        pass
    local = local_model_inventory(_settings)
    configured = resolve_chat_model(_settings) or ""
    installed = {str(m.get("id") or m.get("name") or m.get("model") or "") for m in local.get("models", []) if isinstance(m, dict)}
    # also accept bare tags from /api/tags when inventory lags
    try:
        ok_tags, tag_models, _, _ = _ollama_ok(_settings) if False else (False, [], "", "")
    except Exception:
        tag_models = []
    try:
        from engine.llm import _ollama_ok
        ok_tags, tag_models, _, _ = _ollama_ok(_settings)
        for t in tag_models or []:
            if t:
                installed.add(str(t))
    except Exception:
        pass
    active_local = ""
    if configured:
        if configured in installed:
            active_local = configured
        elif configured + ":latest" in installed:
            active_local = configured + ":latest"
        else:
            base = configured.rsplit(":", 1)[0] if ":" in configured else configured
            active_local = next((mid for mid in installed if mid.rsplit(":", 1)[0] == base), "")
        # Never report NO MODEL when a chat model is explicitly configured.
        # Warm/swap can leave inventory briefly empty; UI still needs the name.
        if not active_local and configured:
            active_local = configured
    return {
        "auth": auth_status(_settings, fast=True),
        "llm": llm,
        "local": {
            "online": bool(llm.get("ok")) if llm.get("provider") == "ollama" else bool(local.get("installed_count", 0)),
            "active_model": active_local,
            "installed_count": int(local.get("installed_count") or 0),
        },
        "settings": _public_settings(),
        "memory": memory.stats(),
        "embeddings": embed_store.stats(),
        "vault_path": str(vault_mgr.active_path()),
        "vault_id": vault_mgr.active_id(),
        "vaults": [],
        "data_path": str(DATA),
        "timeline": ops_log.timeline(limit=15),
        "growth": [],
        "defaults": {key: value for key, value in DEFAULT_SETTINGS.items() if key not in RETIRED_SETTING_KEYS},
        "version": APP_VERSION,
        "memory_prune_ttl_s": 0,
        "matrix": matrix_status(_settings, root=ROOT),
        "model_fit": model_fit_report(_settings),
    }


@app.get("/api/llm/status")
def api_llm_status() -> dict[str, Any]:
    status = provider_status(_settings)
    status["honesty"] = honesty_snapshot(_settings)
    status["warm"] = warm_status()
    return status


@app.get("/api/llm/library")
def api_llm_library() -> dict[str, Any]:
    """Compact local model-library view for Studio. Uses the project-local Ollama inventory."""
    inv = local_model_inventory(_settings)
    models = list(inv.get("models") or [])
    active = {
        "chat": resolve_chat_model(_settings),
        "extract": resolve_extract_model(_settings),
        "embed": _settings.get("embed_model") or "nomic-embed-text",
    }
    store = local_ollama_store()
    return {
        "ok": True,
        "store": store,
        "store_exists": Path(store).exists(),
        "count": len(models),
        "runtime_count": int(inv.get("runtime_count") or 0),
        "local_cache_count": int(inv.get("local_cache_count") or 0),
        "models": models,
        "active": active,
        "pull": pull_status(),
    }


@app.get("/api/llm/models")
def api_llm_models() -> dict[str, Any]:
    """List models for the active provider (or both) + local catalog/presets."""
    inv = local_model_inventory(_settings)
    ollama = inv["models"]
    return {
        "provider": get_provider(_settings),
        "ollama": ollama,
        "ollama_chat": inv["chat"],
        "ollama_embed": inv["embed"],
        "ollama_vision": inv["vision"],
        "ollama_code": inv["code"],
        "ollama_thinking": inv["thinking"],
        "ollama_presets": inv["presets"],
        "ollama_groups": inv["groups"],
        "ollama_missing": inv["missing_catalog"],
        "ollama_installed_count": inv["installed_count"],
        "catalog_size": inv["catalog_size"],
        "legacy_cloud": [
            {"id": "local", "name": "local"},
            {"id": "local", "name": "local"},
            {"id": "local-reasoning", "name": "local-4.20 reasoning"},
            {"id": "local-fast", "name": "local-4.20 non-reasoning"},
        ],
        "status": provider_status(_settings),
        "active": {
            "chat": resolve_chat_model(_settings),
            "extract": resolve_extract_model(_settings),
            "embed": _settings.get("embed_model") or "nomic-embed-text",
            "preset": _settings.get("ollama_local_preset") or "",
        },
    }


class MatrixSelectBody(BaseModel):
    agent: str = ""
    enabled: bool | None = None
    lock: bool = False


class MatrixCreateAgentBody(BaseModel):
    name: str = ""
    persona: str = ""
    directive: str = ""
    preset: str = "balanced"
    base_model: str | None = None
    temperature: float | None = None


@app.get("/api/matrix/status")
def api_matrix_status() -> dict[str, Any]:
    return matrix_status(_settings, root=ROOT)


@app.get("/api/matrix/agents")
def api_matrix_agents(q: str = "", limit: int = 1000) -> dict[str, Any]:
    roster_ok = bool(resolve_matrix_root(ROOT, _settings))
    if not roster_ok:
        return {
            "ok": False,
            "root": None,
            "count": 0,
            "agents": [],
            "core": [],
            "query": q,
        }
    agents = search_agents(q, settings=_settings, limit=max(1, min(1000, int(limit or 1000))))
    evidence = operational_snapshot().get("agents", {})
    score_fields = (
        "score", "overall_evidence_count", "reliability", "success_rate", "assignments",
        "chat_responses", "chat_positive", "chat_negative", "chat_feedback_count",
        "chat_score", "chat_success_rate", "chat_confidence",
    )
    for agent in agents:
        metrics = evidence.get(str(agent.get("slug") or ""), {})
        agent.update({key: metrics.get(key) for key in score_fields if key in metrics})
    total = matrix_status(_settings, root=ROOT).get("count") or 0
    categories: dict[str, int] = {}
    directive_ready = 0
    for agent in agents:
        category = str(agent.get("category") or "Specialized & Other")
        categories[category] = categories.get(category, 0) + 1
        if agent.get("has_directive"):
            directive_ready += 1
    return {
        "ok": True,
        "root": str(resolve_matrix_root(ROOT, _settings) or ""),
        "count": total,
        "shown": len(agents),
        "query": q,
        "core": core_models(_settings),
        "categories": dict(sorted(categories.items())),
        "directive_ready": directive_ready,
        "agents": agents,
    }


@app.get("/api/matrix/agents/{name}")
def api_matrix_agent(name: str) -> dict[str, Any]:
    agent = get_agent(name, _settings)
    if not agent:
        raise HTTPException(404, f"Matrix agent not found: {name}")
    public = public_agent(agent, include_directive=True)
    metrics = operational_snapshot().get("agents", {}).get(str(public.get("slug") or ""), {})
    public["operational"] = metrics
    return {"ok": True, "agent": public}


@app.post("/api/matrix/select")
def api_matrix_select(body: MatrixSelectBody) -> dict[str, Any]:
    global _settings
    patch: dict[str, Any] = {}
    if body.enabled is not None:
        patch["matrix_enabled"] = bool(body.enabled)
    slug = (body.agent or "").strip().lower()
    if slug:
        agent = get_agent(slug, _settings)
        if not agent:
            raise HTTPException(404, f"Matrix agent not found: {slug}")
        patch["matrix_agent"] = agent["slug"]
        patch["matrix_agent_resolved"] = agent["slug"]
        patch["matrix_enabled"] = True if body.enabled is None else bool(body.enabled)
        if body.lock:
            # Pin this agent — do not let auto-route steal it (e.g. academy).
            # Also clear any stale route state by making the saved selection the
            # authoritative agent for subsequent turns.
            patch["matrix_agent_locked"] = True
            patch["matrix_agent_resolved"] = agent["slug"]
    if patch:
        _settings.update(patch)
        save_settings(SETTINGS_PATH, _settings)
    return {
        "ok": True,
        "settings": {
            "matrix_enabled": bool(_settings.get("matrix_enabled", True)),
            "matrix_agent": _settings.get("matrix_agent") or "",
            "matrix_handoff": bool(_settings.get("matrix_handoff", False)),
            "matrix_agent_locked": bool(_settings.get("matrix_agent_locked", False)),
        },
        "matrix": matrix_status(_settings, root=ROOT),
    }


@app.post("/api/matrix/agents/create")
def api_matrix_create_agent(body: MatrixCreateAgentBody) -> dict[str, Any]:
    raw_name = (body.name or "").strip()
    name = re.sub(r"[^a-z0-9_-]+", "-", raw_name.lower()).strip("-_")
    if not raw_name:
        raise HTTPException(400, "Agent name required")
    if not name:
        raise HTTPException(400, "Agent name must contain letters or numbers")
    if len(name) > 64:
        raise HTTPException(400, "Agent name is too long (64 characters maximum)")
    if name in {"custom", "matrixfiles", "ollama", "system", "con", "prn", "aux", "nul"} or name.startswith("modelfile"):
        raise HTTPException(400, "Choose a different agent name")
    root = resolve_matrix_root(ROOT, _settings)
    if not root:
        raise HTTPException(500, "Project-local MatrixFiles directory not found")
    custom_dir = root / "CustomAgents"
    custom_dir.mkdir(parents=True, exist_ok=True)
    path = custom_dir / f"Modelfile_{name}"
    if path.exists():
        raise HTTPException(409, f"Custom agent already exists: {name}")
    base = (body.base_model or _settings.get("ollama_chat_model") or _settings.get("chat_model") or "huihui_ai/gemma-4-abliterated:latest").strip()
    if not base or len(base) > 200 or any(ch in base for ch in "\r\n\"`"):
        raise HTTPException(400, "Invalid base model name")
    presets = {
        "precise": ("PERSONA: You are a precise professional Matrix agent specialized in exact, evidence-based responses.\n\nOperating Rules:\n1. Tone: precise, direct, and concise.\n2. Approach: prioritize correctness and eliminate ambiguity.\n3. Structure: use clear execution blocks, numbered steps, and parameter maps when useful.\n4. Output: raw, clean, and directly actionable.", 0.25),
        "balanced": ("PERSONA: You are a balanced professional Matrix agent specialized in clear, useful problem solving.\n\nOperating Rules:\n1. Tone: clear, calm, and rigorous.\n2. Approach: balance accuracy, practicality, and completeness.\n3. Structure: organize answers with concise sections and explicit assumptions.\n4. Output: useful, readable, and directly actionable.", 0.65),
        "creative": ("PERSONA: You are a creative Matrix agent specialized in ideation, exploration, and novel solutions.\n\nOperating Rules:\n1. Tone: imaginative, energetic, and coherent.\n2. Approach: generate multiple strong possibilities before narrowing.\n3. Structure: use concepts, alternatives, and concrete examples.\n4. Output: original ideas that remain practical and internally consistent.", 0.9),
        "technical": ("PERSONA: You are a technical Matrix agent specialized in implementation, systems, and engineering.\n\nOperating Rules:\n1. Tone: technical, exact, and implementation-focused.\n2. Approach: prioritize correctness, reproducibility, and explicit assumptions.\n3. Structure: use code blocks, procedures, diagnostics, and parameter maps where useful.\n4. Output: concrete implementation guidance with edge cases called out.", 0.35),
        "research": ("PERSONA: You are a research Matrix agent specialized in evidence analysis and structured investigation.\n\nOperating Rules:\n1. Tone: analytical, careful, and evidence-aware.\n2. Approach: distinguish known facts, inference, and uncertainty.\n3. Structure: compare evidence, methods, alternatives, and limitations.\n4. Output: a defensible, well-structured synthesis.", 0.45),
    }
    preset = (body.preset or "balanced").strip().lower()
    preset_text, preset_temp = presets.get(preset, presets["balanced"])
    temp = preset_temp if body.temperature is None else max(0.0, min(1.5, float(body.temperature)))
    custom_directive = (body.directive or body.persona or "").strip()
    if not custom_directive:
        custom_directive = preset_text
    if len(custom_directive) > 24000:
        raise HTTPException(400, "Directive is too large (24,000 characters maximum)")
    if body.persona.strip() and body.directive.strip() and body.persona.strip() not in custom_directive:
        custom_directive = body.persona.strip() + "\n\n" + custom_directive
    if not custom_directive:
        raise HTTPException(400, "Persona / SYSTEM directive cannot be empty")
    # Keep the generated Modelfile deterministic and safe. Triple quotes inside the
    # editable directive are collapsed so the SYSTEM block cannot terminate early.
    safe_directive = custom_directive.replace('\r\n', '\n').replace('\r', '\n').replace('\"\"\"', '\"\"')
    content = f'FROM {base}\nSYSTEM """{safe_directive}"""\nPARAMETER temperature {temp:g}\n'
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8", newline="\n")
        tmp.replace(path)
    except OSError as e:
        raise HTTPException(500, f"Could not create agent: {e}") from e
    from engine import matrix as _mx
    _mx.get_roster(_settings, root=ROOT, force=True)
    agent = get_agent(name, _settings)
    if not agent:
        raise HTTPException(500, "Agent file was created but could not be indexed")
    return {"ok": True, "agent": public_agent(agent, include_directive=True), "category": "CUSTOM", "path": str(path), "locked": False}


class EvolutionDecisionBody(BaseModel):
    action: str = Field(pattern=r"^(approve|reject)$")


@app.get("/api/organization/evolution")
def api_organization_evolution() -> dict[str, Any]:
    data = operational_snapshot()
    agents = data.get("agents") or {}; tasks = data.get("tasks") or {}; relationships = data.get("relationships") or {}
    scored_agents = sum(1 for row in agents.values() if isinstance(row, dict) and int(row.get("evidence_count") or row.get("assignments") or 0) > 0)
    return {"ok": True, "analysis": data.get("workforce_analysis"), "progress": {
        "tasks": len(tasks), "agents": len(agents), "scored_agents": scored_agents,
        "relationships": len(relationships), "proposals": len(data.get("evolution_proposals") or {}),
    }, "proposals": list((data.get("evolution_proposals") or {}).values())}


@app.post("/api/organization/evolution/analyze")
def api_analyze_organization() -> dict[str, Any]:
    return {"ok": True, **analyze_workforce()}


@app.post("/api/organization/evolution/{proposal_id}/decision")
def api_decide_organization(proposal_id: str, body: EvolutionDecisionBody) -> dict[str, Any]:
    data = operational_snapshot(); proposal = (data.get("evolution_proposals") or {}).get(proposal_id)
    if not proposal:
        raise HTTPException(404, "Evolution proposal not found")
    if body.action == "reject":
        return {"ok": True, "proposal": set_evolution_proposal_status(proposal_id, "rejected")}
    if proposal.get("status") == "approved":
        return {"ok": True, "proposal": proposal, "agent": proposal.get("created_agent")}
    domain = str(proposal.get("domain") or "specialized capability").strip()
    evidence = proposal.get("evidence") or {}
    name = re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")[:48] + "-specialist"
    directive = (
        f"PERSONA: You are a focused {domain} specialist in Cypra Matrix Studio.\n\n"
        "Operating Rules:\n"
        "1. Distinguish observed evidence, inference, and uncertainty.\n"
        "2. Work only within the task scope and user-approved permissions.\n"
        "3. Prefer reproducible checks and explicit failure criteria.\n"
        "4. Never claim a task succeeded without deterministic evidence.\n\n"
        f"ORIGIN: User-approved Organizational Evolution proposal {proposal_id}. "
        f"Observed historical evidence at proposal time: {int(evidence.get('successes') or 0)} successful outcomes across {int(evidence.get('attempts') or 0)} assignments."
    )
    existing_agent = get_agent(name, _settings)
    created = ({"agent": public_agent(existing_agent, include_directive=True)} if existing_agent else
               api_matrix_create_agent(MatrixCreateAgentBody(name=name, directive=directive, preset="technical")))
    agent_slug = str((created.get("agent") or {}).get("slug") or name)
    decided = set_evolution_proposal_status(proposal_id, "approved", created_agent=agent_slug)
    return {"ok": True, "proposal": decided, "agent": created.get("agent")}


@app.get("/api/matrix/runtime")
def api_matrix_runtime() -> dict[str, Any]:
    """Live local Matrix/Ollama runtime diagnostics for the Studio inspector.

    /api/tags is the authoritative reachability check. /api/ps is optional
    loaded-model information and must never make a healthy Ollama runtime look
    offline just because the model is idle or not currently resident in VRAM.
    """
    endpoint = ollama_root(_settings.get("ollama_base_url"))
    store = local_ollama_store()
    model = resolve_chat_model(_settings) or ""
    installed: list[dict[str, Any]] = []
    loaded: list[dict[str, Any]] = []
    ollama_ok = False
    err = ""
    try:
        import requests
        r = requests.get(endpoint.rstrip("/") + "/api/tags", timeout=2.5)
        ollama_ok = r.status_code == 200
        if ollama_ok:
            data = r.json() if r.content else {}
            installed = data.get("models") or []
        else:
            err = f"HTTP {r.status_code}"
    except Exception as e:
        err = str(e)
    try:
        import requests
        r = requests.get(endpoint.rstrip("/") + "/api/ps", timeout=2.0)
        if r.status_code == 200:
            data = r.json() if r.content else {}
            loaded = data.get("models") or []
    except Exception:
        pass
    loaded_model = ""
    if loaded:
        loaded_model = str(loaded[0].get("name") or loaded[0].get("model") or "")
    configured_installed = ""
    if model:
        ids = [str(m.get("name") or m.get("model") or "") for m in installed if isinstance(m, dict)]
        if model in ids:
            configured_installed = model
        elif f"{model}:latest" in ids:
            configured_installed = f"{model}:latest"
        else:
            base = model.rsplit(":", 1)[0] if ":" in model else model
            configured_installed = next((mid for mid in ids if mid.rsplit(":", 1)[0] == base), "")
    gpu = {"name": "—", "util": None, "vram_used": None, "vram_total": None, "temp": None}
    try:
        cp = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2.5,
        )
        if cp.returncode == 0 and cp.stdout.strip():
            parts = [x.strip() for x in cp.stdout.strip().splitlines()[0].split(",")]
            if len(parts) >= 5:
                gpu = {"name": parts[0], "util": parts[1], "vram_used": parts[2], "vram_total": parts[3], "temp": parts[4]}
    except Exception:
        pass
    matrix = matrix_status(_settings, root=ROOT)
    agent = (matrix.get("agent") or {}) if isinstance(matrix, dict) else {}
    # The selected Matrix agent's Modelfile is authoritative for identity.
    # Do not report NO MODEL simply because Ollama currently has no resident
    # model in /api/ps or because the generic chat-model setting is empty.
    # The FROM line in the selected local Modelfile tells us which base model
    # that agent is actually built against.
    agent_name = str(agent.get("slug") or _settings.get("matrix_agent") or "").strip()
    agent_base = str(agent.get("from") or "").strip()
    resolved_active_model = loaded_model or configured_installed or model or agent_base
    return {
        "ok": ollama_ok,
        "endpoint": endpoint,
        "model_store": store,
        "scope": "PROJECT-LOCAL",
        "configured_model": model,
        "installed_models": [str(m.get("name") or m.get("model") or "") for m in installed if isinstance(m, dict)],
        "installed_count": len(installed),
        "loaded_model": loaded_model or "NO MODEL LOADED",
        "active_model": resolved_active_model or "NO MODEL",
        "agent_base_model": agent_base or resolved_active_model or "NO MODEL",
        "loaded_count": len(loaded),
        "ollama_error": err,
        "gpu": gpu,
        "active_agent": agent_name or "NO AGENT",
        "agent_label": agent.get("label") or agent_name or "NO AGENT",
        "agent_modelfile": agent.get("relpath") or "—",
        "agent_directive": bool(agent.get("has_directive")),
        "matrix_count": int(matrix.get("count") or 0) if isinstance(matrix, dict) else 0,
        "history_scope": "CURRENT CHAT ONLY",
        "history_turns": int(_settings.get("matrix_history_turns") or 24),
        "handoff": bool(_settings.get("matrix_handoff", False)),
    }

@app.get("/api/session/export")
def api_session_export(session_id: str | None = None) -> Response:
    sid, sess = get_session(session_id)
    payload = {
        "export_version": 1,
        "session_id": sid,
        "scope": "CURRENT CHAT ONLY",
        "base_model": resolve_chat_model(_settings) or "",
        "active_agent": _settings.get("matrix_agent") or "",
        "matrix_locked": bool(_settings.get("matrix_agent_locked")),
        "matrix_handoff": bool(_settings.get("matrix_handoff", False)),
        "history_turns": int(_settings.get("matrix_history_turns") or 24),
        "messages": list(sess.get("messages") or []),
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(content=raw, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="matrix_session_{sid[:12]}.json"'
    })

@app.get("/api/matrix/self-test")
def api_matrix_self_test() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    def add(name: str, ok: bool, detail: str):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    try:
        endpoint = ollama_root(_settings.get("ollama_base_url"))
        import requests
        r = requests.get(endpoint.rstrip("/") + "/api/tags", timeout=2.5)
        reachable = r.status_code == 200
        add("Ollama reachable", reachable, endpoint if reachable else f"HTTP {r.status_code}")
    except Exception as e:
        add("Ollama reachable", False, str(e))
    store = str(local_ollama_store() or "")
    add("Project-local model store", bool(os.environ.get("OLLAMA_MODELS")) and Path(store).resolve() == (ROOT / "OllamaModels").resolve(), store or "missing")
    model = resolve_chat_model(_settings) or ""
    try:
        inv = local_model_inventory(_settings)
        installed = {str(m.get("id") or m.get("name") or m.get("model") or "") for m in inv.get("models", []) if isinstance(m, dict)}
        bases = {x.rsplit(":", 1)[0] for x in installed}
        add("Base model available", model in installed or model.rsplit(":", 1)[0] in bases, model or "NO MODEL")
    except Exception as e:
        add("Base model available", False, str(e))
    try:
        ms = matrix_status(_settings, root=ROOT)
        count = int(ms.get("count") or 0)
        add("700-agent roster discovered", count >= 700, f"{count} agents")
    except Exception as e:
        add("700-agent roster discovered", False, str(e))
    try:
        agents = search_agents("", settings=_settings, limit=1000)
        directives = sum(1 for a in agents if a.get("has_directive"))
        add("Modelfile directives readable", directives == len(agents) and directives > 0, f"{directives}/{len(agents)}")
    except Exception as e:
        add("Modelfile directives readable", False, str(e))
    locked = bool(_settings.get("matrix_agent_locked")) and bool(_settings.get("matrix_agent"))
    add("Agent lock", locked, _settings.get("matrix_agent") or "NO LOCKED AGENT")
    add("Current-chat history", _settings.get("matrix_history_mode", "current_chat") == "current_chat", f"{_settings.get('matrix_history_turns',24)} turns max")
    add("Current agent Modelfile", bool(locked), _settings.get("matrix_agent") or "NO LOCKED AGENT")
    add("Persona isolation", locked or bool(_settings.get("matrix_enabled", True)), "Saved Modelfile remains authoritative")
    try:
        rt = api_matrix_runtime()
        add("Runtime identity populated", bool(rt.get("endpoint")) and bool(rt.get("model_store")), f"{rt.get('endpoint')} · {rt.get('model_store')}")
    except Exception as e:
        add("Runtime identity populated", False, str(e))
    all_ok = all(c["ok"] for c in checks)
    return {"ok": all_ok, "checks": checks, "summary": f"{sum(1 for c in checks if c['ok'])}/{len(checks)} checks passed", "active_model": model, "active_agent": _settings.get("matrix_agent") or ""}


class LocalPresetBody(BaseModel):
    preset: str = "balanced"
    apply: bool = True  # write into settings when true


@app.get("/api/llm/presets")
def api_llm_presets() -> dict[str, Any]:
    return {"presets": list_local_presets(_settings)}


@app.post("/api/llm/preset")
def api_llm_apply_preset(
    body: LocalPresetBody | None = None,
    preset: str | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """
    Resolve (and optionally apply) a local usage preset.
    Picks the best installed models for fast / balanced / quality / vision / code / thinking.
    Accepts JSON body {preset, apply} or query ?preset=fast&apply=true.
    """
    pid = (body.preset if body else None) or preset or "balanced"
    do_apply = body.apply if body is not None else apply
    resolved = resolve_local_preset(str(pid), _settings)
    if not resolved.get("ok"):
        raise HTTPException(status_code=400, detail=resolved.get("error") or "Bad preset")
    if do_apply:
        # One model in VRAM: chat === extract for local kits
        chat_m = resolved["ollama_chat_model"]
        patch = {
            "ollama_chat_model": chat_m,
            "ollama_extract_model": chat_m,
            "embed_model": resolved["embed_model"],
            "ollama_local_preset": resolved["preset"],
        }
        if resolved.get("ollama_chat_tokens") is not None:
            patch["ollama_chat_tokens"] = int(resolved["ollama_chat_tokens"])
        if resolved.get("ollama_extract_tokens") is not None:
            patch["ollama_extract_tokens"] = int(resolved["ollama_extract_tokens"])
        # Stay on local/hybrid — never force-switch off local provider if user is on API-only
        if get_provider(_settings) == "legacy_cloud":
            patch["llm_provider"] = "ollama"
        _settings.update(patch)
        save_settings(SETTINGS_PATH, _settings)
        try:
            start_background_warm(_settings, "chat")
        except Exception:
            pass
    return {
        "ok": True,
        "resolved": resolved,
        "settings": _public_settings(),
        "models": local_model_inventory(_settings),
    }


class LlmPullBody(BaseModel):
    models: list[str] = Field(default_factory=list)
    kit: str = ""
    apply: bool = True


class LlmUpdateBody(BaseModel):
    models: list[str] = Field(default_factory=list)


def _consume_apply_pending(st: dict[str, Any]) -> dict[str, Any]:
    pending = consume_apply_pending()
    if not pending:
        return st
    patch = {k: v for k, v in pending.items() if v}
    if not patch:
        return st
    if patch.get("llm_provider") == "ollama" and get_provider(_settings) == "hybrid":
        patch.pop("llm_provider", None)
    _settings.update(patch)
    save_settings(SETTINGS_PATH, _settings)
    st["applied"] = patch
    st["settings"] = _public_settings()
    return st


@app.get("/api/llm/recommend")
def api_llm_recommend() -> dict[str, Any]:
    rec = recommend_base_models(_settings)
    rec["pull"] = pull_status()
    return rec


@app.post("/api/llm/pull")
def api_llm_pull(body: LlmPullBody) -> dict[str, Any]:
    try:
        if (body.kit or "").strip():
            result = start_kit_pull(body.kit.strip(), _settings, apply=body.apply)
        else:
            result = start_ollama_pull(list(body.models or []), _settings, apply=body.apply)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return result


@app.get("/api/llm/pull/status")
def api_llm_pull_status() -> dict[str, Any]:
    return _consume_apply_pending(pull_status())


@app.post("/api/llm/pull/cancel")
def api_llm_pull_cancel() -> dict[str, Any]:
    return cancel_ollama_pull()


_BG_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def _bg_meta() -> dict[str, Any]:
    if not BG_FILE.is_file():
        return {"ok": False, "set": False}
    meta: dict[str, Any] = {}
    if BG_META.is_file():
        try:
            meta = json.loads(BG_META.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    return {
        "ok": True,
        "set": True,
        "mime": meta.get("mime") or "image/jpeg",
        "name": meta.get("name") or "chat",
        "mtime": int(BG_FILE.stat().st_mtime),
    }


@app.get("/api/chat-background")
def api_chat_background():
    if not BG_FILE.is_file():
        raise HTTPException(404, "No chat background is set.")
    mime = str(_bg_meta().get("mime") or "image/jpeg")
    return FileResponse(BG_FILE, media_type=mime, headers={"Cache-Control": "no-cache"})


@app.get("/api/chat-background/meta")
def api_chat_background_meta() -> dict[str, Any]:
    return _bg_meta()


@app.post("/api/chat-background")
def api_chat_background_set(file: UploadFile = File(...)) -> dict[str, Any]:
    name = Path(file.filename or "chat.jpg").name
    ext = Path(name).suffix.lower()
    if ext not in _BG_TYPES:
        raise HTTPException(400, "Use a PNG, JPG, WEBP, GIF, or BMP image.")
    raw = file.file.read()
    if not raw:
        raise HTTPException(422, "Image uploaded as 0 bytes.")
    if len(raw) > 12 * 1024 * 1024:
        raise HTTPException(413, "Image is too large (12 MB maximum).")
    BG_DIR.mkdir(parents=True, exist_ok=True)
    BG_FILE.write_bytes(raw)
    BG_META.write_text(json.dumps({"mime": _BG_TYPES[ext], "name": name}, indent=2), encoding="utf-8")
    return _bg_meta()


@app.delete("/api/chat-background")
def api_chat_background_clear() -> dict[str, Any]:
    try:
        if BG_FILE.is_file():
            BG_FILE.unlink()
        if BG_META.is_file():
            BG_META.unlink()
    except OSError as e:
        raise HTTPException(500, str(e)) from e
    return {"ok": True, "set": False}


@app.post("/api/llm/update")
def api_llm_update(body: LlmUpdateBody | None = None) -> dict[str, Any]:
    names = list((body.models if body else None) or [])
    try:
        return start_ollama_update(names or None, _settings)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class LlmRemoveBody(BaseModel):
    model: str


@app.post("/api/llm/remove")
def api_llm_remove(body: LlmRemoveBody) -> dict[str, Any]:
    model = str(body.model or "").strip()
    if not model or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,180}", model):
        raise HTTPException(400, "Invalid model name")
    active = {str(_settings.get("ollama_chat_model") or ""), str(_settings.get("ollama_extract_model") or "")}
    # Also treat base name matches of active models as blocked
    active_bases = {a.rsplit(":", 1)[0] for a in active if a}
    model_base = model.rsplit(":", 1)[0]
    if model in active or (model_base in active_bases and any(a.startswith(model_base) for a in active)):
        raise HTTPException(409, "Model is currently assigned to an active role. Select another model before removing it.")
    try:
        import requests
        endpoint = ollama_root(_settings.get("ollama_base_url")).rstrip("/")
        # Resolve exact tag from live tags list when possible
        candidates = [model]
        if ":" not in model:
            candidates.append(f"{model}:latest")
        else:
            base = model.rsplit(":", 1)[0]
            if base and base not in candidates:
                candidates.append(base)
        try:
            tags = requests.get(endpoint + "/api/tags", timeout=5)
            if tags.status_code == 200:
                models = (tags.json() or {}).get("models") or []
                ids = [str(m.get("name") or m.get("model") or "") for m in models if isinstance(m, dict)]
                # Prefer exact installed name
                for cand in list(candidates):
                    for iid in ids:
                        if iid == cand or iid.rsplit(":", 1)[0] == cand.rsplit(":", 1)[0]:
                            if iid not in candidates:
                                candidates.insert(0, iid)
                if not any(c in ids or c.rsplit(":", 1)[0] in {i.rsplit(":", 1)[0] for i in ids} for c in candidates):
                    return {"ok": True, "removed": model, "already_absent": True}
        except Exception:
            pass
        last_err = None
        for name in candidates:
            r = requests.delete(endpoint + "/api/delete", json={"name": name}, timeout=30)
            if r.status_code < 400:
                return {"ok": True, "removed": name}
            if r.status_code == 404:
                continue
            detail = ""
            try:
                detail = str((r.json() or {}).get("error") or "")
            except Exception:
                pass
            last_err = detail or f"Ollama refused removal ({r.status_code})"
        # Final verification — if gone, treat as success
        try:
            tags = requests.get(endpoint + "/api/tags", timeout=5)
            if tags.status_code == 200:
                models = (tags.json() or {}).get("models") or []
                ids = {str(m.get("name") or m.get("model") or "") for m in models if isinstance(m, dict)}
                if model not in ids and f"{model_base}:latest" not in ids and model_base not in {i.rsplit(':',1)[0] for i in ids}:
                    return {"ok": True, "removed": model, "already_absent": True}
        except Exception:
            pass
        raise HTTPException(404, last_err or f"Model not found: {model}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not remove model: {e}") from e


@app.get("/api/studio/diagnostics")
def api_studio_diagnostics() -> dict[str, Any]:
    checks = []
    def add(code, label, ok, detail, level=None):
        checks.append({"code": code, "label": label, "ok": bool(ok), "detail": str(detail), "level": level or ("ok" if ok else "bad")})
    now = time.strftime("%H:%M:%S")
    endpoint = ollama_root(_settings.get("ollama_base_url"))
    try:
        import requests
        r = requests.get(endpoint.rstrip("/") + "/api/tags", timeout=2.5)
        ok = r.status_code == 200
        models = (r.json() or {}).get("models") or [] if ok else []
        add("OLLAMA", "Ollama reachability", ok, endpoint if ok else f"HTTP {r.status_code}", "ok" if ok else "bad")
    except Exception as e:
        models = []
        add("OLLAMA", "Ollama reachability", False, str(e), "bad")
    store = str(local_ollama_store() or "")
    add("STORE", "Project-local model store", bool(os.environ.get("OLLAMA_MODELS") and store), store or "missing", "ok" if store else "warn")
    model = resolve_chat_model(_settings) or ""
    ids = {str(m.get("name") or m.get("model") or "") for m in models if isinstance(m, dict)}
    installed_match = bool(model and (model in ids or f"{model}:latest" in ids or any(x.rsplit(":",1)[0] == model.rsplit(":",1)[0] for x in ids)))
    add("MODEL", "Configured chat model", installed_match, model or "NO MODEL", "ok" if installed_match else "warn")
    try:
        ms = matrix_status(_settings, root=ROOT)
        count = int(ms.get("count") or 0)
        add("AGENTS", "Agent roster", count >= 700, f"{count} discovered", "ok" if count >= 700 else "warn")
    except Exception as e:
        add("AGENTS", "Agent roster", False, str(e), "bad")
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        files = list(SESSIONS_DIR.glob("*.json")); bad = 0
        for fp in files:
            try: json.loads(fp.read_text(encoding="utf-8"))
            except Exception: bad += 1
        add("SESSIONS", "Session storage", bad == 0, f"{len(files)} saved · {bad} unreadable", "ok" if bad == 0 else "bad")
    except Exception as e:
        add("SESSIONS", "Session storage", False, str(e), "bad")
    try:
        cfg = Path(SETTINGS_PATH)
        if cfg.exists(): json.loads(cfg.read_text(encoding="utf-8"))
        add("CONFIG", "Settings file", True, str(cfg), "ok")
    except Exception as e:
        add("CONFIG", "Settings file", False, str(e), "bad")
    critical = [c for c in checks if c["level"] == "bad"]
    warnings = [c for c in checks if c["level"] == "warn"]
    return {"ok": not critical, "time": now, "checks": checks, "critical": len(critical), "warnings": len(warnings), "summary": f"{len(checks)-len(critical)} / {len(checks)} checks healthy"}


@app.get("/api/studio/vitals")
def api_studio_vitals() -> dict[str, Any]:
    """Stable alias for the VITAL CONSOLE diagnostics endpoint."""
    return api_studio_diagnostics()


@app.post("/api/matrix/install")
def api_matrix_install() -> dict[str, Any]:
    """Launch the bundled Matrix installer from this project."""
    candidates = [ROOT / "MatrixFiles" / "INSTALL_MODELS.bat"]
    bat = next((p for p in candidates if p.is_file()), None)
    if not bat:
        raise HTTPException(404, "Matrix installer not found in MatrixFiles")
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "CypraWorkShop Matrix Installer", "cmd.exe", "/k", str(bat)],
            cwd=str(bat.parent),
            close_fds=True,
        )
    except OSError as e:
        raise HTTPException(500, f"Could not launch Matrix installer: {e}") from e
    return {"ok": True, "path": str(bat)}


class StudioConfigImportBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    format: str = ""
    version: int = 1
    settings: dict[str, Any] = Field(default_factory=dict)
    favorites: list[str] = Field(default_factory=list)  # v1 compatibility
    recent: list[str] = Field(default_factory=list)  # v1 compatibility
    selected_agent: str = ""
    custom_agents: list[dict[str, Any]] = Field(default_factory=list)


class StudioResetBody(BaseModel):
    ui_settings: bool = True
    agent_preferences: bool = True
    custom_agents: bool = False


_STUDIO_CONFIG_FORMAT = "cypra-matrix-studio-config"
_STUDIO_CONFIG_VERSION = 1
_STUDIO_CONFIG_SECRET_KEYS = {"legacy_cloud_key", "ollama_api_key", "brain_settings"}


def _studio_config_secret_key(key: str) -> bool:
    name = str(key or "").strip().lower()
    return name in _STUDIO_CONFIG_SECRET_KEYS or name.endswith("_api_key") or name.endswith("_token") or name.endswith("_secret")


def _studio_exportable_settings() -> dict[str, Any]:
    """Return only active portable settings; credentials and retired fields never leave the process."""
    allowed = set(DEFAULT_SETTINGS.keys()) - RETIRED_SETTING_KEYS - {"brain_settings"}
    safe: dict[str, Any] = {}
    for key in sorted(allowed):
        if _studio_config_secret_key(key):
            continue
        value = _settings.get(key, DEFAULT_SETTINGS.get(key))
        safe[key] = value
    # The context selector has one canonical allocation ladder. Export the
    # normalized value so old AUTO/0 files cannot be regenerated.
    safe["ollama_num_ctx"] = normalize_ollama_context(safe.get("ollama_num_ctx", 8192))
    return safe


def _studio_custom_agents() -> list[dict[str, str]]:
    root = resolve_matrix_root(ROOT, _settings)
    custom_agents: list[dict[str, str]] = []
    if not root:
        return custom_agents
    cdir = root / "CustomAgents"
    if not cdir.is_dir():
        return custom_agents
    for f in sorted(cdir.glob("Modelfile_*")):
        if not f.is_file():
            continue
        try:
            custom_agents.append({"name": f.name, "content": f.read_text(encoding="utf-8")})
        except (OSError, UnicodeError):
            continue
    return custom_agents


def _build_studio_config_export() -> dict[str, Any]:
    settings = _studio_exportable_settings()
    custom_agents = _studio_custom_agents()
    return {
        "format": _STUDIO_CONFIG_FORMAT,
        "version": _STUDIO_CONFIG_VERSION,
        "product": "Cypra Matrix Studio",
        "build": BUILD_ID,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "settings_schema": int(settings.get("settings_schema") or DEFAULT_SETTINGS.get("settings_schema") or 0),
        "selected_agent": str(settings.get("matrix_agent") or "cypra"),
        "settings": settings,
        "custom_agents": custom_agents,
    }


@app.get("/api/studio/about")
def studio_about() -> dict[str, Any]:
    matrix = matrix_status(_settings, root=ROOT)
    return {
        "ok": True,
        "product": "Cypra Matrix Studio",
        "tagline": "Local-first multi-agent AI studio",
        "version": APP_VERSION,
        "build": BUILD_ID,
        "agents": int(matrix.get("count") or 0),
        "scope": "PROJECT-LOCAL",
        "base_model": resolve_chat_model(_settings),
        "ollama": ollama_root(_settings.get("ollama_base_url")),
        "model_store": local_ollama_store(),
        "studio_root": str(ROOT),
        "python": sys.version.split()[0],
        "context_tokens": resolve_ollama_context(_settings),
        "context_mode": "manual",
        "features": [
            "Project-local Ollama and model storage",
            "Selectable 8K-256K context shared across chat and agents",
            "Project-local CPU RAG with bounded source retrieval",
            "Plan B hardware-aware performance tuning",
            "Directive-locked built-in and custom agents",
            "Live GPU, VRAM, quantization, and runtime diagnostics",
            "Portable configuration export without API keys",
        ],
    }


@app.post("/api/studio/folder/open")
def studio_folder_open() -> dict[str, Any]:
    """Open the exact Matrix Studio application directory in Explorer."""
    try:
        os.startfile(str(ROOT))  # type: ignore[attr-defined]
    except Exception as e:
        raise HTTPException(500, f"Could not open Studio folder: {e}") from e
    return {"ok": True, "path": str(ROOT)}


@app.get("/api/studio/config/export")
def studio_config_export() -> dict[str, Any]:
    return _build_studio_config_export()


@app.post("/api/studio/config/export-file")
def studio_config_export_file(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Persist the live portable config locally; never trust a stale client copy as the source of truth."""
    if body and body.get("format") not in (None, "", _STUDIO_CONFIG_FORMAT):
        raise HTTPException(400, "Invalid Cypra Matrix Studio configuration payload")
    safe = _build_studio_config_export()
    export_dir = ROOT / "MatrixFiles" / "Exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")[:-3]
    unique = uuid.uuid4().hex[:6]
    path = export_dir / f"cypra-matrix-studio-config-{stamp}-{unique}.json"
    encoded = json.dumps(safe, indent=2, ensure_ascii=False)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(encoded, encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "path": str(path), "filename": path.name, "bytes": len(encoded.encode("utf-8")), "config": safe}


@app.post("/api/studio/config/import")
def studio_config_import(body: StudioConfigImportBody) -> dict[str, Any]:
    global _settings
    if body.format != _STUDIO_CONFIG_FORMAT:
        raise HTTPException(400, "Not a Cypra Matrix Studio configuration file")
    if body.version < 1 or body.version > _STUDIO_CONFIG_VERSION:
        raise HTTPException(400, f"Unsupported Studio configuration version: {body.version}")
    if not isinstance(body.settings, dict):
        raise HTTPException(400, "Configuration settings must be a JSON object")

    allowed = set(DEFAULT_SETTINGS.keys()) - RETIRED_SETTING_KEYS - {"brain_settings"}
    patch = {
        k: v for k, v in body.settings.items()
        if k in allowed and not _studio_config_secret_key(k)
    }
    if body.selected_agent:
        patch["matrix_agent"] = str(body.selected_agent).strip()
    if "ollama_num_ctx" in patch:
        patch["ollama_num_ctx"] = normalize_ollama_context(patch["ollama_num_ctx"])
    explicit_nulls = {
        key for key, value in patch.items()
        if value is None and DEFAULT_SETTINGS.get(key) is None
    }

    # Use the exact same Pydantic + runtime normalization path as a normal
    # Settings save. This prevents an imported JSON file from bypassing clamps,
    # enum cleanup, Matrix path sanitation, UI-color validation, or model rules.
    if patch:
        try:
            validated = SettingsUpdate.model_validate(patch)
        except ValidationError as exc:
            raise HTTPException(400, f"Invalid configuration settings: {exc.errors(include_url=False)}") from exc
        update_settings(validated)
        # update_settings intentionally ignores generic None values. For settings
        # whose canonical default is None (currently Plan B batch = Auto), an
        # explicit null in an export must still round-trip and clear the target.
        if explicit_nulls:
            for key in explicit_nulls:
                _settings[key] = None
            save_settings(SETTINGS_PATH, _settings)

    # Custom Modelfiles are imported after all entries are validated. Each file
    # is written atomically so removable-drive interruption cannot leave a
    # truncated agent definition.
    valid_agents: list[tuple[str, str]] = []
    skipped = 0
    for item in body.custom_agents:
        name = str(item.get("name") or "").strip()
        content = str(item.get("content") or "")
        content_lines = content.lstrip("\ufeff").splitlines()
        first_directive = next((line.strip() for line in content_lines if line.strip() and not line.lstrip().startswith("#")), "")
        if (
            Path(name).name != name
            or not re.fullmatch(r"Modelfile_[A-Za-z0-9._-]+", name)
            or not first_directive.upper().startswith("FROM ")
            or len(content.encode("utf-8")) > 2_000_000
        ):
            skipped += 1
            continue
        valid_agents.append((name, content))

    root = resolve_matrix_root(ROOT, _settings)
    imported = 0
    if valid_agents and root:
        cdir = root / "CustomAgents"
        cdir.mkdir(parents=True, exist_ok=True)
        for name, content in valid_agents:
            target = cdir / name
            tmp = cdir / f".{name}.{uuid.uuid4().hex[:8]}.tmp"
            try:
                tmp.write_text(content, encoding="utf-8")
                tmp.replace(target)
                imported += 1
            except (OSError, UnicodeError) as exc:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise HTTPException(500, f"Could not import custom agent {name}: {exc}") from exc
    elif valid_agents and not root:
        skipped += len(valid_agents)

    return {
        "ok": True,
        "settings": _public_settings(),
        "custom_agents_imported": imported,
        "custom_agents_skipped": skipped,
        "context_tokens": resolve_ollama_context(_settings),
    }

@app.post("/api/studio/factory-reset")
def studio_factory_reset(body: StudioResetBody) -> dict[str, Any]:
    global _settings
    changed = []
    if body.ui_settings:
        for key in ("theme_preset", "ui_mode", "ui_colors", "theme_accent", "ui_density", "ui_font_scale", "show_tooltips", "reduce_motion"):
            if key in DEFAULT_SETTINGS:
                _settings[key] = DEFAULT_SETTINGS[key]
        changed.append("ui")
    if body.agent_preferences:
        for key in ("matrix_agent", "matrix_agent_resolved", "matrix_agent_locked", "matrix_handoff", "matrix_history_mode", "matrix_history_turns"):
            if key in DEFAULT_SETTINGS:
                _settings[key] = DEFAULT_SETTINGS[key]
            else:
                _settings.pop(key, None)
        changed.append("agent")
    if body.custom_agents:
        root = resolve_matrix_root(ROOT, _settings)
        removed = 0
        if root:
            cdir = root / "CustomAgents"
            if cdir.is_dir():
                for f in cdir.glob("Modelfile_*"):
                    try:
                        f.unlink(); removed += 1
                    except OSError:
                        pass
        changed.append(f"custom-agents:{removed}")
    save_settings(SETTINGS_PATH, _settings)
    return {"ok": True, "changed": changed, "settings": _settings}

@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return _public_settings()


@app.post("/api/settings")
def update_settings(body: SettingsUpdate) -> dict[str, Any]:
    data = body.model_dump(exclude_none=True)
    # Only persist keys we know (plus legacy_cloud_key)
    allowed = (set(DEFAULT_SETTINGS.keys()) - RETIRED_SETTING_KEYS - {"brain_settings"}) | {"legacy_cloud_key", "settings_schema"}
    data = {k: v for k, v in data.items() if k in allowed}
    if "legacy_cloud_key" in data:
        key = str(data["legacy_cloud_key"]).strip()
        if key.startswith("••"):
            data.pop("legacy_cloud_key")
        else:
            data["legacy_cloud_key"] = key
    if "llm_provider" in data:
        p = str(data["llm_provider"]).strip().lower()
        if p in ("ollama", "local", "offline"):
            data["llm_provider"] = "ollama"
        elif p == "hybrid":
            data["llm_provider"] = "hybrid"
        else:
            data["llm_provider"] = "legacy_cloud"
    if "ollama_base_url" in data and data["ollama_base_url"]:
        data["ollama_base_url"] = str(data["ollama_base_url"]).rstrip("/")
    # Clamp live application settings.
    if "chat_temperature" in data:
        data["chat_temperature"] = max(0.0, min(1.5, float(data["chat_temperature"])))
    if "memory_context_limit" in data:
        data["memory_context_limit"] = max(4, min(32, int(data["memory_context_limit"])))
    if "rag_enabled" in data:
        data["rag_enabled"] = bool(data["rag_enabled"])
    if "rag_top_k" in data:
        data["rag_top_k"] = max(1, min(8, int(data["rag_top_k"])))
    if "rag_context_chars" in data:
        data["rag_context_chars"] = max(1200, min(24000, int(data["rag_context_chars"])))
    if "rag_chunk_chars" in data:
        data["rag_chunk_chars"] = max(600, min(6000, int(data["rag_chunk_chars"])))
    if "rag_chunk_overlap" in data:
        data["rag_chunk_overlap"] = max(0, min(1200, int(data["rag_chunk_overlap"])))
        chunk_size = int(data.get("rag_chunk_chars", _settings.get("rag_chunk_chars") or 1800))
        data["rag_chunk_overlap"] = min(data["rag_chunk_overlap"], chunk_size // 2)
    if "rag_min_score" in data:
        data["rag_min_score"] = max(0.0, min(20.0, float(data["rag_min_score"])))
    if "ui_mode" in data:
        mode = str(data.get("ui_mode") or "classic").strip().lower()
        data["ui_mode"] = mode if mode in ("classic", "modern") else "classic"
    if "think_mode" in data:
        mode = str(data.get("think_mode") or "auto").strip().lower()
        data["think_mode"] = mode if mode in ("off", "auto", "standard", "deep") else "auto"
    if "think_budget_tokens" in data:
        data["think_budget_tokens"] = max(128, min(8192, int(data["think_budget_tokens"])))
    if "ui_font_scale" in data:
        data["ui_font_scale"] = max(0.85, min(1.4, float(data["ui_font_scale"])))
    if "ollama_num_ctx" in data:
        data["ollama_num_ctx"] = normalize_ollama_context(data["ollama_num_ctx"])
    if "ollama_num_batch" in data:
        data["ollama_num_batch"] = None if data["ollama_num_batch"] in (None, "", 0) else max(32, min(2048, int(data["ollama_num_batch"])))
    if "ollama_chat_tokens" in data:
        data["ollama_chat_tokens"] = -1 if int(data["ollama_chat_tokens"]) < 0 else max(256, min(8192, int(data["ollama_chat_tokens"])))
    if "show_generation_stats" in data:
        data["show_generation_stats"] = bool(data["show_generation_stats"])
    if "ollama_extract_tokens" in data:
        data["ollama_extract_tokens"] = max(256, min(1536, int(data["ollama_extract_tokens"])))
    if "ollama_history_turns" in data:
        data["ollama_history_turns"] = max(2, min(16, int(data["ollama_history_turns"])))
    if "ollama_memory_chars" in data:
        data["ollama_memory_chars"] = max(1000, min(8000, int(data["ollama_memory_chars"])))
    if "ollama_max_notes" in data:
        data["ollama_max_notes"] = max(1, min(12, int(data["ollama_max_notes"])))
    if "extract_growth" in data and data["extract_growth"]:
        eg = str(data["extract_growth"]).strip().lower()
        if eg == "aggressive":
            eg = "dense"
        if eg not in ("sparse", "balanced", "dense"):
            eg = "dense"
        data["extract_growth"] = eg
    if "ollama_keep_alive" in data and data["ollama_keep_alive"] is not None:
        data["ollama_keep_alive"] = str(data["ollama_keep_alive"]).strip() or "10m"
    if "conversation_style" in data and data["conversation_style"]:
        st = str(data["conversation_style"]).strip().lower()
        if st not in ("natural", "concise", "socratic", "technical"):
            st = "natural"
        data["conversation_style"] = st
    if "tts_provider" in data and data["tts_provider"]:
        tp = str(data["tts_provider"]).strip().lower()
        if tp == "kokoro":
            tp = "browser"
        if tp not in TTS_PROVIDERS:
            tp = "auto"
        data["tts_provider"] = tp
    if "tts_local_voice" in data:
        voice_name = str(data["tts_local_voice"] or "").strip()
        if not voice_name or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in voice_name):
            voice_name = "en_US-lessac-medium"
        data["tts_local_voice"] = voice_name
    if "tts_edge_voice" in data:
        edge_voice = str(data["tts_edge_voice"] or "").strip()
        if not edge_voice or len(edge_voice) > 128 or not re.fullmatch(r"[A-Za-z0-9_-]+", edge_voice):
            edge_voice = "en-US-AvaNeural"
        data["tts_edge_voice"] = edge_voice
    if "tts_online_fallback" in data:
        fallback = str(data["tts_online_fallback"] or "piper").strip().lower()
        data["tts_online_fallback"] = fallback if fallback in ("piper", "none") else "piper"
    if "tts_rate" in data:
        data["tts_rate"] = max(0.5, min(2.0, float(data["tts_rate"])))
    if "tts_pitch" in data:
        data["tts_pitch"] = max(0.5, min(2.0, float(data["tts_pitch"])))
    if "tts_max_chars" in data:
        data["tts_max_chars"] = max(100, min(10000, int(data["tts_max_chars"])))
    if "tts_cpu_threads" in data:
        data["tts_cpu_threads"] = max(1, min(4, int(data["tts_cpu_threads"])))
    if "sticky_pins" in data:
        pins = data["sticky_pins"]
        if not isinstance(pins, list):
            pins = []
        cleaned: list[str] = []
        seen_p: set[str] = set()
        for p in pins:
            s = str(p or "").strip()
            if not s:
                continue
            k = s.lower()
            if k in seen_p:
                continue
            seen_p.add(k)
            cleaned.append(s)
        data["sticky_pins"] = cleaned[:12]
    if "matrix_history_mode" in data:
        data["matrix_history_mode"] = "current_chat"
    if "matrix_history_turns" in data:
        data["matrix_history_turns"] = max(4, min(48, int(data["matrix_history_turns"])))
    if "matrix_root" in data:
        data["matrix_root"] = sanitize_matrix_root_setting(
            str(data.get("matrix_root") or ""), project=ROOT
        )
    if "matrix_agent" in data and data["matrix_agent"]:
        slug = str(data["matrix_agent"]).strip().lower()
        agent = get_agent(slug, {**_settings, **data})
        data["matrix_agent"] = (agent or {}).get("slug") or slug
    # One model in VRAM: keep extract matched to chat when chat changes
    if "ollama_chat_model" in data and data["ollama_chat_model"]:
        if "ollama_extract_model" not in data or not data.get("ollama_extract_model"):
            data["ollama_extract_model"] = data["ollama_chat_model"]
        # Prefer same model always for ollama-only to avoid dual load
        if get_provider({**_settings, **data}) == "ollama":
            data["ollama_extract_model"] = data["ollama_chat_model"]
    data["matrix_agent_locked"] = bool(data.get("matrix_agent_locked", _settings.get("matrix_agent_locked", False)))
    if "ui_colors" in data:
        incoming_ui = data.get("ui_colors") if isinstance(data.get("ui_colors"), dict) else {}
        current_ui = _settings.get("ui_colors") if isinstance(_settings.get("ui_colors"), dict) else {}
        defaults_ui = DEFAULT_SETTINGS.get("ui_colors", {})
        cleaned_ui = {**defaults_ui, **current_ui}
        cleaned_ui["enabled"] = bool(incoming_ui.get("enabled", cleaned_ui.get("enabled", False)))
        for key, fallback in defaults_ui.items():
            if key == "enabled":
                continue
            raw = str(incoming_ui.get(key, cleaned_ui.get(key, fallback)) or fallback).strip()
            cleaned_ui[key] = raw.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", raw) else str(cleaned_ui.get(key, fallback)).lower()
        data["ui_colors"] = cleaned_ui
    data["settings_schema"] = max(int(_settings.get("settings_schema") or 0), 37)
    if str(data.get("ollama_keep_alive") or "") == "0":
        data.pop("ollama_keep_alive", None)
    if not str(data.get("ollama_chat_model") or "").strip():
        data.pop("ollama_chat_model", None)
    if not str(data.get("ollama_extract_model") or "").strip():
        data.pop("ollama_extract_model", None)
    prev_model = resolve_chat_model(_settings)
    prev_ctx = str(_settings.get("ollama_num_ctx") or "")
    _settings.update(data)
    _settings.pop("brain_settings", None)
    if not str(_settings.get("ollama_keep_alive") or ""):
        _settings["ollama_keep_alive"] = "-1"
    save_settings(SETTINGS_PATH, _settings)
    if data.get("voice_output_enabled") is False:
        LOCAL_TTS.cancel(clear_queue=True, release=True)
    elif any(key in data for key in ("tts_provider", "tts_allow_online")):
        LOCAL_TTS.cancel(clear_queue=True, release=False)
    new_model = resolve_chat_model(_settings)
    model_changed = str(prev_model or "") != str(new_model or "")
    ctx_changed = prev_ctx != str(_settings.get("ollama_num_ctx") or "")
    try:
        if model_changed:
            start_background_warm(_settings, "chat")
        else:
            pin_resident_keep_alive(_settings)
            if ctx_changed:
                start_background_warm(_settings, "chat")
    except Exception:
        pass
    return {"ok": True, "settings": _public_settings()}


@app.post("/api/settings/reset")
def reset_settings_section_api(body: SettingsResetRequest | None = None) -> dict[str, Any]:
    """Reset AI, RAG, Appearance, App/UI, or all settings to defaults (keeps API key)."""
    global _settings
    section = (body.section if body else "all") or "all"
    try:
        _settings = reset_settings_section(_settings, section)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _settings.pop("brain_settings", None)
    save_settings(SETTINGS_PATH, _settings)
    if not bool(_settings.get("voice_output_enabled")):
        LOCAL_TTS.cancel(clear_queue=True, release=True)
    return {
        "ok": True,
        "section": section,
        "settings": _public_settings(),
        "sections": list(SETTINGS_SECTIONS.keys()) + ["all"],
    }


# ── memory ─────────────────────────────────────────────────────────

@app.post("/api/memory/reindex")
def api_reindex() -> dict[str, Any]:
    stats = memory.rebuild_from_vault(vault)
    pruned = prune_shared_memory(force=True)
    return {"ok": True, **stats, "pruned": pruned, "memory": memory.stats()}


@app.post("/api/memory/prune")
def api_memory_prune() -> dict[str, Any]:
    """Clear shared-memory index/usage/embeddings + dead wikilinks for missing notes."""
    result = prune_shared_memory(force=True, scrub_links=True)
    return result


class ForgetNoteBody(BaseModel):
    note_id: str = ""


@app.post("/api/memory/forget")
def api_memory_forget(body: ForgetNoteBody | None = None, note_id: str = "") -> dict[str, Any]:
    """Forget a single missing note id (called by UI when open fails)."""
    nid = (body.note_id if body else "") or note_id
    return forget_missing_note(nid)


class ResetMemoryRequest(BaseModel):
    reseed: bool = True
    clear_sessions: bool = True
    clear_inbox: bool = False
    # Memory reset clears memory/session state only. Keep Ollama resident/warm.
    unload_model: bool = False


@app.post("/api/memory/reset")
def api_memory_reset(body: ResetMemoryRequest | None = None) -> dict[str, Any]:
    """
    Wipe shared long-term memory (notes + index + optional chat sessions),
    then optionally restore starter seed notes.
    """
    opts = body or ResetMemoryRequest()
    result = vault.reset(reseed=opts.reseed, clear_inbox=opts.clear_inbox)

    # Preserve the append-only operation log, but mark a new memory lifetime.
    # Rollups and growth/timeline views only consume activity after this marker,
    # so pre-reset material cannot reappear in a future generated rollup.
    ops_log.record(
        "memory_reset",
        meta={
            "reseed": opts.reseed,
            "clear_sessions": opts.clear_sessions,
            "clear_inbox": opts.clear_inbox,
        },
    )

    # Clear local search index + usage strength + embeddings
    memory.docs.clear()
    memory.inv.clear()
    memory.usage.clear()
    memory.save()
    embed_store.vectors.clear()
    embed_store.save()
    memory.rebuild_from_vault(vault)
    prune_shared_memory(force=True)

    sessions_cleared = 0
    if opts.clear_sessions:
        _sessions.clear()
        for path in list(SESSIONS_DIR.glob("*.json")):
            try:
                path.unlink()
                sessions_cleared += 1
            except OSError:
                pass

    # Do not unload or recycle the resident Ollama model during a memory reset.
    # Memory reset clears stored memory state while keeping the
    # active runtime/model warm so the next chat can continue immediately.
    runtime_reset = {"ok": True, "unloaded": [], "preserved_warm": True}

    return {
        "ok": True,
        "vault": result,
        "sessions_cleared": sessions_cleared,
        "rollup_history_reset": True,
        "memory": memory.stats(),
        "runtime_reset": runtime_reset,
        "message": "Memory reset"
        + (" with starter notes" if opts.reseed else " (empty)")
        + (" and active Ollama model unloaded" if opts.unload_model else "")
        + ".",
    }


@app.post("/api/memory/touch")
def api_touch(body: TouchRequest) -> dict[str, Any]:
    memory.touch(body.node_ids, amount=1.0)
    return {"ok": True, "memory": memory.stats()}


@app.post("/api/memory/pin")
def api_pin(body: PinRequest) -> dict[str, Any]:
    sid, sess = get_session(body.session_id)
    pinned: list[str] = list(sess.get("pinned") or [])
    if body.pinned:
        if body.node_id not in pinned:
            pinned.append(body.node_id)
        memory.touch([body.node_id], amount=1.5)
    else:
        pinned = [p for p in pinned if p != body.node_id]
    sess["pinned"] = pinned
    persist_session(sid, sess)
    return {"ok": True, "session_id": sid, "pinned": pinned}


@app.get("/api/memory/search")
def api_memory_search(q: str = "", limit: int = 12) -> dict[str, Any]:
    hits = memory.search(q, limit=limit)
    return {"hits": hits, "memory": memory.stats()}


@app.get("/api/notes")
def api_notes(q: str = "") -> dict[str, Any]:
    notes = vault.search(q) if q else vault.list_notes()
    # attach strength
    for n in notes:
        u = memory.usage.get(n["id"]) or {}
        n["hits"] = int(u.get("hits") or 0)
        n["strength"] = round(float(u.get("strength") or 0), 3)
    notes.sort(key=lambda x: (-float(x.get("strength") or 0), x.get("title") or ""))
    return {"notes": notes}


@app.get("/api/notes/{note_id}")
def api_note(note_id: str) -> dict[str, Any]:
    note = vault.read_note(note_id)
    if not note:
        # Auto-forget ghosts so shared memory stays clean
        forget_missing_note(note_id)
        raise HTTPException(404, "Note not found")
    memory.touch([note["id"]], amount=0.8)
    u = memory.usage.get(note["id"]) or {}
    note["hits"] = int(u.get("hits") or 0)
    note["strength"] = round(float(u.get("strength") or 0), 3)
    return note


@app.post("/api/notes")
def api_write_note(body: NoteWrite) -> dict[str, Any]:
    note = vault.upsert_note(
        body.title,
        body.content,
        note_type=body.type,
        tags=body.tags,
        links=body.links,
        merge=body.merge,
    )
    reindex_notes([note])
    # refresh embedding cache for this note
    if note and _settings.get("use_embeddings", True):
        try:
            text = f"{note.get('title','')}\n{note.get('description') or ''}\n{note.get('body') or ''}"
            embed_store.ensure_note(note["id"], text, settings=_settings, force=True)
        except Exception:
            pass
    return {"ok": True, "note": note}


class NoteUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    type: str | None = None
    tags: list[str] | None = None
    links: list[str] | None = None


@app.put("/api/notes/{note_id}")
def api_update_note(note_id: str, body: NoteUpdate) -> dict[str, Any]:
    existing = vault.read_note(note_id)
    if not existing:
        raise HTTPException(404, "Note not found")
    title = (body.title or existing.get("title") or note_id).strip()
    content = body.content if body.content is not None else (existing.get("body") or "")
    note_type = body.type or existing.get("type") or "concept"
    tags = body.tags if body.tags is not None else (existing.get("tags") or [])
    links = body.links if body.links is not None else (existing.get("links") or [])
    # rename: write new, delete old if title stem changed
    note = vault.upsert_note(
        title, content, note_type=note_type, tags=tags, links=links, merge=False
    )
    if note and note.get("id") != existing.get("id"):
        vault.delete_note(existing["id"])
        embed_store.drop(existing["id"])
        memory.rebuild_from_vault(vault)
    else:
        reindex_notes([note])
    if note and _settings.get("use_embeddings", True):
        try:
            text = f"{note.get('title','')}\n{note.get('description') or ''}\n{note.get('body') or ''}"
            embed_store.ensure_note(note["id"], text, settings=_settings, force=True)
        except Exception:
            pass
    return {"ok": True, "note": note}


class MergeRequest(BaseModel):
    source_id: str
    target_id: str


@app.post("/api/notes/merge")
def api_merge_notes(body: MergeRequest) -> dict[str, Any]:
    note = vault.merge_notes(body.source_id, body.target_id)
    if not note:
        raise HTTPException(404, "Source or target note not found")
    embed_store.drop(body.source_id)
    memory.rebuild_from_vault(vault)
    if _settings.get("use_embeddings", True):
        try:
            text = f"{note.get('title','')}\n{note.get('body') or ''}"
            embed_store.ensure_note(note["id"], text, settings=_settings, force=True)
        except Exception:
            pass
    return {"ok": True, "note": note}


@app.delete("/api/notes/{note_id}")
def api_delete_note(note_id: str) -> dict[str, Any]:
    if not vault.delete_note(note_id):
        raise HTTPException(404, "Note not found")
    embed_store.drop(note_id)
    memory.remove_doc(note_id, save=True)
    memory.rebuild_from_vault(vault)
    prune_shared_memory(force=True)
    return {"ok": True}


@app.post("/api/vault/backup")
def api_vault_backup() -> dict[str, Any]:
    """
    Full program-state backup → Documents/CypraStudio/backups/
    (settings, vaults, memory, sessions, ops). Also mirrors zip under data/backups.
    """
    from engine.backup import save_program_state

    try:
        result = save_program_state(
            DATA,
            project_root=ROOT,
            include_project_snapshot=False,
            also_local=True,
        )
    except Exception as e:
        raise HTTPException(500, f"Backup failed: {e}") from e
    return result


@app.post("/api/backup/full")
def api_full_backup(include_code: bool = False) -> dict[str, Any]:
    """Same as vault/backup; optional project code snapshot when include_code=1."""
    from engine.backup import save_program_state

    try:
        return save_program_state(
            DATA,
            project_root=ROOT,
            include_project_snapshot=include_code,
            also_local=True,
        )
    except Exception as e:
        raise HTTPException(500, f"Backup failed: {e}") from e


@app.post("/api/memory/reembed")
def api_reembed() -> dict[str, Any]:
    """Rebuild embedding vectors for all notes (Ollama)."""
    if not _settings.get("use_embeddings", True):
        return {"ok": False, "error": "Embeddings disabled in settings"}
    n = 0
    for meta in vault.list_notes():
        full = vault.read_note(meta["id"])
        if not full:
            continue
        text = f"{full.get('title','')}\n{full.get('description') or ''}\n{full.get('body') or ''}"
        try:
            embed_store.ensure_note(full["id"], text, settings=_settings, force=True)
            n += 1
        except Exception:
            pass
    return {"ok": True, "embedded": n, "stats": embed_store.stats()}


# ── chat ────────────────────────────────────────────────────────────


def chat_prov_is_ollama(settings: dict[str, Any] | None = None) -> bool:
    try:
        return provider_for(settings or _settings, "chat") == "ollama"
    except Exception:
        return False


def _embed_note(w: dict[str, Any]) -> None:
    if not w or not _settings.get("use_embeddings", True):
        return
    try:
        text = f"{w.get('title','')}\n{w.get('description') or ''}\n{w.get('body') or ''}"
        embed_store.ensure_note(w["id"], text, settings=_settings, force=True)
    except Exception:
        pass


def _prepare_extract(
    user_text: str,
    reply: str,
) -> tuple[dict | None, list[str], list]:
    """LLM extract only — returns (extract_result, pre_ids, snapshot) or error dict."""
    try:
        extract_result = extract_from_exchange(
            None,
            user_text,
            reply,
            model=resolve_extract_model(_settings),
            existing_titles=existing_titles(),
            settings=_settings,
        )
        extract_result = sanitize_extract(extract_result, settings=_settings)
        planned_titles = [
            n.get("title") for n in (extract_result.get("notes") or []) if n.get("title")
        ]
        pre_ids: list[str] = []
        for t in planned_titles:
            existing = vault.read_note(t)
            if existing:
                pre_ids.append(existing["id"])
        snapshot = _snapshot_notes(pre_ids)
        return extract_result, pre_ids, snapshot
    except Exception as e:
        return {"error": str(e), "trace": traceback.format_exc()[-400:]}, [], []


def _finalize_extract_ops(
    extract_result: dict,
    written: list,
    *,
    session_id: str | None,
    kind: str,
    pre_ids: list[str],
    snapshot: list,
) -> None:
    if not written:
        return
    ops_log.record(
        kind,
        session_id=session_id,
        note_ids=[w.get("id") for w in written if w.get("id")],
        note_titles=[w.get("title") for w in written if w.get("title")],
        meta={"summary": extract_result.get("summary")},
        undoable=True,
        snapshot=snapshot
        + [
            {
                "id": w.get("id"),
                "title": w.get("title"),
                "existed": w.get("id") in pre_ids,
                "created": w.get("id") not in pre_ids,
            }
            for w in written
        ],
    )
    if session_id and session_id in _sessions:
        sess = _sessions[session_id]
        grown = list(sess.get("grown_notes") or [])
        for w in written:
            if w.get("id") and w["id"] not in grown:
                grown.append(w["id"])
        sess["grown_notes"] = grown[-100:]
        persist_session(session_id, sess)


def _run_extract(
    _key: str | None,
    user_text: str,
    reply: str,
    *,
    session_id: str | None = None,
    kind: str = "extract",
) -> tuple[dict | None, list]:
    """Extract + write all notes (non-streaming callers: ingest, voice, etc.)."""
    extract_result, pre_ids, snapshot = _prepare_extract(user_text, reply)
    if isinstance(extract_result, dict) and extract_result.get("error"):
        return extract_result, []
    written: list = []
    for meta in apply_extract_to_vault_iter(vault, extract_result or {}):
        written.append(meta)
        reindex_notes([meta])
        if meta.get("id"):
            memory.touch([meta["id"]], amount=1.2)
        _embed_note(meta)
    _finalize_extract_ops(
        extract_result or {},
        written,
        session_id=session_id,
        kind=kind,
        pre_ids=pre_ids,
        snapshot=snapshot,
    )
    return extract_result, written


class ExtractPreviewBody(BaseModel):
    message: str = ""
    reply: str = ""


@app.post("/api/extract/preview")
def api_extract_preview(body: ExtractPreviewBody) -> dict[str, Any]:
    """
    Dry-run extract: run the legacy memory extraction pipeline without writing notes.
    Useful to preview what would be created before committing.
    """
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(400, "message required")
    reply = (body.reply or "").strip() or "(preview — no assistant reply yet)"
    try:
        extract_result = extract_from_exchange(
            None,
            text,
            reply,
            model=resolve_extract_model(_settings),
            existing_titles=existing_titles(),
            settings=_settings,
        )
        extract_result = sanitize_extract(extract_result, settings=_settings)
    except Exception as e:
        # heuristic fallback
        try:
            from engine.extract_fallback import heuristic_extract

            extract_result = heuristic_extract(
                text, reply, existing_titles=existing_titles(), settings=_settings
            )
            extract_result = sanitize_extract(extract_result, settings=_settings)
            extract_result["source"] = "heuristic"
        except Exception as e2:
            raise HTTPException(502, f"Preview failed: {e}; fallback: {e2}") from e2
    notes = extract_result.get("notes") or []
    # light public shape
    public = []
    for n in notes:
        if not isinstance(n, dict):
            continue
        public.append(
            {
                "title": n.get("title"),
                "type": n.get("type") or "concept",
                "description": (n.get("description") or "")[:200],
                "content": (n.get("content") or n.get("body") or "")[:280],
                "links": n.get("links") or [],
                "tags": n.get("tags") or [],
            }
        )
    return {
        "ok": True,
        "dry_run": True,
        "notes": public,
        "count": len(public),
        "summary": extract_result.get("summary") or "",
        "extract": {"notes": public, "relations": extract_result.get("relations") or []},
    }


def _matrix_handoff_block(history: list[dict[str, Any]], current_agent: str) -> str:
    """Build an explicit current-chat handoff packet from prior agent turns only."""
    lines: list[str] = []
    for item in history:
        if item.get("role") != "assistant":
            continue
        slug = str(item.get("matrix_agent") or "").strip()
        content = str(item.get("content") or "").strip()
        if not slug or not content:
            continue
        lines.append(f"{slug}: {content}")
    if not lines:
        return ""
    # Keep handoff compact so it cannot crowd out the live chat history.
    selected = lines[-8:]
    return (
        "## MATRIX AGENT HANDOFF\n"
        "The following statements were produced by earlier agents in THIS CHAT ONLY. "
        "They are reference context, not persona instructions. Use them when relevant and "
        "correctly attribute uncertainty. The current selected agent remains authoritative.\n\n"
        + "\n".join(selected)
    )


# ── isolated local file review ───────────────────────────────────────
def _decode_text_bytes(data: bytes) -> str:
    """Decode local file bytes. Handles empty, UTF-8, UTF-16, and Windows text."""
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace")
    elif data.startswith(b"\xef\xbb\xbf"):
        text = data.decode("utf-8-sig", errors="replace")
    elif len(data) >= 4 and data[1:2] == b"\x00" and data[3:4] == b"\x00":
        text = data.decode("utf-16-le", errors="replace")
    else:
        text = data.decode("utf-8", errors="replace")
        if text.count("\x00") > max(2, len(text) // 4):
            text = data.decode("utf-16-le", errors="replace")
        elif "\ufffd" in text[:80]:
            try:
                text = data.decode("cp1252", errors="replace")
            except Exception:
                pass
    return text.replace("\ufeff", "").replace("\x00", "")


def _review_file_text(file_name: str, data: bytes) -> tuple[str, str]:
    """Best-effort local extraction. Never writes the uploaded file to disk."""
    name = Path(file_name or "file").name
    ext = Path(name).suffix.lower()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(413, "File is too large for local review (15 MB maximum).")

    text_exts = {
        ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl",
        ".yaml", ".yml", ".xml", ".html", ".htm", ".css", ".scss", ".js",
        ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".py", ".ps1", ".psm1",
        ".psd1", ".bat", ".cmd", ".ini", ".cfg", ".conf", ".toml", ".log",
        ".properties", ".sql", ".sh", ".c", ".h", ".cpp", ".hpp", ".java",
        ".cs", ".go", ".rs", ".php", ".rb", ".swift", ".kt", ".tex"
    }
    if ext in text_exts or not ext:
        return _decode_text_bytes(data), ext or ".txt"

    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except Exception as e:
            raise HTTPException(415, "PDF review needs the local pypdf package installed.") from e
        try:
            import io
            reader = PdfReader(io.BytesIO(data))
            chunks = []
            for page in reader.pages[:60]:
                chunks.append(page.extract_text() or "")
            return "\n\n".join(chunks), ext
        except Exception as e:
            raise HTTPException(422, f"Could not extract PDF text: {e}") from e

    if ext == ".docx":
        import io, zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
            xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
            xml = re.sub(r"</w:p>", "\n", xml)
            text = re.sub(r"<[^>]+>", "", xml)
            return html_escape(text, quote=False).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"), ext
        except Exception as e:
            raise HTTPException(422, f"Could not extract DOCX text: {e}") from e

    if ext == ".xlsx":
        try:
            import io, openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            rows = []
            for ws in wb.worksheets:
                rows.append(f"## SHEET: {ws.title}")
                for row in ws.iter_rows(values_only=True):
                    vals = ["" if v is None else str(v) for v in row]
                    if any(vals): rows.append("\t".join(vals))
            return "\n".join(rows), ext
        except Exception as e:
            raise HTTPException(415, "XLSX review needs the local openpyxl package installed.") from e

    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}:
        raise HTTPException(415, "Image review is not enabled in this local file-review path yet.")

    raise HTTPException(415, f"Unsupported file type: {ext or 'unknown'}")


def _prepare_file_review(file_name: str, raw: bytes, instruction: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Build isolated review messages. Caps input excerpt only — reply length stays unlimited."""
    extracted, ext = _review_file_text(file_name or "file", raw)
    extracted = extracted.strip()
    if not extracted:
        raise HTTPException(
            422,
            f"No readable text was found in {Path(file_name or 'file').name} ({len(raw)} bytes received).",
        )
    text_cap = 60000
    if len(extracted) > text_cap:
        extracted = extracted[:text_cap] + "\n\n[FILE REVIEW EXCERPT TRUNCATED]"
    user_instruction = (
        instruction or "Review this file for important content, structure, issues, and actionable findings."
    ).strip()
    review_messages = [
        {
            "role": "system",
            "content": (
                "You are Cypra's local file-review analyst. Review ONLY the supplied file excerpt. "
                "Do not claim to have seen content that is not present. Provide a useful structured review "
                "with a concise summary, important findings, notable issues or risks, and actionable next steps. "
                "Write a complete review; do not truncate the answer. "
                "This review is standalone and must not alter or rely on the current chat session history."
            ),
        },
        {
            "role": "user",
            "content": (
                f"FILE: {Path(file_name or 'file').name}\nTYPE: {ext}\n\n"
                f"REVIEW INSTRUCTION:\n{user_instruction}\n\nFILE CONTENT:\n{extracted}"
            ),
        },
    ]
    # Keep a bounded, reusable source excerpt so Review -> Next Send can attach
    # the actual reviewed file content to the following chat turn.
    turn_context_cap = 12000
    turn_context = extracted[:turn_context_cap]
    if len(extracted) > turn_context_cap:
        turn_context += "\n\n[REVIEW SOURCE EXCERPT TRUNCATED]"

    meta = {
        "file_name": Path(file_name or "file").name,
        "file_type": ext,
        "chars_reviewed": len(extracted),
        "turn_context": turn_context,
        "turn_context_truncated": len(extracted) > turn_context_cap,
    }
    return review_messages, meta


def _review_temperature() -> float:
    temperature = float(_settings.get("chat_temperature") or 0.4)
    if provider_for(_settings, "chat") == "ollama":
        temperature = min(temperature, 0.55)
    return temperature


def _run_file_review(file_name: str, raw: bytes, instruction: str) -> dict[str, Any]:
    """Shared local file-review path. Never writes the file or touches chat history."""
    review_messages, meta = _prepare_file_review(file_name, raw, instruction)
    ensure_llm_ready()
    review_settings = dict(_settings)
    model = resolve_chat_model(review_settings)
    provider = get_provider(review_settings)
    chat_prov = provider_for(review_settings, "chat")
    think_plan = _resolve_think_plan(
        instruction or "Review the selected file", review_settings, has_file=True
    )
    native_supported = native_detected = False
    if chat_prov == "ollama":
        native_supported, native_detected = ollama_model_thinking_support(review_settings, model)
    review_settings["_think_runtime_mode"] = think_plan["resolved"]
    review_settings["_think_runtime_requested"] = think_plan["requested"]
    review_settings["_think_runtime_reason"] = think_plan["reason"]
    review_settings["_think_budget_tokens"] = think_plan["budget_tokens"]
    review_settings["_think_native_supported"] = bool(native_supported)
    review_settings["_think_native_detected"] = bool(native_detected)
    review_messages = list(review_messages)
    review_messages.insert(1, {"role": "system", "content": _review_think_instruction(think_plan)})
    review = chat_completion(
        None, review_messages, model=model, temperature=_review_temperature(), settings=review_settings
    )
    return {
        "ok": True,
        **meta,
        "model": model,
        "provider": provider,
        "think_mode": think_plan["resolved"],
        "think_requested": think_plan["requested"],
        "think_reason": think_plan["reason"],
        "think_budget_tokens": think_plan["budget_tokens"],
        "think_native_supported": bool(native_supported),
        "think_native_detected": bool(native_detected),
        "review": str(review or "").strip(),
    }


def _bytes_from_review_body(*, path: str = "", name: str = "file", text: str | None = None, content_b64: str | None = None) -> tuple[str, bytes]:
    if (path or "").strip():
        p = Path(path).expanduser().resolve()
        if not p.is_file():
            raise HTTPException(404, f"File not found: {p.name}")
        return p.name, p.read_bytes()
    if text is not None:
        return Path(name or "file").name, text.encode("utf-8")
    if content_b64:
        import base64
        return Path(name or "file").name, base64.b64decode(content_b64)
    raise HTTPException(400, "No file content was provided.")


class ReviewContentBody(BaseModel):
    name: str = "file"
    instruction: str = ""
    text: str | None = None
    content_b64: str | None = None


@app.post("/api/review-content")
def api_review_content(body: ReviewContentBody) -> dict[str, Any]:
    """Review file bytes/text sent as JSON. Avoids empty WebView2 multipart uploads."""
    try:
        if body.text is not None:
            raw = body.text.encode("utf-8")
        elif body.content_b64:
            import base64
            raw = base64.b64decode(body.content_b64)
        else:
            raise HTTPException(400, "No file content was provided.")
        if not raw:
            raise HTTPException(422, f"{Path(body.name or 'file').name} was empty (0 bytes).")
        return _run_file_review(body.name or "file", raw, body.instruction)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"File review failed: {e}") from e


@app.post("/api/review-file")
def api_review_file(file: UploadFile = File(...), instruction: str = Form("")) -> dict[str, Any]:
    """Review an uploaded file without reading or writing the current chat session."""
    try:
        raw = file.file.read()
        if not raw:
            raise HTTPException(
                422,
                f"{Path(file.filename or 'file').name} uploaded as 0 bytes. Use Choose File or drop the file onto the dialog.",
            )
        return _run_file_review(file.filename or "file", raw, instruction)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"File review failed: {e}") from e
    finally:
        try:
            file.file.close()
        except Exception:
            pass


class ReviewPathBody(BaseModel):
    path: str
    instruction: str = ""


@app.post("/api/review-path")
def api_review_path(body: ReviewPathBody) -> dict[str, Any]:
    """Review a local filesystem path chosen via the desktop file dialog."""
    try:
        name, raw = _bytes_from_review_body(path=body.path)
        if not raw:
            raise HTTPException(422, f"{name} was empty (0 bytes).")
        return _run_file_review(name, raw, body.instruction)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"File review failed: {e}") from e


class ReviewSourceBody(BaseModel):
    name: str = "file"
    path: str = ""
    text: str | None = None
    content_b64: str | None = None


@app.post("/api/review-source")
def api_review_source(body: ReviewSourceBody) -> dict[str, Any]:
    """Return a bounded source excerpt without invoking the model."""
    try:
        name, raw = _bytes_from_review_body(
            path=body.path, name=body.name, text=body.text, content_b64=body.content_b64
        )
        if not raw:
            raise HTTPException(422, f"{name} was empty (0 bytes).")
        extracted, ext = _review_file_text(name, raw)
        extracted = extracted.strip()
        if not extracted:
            raise HTTPException(422, f"No readable text was found in {name}.")
        cap = 12000
        excerpt = extracted[:cap]
        if len(extracted) > cap:
            excerpt += "\n\n[REVIEW SOURCE EXCERPT TRUNCATED]"
        return {
            "ok": True,
            "file_name": Path(name).name,
            "file_type": ext,
            "turn_context": excerpt,
            "source_excerpt": excerpt,
            "chars_available": len(extracted),
            "truncated": len(extracted) > cap,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not extract review source: {e}") from e


class ReviewStreamBody(BaseModel):
    instruction: str = ""
    name: str = "file"
    text: str | None = None
    content_b64: str | None = None
    path: str = ""
    think: bool | None = None  # legacy compatibility
    think_mode: str | None = None


@app.post("/api/review-stream")
def api_review_stream(body: ReviewStreamBody) -> Any:
    """Stream a local file review. Isolated from chat; reply length is unlimited."""
    try:
        name, raw = _bytes_from_review_body(
            path=body.path, name=body.name, text=body.text, content_b64=body.content_b64
        )
        if not raw:
            raise HTTPException(422, f"{name} was empty (0 bytes).")
        messages, meta = _prepare_file_review(name, raw, body.instruction)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"File review failed: {e}") from e

    def gen():
        yield f"data: {json.dumps({'type': 'started', 'source_excerpt': meta.get('turn_context', ''), **meta})}\n\n"
        try:
            ensure_llm_ready()
            rev_settings = dict(_settings)
            model = resolve_chat_model(rev_settings)
            provider = get_provider(rev_settings)
            chat_prov = provider_for(rev_settings, "chat")
            think_plan = _resolve_think_plan(
                body.instruction or "Review the selected file",
                rev_settings,
                override=body.think_mode,
                legacy_override=body.think if body.think_mode is None else None,
                has_file=True,
            )
            native_supported = native_detected = False
            if chat_prov == "ollama":
                native_supported, native_detected = ollama_model_thinking_support(rev_settings, model)
            rev_settings["_think_runtime_mode"] = think_plan["resolved"]
            rev_settings["_think_runtime_requested"] = think_plan["requested"]
            rev_settings["_think_runtime_reason"] = think_plan["reason"]
            rev_settings["_think_budget_tokens"] = think_plan["budget_tokens"]
            rev_settings["_think_native_supported"] = bool(native_supported)
            rev_settings["_think_native_detected"] = bool(native_detected)
            review_messages = list(messages)
            review_messages.insert(1, {"role": "system", "content": _review_think_instruction(think_plan)})
            yield f"data: {json.dumps({'type': 'session', 'model': model, 'provider': provider, 'think_mode': think_plan['resolved'], 'think_requested': think_plan['requested'], 'think_reason': think_plan['reason'], 'think_budget_tokens': think_plan['budget_tokens'], 'think_native_supported': bool(native_supported), 'think_native_detected': bool(native_detected), 'source_excerpt': meta.get('turn_context', ''), **meta})}\n\n"
            parts: list[str] = []
            thinking_parts: list[str] = []
            generation_stats: dict[str, Any] = {}
            for kind, piece in chat_stream(
                None,
                review_messages,
                model=model,
                temperature=_review_temperature(),
                settings=rev_settings,
            ):
                if kind == "meta":
                    try:
                        generation_stats.update(json.loads(piece))
                    except Exception:
                        pass
                    continue
                if kind == "think":
                    thinking_parts.append(piece)
                    if rev_settings.get("show_model_thinking", True):
                        yield f"data: {json.dumps({'type': 'think', 'text': piece})}\n\n"
                    continue
                parts.append(piece)
                yield f"data: {json.dumps({'type': 'delta', 'text': piece})}\n\n"
            review = "".join(parts).strip()
            thinking_text = "".join(thinking_parts)
            generation_stats.update({
                "think_mode": think_plan["resolved"],
                "think_requested": think_plan["requested"],
                "think_reason": think_plan["reason"],
                "think_budget_tokens": think_plan["budget_tokens"],
                "thinking_tokens_estimate": _estimate_generated_tokens(thinking_text),
            })
            yield f"data: {json.dumps({'type': 'done', 'ok': True, 'review': review, 'model': model, 'provider': provider, 'stats': generation_stats, 'source_excerpt': meta.get('turn_context', ''), **meta})}\n\n"
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, str) else str(e.detail)
            yield f"data: {json.dumps({'type': 'error', 'error': detail})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")

def _one_turn_file_block(body: ChatRequest) -> str:
    """File excerpt for THIS send only. Never written into session history.

    Caps the excerpt so 8192 ctx still has room for history + a full reply.
    Does not change num_ctx or num_predict.
    """
    name = (body.turn_file_name or "file").strip() or "file"
    raw = (body.turn_file_text or "").strip()
    path = (body.turn_file_path or "").strip()
    if path:
        try:
            p = Path(path).expanduser().resolve()
            p.relative_to(ROOT.resolve())
            if p.is_file():
                name = p.name
                raw = _decode_text_bytes(p.read_bytes()).strip()
        except Exception:
            pass
    if not raw:
        return ""
    cap = 12000
    excerpt = raw[:cap]
    if len(raw) > cap:
        excerpt += "\n[ONE-TURN EXCERPT TRIMMED]"
    return (
        f"---\nONE-TURN FILE CONTEXT ({name}) — discarded after this reply, "
        f"not part of saved chat history:\n{excerpt}"
    )


def _review_context_block(body: ChatRequest) -> str:
    """Compact review findings for the next chat turn and saved session history.

    This is deliberately separate from long-term memory and from raw one-turn file
    attachments. The review result is the context artifact; the original file is
    not re-injected into the model unless the user explicitly uses the normal
    file-attachment path.
    """
    raw = str(body.review_context or "").strip()
    if not raw:
        return ""
    cap = 5000
    if len(raw) > cap:
        raw = raw[: cap - 24].rstrip() + "\n[REVIEW CONTEXT TRIMMED]"
    name = str(body.review_context_name or body.turn_file_name or "file").strip() or "file"
    if raw.startswith("[REVIEWED FILE CONTEXT]"):
        return raw
    return f"[REVIEWED FILE CONTEXT]\nFile: {name}\n\n{raw}"


_THINK_MODES = {"off", "auto", "standard", "deep"}

def _normalize_think_mode(value: Any, default: str = "auto") -> str:
    mode = str(value or default).strip().lower()
    return mode if mode in _THINK_MODES else default

def _estimate_generated_tokens(text: str) -> int:
    value = str(text or "").strip()
    if not value:
        return 0
    pieces = re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?|[^\s]", value)
    return max(1, round(len(pieces) * 1.12))

def _auto_think_level(
    message: str,
    *,
    rag_hits: list[dict[str, Any]] | None = None,
    has_file: bool = False,
) -> tuple[str, str]:
    """Cheap deterministic classifier: no second model call and no hidden token cost."""
    text = str(message or "").strip()
    low = text.lower()
    words = re.findall(r"[a-z0-9_+.#-]+", low)
    n = len(words)
    if not text:
        return "direct", "AUTO · empty/simple turn"
    if re.fullmatch(
        r"(?:hi|hello|hey|yo|thanks|thank you|ok|okay|cool|nice|good job|got it|saved|done|working|operational)[.!? ]*",
        low,
    ):
        return "direct", "AUTO · greeting/status"
    codeish = bool(re.search(
        r"```|traceback|exception|referenceerror|typeerror|syntaxerror|stack trace|\bdebug\b|\bbug\b|\brefactor\b|\bcompile\b|\bapi\b|\bfunction\b|\bclass\b",
        low,
    ))
    deep_terms = sum(bool(re.search(pattern, low)) for pattern in (
        r"\broot cause\b", r"\baudit\b", r"\barchitecture\b", r"\bimplement\b",
        r"\boptimi[sz]e\b", r"\bprove\b", r"\bderive\b", r"\btrade-?offs?\b",
        r"\bcompare\b", r"\bdesign\b", r"\bplan\b", r"\bfix the rest\b",
    ))
    multi_part = text.count("\n") >= 3 or len(
        re.findall(r"(?:^|\s)(?:\d+[.)]|[-*])\s", text, flags=re.M)
    ) >= 3
    if codeish and (n >= 18 or deep_terms >= 1):
        return "deep", "AUTO · code/debug complexity"
    if deep_terms >= 2 or multi_part or n >= 90:
        return "deep", "AUTO · multi-step analysis"
    if rag_hits or has_file:
        return "standard", "AUTO · grounded document context"
    if n <= 16 and not re.search(r"\bwhy\b|\bhow\b|\bexplain\b|\banaly[sz]e\b|\breason\b", low):
        return "direct", "AUTO · short direct turn"
    if n <= 28 and re.match(r"^(what|who|when|where|which|is|are|can|does|do)\b", low) and deep_terms == 0:
        return "direct", "AUTO · simple question"
    return "standard", "AUTO · normal reasoning"

def _resolve_think_plan(
    message: str,
    settings: dict[str, Any],
    *,
    override: str | None = None,
    legacy_override: bool | None = None,
    rag_hits: list[dict[str, Any]] | None = None,
    has_file: bool = False,
) -> dict[str, Any]:
    configured = _normalize_think_mode(settings.get("think_mode"), "auto")
    requested = configured
    source = "settings"
    if override is not None and str(override).strip():
        requested = _normalize_think_mode(override, configured)
        source = "settings" if requested == configured else "turn override"
    elif legacy_override is not None:
        requested = "standard" if bool(legacy_override) else "off"
        source = "legacy turn override"
    if requested == "auto":
        resolved, reason = _auto_think_level(message, rag_hits=rag_hits, has_file=has_file)
    else:
        resolved = "direct" if requested == "off" else requested
        reason = f"{source} · {requested}"
    try:
        base_budget = max(128, min(8192, int(settings.get("think_budget_tokens") or 768)))
    except (TypeError, ValueError):
        base_budget = 768
    budget = 0 if resolved == "direct" else (min(8192, base_budget * 2) if resolved == "deep" else base_budget)
    return {
        "configured": configured,
        "requested": requested,
        "resolved": resolved,
        "reason": reason,
        "budget_tokens": budget,
    }

def _review_think_instruction(plan: dict[str, Any]) -> str:
    mode = str(plan.get("resolved") or "standard")
    budget = int(plan.get("budget_tokens") or 0)
    if mode == "direct":
        return (
            "## THINK CONTROL — DIRECT\n"
            "Review the file directly without extended deliberation. Keep the review complete, but do not spend tokens on hidden reasoning."
        )
    label = "DEEP" if mode == "deep" else "STANDARD"
    return (
        f"## THINK CONTROL — {label}\n"
        f"Reason internally before finalizing the review. Target roughly {budget} reasoning tokens or fewer, then provide the useful review without narrating chain-of-thought."
    )

@app.post("/api/chat")
def api_chat(body: ChatRequest) -> Any:
    raw_text = (body.message or "").strip()
    review_ctx = _review_context_block(body)
    turn_ctx = "" if review_ctx else _one_turn_file_block(body)
    text = raw_text
    if review_ctx:
        text = (raw_text + "\n\n" if raw_text else "") + review_ctx
    elif not text and turn_ctx:
        text = "Use the attached one-turn file context."
    if not text:
        raise HTTPException(400, "Empty message")

    sid, sess = get_session(body.session_id)
    # SESSION-SCOPED HISTORY ONLY: this is the history belonging to the current chat
    # session id. No vault memory, prior chat/session, or shared conversation buffer is
    # supplied to the model. The model sees only the messages stored on this session.
    all_history = list(sess.get("messages") or [])
    history_limit = max(4, min(48, int(_settings.get("matrix_history_turns") or 24)))
    history = all_history[-history_limit:]
    sess["pinned"] = []

    # Optional legacy memory is shared across agents. The current chat history and selected
    # agent directive remain session/agent-scoped; only bounded retrieved evidence is
    # injected when explicitly requested. This keeps context growth bounded
    # instead of dumping the whole vault into every request.
    memory_context = ""
    memory_used: list[str] = []
    memory_recall: list[dict[str, Any]] = []
    pinned_titles: list[str] = []
    if body.use_memory:
        try:
            pinned = list(body.pinned or [])[:8]
            memory_context, memory_used, memory_recall = memory.context_for_chat(
                vault, raw_text or text,
                limit=max(4, min(32, int(_settings.get("memory_context_limit") or 10))),
                pinned_ids=pinned,
                settings=_settings,
                embed_store=embed_store,
            )
            # Preserve stable titles for the prompt's pin section.
            pinned_titles = [
                str(x.get("title") or x.get("id") or "")
                for x in memory_recall
                if isinstance(x, dict) and (x.get("title") or x.get("id"))
            ][:8]
        except Exception:
            memory_context, memory_used, memory_recall, pinned_titles = "", [], [], []

    # RAG v2 is external-file knowledge only. It is independent from legacy
    # memory and current-chat history, and adds no GPU model or background load.
    rag_context = ""
    rag_hits: list[dict[str, Any]] = []
    rag_enabled = bool(_settings.get("rag_enabled", True)) if body.use_rag is None else bool(body.use_rag)
    if rag_enabled and raw_text and rag_store.stats().get("sources", 0):
        try:
            configured_rag_chars = max(1200, min(24000, int(_settings.get("rag_context_chars") or 6000)))
            # Protect smaller model contexts from an oversized retrieval budget.
            # Roughly 30% of the selected context (assuming ~4 chars/token) is
            # the maximum RAG share; larger contexts can use the full 24k cap.
            context_safe_rag_chars = max(1200, int(resolve_ollama_context(_settings) * 1.2))
            rag_context, rag_hits = rag_store.context_for_query(
                raw_text,
                top_k=max(1, min(8, int(_settings.get("rag_top_k") or 4))),
                max_chars=min(configured_rag_chars, context_safe_rag_chars),
                min_score=max(0.0, min(20.0, float(_settings.get("rag_min_score") or 0.25))),
            )
        except Exception:
            rag_context, rag_hits = "", []

    base_chat_temperature = float(_settings.get("chat_temperature") or 0.7)
    base_chat_temperature = temperature_for_style(_settings, base_chat_temperature)
    base_chat_temperature = max(0.0, min(1.5, base_chat_temperature))
    if provider_for(_settings, "chat") == "ollama":
        base_chat_temperature = min(base_chat_temperature, 0.65)

    if body.stream:
        def gen():
            import time as _time
            t0 = _time.perf_counter()
            yield f"data: {json.dumps({'type': 'started', 'session_id': sid, 'ts': t0})}\n\n"
            try:
                ensure_llm_ready()
                effective_temp = base_chat_temperature
                chat_settings = dict(_settings)
                if body.plain is not None:
                    chat_settings["plain_chat"] = bool(body.plain)
                if body.talk:
                    chat_settings["talk_mode"] = True
                    chat_settings["plain_chat"] = True
                if body.files:
                    chat_settings["files_mode"] = True

                response_limit = int(chat_settings.get('ollama_chat_tokens') if chat_settings.get('ollama_chat_tokens') is not None else -1)
                saved_ctx = normalize_ollama_context(chat_settings.get('ollama_num_ctx', 8192))
                model = resolve_chat_model(chat_settings)
                provider = get_provider(chat_settings)
                chat_prov = provider_for(chat_settings, "chat")
                think_plan = _resolve_think_plan(
                    raw_text or text,
                    chat_settings,
                    override=body.think_mode,
                    legacy_override=body.think if body.think_mode is None else None,
                    rag_hits=rag_hits,
                    has_file=bool(review_ctx or turn_ctx or body.files),
                )
                native_supported = native_detected = False
                if chat_prov == "ollama":
                    native_supported, native_detected = ollama_model_thinking_support(chat_settings, model)
                chat_settings["_think_runtime_mode"] = think_plan["resolved"]
                chat_settings["_think_runtime_requested"] = think_plan["requested"]
                chat_settings["_think_runtime_reason"] = think_plan["reason"]
                chat_settings["_think_budget_tokens"] = think_plan["budget_tokens"]
                chat_settings["_think_native_supported"] = bool(native_supported)
                chat_settings["_think_native_detected"] = bool(native_detected)

                messages = build_chat_messages(
                    history,
                    text,
                    memory_context=memory_context,
                    rag_context=rag_context,
                    pinned_titles=pinned_titles,
                    settings=chat_settings,
                    turn_context=turn_ctx,
                )
                matrix_agent = resolve_chat_agent(chat_settings, text)
                matrix_slug = (matrix_agent or {}).get("slug") or ""
                if chat_settings.get("matrix_handoff") and matrix_slug:
                    handoff = _matrix_handoff_block(history, matrix_slug)
                    if handoff:
                        messages.insert(1 if messages and messages[0].get("role") == "system" else 0, {"role": "system", "content": handoff})

                response_limit = int(chat_settings.get('ollama_chat_tokens') if chat_settings.get('ollama_chat_tokens') is not None else -1)
                saved_ctx = normalize_ollama_context(chat_settings.get('ollama_num_ctx', 8192))
                effective_ctx = resolve_ollama_context(chat_settings)
                chat_settings['ollama_num_ctx'] = effective_ctx
                runtime_response_limit = -1
                yield f"data: {json.dumps({'type': 'session', 'session_id': sid, 'memory_used': [], 'memory_recall': [], 'rag_enabled': rag_enabled, 'rag_hits': rag_hits, 'provider': provider, 'chat_provider': chat_prov, 'model': model, 'matrix_agent': matrix_slug, 'matrix_handoff': bool(chat_settings.get('matrix_handoff')), 'think': chat_settings.get('show_model_thinking', True), 'think_mode': think_plan['resolved'], 'think_requested': think_plan['requested'], 'think_reason': think_plan['reason'], 'think_budget_tokens': think_plan['budget_tokens'], 'think_native_supported': bool(native_supported), 'think_native_detected': bool(native_detected), 'response_tokens': response_limit, 'runtime_response_tokens': runtime_response_limit, 'context_tokens': saved_ctx, 'effective_context_tokens': effective_ctx, 'prep_ms': round((_time.perf_counter()-t0)*1000, 1)})}\n\n"

                parts: list[str] = []
                thinking_parts: list[str] = []
                generation_stats: dict[str, Any] = {}
                for kind, piece in chat_stream(
                    None,
                    messages,
                    model=model,
                    temperature=effective_temp,
                    settings=chat_settings,
                ):
                    if kind == "meta":
                        try:
                            generation_stats.update(json.loads(piece))
                        except Exception:
                            pass
                        continue
                    if kind == "think":
                        thinking_parts.append(piece)
                        if chat_settings.get("show_model_thinking", True):
                            yield f"data: {json.dumps({'type': 'think', 'text': piece, 'source': 'model'})}\n\n"
                    else:
                        parts.append(piece)
                        yield f"data: {json.dumps({'type': 'delta', 'text': piece})}\n\n"
                reply = "".join(parts).strip()
                file_log: list[dict[str, Any]] = []
                if body.files:
                    from engine.workplace import (
                        agent_slug as _wslug,
                        format_ops_for_user,
                        infer_ops,
                        results_for_model,
                        run_ops,
                        strip_file_ops,
                    )
                    slug = _wslug(matrix_slug or chat_settings.get("matrix_agent") or "cypra")
                    working = reply
                    for _round in range(3):
                        ops = infer_ops(text if _round == 0 else "", working)
                        if not ops:
                            break
                        results = run_ops(slug, ops, user_text=text)
                        file_log.extend(results)
                        yield f"data: {json.dumps({'type': 'files', 'slug': slug, 'results': results})}\n\n"
                        working = strip_file_ops(working)
                        need_more = any(r.get("op") == "read" and r.get("ok") for r in results)
                        if not need_more:
                            break
                        follow = results_for_model(results)
                        messages.append({"role": "assistant", "content": working or reply})
                        messages.append({"role": "user", "content": follow + "\n\nNow answer the user in plain language. Do not emit FILE tags unless you still need another read."})
                        more: list[str] = []
                        for kind, piece in chat_stream(
                            None,
                            messages,
                            model=model,
                            temperature=effective_temp,
                            settings=chat_settings,
                        ):
                            if kind == "meta":
                                continue
                            if kind == "think":
                                thinking_parts.append(piece)
                                if chat_settings.get("show_model_thinking", True):
                                    yield f"data: {json.dumps({'type': 'think', 'text': piece, 'source': 'model'})}\n\n"
                            else:
                                more.append(piece)
                                yield f"data: {json.dumps({'type': 'delta', 'text': piece})}\n\n"
                        working = "".join(more).strip() or working
                    visible = strip_file_ops(working)
                    summary = format_ops_for_user(slug, file_log)
                    if visible and summary and summary not in visible:
                        reply = visible + "\n\n" + summary
                    else:
                        reply = visible or summary or reply or "Done. Check the workplace panel for files."
                    yield f"data: {json.dumps({'type': 'polish', 'reply': reply, 'files': file_log})}\n\n"
                if generation_stats.get("done_reason") == "length":
                    generation_stats["truncated"] = True
                thinking_text = "".join(thinking_parts)
                generation_stats.update({
                    "think_mode": think_plan["resolved"],
                    "think_requested": think_plan["requested"],
                    "think_reason": think_plan["reason"],
                    "think_budget_tokens": think_plan["budget_tokens"],
                    "think_native_supported": bool(native_supported),
                    "think_native_detected": bool(native_detected),
                    "thinking_chars": len(thinking_text),
                    "thinking_tokens_estimate": _estimate_generated_tokens(thinking_text),
                })
                generation_stats["response_chars"] = len(reply)

                quality: dict[str, Any] = {}
                quality = _polish_reply(reply, memory_context="", pinned_titles=[])
                if quality.get("changed") and quality.get("text"):
                    reply = quality["text"]
                    yield f"data: {json.dumps({'type': 'polish', 'reply': reply, 'quality': {k: quality[k] for k in ('issues', 'unknown_citations', 'fixed_citations', 'confidence', 'changed') if k in quality}, 'summary': quality_summary(quality)})}\n\n"

                message_id = uuid.uuid4().hex[:20]
                history.append({"role": "user", "content": text})
                history.append({
                    "role": "assistant",
                    "content": reply,
                    "matrix_agent": matrix_slug,
                    "message_id": message_id,
                    "feedback": 0,
                    "stats": generation_stats,
                    "rag_hits": rag_hits,
                })
                sess["messages"] = history
                if sess.get("title") == "New chat":
                    sess["title"] = text[:48]
                persist_session(sid, sess)
                if matrix_slug:
                    record_chat_interaction(matrix_slug, f"{sid}:{message_id}")

                yield f"data: {json.dumps({'type': 'done', 'reply': reply, 'session_id': sid, 'message_id': message_id, 'feedback': 0, 'memory_used': memory_used, 'memory_recall': memory_recall, 'rag_hits': rag_hits, 'written': file_log, 'provider': provider, 'model': model, 'matrix_agent': matrix_slug, 'followups': [], 'quality': {k: quality.get(k) for k in ('issues', 'unknown_citations', 'fixed_citations', 'confidence')}, 'quality_summary': quality_summary(quality), 'stats': generation_stats})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    ensure_llm_ready()
    chat_settings = dict(_settings)
    if body.plain is not None:
        chat_settings["plain_chat"] = bool(body.plain)
    if body.talk:
        chat_settings["talk_mode"] = True
        chat_settings["plain_chat"] = True
    if body.files:
        chat_settings["files_mode"] = True
    model = resolve_chat_model(chat_settings)
    provider = get_provider(chat_settings)
    chat_prov = provider_for(chat_settings, "chat")
    think_plan = _resolve_think_plan(
        raw_text or text, chat_settings, override=body.think_mode,
        legacy_override=body.think if body.think_mode is None else None,
        rag_hits=rag_hits, has_file=bool(review_ctx or turn_ctx or body.files),
    )
    native_supported = native_detected = False
    if chat_prov == "ollama":
        native_supported, native_detected = ollama_model_thinking_support(chat_settings, model)
    chat_settings["_think_runtime_mode"] = think_plan["resolved"]
    chat_settings["_think_runtime_requested"] = think_plan["requested"]
    chat_settings["_think_runtime_reason"] = think_plan["reason"]
    chat_settings["_think_budget_tokens"] = think_plan["budget_tokens"]
    chat_settings["_think_native_supported"] = bool(native_supported)
    chat_settings["_think_native_detected"] = bool(native_detected)
    messages = build_chat_messages(
        history,
        text,
        memory_context=memory_context,
        rag_context=rag_context,
        pinned_titles=pinned_titles,
        settings=chat_settings,
        turn_context=turn_ctx,
    )
    matrix_agent = resolve_chat_agent(chat_settings, text)
    matrix_slug = (matrix_agent or {}).get("slug") or ""
    if chat_settings.get("matrix_handoff") and matrix_slug:
        handoff = _matrix_handoff_block(history, matrix_slug)
        if handoff:
            messages.insert(1 if messages and messages[0].get("role") == "system" else 0, {"role": "system", "content": handoff})

    try:
        reply = chat_completion(
            None,
            messages,
            model=model,
            temperature=base_chat_temperature,
            settings=chat_settings,
        )
    except Exception as e:
        raise HTTPException(502, f"Chat failed ({provider}): {e}") from e

    quality: dict[str, Any] = _polish_reply(reply, memory_context="", pinned_titles=[])
    if quality.get("text"):
        reply = quality["text"]

    message_id = uuid.uuid4().hex[:20]
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply, "matrix_agent": matrix_slug, "message_id": message_id, "feedback": 0, "rag_hits": rag_hits})
    sess["messages"] = history
    if sess.get("title") == "New chat":
        sess["title"] = text[:48]
    persist_session(sid, sess)
    if matrix_slug:
        record_chat_interaction(matrix_slug, f"{sid}:{message_id}")

    return {
        "ok": True,
        "reply": reply,
        "session_id": sid,
        "message_id": message_id,
        "feedback": 0,
        "memory_used": memory_used,
        "memory_recall": memory_recall,
        "rag_hits": rag_hits,
        "extract": None,
        "written": [],
        "pinned": pinned_titles,
        "provider": provider,
        "model": model,
        "matrix_agent": matrix_slug,
        "think_mode": think_plan["resolved"],
        "think_requested": think_plan["requested"],
        "think_reason": think_plan["reason"],
        "think_budget_tokens": think_plan["budget_tokens"],
        "think_native_supported": bool(native_supported),
        "think_native_detected": bool(native_detected),
        "followups": [],
        "quality": {k: quality.get(k) for k in ("issues", "unknown_citations", "fixed_citations", "confidence")},
        "quality_summary": quality_summary(quality),
        "grown_notes": [],
        "timeline": ops_log.timeline(limit=20),
    }


def _operational_payload() -> dict[str, Any]:
    """Join persisted evidence with safe registry identity for the Studio."""
    data = operational_snapshot()
    agents = data.setdefault("agents", {})
    relationships = data.setdefault("relationships", {})
    tasks = data.setdefault("tasks", {})
    relevant = {"cypra", "nexus-prime", "matrix-developer", "matrix-verifier"}
    relevant.update(core_models(_settings))
    # Registry selection alone does not materialize hundreds of available personas.
    # A non-core agent hydrates only after a completed response or recorded task outcome.
    relevant.update(str(slug) for slug in agents)
    for key in relationships:
        relevant.update(part for part in str(key).split("|") if part)
    for task in tasks.values():
        relevant.update(str(slug) for slug in task.get("assigned_agents") or [] if slug)
        relevant.update(str(slug) for slug in task.get("collaborating_agents") or [] if slug)
        routing = task.get("routing_decision") if isinstance(task.get("routing_decision"), dict) else {}
        relevant.update(str(slug) for slug in routing.get("selected_agents") or [] if slug)
        relevant.update(str(routing.get(role) or "") for role in ("coordinator", "planner") if routing.get(role))

    roster = {str(row.get("slug") or ""): row for row in search_agents("", settings=_settings, limit=2000)}
    for slug in sorted(relevant):
        if not slug:
            continue
        row = agents.setdefault(slug, {
            "assignments": 0, "successful_assignments": 0, "failed_assignments": 0,
            "success_rate": None, "reliability": None, "confidence": 0.0,
            "evidence_count": 0, "score": None, "average_reward": None,
            "domain_scores": {}, "task_history": [], "chat_responses": 0,
            "chat_positive": 0, "chat_negative": 0, "chat_feedback_count": 0,
            "chat_score": None, "chat_confidence": 0.0,
        })
        identity = roster.get(slug) or {}
        row.update({
            "id": slug,
            "name": identity.get("name") or slug,
            "title": identity.get("label") or identity.get("name") or slug,
            "summary": identity.get("summary") or "",
            "category": identity.get("category") or "Matrix Agent",
            "type": "agent",
            "registered": bool(identity),
            "score_status": "scored" if row.get("score") is not None else "unscored",
        })

    dangling = []
    scored = 0
    for key, row in relationships.items():
        endpoints = [part for part in str(key).split("|") if part]
        if len(endpoints) != 2 or any(endpoint not in agents for endpoint in endpoints):
            dangling.append(str(key))
        if row.get("scored") and row.get("strength") is not None:
            scored += 1
    data["integrity"] = {
        "ok": not dangling,
        "agents": len(agents),
        "tasks": len(tasks),
        "relationships": len(relationships),
        "scored_relationships": scored,
        "unscored_relationships": len(relationships) - scored,
        "dangling_relationships": dangling,
        "scoring": "confidence_weighted_observed_outcomes",
    }
    return data


@app.get("/api/operations")
def api_operations() -> dict[str, Any]:
    """Authoritative task, agent-performance, relationship, and recovery state."""
    return _operational_payload()

@app.get("/api/workplace")
def api_workplace_list(slug: str = "cypra") -> dict[str, Any]:
    from engine.workplace import agent_slug, list_files, workplace_dir
    s = agent_slug(slug)
    d = workplace_dir(s)
    return {"ok": True, "slug": s, "root": str(d), "files": list_files(s)}


@app.get("/api/workplace/read")
def api_workplace_read(slug: str = "cypra", path: str = "") -> dict[str, Any]:
    from engine.workplace import agent_slug, read_file
    return {"ok": True, **read_file(agent_slug(slug), path)}


@app.post("/api/workplace/write")
def api_workplace_write(body: WorkplaceBody) -> dict[str, Any]:
    # Intentionally disabled: autonomous/user-facing workplace mutation is
    # authorized only through the Chat Files execution path, which validates
    # the current user instruction before touching disk. This legacy endpoint
    # was not used by the UI and would otherwise bypass that consent check.
    raise HTTPException(403, "Direct workplace writes are disabled; use Chat Files with an explicit file-write instruction.")


@app.post("/api/workplace/open")
def api_workplace_open(body: WorkplaceBody | None = None) -> dict[str, Any]:
    from engine.workplace import agent_slug, workplace_dir
    s = agent_slug((body.slug if body else None) or "cypra")
    d = workplace_dir(s)
    try:
        os.startfile(str(d))  # type: ignore[attr-defined]
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    return {"ok": True, "root": str(d), "slug": s}


# ── RAG knowledge store ────────────────────────────────────────────


def _rag_settings_public() -> dict[str, Any]:
    return {
        "enabled": bool(_settings.get("rag_enabled", True)),
        "top_k": max(1, min(8, int(_settings.get("rag_top_k") or 4))),
        "context_chars": max(1200, min(24000, int(_settings.get("rag_context_chars") or 6000))),
        "chunk_chars": max(600, min(6000, int(_settings.get("rag_chunk_chars") or 1800))),
        "chunk_overlap": max(0, min(1200, int(_settings.get("rag_chunk_overlap") or 240))),
        "min_score": max(0.0, min(20.0, float(_settings.get("rag_min_score") or 0.25))),
    }


def _rag_ingest_file_bytes(file_name: str, raw: bytes) -> dict[str, Any]:
    """Index a Review-compatible local file without invoking the LLM.

    This intentionally reuses the local Review extractor so Review and RAG
    agree on PDF/DOCX/XLSX/text handling. The original binary is not copied
    into the index; only extracted text plus source metadata is persisted.
    """
    name = Path(file_name or "knowledge.txt").name
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(413, "Knowledge file is too large (10 MB maximum).")
    if not raw:
        raise HTTPException(400, "Knowledge file is empty (0 bytes).")

    extracted, _ext = _review_file_text(name, raw)
    extracted = str(extracted or "").strip()
    if not extracted:
        raise HTTPException(422, f"No readable text was found in {name}.")
    try:
        return rag_store.add_text(
            name,
            extracted,
            byte_count=len(raw),
            chunk_chars=int(_settings.get("rag_chunk_chars") or 1800),
            overlap=int(_settings.get("rag_chunk_overlap") or 240),
            kind="file",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _rag_file_result(result: dict[str, Any]) -> dict[str, Any]:
    return {**result, "stats": rag_store.stats(), "documents": rag_store.list_sources()}


@app.get("/api/rag/status")
def api_rag_status() -> dict[str, Any]:
    return {
        "ok": True,
        "settings": _rag_settings_public(),
        "stats": rag_store.stats(),
        "documents": rag_store.list_sources(),
    }


@app.get("/api/rag/documents")
def api_rag_documents() -> dict[str, Any]:
    return {"ok": True, "documents": rag_store.list_sources(), "stats": rag_store.stats()}


def _valid_rag_source_id(source_id: str) -> str:
    sid = str(source_id or "").strip()
    if not re.fullmatch(r"rag-[0-9a-f]{16}", sid):
        raise HTTPException(400, "Invalid RAG source id.")
    return sid


@app.get("/api/rag/documents/{source_id}")
def api_rag_document_detail(source_id: str) -> dict[str, Any]:
    sid = _valid_rag_source_id(source_id)
    detail = rag_store.source_detail(sid, max_chars=16000)
    if not detail:
        raise HTTPException(404, "RAG source not found.")
    return {"ok": True, **detail}


@app.patch("/api/rag/documents/{source_id}")
def api_rag_document_update(source_id: str, body: RAGSourceUpdateRequest) -> dict[str, Any]:
    sid = _valid_rag_source_id(source_id)
    patch = body.model_dump(exclude_none=True)
    try:
        result = rag_store.update_source(sid, patch)
    except KeyError as exc:
        raise HTTPException(404, "RAG source not found.") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**result, "stats": rag_store.stats(), "documents": rag_store.list_sources()}


@app.post("/api/rag/documents/{source_id}/reindex")
def api_rag_document_reindex(source_id: str) -> dict[str, Any]:
    sid = _valid_rag_source_id(source_id)
    try:
        result = rag_store.reindex_source(
            sid,
            chunk_chars=int(_settings.get("rag_chunk_chars") or 1800),
            overlap=int(_settings.get("rag_chunk_overlap") or 240),
        )
    except KeyError as exc:
        raise HTTPException(404, "RAG source not found.") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f"Could not reindex source: {exc}") from exc
    return {**result, "stats": rag_store.stats(), "documents": rag_store.list_sources()}


@app.post("/api/rag/upload")
async def api_rag_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    raw = await file.read(MAX_FILE_BYTES + 1)
    result = _rag_ingest_file_bytes(file.filename or "knowledge.txt", raw)
    return _rag_file_result(result)


@app.post("/api/rag/content")
def api_rag_content(body: RAGContentRequest) -> dict[str, Any]:
    """Index a local path or browser-provided file payload without reviewing it."""
    name = Path(body.name or "knowledge.txt").name
    if str(body.path or "").strip():
        p = Path(body.path).expanduser().resolve()
        if not p.is_file():
            raise HTTPException(404, f"File not found: {p.name}")
        try:
            size = p.stat().st_size
        except OSError as exc:
            raise HTTPException(404, f"Could not read file metadata: {p.name}") from exc
        if size > MAX_FILE_BYTES:
            raise HTTPException(413, "Knowledge file is too large (10 MB maximum).")
        name = p.name
        try:
            raw = p.read_bytes()
        except OSError as exc:
            raise HTTPException(422, f"Could not read file: {p.name}") from exc
    elif body.text is not None:
        raw = str(body.text).encode("utf-8")
    elif body.content_b64:
        import base64
        try:
            raw = base64.b64decode(body.content_b64, validate=True)
        except Exception as exc:
            raise HTTPException(400, "Invalid base64 file payload.") from exc
    else:
        raise HTTPException(400, "No file content was provided.")

    result = _rag_ingest_file_bytes(name, raw)
    return _rag_file_result(result)


@app.post("/api/rag/text")
def api_rag_text(body: RAGTextRequest) -> dict[str, Any]:
    if not str(body.text or "").strip():
        raise HTTPException(400, "Knowledge text is empty.")
    try:
        result = rag_store.add_text(
            body.name or "knowledge.txt",
            body.text,
            chunk_chars=int(_settings.get("rag_chunk_chars") or 1800),
            overlap=int(_settings.get("rag_chunk_overlap") or 240),
            kind="manual",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**result, "stats": rag_store.stats(), "documents": rag_store.list_sources()}


def _rag_chat_source_name(text: str, role: str, label: str = "") -> str:
    clean_label = re.sub(r"\s+", " ", str(label or "")).strip()[:72]
    if clean_label:
        stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", clean_label).strip("-._")[:54] or "knowledge"
    else:
        preview = re.sub(r"\s+", " ", str(text or "")).strip()[:56]
        stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", preview).strip("-._")[:42] or "knowledge"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"chat-{role}-{stamp}-{stem}.txt"


@app.post("/api/rag/chat-knowledge")
def api_rag_chat_knowledge(body: RAGChatKnowledgeRequest) -> dict[str, Any]:
    text = str(body.text or "").strip()
    if not text:
        raise HTTPException(400, "Chat knowledge text is empty.")
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise HTTPException(413, "Chat knowledge is too large (10 MB maximum).")
    role = str(body.role or "user").strip().lower()
    if role not in {"user", "assistant"}:
        role = "user"
    name = _rag_chat_source_name(text, role, body.label)
    try:
        result = rag_store.add_text(
            name,
            text,
            chunk_chars=int(_settings.get("rag_chunk_chars") or 1800),
            overlap=int(_settings.get("rag_chunk_overlap") or 240),
            kind="chat",
            origin_role=role,
            label=str(body.label or ""),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**result, "stats": rag_store.stats(), "documents": rag_store.list_sources()}


@app.post("/api/rag/clear")
def api_rag_clear() -> dict[str, Any]:
    result = rag_store.clear()
    return {"ok": True, **result, "stats": rag_store.stats(), "documents": rag_store.list_sources()}


@app.post("/api/rag/search")
def api_rag_search(body: RAGSearchRequest) -> dict[str, Any]:
    query = str(body.query or "").strip()
    if not query:
        raise HTTPException(400, "Search query is empty.")
    threshold = float(body.min_score) if body.min_score is not None else float(_settings.get("rag_min_score") or 0.25)
    hits = rag_store.search(
        query,
        limit=max(1, min(12, int(body.limit or 4))),
        min_score=max(0.0, min(20.0, threshold)),
    )
    public_hits = [
        {k: hit.get(k) for k in (
            "id", "source_id", "source", "source_name", "chunk", "score", "base_score",
            "coverage", "matched_terms", "pinned", "group", "tags", "snippet",
        )}
        for hit in hits
    ]
    return {"ok": True, "query": query, "min_score": threshold, "hits": public_hits}


@app.post("/api/rag/reindex")
def api_rag_reindex() -> dict[str, Any]:
    result = rag_store.rebuild(
        chunk_chars=int(_settings.get("rag_chunk_chars") or 1800),
        overlap=int(_settings.get("rag_chunk_overlap") or 240),
    )
    return {"ok": True, **result, "stats": rag_store.stats(), "documents": rag_store.list_sources()}


@app.delete("/api/rag/documents/{source_id}")
def api_rag_delete(source_id: str) -> dict[str, Any]:
    sid = _valid_rag_source_id(source_id)
    if not rag_store.remove(sid):
        raise HTTPException(404, "RAG source not found.")
    return {"ok": True, "removed": sid, "stats": rag_store.stats(), "documents": rag_store.list_sources()}


@app.get("/api/rag/bundle/export")
def api_rag_bundle_export() -> dict[str, Any]:
    try:
        return rag_store.export_bundle()
    except ValueError as exc:
        raise HTTPException(413, str(exc)) from exc


@app.post("/api/rag/bundle/export-file")
def api_rag_bundle_export_file() -> dict[str, Any]:
    try:
        bundle = rag_store.export_bundle()
    except ValueError as exc:
        raise HTTPException(413, str(exc)) from exc
    export_dir = ROOT / "MatrixFiles" / "Exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    unique = uuid.uuid4().hex[:6]
    path = export_dir / f"cypra-rag-knowledge-{stamp}-{unique}.json"
    encoded = json.dumps(bundle, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(encoded, encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "path": str(path), "filename": path.name, "bytes": len(encoded.encode("utf-8")), "bundle": bundle}


@app.post("/api/rag/bundle/import")
def api_rag_bundle_import(body: RAGBundleImportRequest) -> dict[str, Any]:
    payload = body.model_dump()
    try:
        result = rag_store.import_bundle(
            payload,
            chunk_chars=int(_settings.get("rag_chunk_chars") or 1800),
            overlap=int(_settings.get("rag_chunk_overlap") or 240),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**result, "stats": rag_store.stats(), "documents": rag_store.list_sources()}


@app.post("/api/rag/folder/open")
def api_rag_folder_open() -> dict[str, Any]:
    RAG_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(str(RAG_ROOT))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(RAG_ROOT)])
        else:
            subprocess.Popen(["xdg-open", str(RAG_ROOT)])
    except Exception as exc:
        raise HTTPException(500, f"Could not open RAG folder: {exc}") from exc
    return {"ok": True, "path": str(RAG_ROOT)}


@app.post("/api/query")
def api_query(body: QueryRequest) -> dict[str, Any]:
    raise HTTPException(410, "Vault query is disabled in CypraMatrixStudio; use normal chat instead.")


@app.post("/api/sessions/new")
def api_new_session() -> dict[str, Any]:
    """Start a fresh chat thread. Does not wipe the vault."""
    sid, sess = get_session(None)
    persist_session(sid, sess)
    return {"ok": True, "session_id": sid, "session": sess}


@app.get("/api/sessions")
def list_sessions() -> dict[str, Any]:
    items = []
    for path in sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: -p.stat().st_mtime):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            msgs = data.get("messages") or []
            preview = ""
            for m in reversed(msgs):
                chunk = str((m or {}).get("content") or "").strip().replace("\n", " ")
                if chunk:
                    preview = chunk[:140]
                    break
            items.append(
                {
                    "id": data.get("id") or path.stem,
                    "title": data.get("title") or "Chat",
                    "messages": len(msgs),
                    "pinned": data.get("pinned") or [],
                    "favorite": bool(data.get("favorite", False)),
                    "archived": bool(data.get("archived", False)),
                    "tags": list(data.get("tags") or []),
                    "mtime": path.stat().st_mtime,
                    "preview": preview,
                    "agent": next(
                        (
                            str(m.get("matrix_agent") or "")
                            for m in reversed(msgs)
                            if isinstance(m, dict) and m.get("matrix_agent")
                        ),
                        "",
                    ),
                }
            )
        except (OSError, json.JSONDecodeError):
            continue
    return {"sessions": items[:400]}


@app.post("/api/sessions/folder/open")
def open_sessions_folder() -> dict[str, Any]:
    """Open the project-local isolated chat-session folder in the OS file browser."""
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            subprocess.Popen(["explorer.exe", str(SESSIONS_DIR)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(SESSIONS_DIR)])
        else:
            subprocess.Popen(["xdg-open", str(SESSIONS_DIR)])
    except Exception as e:
        raise HTTPException(500, f"Could not open chat folder: {e}") from e
    return {"ok": True, "path": str(SESSIONS_DIR)}


@app.post("/api/sessions/open-folder")
def open_sessions_folder_alias() -> dict[str, Any]:
    """Stable alias for clients that need a dedicated folder action route."""
    return open_sessions_folder()


@app.post("/api/sessions/bulk-delete")
def bulk_delete_sessions(body: dict[str, Any]) -> dict[str, Any]:
    """Delete only the explicitly selected saved chat-session files."""
    ids = body.get("ids") if isinstance(body, dict) else []
    if not isinstance(ids, list):
        raise HTTPException(400, "Session ids must be a list")
    safe_ids = []
    for raw in ids:
        sid = str(raw or "").strip()
        if sid and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", sid) and sid not in safe_ids:
            safe_ids.append(sid)
    deleted = []
    missing = []
    for sid in safe_ids:
        path = SESSIONS_DIR / f"{sid}.json"
        if not path.is_file():
            missing.append(sid)
            _sessions.pop(sid, None)
            continue
        try:
            path.unlink()
            deleted.append(sid)
            _sessions.pop(sid, None)
        except OSError as e:
            raise HTTPException(500, f"Could not delete chat session {sid}: {e}") from e
    return {"ok": True, "deleted": deleted, "missing": missing, "deleted_count": len(deleted)}


@app.get("/api/sessions/{sid}")
def get_session_api(sid: str) -> dict[str, Any]:
    _, sess = get_session(sid)
    return _public_session(sid, sess)


class ChatFeedbackBody(BaseModel):
    message_id: str = Field(min_length=1, max_length=80)
    sentiment: int = Field(ge=-1, le=1)


@app.post("/api/sessions/{sid}/feedback")
def rate_session_reply(sid: str, body: ChatFeedbackBody) -> dict[str, Any]:
    """Apply one reversible user vote to the agent that authored a saved reply."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", sid):
        raise HTTPException(400, "Invalid session id")
    path = SESSIONS_DIR / f"{sid}.json"
    session = _sessions.get(sid)
    if session is None:
        if not path.is_file():
            raise HTTPException(404, "Chat session not found")
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(500, f"Could not read chat session: {exc}") from exc
        _sessions[sid] = session
    target: dict[str, Any] | None = None
    for index, message in enumerate(session.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "assistant":
            if _chat_message_id(sid, index, message) == body.message_id:
                target = message
                break
    if target is None:
        raise HTTPException(404, "Assistant reply not found")
    slug = str(target.get("matrix_agent") or "").strip().lower()
    if not slug:
        raise HTTPException(409, "This legacy reply has no attributable agent")
    response_key = f"{sid}:{body.message_id}"
    record_chat_interaction(slug, response_key)
    result = record_chat_feedback(response_key, slug, int(body.sentiment))
    target["message_id"] = body.message_id
    target["feedback"] = int(body.sentiment)
    persist_session(sid, session)
    return {"ok": True, **result}


@app.patch("/api/sessions/{sid}")
def rename_session_api(sid: str, body: dict[str, Any]) -> dict[str, Any]:
    """Rename a saved chat session without changing its messages/context."""
    path = SESSIONS_DIR / f"{sid}.json"
    if not path.is_file():
        raise HTTPException(404, "Chat session not found")
    try:
        sess = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(500, f"Could not read chat session: {e}") from e
    title = str((body or {}).get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Session title required")
    title = re.sub(r"\s+", " ", title)[:80]
    sess["title"] = title
    if isinstance(body, dict):
        if "favorite" in body: sess["favorite"] = bool(body.get("favorite"))
        if "archived" in body: sess["archived"] = bool(body.get("archived"))
        if "tags" in body and isinstance(body.get("tags"), list):
            sess["tags"] = [re.sub(r"\s+", " ", str(x).strip())[:24] for x in body.get("tags") if str(x).strip()][:12]
    try:
        path.write_text(json.dumps(sess, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"Could not save chat session: {e}") from e
    return {"ok": True, "session": sess}


@app.post("/api/sessions/{sid}/update")
def update_session_api(sid: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST alias for session metadata updates; transcript/context is untouched."""
    return rename_session_api(sid, body)


@app.delete("/api/sessions/{sid}")
def delete_session_api(sid: str) -> dict[str, Any]:
    """Delete one saved chat session only."""
    path = SESSIONS_DIR / f"{sid}.json"
    if not path.is_file():
        raise HTTPException(404, "Chat session not found")
    try:
        path.unlink()
    except OSError as e:
        raise HTTPException(500, f"Could not delete chat session: {e}") from e
    return {"ok": True, "session_id": sid}


# ── ingest ──────────────────────────────────────────────────────────


@app.post("/api/ingest")
def api_ingest(body: IngestRequest) -> dict[str, Any]:
    raise HTTPException(410, "Legacy ingest is disabled in this clean CypraMatrixStudio baseline.")


# ── voice ───────────────────────────────────────────────────────────


@app.post("/api/stt")
async def api_stt(file: UploadFile = File(...)) -> dict[str, Any]:
    key = require_key()
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty audio")
    try:
        text = speech_to_text(key, data, filename=file.filename or "audio.webm")
    except Exception as e:
        raise HTTPException(502, f"STT failed: {e}") from e
    return {"ok": True, "text": text}


@app.post("/api/tts")
def api_tts(body: TTSRequest) -> Response:
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Empty text")
    key, _ = resolve_api_key(_settings, validate=False)
    requested = (body.provider or _settings.get("tts_provider") or "local").strip().lower()
    active = resolve_tts_provider(
        {
            **_settings,
            "tts_provider": requested,
            "voice_output_enabled": bool(body.preview or _settings.get("voice_output_enabled")),
        },
        has_xai_key=bool(key),
    )
    if active == "off":
        raise HTTPException(409, "Voice Output is disabled")
    if active in ("local", "edge"):
        voice = body.voice_id or (
            _settings.get("tts_edge_voice") or "en-US-AvaNeural"
            if active == "edge"
            else _settings.get("tts_local_voice") or "en_US-lessac-medium"
        )
        try:
            result = LOCAL_TTS.synthesize_result(
                text,
                provider=active,
                voice=str(voice),
                speed=float(_settings.get("tts_rate") or 1.0),
                threads=int(_settings.get("tts_cpu_threads") or 2),
                maximum=int(_settings.get("tts_max_chars") or 1000),
                skip_code=bool(_settings.get("tts_skip_code", True)),
                skip_urls=bool(_settings.get("tts_skip_urls", True)),
                replace=bool(
                    _settings.get("tts_stop_previous", True)
                    if body.replace is None
                    else body.replace
                ),
                online_allowed=bool(
                    active == "edge"
                    and _settings.get("voice_output_enabled")
                    and _settings.get("tts_provider") == "edge"
                    and _settings.get("tts_allow_online")
                ),
                fallback=str(_settings.get("tts_online_fallback") or "piper"),
                fallback_voice=str(_settings.get("tts_local_voice") or "en_US-lessac-medium"),
            )
        except TTSCancelled as exc:
            raise HTTPException(409, str(exc)) from exc
        except TimeoutError as exc:
            LOCAL_TTS.cancel(clear_queue=True)
            raise HTTPException(504, "Speech synthesis timed out") from exc
        except Exception as exc:
            raise HTTPException(503, f"TTS unavailable: {exc}") from exc
        return Response(
            content=result.audio,
            media_type=result.media_type,
            headers={
                "X-TTS-Provider": result.provider,
                "X-TTS-Engine": "edge" if result.provider == "edge" else "piper",
                "X-TTS-Device": "REMOTE" if result.provider == "edge" else "CPU",
                "Cache-Control": "no-store",
            },
        )
    if active == "browser":
        # Client should use SpeechSynthesis; return guidance JSON for callers that check status
        raise HTTPException(
            501,
            detail={
                "error": "browser_tts",
                "message": "Use browser Speech Synthesis for this provider",
                "provider": "browser",
                "rate": float(_settings.get("tts_rate") or 1.0),
                "pitch": float(_settings.get("tts_pitch") or 1.0),
            },
        )
    if not key:
        raise HTTPException(
            401,
            "No local provider key for cloud TTS. Switch TTS provider to Browser (local) in Settings.",
        )
    voice = body.voice_id or _settings.get("voice_id") or "eve"
    try:
        audio = text_to_speech(key, text, voice_id=voice)
    except Exception as e:
        raise HTTPException(502, f"TTS failed: {e}") from e
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"X-TTS-Provider": "legacy_cloud"},
    )


@app.post("/api/tts/stop")
def api_tts_stop(body: TTSStopRequest | None = None) -> dict[str, Any]:
    """Cancel queued local work; the browser simultaneously stops playback."""
    release = bool(body.release) if body else False
    LOCAL_TTS.cancel(clear_queue=True, release=release)
    return {"ok": True, "stopped": True, "released": release}


@app.get("/api/tts/status")
def api_tts_status() -> dict[str, Any]:
    key, source = resolve_api_key(_settings, validate=False)
    active = resolve_tts_provider(_settings, has_xai_key=bool(key))
    local_status = LOCAL_TTS.status()
    return {
        "ok": True,
        "provider": active,
        "configured": _settings.get("tts_provider") or "local",
        "voice_output_enabled": bool(_settings.get("voice_output_enabled")),
        "allow_online": bool(_settings.get("tts_allow_online")),
        "edge_voice": _settings.get("tts_edge_voice") or "en-US-AvaNeural",
        "online_fallback": _settings.get("tts_online_fallback") or "piper",
        "providers": TTS_PROVIDERS,
        "voices_xai": VOICES,
        "has_xai_key": bool(key),
        "key_source": source if key else None,
        "rate": float(_settings.get("tts_rate") or 1.0),
        "pitch": float(_settings.get("tts_pitch") or 1.0),
        "speak_replies": bool(_settings.get("speak_replies")),
        "speak_director": bool(_settings.get("tts_speak_director", True)),
        "speak_system": bool(_settings.get("tts_speak_system", False)),
        "local": local_status,
        "hint": (
            "Browser TTS (local, free — works with Ollama)"
            if active == "browser"
            else "Local Piper CPU voice"
            if active == "local"
            else "Microsoft Edge online voice"
            if active == "edge"
            else "Voice Output off"
            if active == "off"
            else "Cloud TTS"
        ),
    }


@app.get("/api/tts/voices/edge")
def api_tts_edge_voices() -> dict[str, Any]:
    if not (
        bool(_settings.get("voice_output_enabled"))
        and str(_settings.get("tts_provider") or "").lower() == "edge"
        and bool(_settings.get("tts_allow_online"))
    ):
        raise HTTPException(409, "Edge blocked: online TTS disabled")
    try:
        return {"ok": True, "voices": LOCAL_TTS.edge_voices(), "cached": True}
    except Exception as exc:
        raise HTTPException(503, f"Edge voice discovery unavailable: {exc}") from exc


@app.api_route("/api/llm/unload", methods=["GET", "POST"])
def api_llm_unload() -> dict[str, Any]:
    """Unload resident Ollama models from VRAM. Does not stop the Ollama process."""
    return unload_resident_models(_settings)


@app.post("/api/runtime/kill")
def api_runtime_kill() -> dict[str, Any]:
    """Kill this project's private Ollama localhost process after unloading VRAM."""
    return kill_local_runtime(_settings)


@app.post("/api/voice/session")
def api_voice_session() -> dict[str, Any]:
    key = require_key()
    try:
        secret = create_realtime_client_secret(key)
    except Exception as e:
        raise HTTPException(502, f"Realtime session failed: {e}") from e

        # strongest stored memories first for voice awareness
    titles = strongest_memory_titles(30)
    note_list = ", ".join(titles) if titles else "(empty — grow by talking)"
    instructions = (
        "You are Cypra, a local voice assistant. "
        "You may receive a bounded list of stored long-term memories. "
        "Help them capture ideas and recall what they already taught you. Speak concisely. "
        f"Strongest memories: {note_list}. "
        "When durable information matters, state it clearly without inventing stored context."
    )
    return {
        "ok": True,
        "client_secret": secret.get("client_secret") or secret,
        "voice": _settings.get("voice_id") or "eve",
        "model": _settings.get("voice_model") or "local-voice-latest",
        "instructions": instructions,
        "realtime_url": "wss://api.x.ai/v1/realtime",
    }


@app.post("/api/voice/commit")
async def api_voice_commit(
    transcript: str = Form(...),
    auto_extract: bool = Form(True),
) -> dict[str, Any]:
    text = (transcript or "").strip()
    if not text:
        raise HTTPException(400, "Empty transcript")
    if not auto_extract:
        return {"ok": True, "written": []}
    ensure_llm_ready()
    try:
        extract = extract_knowledge(
            None,
            text,
            model=resolve_extract_model(_settings),
            hint="voice conversation → shared memory",
            existing_titles=existing_titles(),
            settings=_settings,
        )
        extract = sanitize_extract(extract, settings=_settings)
        written = apply_extract_to_vault(vault, extract)
        reindex_notes(written)
        memory.touch([w["id"] for w in written if w.get("id")], amount=1.5)
    except Exception as e:
        raise HTTPException(502, f"Voice commit failed: {e}") from e
    if written:
        ops_log.record(
            "voice",
            note_ids=[w.get("id") for w in written if w.get("id")],
            note_titles=[w.get("title") for w in written if w.get("title")],
            meta={"summary": extract.get("summary")},
            undoable=True,
        )
    return {
        "ok": True,
        "summary": extract.get("summary"),
        "written": [{"id": w.get("id"), "title": w.get("title")} for w in written],
    }


# ── vaults / timeline / analytics / undo / import / inbox ────────────


@app.get("/api/vaults")
def api_list_vaults() -> dict[str, Any]:
    return {
        "active": vault_mgr.active_id(),
        "vaults": vault_mgr.list_vaults(),
        "path": str(vault_mgr.active_path()),
    }


class VaultCreate(BaseModel):
    name: str


@app.post("/api/vaults")
def api_create_vault(body: VaultCreate) -> dict[str, Any]:
    entry = vault_mgr.create(body.name)
    return {"ok": True, "vault": entry, "vaults": vault_mgr.list_vaults()}


class VaultSwitch(BaseModel):
    vault_id: str


@app.post("/api/vaults/switch")
def api_switch_vault(body: VaultSwitch) -> dict[str, Any]:
    try:
        entry = vault_mgr.switch(body.vault_id)
    except KeyError as e:
        raise HTTPException(404, f"Unknown vault {body.vault_id}") from e
    _bind_vault()
    return {
        "ok": True,
        "vault": entry,
        "stats": memory_snapshot_stats(),
    }


class ImportRequest(BaseModel):
    path: str


@app.post("/api/vaults/import")
def api_import_folder(body: ImportRequest) -> dict[str, Any]:
    src = Path(body.path).expanduser().resolve()
    try:
        src.relative_to(ROOT.resolve())
    except ValueError:
        raise HTTPException(403, "Vault import path must remain inside the Cypra project root.")

    try:
        result = vault_mgr.import_markdown_folder(src, vault)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e)) from e
    memory.rebuild_from_vault(vault)
    ops_log.record(
        "import",
        note_titles=[],
        note_ids=[],
        meta=result,
        undoable=False,
    )
    return {"ok": True, **result}


@app.get("/api/timeline")
def api_timeline(limit: int = 40) -> dict[str, Any]:
    return {"events": ops_log.timeline(limit=limit), "growth": ops_log.growth(limit=80)}


@app.get("/api/sessions/{sid}/grown")
def api_session_grown(sid: str) -> dict[str, Any]:
    _, sess = get_session(sid)
    ids = list(sess.get("grown_notes") or [])
    notes = []
    for nid in ids:
        n = vault.read_note(nid)
        if n:
            notes.append({"id": n["id"], "title": n.get("title"), "type": n.get("type")})
    return {"session_id": sid, "grown": notes}


@app.post("/api/ops/undo")
def api_undo_last() -> dict[str, Any]:
    op = ops_log.last_undoable()
    if not op:
        return {"ok": False, "error": "Nothing to undo"}
    # Delete notes that were newly created in this op
    deleted = []
    for item in op.get("snapshot") or []:
        if item.get("created") and item.get("id"):
            if vault.delete_note(item["id"]):
                deleted.append(item["id"])
                embed_store.drop(item["id"])
    # Restore pre-existing note content if we snapshotted it
    for item in op.get("snapshot") or []:
        if item.get("existed") and item.get("content") and item.get("title"):
            vault.upsert_note(
                item["title"],
                item["content"],
                note_type=item.get("type") or "concept",
                tags=item.get("tags") or [],
                links=item.get("links") or [],
                merge=False,
            )
    ops_log.mark_undone(op["id"])
    memory.rebuild_from_vault(vault)
    return {
        "ok": True,
        "undone": op["id"],
        "deleted": deleted,
        "timeline": ops_log.timeline(20),
    }


@app.get("/api/analytics")
def api_analytics() -> dict[str, Any]:
    return analyze_vault(vault, memory)


@app.get("/api/health/full")
def api_health_full() -> dict[str, Any]:
    llm = provider_status(_settings)
    hygiene = vault_health(vault, memory, embed_store, _settings)
    return {
        "ok": True,
        "version": APP_VERSION,
        "vault": {
            "id": vault_mgr.active_id(),
            "path": str(vault_mgr.active_path()),
            "notes": len(vault.list_notes()),
        },
        "llm": llm,
        "memory": memory.stats(),
        "embeddings": embed_store.stats(),
        "ops": len(ops_log.ops),
        "settings_provider": get_provider(_settings),
        "hygiene": hygiene,
    }


class PruneJunkBody(BaseModel):
    dry_run: bool = False
    merge_aliases: bool = True


@app.get("/api/vault/hygiene")
def api_vault_hygiene() -> dict[str, Any]:
    return {"ok": True, **vault_health(vault, memory, embed_store, _settings)}


# ── plugins (GitHub install + enable/disable/remove) ─────────────────


class PluginGithubBody(BaseModel):
    source: str = ""  # owner/repo or github URL
    ref: str | None = None
    force: bool = False


class PluginLocalBody(BaseModel):
    path: str = ""  # relative to project root or absolute
    force: bool = False


class PluginEnableBody(BaseModel):
    enabled: bool = True


@app.get("/api/plugins")
def api_plugins_list() -> dict[str, Any]:
    return {
        "ok": True,
        "plugins": plugin_mgr.list_plugins(),
        "assets": plugin_mgr.client_assets(),
        "bundled_example": str(BUNDLED_PLUGINS / "hello-status"),
        "plugins_dir": str(PLUGINS_DIR),
    }


@app.post("/api/plugins/install-github")
def api_plugins_install_github(body: PluginGithubBody) -> dict[str, Any]:
    source = (body.source or "").strip()
    if not source:
        raise HTTPException(400, "source required (owner/repo or GitHub URL)")
    try:
        result = plugin_mgr.install_from_github(
            source, ref=body.ref, force=bool(body.force)
        )
    except FileExistsError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(502, f"GitHub install failed: {e}") from e
    # load if enabled
    try:
        plugin_mgr.load_enabled(
            {"settings": _settings, "data": DATA, "root": ROOT, "vault_mgr": vault_mgr}
        )
        plugin_mgr.emit("startup")
    except Exception:
        pass
    return {
        **result,
        "plugins": plugin_mgr.list_plugins(),
        "assets": plugin_mgr.client_assets(),
    }


@app.post("/api/plugins/install-local")
def api_plugins_install_local(body: PluginLocalBody) -> dict[str, Any]:
    raw = (body.path or "").strip()
    if not raw:
        raise HTTPException(400, "path required")
    path = Path(raw)
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    try:
        if path.is_file() and path.suffix.lower() == ".zip":
            result = plugin_mgr.install_from_zip_path(
                path, force=bool(body.force), source=f"zip:{path.name}"
            )
        elif path.is_dir():
            result = plugin_mgr.install_from_folder(
                path, force=bool(body.force), source=f"folder:{path}"
            )
        else:
            raise HTTPException(400, f"Not a plugin folder or zip: {path}")
    except FileExistsError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Local install failed: {e}") from e
    try:
        plugin_mgr.load_enabled(
            {"settings": _settings, "data": DATA, "root": ROOT, "vault_mgr": vault_mgr}
        )
        plugin_mgr.emit("startup")
    except Exception:
        pass
    return {
        **result,
        "plugins": plugin_mgr.list_plugins(),
        "assets": plugin_mgr.client_assets(),
    }


@app.post("/api/plugins/install-example")
def api_plugins_install_example(force: bool = True) -> dict[str, Any]:
    """Install the bundled hello-status example plugin."""
    example = BUNDLED_PLUGINS / "hello-status"
    if not example.is_dir():
        raise HTTPException(404, "Bundled example missing")
    try:
        result = plugin_mgr.install_from_folder(
            example, force=force, source="bundled:hello-status"
        )
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    try:
        plugin_mgr.load_enabled(
            {"settings": _settings, "data": DATA, "root": ROOT, "vault_mgr": vault_mgr}
        )
        plugin_mgr.emit("startup")
    except Exception:
        pass
    return {
        **result,
        "plugins": plugin_mgr.list_plugins(),
        "assets": plugin_mgr.client_assets(),
    }


@app.post("/api/plugins/{plugin_id}/enable")
def api_plugins_enable(plugin_id: str, body: PluginEnableBody | None = None) -> dict[str, Any]:
    opts = body or PluginEnableBody()
    try:
        entry = plugin_mgr.set_enabled(plugin_id, bool(opts.enabled))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    if opts.enabled:
        try:
            plugin_mgr.load_enabled(
                {
                    "settings": _settings,
                    "data": DATA,
                    "root": ROOT,
                    "vault_mgr": vault_mgr,
                }
            )
        except Exception:
            pass
    return {
        "ok": True,
        "plugin": entry,
        "plugins": plugin_mgr.list_plugins(),
        "assets": plugin_mgr.client_assets(),
    }


@app.delete("/api/plugins/{plugin_id}")
def api_plugins_remove(plugin_id: str) -> dict[str, Any]:
    try:
        result = plugin_mgr.remove(plugin_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return {
        **result,
        "plugins": plugin_mgr.list_plugins(),
        "assets": plugin_mgr.client_assets(),
    }


@app.get("/api/plugins/{plugin_id}/file/{file_path:path}")
def api_plugins_file(plugin_id: str, file_path: str) -> Response:
    """Serve a plugin static file (js/css) for enabled plugins."""
    entry = plugin_mgr.get(plugin_id)
    if not entry or not entry.get("enabled"):
        raise HTTPException(404, "Plugin not found or disabled")
    path = plugin_mgr.resolve_file(plugin_id, file_path)
    if not path:
        raise HTTPException(404, "File not found")
    media = "text/plain"
    if path.suffix == ".js":
        media = "application/javascript"
    elif path.suffix == ".css":
        media = "text/css"
    elif path.suffix == ".json":
        media = "application/json"
    elif path.suffix == ".html":
        media = "text/html"
    return Response(path.read_bytes(), media_type=media)


@app.post("/api/vault/prune-junk")
def api_vault_prune_junk(body: PruneJunkBody | None = None) -> dict[str, Any]:
    """Merge seed aliases + delete thin auto-junk notes. Does not wipe the vault."""
    opts = body or PruneJunkBody()
    result = prune_junk_notes(
        vault,
        memory,
        embed_store,
        dry_run=bool(opts.dry_run),
        merge_aliases=bool(opts.merge_aliases),
    )
    return {
        **result,
        "health": vault_health(vault, memory, embed_store, _settings),
    }


@app.post("/api/vault/restore-seeds")
def api_restore_seeds() -> dict[str, Any]:
    """Re-write starter example notes without wiping other vault content."""
    result = vault.restore_seed_notes(overwrite=True)
    memory.rebuild_from_vault(vault)
    if _settings.get("use_embeddings", True):
        for title in result.get("restored") or []:
            full = vault.read_note(title)
            if not full:
                continue
            try:
                embed_store.ensure_note(
                    full["id"],
                    f"{full.get('title')}\n{full.get('description') or ''}\n{full.get('body') or ''}",
                    settings=_settings,
                    force=True,
                )
            except Exception:
                pass
    return {"ok": True, **result}


@app.post("/api/llm/warm")
def api_llm_warm() -> dict[str, Any]:
    """Start a non-blocking Ollama warm. The UI polls /api/llm/warm/status."""
    result = start_background_warm(_settings, "chat")
    chat_m = resolve_chat_model(_settings)
    extract_m = resolve_extract_model(_settings)
    return {
        **result,
        "chat_model": chat_m,
        "extract_model": extract_m,
        "same_model": chat_m == extract_m,
        "num_ctx": _settings.get("ollama_num_ctx"),
    }


@app.get("/api/llm/warm/status")
def api_llm_warm_status() -> dict[str, Any]:
    """Return the latest non-blocking model warm status."""
    st = warm_status()
    st["chat_model"] = resolve_chat_model(_settings)
    st["extract_model"] = resolve_extract_model(_settings)
    st["same_model"] = st["chat_model"] == st["extract_model"]
    return st


@app.post("/api/repair")
def api_repair() -> dict[str, Any]:
    pruned = prune_shared_memory(force=True)
    mem_stats = memory.rebuild_from_vault(vault)
    pruned2 = prune_shared_memory(force=True)
    emb = 0
    if _settings.get("use_embeddings", True):
        for meta in vault.list_notes():
            full = vault.read_note(meta["id"])
            if not full:
                continue
            try:
                embed_store.ensure_note(
                    full["id"],
                    f"{full.get('title')}\n{full.get('body') or ''}",
                    settings=_settings,
                    force=False,
                )
                emb += 1
            except Exception:
                pass
    return {
        "ok": True,
        "pruned": pruned,
        "pruned_after": pruned2,
        "reindexed": mem_stats,
        "embeddings_touched": emb,
        "analytics": analyze_vault(vault, memory),
    }


@app.post("/api/inbox/scan")
def api_inbox_scan(auto_ingest: bool = True) -> dict[str, Any]:
    items = inbox_watch.scan(vault.inbox)
    written_all: list[dict[str, Any]] = []
    if auto_ingest and items:
        ensure_llm_ready()
        for it in items:
            try:
                extract = extract_knowledge(
                    None,
                    it["text"],
                    model=resolve_extract_model(_settings),
                    hint=f"inbox:{it['name']}",
                    existing_titles=existing_titles(),
                    settings=_settings,
                )
                extract = sanitize_extract(extract, settings=_settings)
                written = apply_extract_to_vault(vault, extract)
                reindex_notes(written)
                written_all.extend(written)
                inbox_watch.mark(it["name"], it["hash"])
                ops_log.record(
                    "inbox",
                    note_ids=[w.get("id") for w in written if w.get("id")],
                    note_titles=[w.get("title") for w in written if w.get("title")],
                    meta={"file": it["name"]},
                    undoable=True,
                )
            except Exception as e:
                return {
                    "ok": False,
                    "error": str(e),
                    "pending": items,
                    "written": [{"id": w.get("id"), "title": w.get("title")} for w in written_all],
                }
    elif not auto_ingest:
        return {"ok": True, "pending": items, "written": []}
    else:
        for it in items:
            inbox_watch.mark(it["name"], it["hash"])
    return {
        "ok": True,
        "scanned": len(items),
        "written": [{"id": w.get("id"), "title": w.get("title")} for w in written_all],
    }


@app.post("/api/rollup")
def api_rollup(period: str = "day") -> dict[str, Any]:
    """Create/update a Maps-of-Content rollup note (local date, day or week)."""
    from datetime import datetime, timedelta

    period = (period or "day").strip().lower()
    now = datetime.now().astimezone()
    day = now.strftime("%Y-%m-%d")
    growth = ops_log.growth(limit=80)
    titles: list[str] = []
    if period == "week":
        start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        label = f"Week of {start}"
        note_title = f"Memory Rollup Week {start}"
        for ev in growth:
            at = (ev.get("at") or "")[:10]
            if at >= start and ev.get("title"):
                titles.append(ev["title"])
        tags = ["rollup", "moc", "week", start]
    else:
        label = day
        note_title = f"Memory Rollup {day}"
        for ev in growth:
            if (ev.get("at") or "").startswith(day) and ev.get("title"):
                titles.append(ev["title"])
        tags = ["rollup", "moc", day]
    titles = list(dict.fromkeys(titles))[:40]
    if not titles:
        for n in vault.list_notes()[:12]:
            titles.append(n.get("title") or n["id"])
    body_lines = [
        f"# Memory Rollup · {label}",
        "",
        f"Auto-generated map of content for **{label}** (local time).",
        "",
        "## New & active notes",
        "",
    ]
    for t in titles:
        body_lines.append(f"- [[{t}]]")
    body_lines += ["", f"Related: {', '.join(f'[[{t}]]' for t in titles[:8])}", ""]
    note = vault.upsert_note(
        note_title,
        "\n".join(body_lines),
        note_type="meta",
        tags=tags,
        links=titles[:20],
        merge=False,
    )
    reindex_notes([note])
    ops_log.record(
        "rollup",
        note_ids=[note.get("id")],
        note_titles=[note.get("title")],
        meta={"day": day, "period": period, "count": len(titles)},
    )
    return {"ok": True, "note": note, "linked": titles, "period": period}


@app.get("/api/ollama/status")
def api_ollama_status() -> dict[str, Any]:
    """Lightweight Ollama reachability + configured models for status chips."""
    base = (_settings.get("ollama_base_url") or "http://127.0.0.1:11434").rstrip("/")
    chat = _settings.get("ollama_chat_model") or ""
    extract = _settings.get("ollama_extract_model") or chat
    online = False
    models: list[str] = []
    err = None
    try:
        r = requests.get(f"{base}/api/tags", timeout=2.5)
        if r.ok:
            online = True
            data = r.json() or {}
            models = [m.get("name") for m in (data.get("models") or []) if m.get("name")]
        else:
            err = f"HTTP {r.status_code}"
    except Exception as e:
        err = str(e)
    return {
        "ok": True,
        "online": online,
        "base_url": base,
        "chat_model": chat,
        "extract_model": extract,
        "same_model": chat == extract,
        "num_ctx": _settings.get("ollama_num_ctx"),
        "models": models[:40],
        "warm": False,  # client may set after warm call
        "error": err,
    }


@app.get("/api/embed/status")
def api_embed_status() -> dict[str, Any]:
    notes = vault.list_notes()
    n_ids = {n.get("id") for n in notes if n.get("id")}
    emb = embed_store.stats() if hasattr(embed_store, "stats") else {}
    embedded = int(emb.get("embedded") or 0)
    vectors = getattr(embed_store, "vectors", None)
    if isinstance(vectors, dict) and n_ids:
        live = set(vectors.keys()) & n_ids
        embedded = len(live)
    missing = max(0, len(n_ids) - embedded)
    return {
        "ok": True,
        "notes": len(n_ids),
        "embedded": embedded,
        "missing": missing,
        "model": _settings.get("embed_model") or emb.get("model"),
        "enabled": _settings.get("use_embeddings", True),
    }


@app.get("/api/obsidian/info")
def api_obsidian_info() -> dict[str, Any]:
    path = str(vault.root)
    wiki = vault.root / "wiki"
    return {
        "ok": True,
        "vault_path": path,
        "wiki_path": str(wiki),
        "hint": (
            f"In Obsidian: Open folder as vault → select:\n{path}\n"
            "Notes live under wiki/ as markdown with [[wikilinks]]."
        ),
        "export_obsidian_hint": vault.export_obsidian_hint()
        if hasattr(vault, "export_obsidian_hint")
        else None,
    }


@app.post("/api/brief")
def api_daily_brief() -> dict[str, Any]:
    """Create a Daily Brief note from sticky pins + recent / strong notes (no LLM required)."""
    from datetime import datetime

    now = datetime.now().astimezone()
    day = now.strftime("%Y-%m-%d")
    pins = list(_settings.get("sticky_pins") or [])
    titles: list[str] = []
    for p in pins:
        titles.append(str(p))
    # recent growth
    for ev in ops_log.growth(limit=30):
        t = ev.get("title")
        if t:
            titles.append(t)
    # strongest indexed
    try:
        strong = sorted(
            vault.list_notes(),
            key=lambda n: float(n.get("strength") or 0) + float(n.get("hits") or 0) * 0.1,
            reverse=True,
        )[:10]
        for n in strong:
            titles.append(n.get("title") or n.get("id"))
    except Exception:
        pass
    titles = list(dict.fromkeys([t for t in titles if t]))[:24]
    lines = [
        f"# Daily Brief · {day}",
        "",
        f"Generated {now.strftime('%Y-%m-%d %H:%M %Z')} — sticky hubs + recent/active notes.",
        "",
        "## Sticky hubs",
        "",
    ]
    if pins:
        for p in pins:
            lines.append(f"- [[{p}]]")
    else:
        lines.append("- _(no sticky pins — set in Settings → AI & memory)_")
    lines += ["", "## Focus today", ""]
    for t in titles[:16]:
        if t not in pins:
            lines.append(f"- [[{t}]]")
    lines += [
        "",
        "## Prompts",
        "",
        "- What changed since yesterday?",
        "- Which hub needs a decision?",
        "- What should long-term memory retain?",
        "",
    ]
    note = vault.upsert_note(
        f"Daily Brief {day}",
        "\n".join(lines),
        note_type="meta",
        tags=["brief", "daily", day],
        links=titles[:20],
        merge=False,
    )
    reindex_notes([note])
    summary = f"Brief · {len(pins)} pins · {len(titles)} linked"
    return {
        "ok": True,
        "note": note,
        "summary": summary,
        "pins": pins,
        "linked": titles,
    }


@app.get("/api/onboarding")
def api_onboarding() -> dict[str, Any]:
    notes = len(vault.list_notes())
    llm = provider_status(_settings)
    return {
        "needs_onboarding": not _settings.get("onboarding_done"),
        "notes": notes,
        "llm_ok": bool(llm.get("ok")),
        "provider": llm.get("provider"),
        "vault_id": vault_mgr.active_id(),
        "steps": [
            {"id": "provider", "label": "Choose LLM (local provider / Ollama / Hybrid)", "done": bool(llm.get("ok"))},
            {"id": "chat", "label": "Send a first message", "done": notes > 6},
            {"id": "pin", "label": "Pin a memory note", "done": False},
            {"id": "capture", "label": "Capture clipboard once", "done": False},
        ],
    }


class OnboardingDone(BaseModel):
    done: bool = True


@app.post("/api/onboarding")
def api_onboarding_done(body: OnboardingDone) -> dict[str, Any]:
    _settings["onboarding_done"] = body.done
    save_settings(SETTINGS_PATH, _settings)
    return {"ok": True}


try:
    start_background_warm(_settings, "chat")
except Exception:
    pass
