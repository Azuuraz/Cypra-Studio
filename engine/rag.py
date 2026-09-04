"""Portable project-local Retrieval-Augmented Generation (RAG) store.

RAG is deliberately separate from legacy Cypra Memory/vault state.  Sources are
stored as normalized text plus metadata under MatrixFiles/RAG and retrieved with
CPU-only BM25.  No embedding model is loaded, so retrieval cannot consume or
evict the chat model's VRAM.

RAG v2 adds source management (enable/disable, pin, label, tags, groups), exact
content-fingerprint duplicate detection, source-level reindexing, score/coverage
diagnostics, and portable knowledge-bundle export/import.  The v1 on-disk
layout is migrated in place without discarding existing sources.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-./:]{1,63}", re.I)
SPACE_RE = re.compile(r"[ \t\f\v]+")
BLANK_RE = re.compile(r"\n{3,}")

STOP = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of",
    "is", "are", "was", "were", "be", "been", "being", "it", "this", "that",
    "with", "as", "by", "from", "you", "i", "we", "they", "he", "she", "my",
    "your", "our", "their", "not", "no", "yes", "do", "does", "did", "have",
    "has", "had", "will", "would", "could", "should", "can", "may", "if",
    "then", "than", "so", "just", "about", "into", "over", "also", "how",
    "what", "when", "where", "who", "why", "which", "there", "here", "its",
}

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".json", ".jsonl", ".csv", ".tsv",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".htm",
    ".css", ".scss", ".ps1", ".psm1", ".psd1", ".bat", ".cmd", ".sh",
    ".sql", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".log", ".properties", ".c", ".h", ".cpp", ".hpp", ".java", ".cs",
    ".go", ".rs", ".php", ".rb", ".swift", ".kt", ".tex",
}
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
INDEX_VERSION = 2
BUNDLE_FORMAT = "cypra-rag-knowledge-bundle"
BUNDLE_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_name(name: str) -> str:
    base = Path(str(name or "knowledge.txt")).name.strip() or "knowledge.txt"
    base = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", base).strip(" .")
    return (base or "knowledge.txt")[:180]


def _clean_label(value: Any, limit: int = 120) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _clean_group(value: Any) -> str:
    return _clean_label(value, 64)


def _clean_tags(values: Any) -> list[str]:
    if isinstance(values, str):
        raw = re.split(r"[,;\n]", values)
    elif isinstance(values, (list, tuple, set)):
        raw = list(values)
    else:
        raw = []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tag = _clean_label(item, 36)
        key = tag.casefold()
        if not tag or key in seen:
            continue
        seen.add(key)
        out.append(tag)
        if len(out) >= 16:
            break
    return out


def _content_fingerprint(text: str) -> str:
    # Formatting/case-insensitive duplicate detection while preserving the exact
    # normalized text separately for provenance and display.
    canonical = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
    return hashlib.sha256(canonical.encode("utf-8", errors="ignore")).hexdigest()


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    for match in TOKEN_RE.finditer(text or ""):
        token = match.group(0).lower().strip("./:")
        if len(token) < 2 or token in STOP or token.isdigit():
            continue
        out.append(token)
    return out


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "svg", "noscript"}:
            self._skip += 1
        elif not self._skip and tag.lower() in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "svg", "noscript"} and self._skip:
            self._skip -= 1
        elif not self._skip and tag.lower() in {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip and data:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def _decode_bytes(raw: bytes) -> str:
    if not raw:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeError:
            pass
    if raw.count(b"\x00") > max(16, len(raw) // 200):
        raise ValueError("This file appears to be binary; use a text-based knowledge file.")
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeError:
            continue
    raise ValueError("Could not decode this file as text.")


def _normalize_text(name: str, text: str) -> str:
    ext = Path(name).suffix.lower()
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if ext in {".html", ".htm"}:
        parser = _HTMLText()
        try:
            parser.feed(value)
            value = parser.text()
        except Exception:
            value = re.sub(r"<[^>]+>", " ", value)
    elif ext == ".json":
        try:
            value = json.dumps(json.loads(value), ensure_ascii=False, indent=2)
        except Exception:
            pass
    lines = [SPACE_RE.sub(" ", line).rstrip() for line in value.split("\n")]
    value = "\n".join(lines).strip()
    return BLANK_RE.sub("\n\n", value)


def extract_text(name: str, raw: bytes) -> str:
    safe = _safe_name(name)
    ext = Path(safe).suffix.lower()
    if ext not in TEXT_EXTENSIONS:
        supported = ", ".join(sorted(TEXT_EXTENSIONS))
        raise ValueError(f"Unsupported RAG file type '{ext or '(none)'}'. Supported: {supported}")
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("Knowledge file is too large (10 MB maximum).")
    text = _normalize_text(safe, _decode_bytes(raw))
    if not text.strip():
        raise ValueError("Knowledge file contains no readable text.")
    return text


def _chunk_text(text: str, *, chunk_chars: int, overlap: int) -> list[str]:
    chunk_chars = max(600, min(6000, int(chunk_chars)))
    overlap = max(0, min(min(1200, chunk_chars // 2), int(overlap)))
    value = str(text or "").strip()
    if not value:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", value) if p.strip()]
    chunks: list[str] = []
    current = ""

    def push(part: str) -> None:
        nonlocal current
        part = part.strip()
        if not part:
            return
        if not current:
            current = part
            return
        candidate = current + "\n\n" + part
        if len(candidate) <= chunk_chars:
            current = candidate
            return
        chunks.append(current.strip())
        tail = current[-overlap:].strip() if overlap else ""
        current = (tail + "\n\n" + part).strip() if tail else part

    for para in paragraphs:
        if len(para) <= chunk_chars:
            push(para)
            continue
        if current:
            chunks.append(current.strip())
            current = ""
        step = max(1, chunk_chars - overlap)
        start = 0
        while start < len(para):
            part = para[start : start + chunk_chars].strip()
            if part:
                chunks.append(part)
            if start + chunk_chars >= len(para):
                break
            start += step
    if current:
        chunks.append(current.strip())

    out: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        key = hashlib.sha1(chunk.encode("utf-8", errors="ignore")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        out.append(chunk)
    return out


class RAGStore:
    """CPU-only BM25 knowledge index rooted in MatrixFiles/RAG."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.sources_dir = self.root / "sources"
        self.index_path = self.root / "index.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.sources: dict[str, dict[str, Any]] = {}
        self.chunks: dict[str, dict[str, Any]] = {}
        self.inv: dict[str, dict[str, int]] = {}
        self._load()

    def _source_defaults(self, source: dict[str, Any], *, text: str = "") -> dict[str, Any]:
        meta = dict(source or {})
        sid = str(meta.get("id") or "").strip()
        name = _safe_name(meta.get("name") or f"{sid or 'knowledge'}.txt")
        kind = str(meta.get("kind") or "file").strip().lower()
        if kind not in {"file", "chat", "manual"}:
            kind = "file"
        role = str(meta.get("origin_role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            role = ""
        label = _clean_label(meta.get("label"), 120)
        group = _clean_group(meta.get("group"))
        tags = _clean_tags(meta.get("tags"))
        if text:
            normalized = _normalize_text(name, text)
            encoded = normalized.encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            fingerprint = _content_fingerprint(normalized)
            preview = re.sub(r"\s+", " ", normalized).strip()[:280]
            chars = len(normalized)
        else:
            digest = str(meta.get("sha256") or "")
            fingerprint = str(meta.get("content_fingerprint") or digest)
            preview = str(meta.get("preview") or "")[:280]
            chars = int(meta.get("chars") or 0)
        meta.update({
            "id": sid,
            "name": name,
            "extension": Path(name).suffix.lower(),
            "sha256": digest,
            "content_fingerprint": fingerprint,
            "kind": kind,
            "origin_role": role,
            "label": label,
            "group": group,
            "tags": tags,
            "enabled": bool(meta.get("enabled", True)),
            "pinned": bool(meta.get("pinned", False)),
            "preview": preview,
            "chars": chars,
            "created_at": str(meta.get("created_at") or _now_iso()),
            "updated_at": str(meta.get("updated_at") or _now_iso()),
        })
        return meta

    def _load(self) -> None:
        if not self.index_path.is_file():
            # Existing source files can still be recovered if index.json was lost.
            if any(self.sources_dir.glob("rag-*.json")):
                self.rebuild()
            return
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            version = int(data.get("version") or 0)
            if version not in {1, INDEX_VERSION}:
                return
            self.sources = data.get("sources") if isinstance(data.get("sources"), dict) else {}
            self.chunks = data.get("chunks") if isinstance(data.get("chunks"), dict) else {}
            self.inv = data.get("inv") if isinstance(data.get("inv"), dict) else {}
            migrated = version != INDEX_VERSION
            for sid, source in list(self.sources.items()):
                normalized = self._source_defaults(source)
                normalized["id"] = sid
                if normalized != source:
                    migrated = True
                self.sources[sid] = normalized
            if migrated:
                # Rebuild so metadata terms are represented in the v2 index.
                self.rebuild()
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self.sources, self.chunks, self.inv = {}, {}, {}
            if any(self.sources_dir.glob("rag-*.json")):
                self.rebuild()

    def _save(self) -> None:
        payload = {
            "version": INDEX_VERSION,
            "saved_at": _now_iso(),
            "retrieval": "bm25-cpu-v2",
            "sources": self.sources,
            "chunks": self.chunks,
            "inv": self.inv,
        }
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.index_path)

    def _meta_path(self, source_id: str) -> Path:
        return self.sources_dir / f"{source_id}.json"

    def _text_path(self, source_id: str) -> Path:
        return self.sources_dir / f"{source_id}.txt"

    def _read_source_text(self, source_id: str) -> str:
        path = self._text_path(source_id)
        if not path.is_file():
            raise FileNotFoundError(source_id)
        return path.read_text(encoding="utf-8")

    def _write_source_files(self, meta: dict[str, Any], text: str) -> None:
        sid = str(meta["id"])
        text_path = self._text_path(sid)
        meta_path = self._meta_path(sid)
        ttmp = text_path.with_suffix(".txt.tmp")
        mtmp = meta_path.with_suffix(".json.tmp")
        ttmp.write_text(text, encoding="utf-8")
        mtmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        ttmp.replace(text_path)
        mtmp.replace(meta_path)

    def _remove_index_source(self, source_id: str) -> None:
        source = self.sources.pop(source_id, None) or {}
        chunk_ids = list(source.get("chunk_ids") or [])
        if not chunk_ids:
            chunk_ids = [cid for cid, chunk in self.chunks.items() if chunk.get("source_id") == source_id]
        for cid in chunk_ids:
            chunk = self.chunks.pop(cid, None)
            if not chunk:
                continue
            for token in (chunk.get("tf") or {}):
                bucket = self.inv.get(token)
                if not bucket:
                    continue
                bucket.pop(cid, None)
                if not bucket:
                    self.inv.pop(token, None)

    def _index_source(self, meta: dict[str, Any], text: str, *, chunk_chars: int, overlap: int) -> None:
        sid = str(meta["id"])
        self._remove_index_source(sid)
        meta = self._source_defaults(meta, text=text)
        chunk_ids: list[str] = []
        chunks = _chunk_text(text, chunk_chars=chunk_chars, overlap=overlap)
        metadata_text = " ".join([
            str(meta.get("name") or ""), str(meta.get("label") or ""),
            str(meta.get("group") or ""), " ".join(meta.get("tags") or []),
        ]).strip()
        title_tokens = _tokens(metadata_text)
        for index, chunk_text in enumerate(chunks):
            cid = f"{sid}:{index}"
            tf: dict[str, int] = defaultdict(int)
            toks = _tokens(chunk_text)
            for token in toks:
                tf[token] += 1
            for token in title_tokens:
                tf[token] += 4
            record = {
                "id": cid,
                "source_id": sid,
                "index": index,
                "text": chunk_text,
                "tf": dict(tf),
                "len": max(1, len(toks)),
            }
            self.chunks[cid] = record
            for token, count in tf.items():
                self.inv.setdefault(token, {})[cid] = int(count)
            chunk_ids.append(cid)
        meta["chunk_ids"] = chunk_ids
        meta["chunks"] = len(chunk_ids)
        meta["chunk_chars"] = int(chunk_chars)
        meta["chunk_overlap"] = int(overlap)
        self.sources[sid] = meta

    def _duplicate_source(self, digest: str, fingerprint: str) -> dict[str, Any] | None:
        for source in self.sources.values():
            if digest and source.get("sha256") == digest:
                return source
            if fingerprint and source.get("content_fingerprint") == fingerprint:
                return source
        return None

    def add_bytes(self, name: str, raw: bytes, *, chunk_chars: int = 1800, overlap: int = 240) -> dict[str, Any]:
        safe_name = _safe_name(name)
        text = extract_text(safe_name, raw)
        return self.add_text(safe_name, text, byte_count=len(raw), chunk_chars=chunk_chars, overlap=overlap, kind="file")

    def add_text(
        self,
        name: str,
        text: str,
        *,
        byte_count: int | None = None,
        chunk_chars: int = 1800,
        overlap: int = 240,
        kind: str = "manual",
        origin_role: str = "",
        label: str = "",
        tags: Any = None,
        group: str = "",
        enabled: bool = True,
        pinned: bool = False,
        created_at: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            safe_name = _safe_name(name)
            text = _normalize_text(safe_name, str(text or ""))
            if not text:
                raise ValueError("Knowledge source contains no readable text.")
            encoded = text.encode("utf-8")
            if len(encoded) > MAX_FILE_BYTES:
                raise ValueError("Knowledge source is too large (10 MB maximum).")
            digest = hashlib.sha256(encoded).hexdigest()
            fingerprint = _content_fingerprint(text)
            duplicate = self._duplicate_source(digest, fingerprint)
            if duplicate:
                return {
                    "ok": True,
                    "duplicate": True,
                    "duplicate_reason": "content",
                    "source": self._public_source(duplicate),
                }

            sid = f"rag-{uuid.uuid4().hex[:16]}"
            now = _now_iso()
            source_kind = str(kind or "manual").strip().lower()
            if source_kind not in {"file", "chat", "manual"}:
                source_kind = "manual"
            role = str(origin_role or "").strip().lower()
            if role not in {"user", "assistant"}:
                role = ""
            meta: dict[str, Any] = {
                "id": sid,
                "name": safe_name,
                "extension": Path(safe_name).suffix.lower(),
                "sha256": digest,
                "content_fingerprint": fingerprint,
                "bytes": int(byte_count if byte_count is not None else len(encoded)),
                "chars": len(text),
                "kind": source_kind,
                "origin_role": role,
                "label": _clean_label(label, 120),
                "group": _clean_group(group),
                "tags": _clean_tags(tags),
                "enabled": bool(enabled),
                "pinned": bool(pinned),
                "preview": re.sub(r"\s+", " ", text).strip()[:280],
                "created_at": str(created_at or now),
                "updated_at": now,
            }
            self._write_source_files(meta, text)
            self._index_source(meta, text, chunk_chars=chunk_chars, overlap=overlap)
            final_meta = dict(self.sources[sid])
            self._write_source_files(final_meta, text)
            self._save()
            return {"ok": True, "duplicate": False, "source": self._public_source(final_meta)}

    def remove(self, source_id: str) -> bool:
        with self._lock:
            sid = str(source_id or "").strip()
            if sid not in self.sources:
                return False
            self._remove_index_source(sid)
            for path in (self._meta_path(sid), self._text_path(sid)):
                try:
                    if path.is_file():
                        path.unlink()
                except OSError:
                    pass
            self._save()
            return True

    def clear(self) -> dict[str, int]:
        with self._lock:
            ids = list(self.sources.keys())
            removed = 0
            for sid in ids:
                if self.remove(sid):
                    removed += 1
            return {"sources_removed": removed}

    def rebuild(self, *, chunk_chars: int = 1800, overlap: int = 240) -> dict[str, int]:
        with self._lock:
            self.sources, self.chunks, self.inv = {}, {}, {}
            indexed = 0
            skipped = 0
            for meta_path in sorted(self.sources_dir.glob("rag-*.json")):
                sid = meta_path.stem
                text_path = self._text_path(sid)
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    text = text_path.read_text(encoding="utf-8")
                    if not isinstance(meta, dict) or str(meta.get("id") or "") != sid or not text.strip():
                        skipped += 1
                        continue
                    meta = self._source_defaults(meta, text=text)
                    meta["updated_at"] = _now_iso()
                    self._index_source(meta, text, chunk_chars=chunk_chars, overlap=overlap)
                    self._write_source_files(dict(self.sources[sid]), text)
                    indexed += 1
                except Exception:
                    skipped += 1
            self._save()
            return {"sources": indexed, "chunks": len(self.chunks), "skipped": skipped}

    def reindex_source(self, source_id: str, *, chunk_chars: int | None = None, overlap: int | None = None) -> dict[str, Any]:
        with self._lock:
            sid = str(source_id or "").strip()
            source = self.sources.get(sid)
            if not source:
                raise KeyError(sid)
            text = self._read_source_text(sid)
            cchars = int(chunk_chars if chunk_chars is not None else source.get("chunk_chars") or 1800)
            coverlap = int(overlap if overlap is not None else source.get("chunk_overlap") or 240)
            meta = dict(source)
            meta["updated_at"] = _now_iso()
            self._index_source(meta, text, chunk_chars=cchars, overlap=coverlap)
            final_meta = dict(self.sources[sid])
            self._write_source_files(final_meta, text)
            self._save()
            return {"ok": True, "source": self._public_source(final_meta)}

    def update_source(self, source_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            sid = str(source_id or "").strip()
            source = self.sources.get(sid)
            if not source:
                raise KeyError(sid)
            meta = dict(source)
            reindex = False
            if "name" in patch and patch.get("name") is not None:
                new_name = _safe_name(str(patch.get("name") or meta.get("name") or "knowledge.txt"))
                if new_name != meta.get("name"):
                    meta["name"] = new_name
                    meta["extension"] = Path(new_name).suffix.lower()
                    reindex = True
            if "label" in patch and patch.get("label") is not None:
                value = _clean_label(patch.get("label"), 120)
                if value != meta.get("label", ""):
                    meta["label"] = value
                    reindex = True
            if "group" in patch and patch.get("group") is not None:
                value = _clean_group(patch.get("group"))
                if value != meta.get("group", ""):
                    meta["group"] = value
                    reindex = True
            if "tags" in patch and patch.get("tags") is not None:
                value = _clean_tags(patch.get("tags"))
                if value != meta.get("tags", []):
                    meta["tags"] = value
                    reindex = True
            if "enabled" in patch and patch.get("enabled") is not None:
                meta["enabled"] = bool(patch.get("enabled"))
            if "pinned" in patch and patch.get("pinned") is not None:
                meta["pinned"] = bool(patch.get("pinned"))
            meta["updated_at"] = _now_iso()
            text = self._read_source_text(sid)
            if reindex:
                self._index_source(
                    meta,
                    text,
                    chunk_chars=int(meta.get("chunk_chars") or 1800),
                    overlap=int(meta.get("chunk_overlap") or 240),
                )
                meta = dict(self.sources[sid])
            else:
                self.sources[sid] = meta
            self._write_source_files(meta, text)
            self._save()
            return {"ok": True, "source": self._public_source(meta)}

    def _public_source(self, source: dict[str, Any]) -> dict[str, Any]:
        source = self._source_defaults(source)
        display = str(source.get("label") or source.get("name") or source.get("id") or "knowledge")
        return {
            "id": source.get("id"),
            "name": source.get("name"),
            "label": source.get("label") or "",
            "display_name": display,
            "extension": source.get("extension") or "",
            "bytes": int(source.get("bytes") or 0),
            "chars": int(source.get("chars") or 0),
            "chunks": int(source.get("chunks") or len(source.get("chunk_ids") or [])),
            "kind": source.get("kind") or "file",
            "origin_role": str(source.get("origin_role") or ""),
            "group": str(source.get("group") or ""),
            "tags": list(source.get("tags") or []),
            "enabled": bool(source.get("enabled", True)),
            "pinned": bool(source.get("pinned", False)),
            "preview": str(source.get("preview") or "")[:280],
            "created_at": source.get("created_at") or "",
            "updated_at": source.get("updated_at") or "",
            "sha256": str(source.get("sha256") or "")[:16],
        }

    def list_sources(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [self._public_source(source) for source in self.sources.values()]
            rows.sort(key=lambda row: (
                not bool(row.get("pinned")),
                str(row.get("group") or "").casefold(),
                str(row.get("display_name") or "").casefold(),
                str(row.get("id") or ""),
            ))
            return rows

    def source_detail(self, source_id: str, *, max_chars: int = 12000) -> dict[str, Any] | None:
        with self._lock:
            source = self.sources.get(str(source_id or "").strip())
            if not source:
                return None
            text = self._read_source_text(str(source["id"]))
            cap = max(500, min(24000, int(max_chars)))
            trimmed = len(text) > cap
            body = text[:cap].rstrip() + ("\n\n[PREVIEW TRIMMED]" if trimmed else "")
            return {"source": self._public_source(source), "text": body, "trimmed": trimmed, "total_chars": len(text)}

    def search(self, query: str, *, limit: int = 4, min_score: float = 0.0, include_disabled: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            query = str(query or "").strip()
            if not query or not self.chunks:
                return []
            qtokens = _tokens(query)
            if not qtokens:
                return []
            uq = set(qtokens)
            enabled_chunk_ids = [
                cid for cid, row in self.chunks.items()
                if include_disabled or bool((self.sources.get(str(row.get("source_id") or "")) or {}).get("enabled", True))
            ]
            if not enabled_chunk_ids:
                return []
            n_docs = max(1, len(enabled_chunk_ids))
            avgdl = sum(int(self.chunks[cid].get("len") or 1) for cid in enabled_chunk_ids) / n_docs
            k1, b = 1.5, 0.75
            scores: dict[str, float] = defaultdict(float)
            matched: dict[str, set[str]] = defaultdict(set)
            enabled_set = set(enabled_chunk_ids)
            for token in uq:
                postings = self.inv.get(token) or {}
                active_postings = {cid: tf for cid, tf in postings.items() if cid in enabled_set}
                if not active_postings:
                    continue
                df = len(active_postings)
                idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
                for cid, tf in active_postings.items():
                    row = self.chunks.get(cid)
                    if not row:
                        continue
                    dl = int(row.get("len") or 1)
                    denom = tf + k1 * (1.0 - b + b * dl / max(1.0, avgdl))
                    scores[cid] += idf * (tf * (k1 + 1.0)) / max(0.0001, denom)
                    matched[cid].add(token)

            qlow = query.casefold()
            max_hits = max(1, min(12, int(limit)))
            threshold = max(0.0, min(20.0, float(min_score or 0.0)))
            ranked: list[tuple[str, float, float, float, list[str]]] = []
            for cid, base_score in scores.items():
                chunk = self.chunks.get(cid) or {}
                sid = str(chunk.get("source_id") or "")
                source = self.sources.get(sid) or {}
                if not source or (not include_disabled and not bool(source.get("enabled", True))):
                    continue
                score = float(base_score)
                text = str(chunk.get("text") or "")
                if len(qlow) >= 5 and qlow in text.casefold():
                    score *= 1.75
                metadata = " ".join([
                    str(source.get("name") or ""), str(source.get("label") or ""),
                    str(source.get("group") or ""), " ".join(source.get("tags") or []),
                ]).casefold()
                metadata_matches = sum(1 for token in uq if token in metadata)
                if metadata_matches:
                    score *= 1.0 + min(0.30, metadata_matches * 0.06)
                pin_boost = 1.35 if bool(source.get("pinned", False)) else 1.0
                score *= pin_boost
                terms = sorted(matched.get(cid) or [])
                coverage = len(terms) / max(1, len(uq))
                # Very low token coverage is useful for broad discovery but should
                # not outrank a multi-term match merely because a term is rare.
                score *= 0.72 + 0.28 * coverage
                if score < threshold:
                    continue
                ranked.append((cid, score, float(base_score), coverage, terms))

            ranked.sort(key=lambda row: (-row[1], row[0]))
            results: list[dict[str, Any]] = []
            per_source: dict[str, int] = defaultdict(int)
            for cid, score, base_score, coverage, terms in ranked:
                chunk = self.chunks.get(cid) or {}
                sid = str(chunk.get("source_id") or "")
                source = self.sources.get(sid) or {}
                if per_source[sid] >= 2:
                    continue
                per_source[sid] += 1
                text = str(chunk.get("text") or "").strip()
                display = str(source.get("label") or source.get("name") or sid)
                results.append({
                    "id": cid,
                    "source_id": sid,
                    "source": display,
                    "source_name": str(source.get("name") or sid),
                    "chunk": int(chunk.get("index") or 0) + 1,
                    "score": round(float(score), 4),
                    "base_score": round(float(base_score), 4),
                    "coverage": round(float(coverage), 4),
                    "matched_terms": terms,
                    "pinned": bool(source.get("pinned", False)),
                    "group": str(source.get("group") or ""),
                    "tags": list(source.get("tags") or []),
                    "text": text,
                    "snippet": text[:300] + ("…" if len(text) > 300 else ""),
                })
                if len(results) >= max_hits:
                    break
            return results

    def context_for_query(
        self,
        query: str,
        *,
        top_k: int = 4,
        max_chars: int = 6000,
        min_score: float = 0.0,
    ) -> tuple[str, list[dict[str, Any]]]:
        hits = self.search(query, limit=top_k, min_score=min_score)
        if not hits:
            return "", []
        cap = max(1200, min(24000, int(max_chars)))
        blocks: list[str] = []
        public: list[dict[str, Any]] = []
        used_chars = 0
        for index, hit in enumerate(hits, 1):
            meta_bits = []
            if hit.get("group"):
                meta_bits.append(f"Group: {hit['group']}")
            if hit.get("pinned"):
                meta_bits.append("Pinned: yes")
            prefix = f"[RAG {index}]\nSource: {hit['source']}\nChunk: {hit['chunk']}\n"
            if meta_bits:
                prefix += "\n".join(meta_bits) + "\n"
            remaining = cap - used_chars - len(prefix)
            if remaining < 180:
                break
            body = str(hit.get("text") or "").strip()
            if len(body) > remaining:
                body = body[: max(0, remaining - 16)].rstrip() + "\n[TRIMMED]"
            block = prefix + body
            blocks.append(block)
            used_chars += len(block) + 2
            public.append({
                "ref": f"RAG {index}",
                "source_id": hit["source_id"],
                "source": hit["source"],
                "source_name": hit.get("source_name") or hit["source"],
                "chunk": hit["chunk"],
                "score": hit["score"],
                "base_score": hit["base_score"],
                "coverage": hit["coverage"],
                "matched_terms": hit["matched_terms"],
                "pinned": hit["pinned"],
                "group": hit["group"],
                "snippet": hit["snippet"],
            })
            if used_chars >= cap:
                break
        return "\n\n".join(blocks), public

    def export_bundle(self) -> dict[str, Any]:
        with self._lock:
            sources: list[dict[str, Any]] = []
            total = 0
            for row in self.list_sources():
                sid = str(row.get("id") or "")
                text = self._read_source_text(sid)
                total += len(text.encode("utf-8"))
                if total > MAX_BUNDLE_BYTES:
                    raise ValueError("RAG knowledge bundle exceeds the 64 MB portable export limit.")
                sources.append({
                    "name": row.get("name") or "knowledge.txt",
                    "kind": row.get("kind") or "file",
                    "origin_role": row.get("origin_role") or "",
                    "label": row.get("label") or "",
                    "group": row.get("group") or "",
                    "tags": row.get("tags") or [],
                    "enabled": bool(row.get("enabled", True)),
                    "pinned": bool(row.get("pinned", False)),
                    "created_at": row.get("created_at") or "",
                    "text": text,
                })
            return {
                "format": BUNDLE_FORMAT,
                "version": BUNDLE_VERSION,
                "exported_at": _now_iso(),
                "retrieval": "bm25-cpu-v2",
                "source_count": len(sources),
                "sources": sources,
            }

    def import_bundle(self, bundle: dict[str, Any], *, chunk_chars: int = 1800, overlap: int = 240) -> dict[str, Any]:
        if not isinstance(bundle, dict) or bundle.get("format") != BUNDLE_FORMAT:
            raise ValueError("Not a Cypra RAG knowledge bundle.")
        if int(bundle.get("version") or 0) != BUNDLE_VERSION:
            raise ValueError(f"Unsupported RAG bundle version: {bundle.get('version')}")
        rows = bundle.get("sources")
        if not isinstance(rows, list):
            raise ValueError("RAG bundle sources are missing or invalid.")
        if len(rows) > 5000:
            raise ValueError("RAG bundle contains too many sources.")
        total = 0
        imported = 0
        duplicates = 0
        failed = 0
        for item in rows:
            if not isinstance(item, dict):
                failed += 1
                continue
            text = str(item.get("text") or "")
            total += len(text.encode("utf-8"))
            if total > MAX_BUNDLE_BYTES:
                raise ValueError("RAG knowledge bundle exceeds the 64 MB portable import limit.")
            if not text.strip():
                failed += 1
                continue
            try:
                result = self.add_text(
                    str(item.get("name") or "knowledge.txt"),
                    text,
                    chunk_chars=chunk_chars,
                    overlap=overlap,
                    kind=str(item.get("kind") or "manual"),
                    origin_role=str(item.get("origin_role") or ""),
                    label=str(item.get("label") or ""),
                    tags=item.get("tags") or [],
                    group=str(item.get("group") or ""),
                    enabled=bool(item.get("enabled", True)),
                    pinned=bool(item.get("pinned", False)),
                    created_at=str(item.get("created_at") or ""),
                )
                if result.get("duplicate"):
                    duplicates += 1
                else:
                    imported += 1
            except (ValueError, TypeError, OSError):
                failed += 1
        return {
            "ok": True,
            "imported": imported,
            "duplicates": duplicates,
            "failed": failed,
            "sources": len(self.sources),
            "chunks": len(self.chunks),
        }

    def stats(self) -> dict[str, Any]:
        with self._lock:
            try:
                disk_bytes = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
            except OSError:
                disk_bytes = 0
            kinds = {"file": 0, "chat": 0, "manual": 0}
            enabled = 0
            pinned = 0
            groups: set[str] = set()
            for source in self.sources.values():
                kind = str(source.get("kind") or "file").strip().lower()
                if kind not in kinds:
                    kind = "file"
                kinds[kind] += 1
                if bool(source.get("enabled", True)):
                    enabled += 1
                if bool(source.get("pinned", False)):
                    pinned += 1
                group = _clean_group(source.get("group"))
                if group:
                    groups.add(group)
            return {
                "sources": len(self.sources),
                "enabled_sources": enabled,
                "disabled_sources": len(self.sources) - enabled,
                "pinned_sources": pinned,
                "groups": len(groups),
                "chunks": len(self.chunks),
                "files": kinds["file"],
                "chat_sources": kinds["chat"],
                "manual_sources": kinds["manual"],
                "retrieval": "bm25-cpu-v2",
                "embedding_model": None,
                "gpu_required": False,
                "disk_bytes": int(disk_bytes),
                "path": str(self.root),
                "supported_extensions": sorted(TEXT_EXTENSIONS),
                "max_file_bytes": MAX_FILE_BYTES,
                "max_bundle_bytes": MAX_BUNDLE_BYTES,
            }
