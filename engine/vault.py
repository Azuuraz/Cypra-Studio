"""Local markdown vault: durable notes, wikilinks, provenance, and retrieval metadata."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
TAG_RE = re.compile(r"(?<!\S)#([A-Za-z0-9_/-]+)")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

SEED_NOTES: list[dict[str, str]] = [
    {
        "title": "Cypra Memory",
        "content": """---
type: concept
tags: [memory, seed]
---

# Cypra Memory

Cypra stores durable information as plain Markdown notes with explicit links and local provenance.
Retrieval should prefer verified stored context over invention, stay bounded to the active request,
and keep source information available for future migration into a newer memory engine.

Related: [[Cypra Matrix Studio]], [[Wikilinks]], [[Memory Provenance]], [[Ollama]]
""",
    },
    {
        "title": "Cypra Matrix Studio",
        "content": """---
type: project
tags: [cypra, studio, seed]
---

# Cypra Matrix Studio

A local-first Windows AI workspace for project-local Ollama models, specialist agents, chat,
file review, voice, runtime controls, and durable memory experiments.

Related: [[Cypra Memory]], [[Ollama]], [[Memory Provenance]]
""",
    },
    {
        "title": "Ollama",
        "content": """---
type: entity
tags: [ai, local, seed]
---

# Ollama

The local model runtime used by Cypra Matrix Studio. Project settings control the loopback
endpoint, selected model, context window, keep-alive behavior, and local model storage.

Related: [[Cypra Matrix Studio]], [[Cypra Memory]]
""",
    },
    {
        "title": "Wikilinks",
        "content": """---
type: concept
tags: [markdown, relationships, seed]
---

# Wikilinks

Links written as `[[Note Title]]` represent explicit relationships between durable notes.
They are retrieval metadata, not a requirement for a visualizer. Relationship traversal may
be used to find relevant neighboring notes when it improves recall.

Related: [[Cypra Memory]], [[Memory Provenance]]
""",
    },
    {
        "title": "Memory Provenance",
        "content": """---
type: concept
tags: [memory, provenance, seed]
---

# Memory Provenance

Durable memory should retain where information came from, when it was stored, and whether it
was user-provided, imported, inferred, or generated. Provenance supports safer retrieval,
deduplication, later correction, and migration into future memory versions.

Related: [[Cypra Memory]], [[Wikilinks]]
""",
    },
]


# Compatibility cleanup: old example seed notes that must not be restored.
RETIRED_SEED_TITLES = ("Project Orion", "Steve")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(title: str) -> str:
    s = (title or "").strip()
    s = re.sub(r'[<>:"/\\|?*]', "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or f"Note-{uuid.uuid4().hex[:8]}"


class Vault:
    """Markdown vault rooted at `root` (contains wiki/, inbox/)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.wiki = self.root / "wiki"
        self.inbox = self.root / "inbox"
        self.meta = self.root / "_meta"
        self.wiki.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.meta.mkdir(parents=True, exist_ok=True)
        self._ensure_seed()
        self.purge_retired_seeds()

    def _ensure_seed(self) -> None:
        if any(self.wiki.glob("*.md")):
            return
        for note in SEED_NOTES:
            self.write_note(note["title"], note["content"], overwrite=False)

    def purge_retired_seeds(self) -> list[str]:
        """Drop old example seeds that must not come back on reset."""
        removed: list[str] = []
        for title in RETIRED_SEED_TITLES:
            path = self.note_path(title)
            if path.is_file():
                try:
                    path.unlink()
                    removed.append(title)
                except OSError:
                    continue
        return removed

    def restore_seed_notes(self, *, overwrite: bool = True) -> dict[str, Any]:
        """
        Re-write starter / example notes (concept, entity, person, project, …)
        without wiping the rest of the vault.
        """
        retired = self.purge_retired_seeds()
        written: list[str] = []
        for note in SEED_NOTES:
            title = note["title"]
            path = self.note_path(title)
            if path.exists() and not overwrite:
                continue
            self.write_note(title, note["content"], overwrite=True)
            written.append(title)
        return {
            "restored": written,
            "removed": retired,
            "count": len(written),
            "notes_now": len(list(self.wiki.glob("*.md"))),
        }

    def reset(
        self,
        *,
        reseed: bool = True,
        clear_inbox: bool = False,
    ) -> dict[str, Any]:
        """Wipe wiki notes (and optionally inbox), then optionally re-seed starters."""
        removed = 0
        for path in list(self.wiki.glob("*.md")):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        inbox_cleared = 0
        if clear_inbox:
            for path in list(self.inbox.iterdir()):
                if path.is_file():
                    try:
                        path.unlink()
                        inbox_cleared += 1
                    except OSError:
                        pass
        if reseed:
            for note in SEED_NOTES:
                self.write_note(note["title"], note["content"], overwrite=True)
        return {
            "notes_removed": removed,
            "inbox_cleared": inbox_cleared,
            "reseeded": reseed,
            "notes_now": len(list(self.wiki.glob("*.md"))),
        }

    def note_path(self, title: str) -> Path:
        return self.wiki / f"{slugify(title)}.md"

    def resolve_note_id(self, title_or_id: str) -> str | None:
        """Return live note id if title/id resolves (case-insensitive), else None."""
        if not title_or_id:
            return None
        note = self.read_note(title_or_id)
        if note and note.get("id"):
            return note["id"]
        want = slugify(str(title_or_id)).lower()
        raw = str(title_or_id).strip().lower()
        for meta in self.list_notes():
            mid = (meta.get("id") or "").lower()
            mtitle = (meta.get("title") or "").lower()
            if mid == want or mid == raw or mtitle == raw or mtitle == want:
                return meta["id"]
        return None

    def scrub_dead_wikilinks(self) -> dict[str, int]:
        """
        Remove [[wikilinks]] that no longer resolve to a note file.
        Keeps graph free of unopenable ghost nodes.
        """
        live_ids = {m["id"] for m in self.list_notes()}
        live_lower = {i.lower(): i for i in live_ids}
        # also map titles
        for m in self.list_notes():
            t = (m.get("title") or "").strip()
            if t:
                live_lower[t.lower()] = m["id"]
                live_lower[slugify(t).lower()] = m["id"]

        def target_exists(label: str) -> bool:
            lab = (label or "").strip()
            if not lab:
                return False
            if self.resolve_note_id(lab):
                return True
            key = slugify(lab).lower()
            return key in live_lower or lab.lower() in live_lower

        notes_changed = 0
        links_removed = 0
        for meta in list(self.list_notes()):
            path = self.note_path(meta["id"])
            if not path.exists():
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            removed_here = 0

            def repl(m: re.Match[str]) -> str:
                nonlocal removed_here
                label = (m.group(1) or "").strip()
                if target_exists(label):
                    return m.group(0)
                removed_here += 1
                # keep readable text without a broken link
                return label

            new_raw = WIKILINK_RE.sub(repl, raw)
            # Drop Related: lines that no longer contain any live wikilinks
            def scrub_related(line: str) -> str:
                if not re.match(r"(?i)^\s*Related:\s*", line):
                    return line
                if not WIKILINK_RE.search(line):
                    return ""  # only dead plain-text leftovers
                # tidy commas
                line = re.sub(r",\s*,+", ", ", line)
                line = re.sub(r"(?i)^(\s*Related:\s*),\s*", r"\1", line)
                line = re.sub(r",\s*$", "", line)
                if re.match(r"(?i)^\s*Related:\s*$", line):
                    return ""
                return line

            new_raw = "\n".join(
                scrub_related(ln) for ln in new_raw.splitlines()
            )
            new_raw = re.sub(r"\n{3,}", "\n\n", new_raw).rstrip() + "\n"
            if new_raw != raw and removed_here:
                try:
                    path.write_text(new_raw, encoding="utf-8")
                    notes_changed += 1
                    links_removed += removed_here
                except OSError:
                    pass

        return {
            "notes_changed": notes_changed,
            "links_removed": links_removed,
            "live_notes": len(live_ids),
        }

    def list_notes(self) -> list[dict[str, Any]]:
        notes = []
        for path in sorted(self.wiki.glob("*.md")):
            meta = self.read_note(path.stem)
            if meta:
                notes.append(
                    {
                        "id": path.stem,
                        "title": meta["title"],
                        "type": meta.get("type") or "concept",
                        "tags": meta.get("tags") or [],
                        "links": meta.get("links") or [],
                        "updated": meta.get("updated"),
                        "preview": meta.get("preview") or "",
                        "summary": meta.get("summary") or "",
                        "description": meta.get("description") or "",
                        "word_count": meta.get("word_count") or 0,
                        "link_count": meta.get("link_count") or 0,
                        "sections": meta.get("sections") or [],
                    }
                )
        return notes

    def read_note(self, title_or_id: str) -> dict[str, Any] | None:
        path = self.note_path(title_or_id)
        if not path.exists():
            # try exact stem match among files
            for p in self.wiki.glob("*.md"):
                if p.stem.lower() == title_or_id.lower() or p.stem == title_or_id:
                    path = p
                    break
            else:
                return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        fm: dict[str, Any] = {}
        body = raw
        m = FRONTMATTER_RE.match(raw)
        if m:
            fm = _parse_simple_yaml(m.group(1))
            body = raw[m.end() :]
        title = path.stem
        # prefer first H1
        for line in body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip() or title
                break
        links = [slugify(x.strip()) for x in WIKILINK_RE.findall(body)]
        tags = list(fm.get("tags") or [])
        tags += [t for t in TAG_RE.findall(body) if t not in tags]
        body_s = body.strip()
        enriched = _enrich_body(body_s, fm)
        return {
            "id": path.stem,
            "title": title,
            "type": fm.get("type") or "concept",
            "tags": tags,
            "links": links,
            "body": body_s,
            "content": raw,
            "path": str(path.relative_to(self.root)),
            "updated": datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **enriched,
        }

    def write_note(
        self,
        title: str,
        content: str,
        *,
        overwrite: bool = True,
        note_type: str | None = None,
        tags: list[str] | None = None,
        links: list[str] | None = None,
    ) -> dict[str, Any]:
        path = self.note_path(title)
        if path.exists() and not overwrite:
            return self.read_note(path.stem) or {"id": path.stem, "title": title}

        # If content has no frontmatter, add a minimal one
        text = content.strip()
        if not text.startswith("---"):
            tag_list = tags or []
            ntype = note_type or "concept"
            fm_lines = ["---", f"type: {ntype}", f"updated: {_now_iso()}"]
            if tag_list:
                fm_lines.append("tags: [" + ", ".join(tag_list) + "]")
            fm_lines.append("---")
            body = text
            if not body.lstrip().startswith("#"):
                body = f"# {title}\n\n{body}"
            if links:
                existing = set(WIKILINK_RE.findall(body))
                extra = [f"[[{slugify(l)}]]" for l in links if slugify(l) not in existing]
                if extra:
                    body = body.rstrip() + "\n\nRelated: " + ", ".join(extra) + "\n"
            text = "\n".join(fm_lines) + "\n\n" + body.strip() + "\n"
        else:
            # ensure related links if provided
            if links:
                existing = set(WIKILINK_RE.findall(text))
                extra = [f"[[{slugify(l)}]]" for l in links if slugify(l) not in existing]
                if extra:
                    text = text.rstrip() + "\n\nRelated: " + ", ".join(extra) + "\n"

        path.write_text(text, encoding="utf-8")
        return self.read_note(path.stem) or {"id": path.stem, "title": title}

    def upsert_note(
        self,
        title: str,
        body: str,
        *,
        note_type: str = "concept",
        tags: list[str] | None = None,
        links: list[str] | None = None,
        merge: bool = True,
    ) -> dict[str, Any]:
        # Light title hygiene on write (regex only — free)
        try:
            from engine.quality import clean_note_title

            cleaned = clean_note_title(title)
            if cleaned:
                title = cleaned
        except Exception:
            title = re.sub(r"\s+", " ", (title or "").strip())
        existing = self.read_note(title)
        if existing and merge:
            # append new content if not already present
            old_body = existing.get("body") or ""
            snippet = body.strip()
            if snippet and snippet not in old_body:
                new_body = old_body.rstrip() + "\n\n---\n\n" + snippet + "\n"
            else:
                new_body = old_body
            merged_tags = list(dict.fromkeys((existing.get("tags") or []) + (tags or [])))
            merged_links = list(
                dict.fromkeys((existing.get("links") or []) + (links or []))
            )
            # rewrite cleanly
            content = self._compose(title, new_body, note_type or existing.get("type"), merged_tags, merged_links)
            return self.write_note(title, content, overwrite=True)
        content = self._compose(title, body, note_type, tags or [], links or [])
        return self.write_note(title, content, overwrite=True)

    def _compose(
        self,
        title: str,
        body: str,
        note_type: str | None,
        tags: list[str],
        links: list[str],
    ) -> str:
        clean = body.strip()
        if not clean.lstrip().startswith("#"):
            clean = f"# {title}\n\n{clean}"
        existing_links = set(WIKILINK_RE.findall(clean))
        extra = [f"[[{slugify(l)}]]" for l in links if slugify(l) not in existing_links]
        if extra:
            clean = clean.rstrip() + "\n\nRelated: " + ", ".join(extra)
        fm = [
            "---",
            f"type: {note_type or 'concept'}",
            f"updated: {_now_iso()}",
        ]
        if tags:
            fm.append("tags: [" + ", ".join(tags) + "]")
        fm.append("---")
        return "\n".join(fm) + "\n\n" + clean.strip() + "\n"

    def delete_note(self, title_or_id: str) -> bool:
        path = self.note_path(title_or_id)
        if not path.exists():
            for p in self.wiki.glob("*.md"):
                if p.stem.lower() == title_or_id.lower():
                    path = p
                    break
            else:
                return False
        path.unlink(missing_ok=True)
        return True

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        q = (query or "").strip().lower()
        if not q:
            return self.list_notes()[:limit]
        scored: list[tuple[int, dict[str, Any]]] = []
        for n in self.list_notes():
            full = self.read_note(n["id"])
            if not full:
                continue
            hay = f"{full['title']}\n{full.get('body','')}\n{' '.join(full.get('tags') or [])}".lower()
            score = 0
            if q in full["title"].lower():
                score += 10
            score += hay.count(q)
            for word in q.split():
                if word in hay:
                    score += 1
            if score:
                scored.append((score, n))
        scored.sort(key=lambda x: -x[0])
        return [n for _, n in scored[:limit]]

    def vault_context(self, query: str = "", limit: int = 12) -> str:
        """Compact text context for Local grounded answers."""
        hits = self.search(query, limit=limit) if query else self.list_notes()[:limit]
        chunks: list[str] = []
        for h in hits:
            full = self.read_note(h["id"])
            if not full:
                continue
            body = (full.get("body") or "")[:1200]
            chunks.append(f"### [[{full['title']}]]\n{body}")
        if not chunks:
            return "(vault is empty)"
        return "\n\n".join(chunks)

    def save_inbox(self, name: str, content: str) -> Path:
        safe = slugify(name) + ".md"
        path = self.inbox / safe
        path.write_text(content, encoding="utf-8")
        return path

    def export_obsidian_hint(self) -> str:
        return (
            "This vault is plain Markdown with [[wikilinks]]. "
            f"Open `{self.root}` in Obsidian or another Markdown editor if desired."
        )

    def merge_notes(self, source_id: str, target_id: str) -> dict[str, Any] | None:
        """Append source into target, then delete source. Returns target meta."""
        src = self.read_note(source_id)
        tgt = self.read_note(target_id)
        if not src or not tgt:
            return None
        if src["id"] == tgt["id"]:
            return tgt
        tags = list(dict.fromkeys((tgt.get("tags") or []) + (src.get("tags") or [])))
        links = list(
            dict.fromkeys(
                (tgt.get("links") or [])
                + (src.get("links") or [])
                + [src.get("title") or src["id"]]
            )
        )
        body = (tgt.get("body") or "").rstrip()
        add = (src.get("body") or "").strip()
        # strip leading H1 from source if present
        if add.startswith("# "):
            add = "\n".join(add.splitlines()[1:]).strip()
        merged = (
            body
            + f"\n\n---\n\n## Merged from [[{src.get('title') or src['id']}]]\n\n"
            + add
            + "\n"
        )
        meta = self.upsert_note(
            tgt.get("title") or tgt["id"],
            merged,
            note_type=tgt.get("type") or "concept",
            tags=tags,
            links=[l for l in links if l and l != tgt["id"] and l != tgt.get("title")],
            merge=False,
        )
        self.delete_note(src["id"])
        return meta

    def export_zip(self, dest: Path) -> Path:
        """Zip wiki + inbox into dest path."""
        import zipfile

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            for folder in (self.wiki, self.inbox):
                if not folder.exists():
                    continue
                for path in folder.rglob("*"):
                    if path.is_file():
                        zf.write(path, arcname=str(path.relative_to(self.root)))
        return dest


def _parse_simple_yaml(block: str) -> dict[str, Any]:
    """Minimal YAML subset for frontmatter (key: value, tags list)."""
    out: dict[str, Any] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            if not inner:
                out[key] = []
            else:
                out[key] = [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
        else:
            out[key] = val.strip("'\"")
    return out


def _enrich_body(body: str, fm: dict[str, Any] | None = None) -> dict[str, Any]:
    """Derive summary, description, sections, word counts from note body."""
    fm = fm or {}
    text = body or ""
    # strip headings for prose analysis
    lines = text.splitlines()
    sections: list[str] = []
    prose_lines: list[str] = []
    for line in lines:
        s = line.strip()
        if s.startswith("#"):
            sections.append(re.sub(r"^#+\s*", "", s).strip())
            continue
        if s.startswith("---") and len(s) <= 5:
            continue
        if s.startswith("Related:"):
            continue
        prose_lines.append(s)

    prose = "\n".join(prose_lines).strip()
    # first non-empty paragraph
    paras = [p.strip() for p in re.split(r"\n\s*\n", prose) if p.strip()]
    first_para = paras[0] if paras else ""
    first_para = re.sub(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]", r"\1", first_para)
    first_para = re.sub(r"[*`_#]+", "", first_para).strip()

    description = (fm.get("description") or fm.get("summary") or "").strip()
    if not description:
        description = first_para
    if len(description) > 320:
        description = description[:317].rstrip() + "…"

    summary = description
    if len(summary) > 160:
        summary = summary[:157].rstrip() + "…"

    words = re.findall(r"[A-Za-z0-9']+", prose)
    return {
        "description": description,
        "summary": summary,
        "preview": (first_para or description)[:240],
        "word_count": len(words),
        "link_count": len(WIKILINK_RE.findall(text)),
        "sections": [s for s in sections if s][:12],
    }


DEFAULT_SETTINGS: dict[str, Any] = {
    # LLM: xai | ollama | hybrid
    "llm_provider": "ollama",
    "hybrid_chat": "ollama",
    "hybrid_extract": "ollama",
    "chat_model": "local-4.5",
    "extract_model": "local-4.3",
    "ollama_base_url": "http://127.0.0.1:11434",
    "ollama_chat_model": "huihui_ai/gemma-4-abliterated:e2b",
    "ollama_extract_model": "huihui_ai/gemma-4-abliterated:e2b",
    "ollama_api_key": "ollama",
    # Local usage kit: fast | balanced | quality | vision | code | thinking
    "ollama_local_preset": "balanced",
    # Ollama efficiency and bounded context for 6GB-class GPUs
    "ollama_num_ctx": 16384,
    "ollama_keep_alive": "-1",
    "ollama_num_batch": None,
    "ollama_chat_tokens": -1,
    "show_generation_stats": True,
    "ollama_extract_tokens": 768,
    "ollama_history_turns": 6,
    "ollama_memory_chars": 3500,
    "ollama_max_notes": 6,
    "matrix_history_mode": "current_chat",
    "matrix_history_turns": 24,
    "extract_growth": "balanced",
    "extract_fallback": True,  # heuristic nodes when LLM extract is empty
    "use_embeddings": True,
    "embed_model": "nomic-embed-text",
    # RAG v2: explicit external knowledge store, separate from legacy Memory v1.
    # CPU-only BM25 retrieval keeps the chat model resident on 6GB GPUs.
    "rag_enabled": True,
    "rag_top_k": 4,
    "rag_context_chars": 6000,
    "rag_chunk_chars": 1800,
    "rag_chunk_overlap": 240,
    "rag_min_score": 0.25,
    "hybrid_auto": False,
    "reduce_motion": False,
    "onboarding_done": False,
    "theme_preset": "ember",
    "ui_mode": "classic",  # classic | modern; visual style independent of color theme
    "ui_colors": {
        "enabled": True,
        "background": "#050505",
        "panel": "#000000",
        "surface": "#000000",
        "border": "#43005c",
        "text": "#ff0088",
        "muted": "#2b00ff",
        "accent": "#7300ff",
        "accent2": "#ff0026",
        "success": "#00ff04",
        "warning": "#fbbf24",
        "danger": "#ff002f",
        "chatBackground": "#050505",
        "userMessage": "#33000c",
        "assistantMessage": "#070024",
        "thinkingBackground": "#100609",
        "thinkingText": "#99a0ff",
    },
    "voice_id": "eve",
    "voice_model": "local-voice-latest",
    "auto_extract": False,
    "speak_replies": True,
    # Conversational flow + quality + TTS
    "conversation_flow": True,
    "conversation_style": "concise",  # natural | concise | socratic | technical
    "error_reduction": True,
    "voice_output_enabled": False,
    "tts_provider": "local",  # local | edge | browser | legacy_cloud | off
    "tts_local_voice": "en_US-lessac-medium",
    "tts_allow_online": False,
    "tts_edge_voice": "en-US-AvaNeural",
    "tts_online_fallback": "piper",
    "tts_rate": 1.0,
    "tts_pitch": 1.0,
    "tts_speak_director": True,
    "tts_speak_system": False,
    "tts_skip_code": True,
    "tts_skip_urls": True,
    "tts_max_chars": 1000,
    "tts_stop_previous": True,
    "tts_cpu_threads": 2,
    "legacy_cloud_key": "",
    "port": 8765,
    "window_width": 1440,
    "window_height": 900,
    "ui_density": "comfortable",
    "chat_temperature": 0.65,
    "memory_context_limit": 10,
    "auto_open_new_notes": False,
    # Legacy v1 memory compatibility: explicitly pinned note titles/ids.
    "sticky_pins": [],
    # CypraMatrix — local MatrixFiles/ Modelfiles + SYSTEM directives
    "matrix_enabled": True,
    "matrix_agent": "chloe",
    "matrix_root": "",  # empty = auto-detect ./MatrixFiles (never Documents\CypraTeam)
    "matrix_handoff": False,
    "show_model_thinking": True,
    "think_mode": "auto",  # off | auto | standard | deep
    "think_budget_tokens": 768,  # soft Standard reasoning budget; Deep may use 2x
    "plain_chat": False,
    "confirm_destructive": True,
    "ui_font_scale": 1.0,
    "chat_font_scale": 1.0,
    "chat_bg_strength": 20.0,
    "settings_schema": 38,
}

# Keys reset per Settings tab (API + UI)
SETTINGS_SECTIONS: dict[str, list[str]] = {
    # Only controls exposed by the current Settings workspace belong here.
    # Dormant Memory v1 configuration is deliberately excluded from tab resets.
    "ai": [
        "llm_provider", "chat_model", "ollama_base_url", "ollama_chat_model",
        "ollama_local_preset", "ollama_num_ctx", "ollama_keep_alive",
        "ollama_num_batch", "ollama_chat_tokens", "show_generation_stats",
        "ollama_history_turns", "speak_replies", "conversation_flow",
        "conversation_style", "error_reduction", "tts_provider",
        "voice_output_enabled", "tts_local_voice", "tts_allow_online",
        "tts_edge_voice", "tts_online_fallback", "tts_rate",
        "tts_speak_director", "tts_speak_system", "tts_skip_code",
        "tts_skip_urls", "tts_max_chars", "tts_stop_previous",
        "tts_cpu_threads", "chat_temperature", "matrix_enabled",
        "matrix_agent", "show_model_thinking", "think_mode", "think_budget_tokens", "plain_chat", "matrix_handoff",
    ],
    "rag": [
        "rag_enabled", "rag_top_k", "rag_context_chars",
        "rag_chunk_chars", "rag_chunk_overlap", "rag_min_score",
    ],
    "visuals": ["theme_preset", "ui_mode", "ui_colors"],
    "ui": [
        "reduce_motion", "ui_density", "window_width", "window_height",
        "confirm_destructive", "ui_font_scale", "chat_font_scale",
        "chat_bg_strength",
    ],
}

# Explicit retired settings allowlist. Schema 36 deletes only these keys.
RETIRED_SETTING_KEYS = frozenset(['show_graph_hint', 'show_minimap', 'show_tooltips', 'bg_color', 'bg_edge', 'bg_mid', 'bloom_enabled', 'bloom_strength', 'brain_aspect', 'brain_settings', 'camera_auto_rotate', 'camera_rotate_speed', 'camera_zoom', 'center_strength', 'charge_strength', 'click_empty_deselect', 'cluster_analytical', 'cluster_creative', 'cluster_interactive', 'cluster_pull', 'cluster_speech', 'collision_strength', 'cooldown_ms', 'core_color', 'core_glow', 'core_link_color', 'core_link_distance', 'core_particle_count', 'core_particle_size', 'core_particle_speed', 'core_pulse_strength', 'core_ring_count', 'core_ring_speed', 'core_shape', 'core_size', 'cortex_count', 'cortex_fillers', 'depth_scale', 'dim_others_on_select', 'flash_color', 'fog_enabled', 'galaxy_arms', 'galaxy_mode', 'galaxy_turns', 'ghost_color', 'glow_intensity', 'graph_mode', 'highlight_color', 'label_bg', 'label_bg_opacity', 'label_color', 'label_mode', 'label_scale', 'layout_strength', 'layout_style', 'link_color', 'link_distance', 'link_opacity', 'link_particles', 'link_strength', 'link_width', 'market_data_enabled', 'market_data_provider', 'membrane', 'membrane_color', 'membrane_hex', 'membrane_pulse', 'membrane_rgb', 'membrane_ripples', 'membrane_shape', 'neural_style', 'node_color', 'node_look', 'node_rel_size', 'node_rim', 'node_shape', 'note_border_width', 'note_color_mode', 'note_fill_opacity', 'note_glow', 'note_use_cluster_tint', 'orbit_inertia', 'orbit_radius', 'particle_color', 'particle_count', 'path_bend', 'path_color', 'path_dash', 'path_style', 'phosphor_bloom', 'physics_enabled', 'physics_profile', 'pin_color', 'radial_strength', 'retro_crt', 'retro_grid', 'scanlines', 'second_brain_accent', 'second_brain_color', 'second_brain_glow', 'second_brain_label', 'second_brain_shape', 'second_brain_show_label', 'second_brain_size', 'second_brain_sub', 'show_branch_labels', 'show_cluster_labels', 'show_node_labels', 'starfield_enabled', 'tag_link_color', 'terminal_hud', 'theme_accent', 'type_concept', 'type_decision', 'type_entity', 'type_fact', 'type_person', 'type_preference', 'type_project', 'type_session', 'velocity_decay', 'warmup_ticks', 'zoom_max', 'zoom_min', 'side_pane_wide', 'chat_box_opacity', 'text_color', 'kokoro_voice'])

def reset_settings_section(settings: dict[str, Any], section: str) -> dict[str, Any]:
    """Reset one settings group (or all) to DEFAULT_SETTINGS. Preserves API key."""
    s = dict(settings)
    section = (section or "all").strip().lower()
    keep_key = s.get("legacy_cloud_key", "")
    if section == "all":
        out = dict(DEFAULT_SETTINGS)
        out["legacy_cloud_key"] = keep_key
        out["onboarding_done"] = s.get("onboarding_done", True)
        out["settings_schema"] = max(int(DEFAULT_SETTINGS.get("settings_schema") or 0), 8)
        for key in RETIRED_SETTING_KEYS:
            out.pop(key, None)
        return out
    keys = SETTINGS_SECTIONS.get(section)
    if not keys:
        raise ValueError(f"Unknown settings section: {section}")
    for k in keys:
        if k in DEFAULT_SETTINGS:
            s[k] = DEFAULT_SETTINGS[k]
    for key in RETIRED_SETTING_KEYS:
        s.pop(key, None)
    s["settings_schema"] = max(int(s.get("settings_schema") or 0), 8)
    return s


def migrate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Schema 38: preserve RAG/UI settings and normalize Adaptive Think Control.

    Unknown and unrelated keys are preserved so cleanup cannot wipe current or
    future settings. Legacy memory compatibility, chat, agents, runtime, TTS, plugins, themes,
    and UI color settings are not migrated away.
    """
    s = dict(settings or {})
    retired = set(RETIRED_SETTING_KEYS) | {
        "brain_settings", "market_data_enabled", "market_data_provider",
        "code_swarm_agent_timeout", "code_swarm_shell_timeout",
        "code_swarm_model_profile", "code_swarm_json_mode",
        "code_swarm_repairs", "code_swarm_protected_paths",
    }
    for key in retired:
        s.pop(key, None)
    mode = str(s.get("ui_mode") or "classic").strip().lower()
    s["ui_mode"] = mode if mode in ("classic", "modern") else "classic"
    think_mode = str(s.get("think_mode") or "auto").strip().lower()
    s["think_mode"] = think_mode if think_mode in ("off", "auto", "standard", "deep") else "auto"
    try:
        s["think_budget_tokens"] = max(128, min(8192, int(s.get("think_budget_tokens") or 768)))
    except (TypeError, ValueError):
        s["think_budget_tokens"] = 768
    s["settings_schema"] = 38
    return s


def load_settings(path: Path) -> dict[str, Any]:
    defaults = dict(DEFAULT_SETTINGS)
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw = data
                defaults.update(data)
        except (OSError, json.JSONDecodeError):
            pass
    migrated = migrate_settings(defaults)
    # Persist when schema advanced or physics/theme still look legacy
    dirty = int(raw.get("settings_schema") or 0) < int(DEFAULT_SETTINGS["settings_schema"]) or any(
        key in raw for key in RETIRED_SETTING_KEYS
    )
    try:
        if float(raw.get("charge_strength", -180)) <= -300:
            dirty = True
        if float(raw.get("link_distance", 48)) >= 80:
            dirty = True
        if float(raw.get("velocity_decay", 0.87)) < 0.55:
            dirty = True
    except (TypeError, ValueError):
        dirty = True
    if dirty:
        try:
            save_settings(path, migrated)
        except OSError:
            pass
    return migrated


def save_settings(path: Path, settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: settings.json is rewritten often, and on removable/USB media
    # a write can be interrupted (drive pulled, sleep, power loss) mid-save. A
    # direct write_text() can leave a truncated/corrupt file that breaks the next
    # launch. Writing to a temp file then replacing keeps the old file intact
    # until the new one is fully flushed to disk.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    tmp.replace(path)
