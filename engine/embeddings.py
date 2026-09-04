"""Local embeddings via Ollama (nomic-embed-text etc.) for smarter memory recall."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import requests

from engine.llm import ollama_root


def embed_texts(
    texts: list[str],
    *,
    settings: dict[str, Any] | None = None,
    model: str | None = None,
) -> list[list[float]]:
    s = settings or {}
    if not s.get("use_embeddings", True):
        return []
    root = ollama_root(s.get("ollama_base_url"))
    model = model or s.get("embed_model") or "nomic-embed-text"
    out: list[list[float]] = []
    for text in texts:
        t = (text or "").strip()
        if not t:
            out.append([])
            continue
        try:
            # keep_alive so embed model stays warm when using local semantic memory
            keep = s.get("ollama_keep_alive") or "10m"
            r = requests.post(
                f"{root}/api/embeddings",
                json={
                    "model": model,
                    "prompt": t[:4000],  # shorter prompts = faster local embeds
                    "keep_alive": keep,
                },
                timeout=45,
            )
            if not r.ok:
                out.append([])
                continue
            emb = r.json().get("embedding") or []
            out.append([float(x) for x in emb])
        except Exception:
            out.append([])
    return out


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class EmbeddingStore:
    """Caches note embeddings on D: next to the memory index."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "embeddings.json"
        self.vectors: dict[str, list[float]] = {}
        self.model: str = "nomic-embed-text"
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.vectors = data.get("vectors") or {}
            self.model = data.get("model") or self.model
        except (OSError, json.JSONDecodeError):
            self.vectors = {}

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"model": self.model, "vectors": self.vectors}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def ensure_note(
        self,
        note_id: str,
        text: str,
        *,
        settings: dict[str, Any] | None = None,
        force: bool = False,
    ) -> list[float]:
        if not force and note_id in self.vectors and self.vectors[note_id]:
            return self.vectors[note_id]
        vecs = embed_texts([text], settings=settings, model=(settings or {}).get("embed_model"))
        v = vecs[0] if vecs else []
        if v:
            self.vectors[note_id] = v
            self.model = (settings or {}).get("embed_model") or self.model
            self.save()
        return v

    def drop(self, note_id: str) -> None:
        if note_id in self.vectors:
            del self.vectors[note_id]
            self.save()

    def prune_missing(
        self,
        live_ids: set[str] | list[str],
        *,
        drop_empty: bool = True,
    ) -> dict[str, int]:
        """
        Remove embedding vectors for notes that no longer exist (or empty vectors).
        """
        live = set(live_ids or [])
        removed = 0
        empty = 0
        for did in list(self.vectors.keys()):
            vec = self.vectors.get(did)
            if did not in live:
                del self.vectors[did]
                removed += 1
            elif drop_empty and (not vec or not isinstance(vec, list)):
                del self.vectors[did]
                empty += 1
        if removed or empty:
            self.save()
        return {
            "embeddings_removed": removed,
            "empty_removed": empty,
            "embeddings_remaining": sum(1 for v in self.vectors.values() if v),
        }

    def search(
        self,
        query: str,
        *,
        settings: dict[str, Any] | None = None,
        limit: int = 12,
        live_ids: set[str] | list[str] | None = None,
    ) -> list[tuple[str, float]]:
        qv = embed_texts([query], settings=settings)
        if not qv or not qv[0]:
            return []
        q = qv[0]
        allow = set(live_ids) if live_ids is not None else None
        scored: list[tuple[str, float]] = []
        for did, vec in self.vectors.items():
            if not vec:
                continue
            if allow is not None and did not in allow:
                continue
            scored.append((did, cosine(q, vec)))
        scored.sort(key=lambda x: -x[1])
        return scored[:limit]

    def stats(self) -> dict[str, Any]:
        return {
            "embedded": sum(1 for v in self.vectors.values() if v),
            "vectors_total": len(self.vectors),
            "model": self.model,
            "path": str(self.path),
        }
