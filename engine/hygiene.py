"""
Vault hygiene: seed merge aliases, junk prune, health snapshot.

All pure Python / vault IO — no LLM calls (safe on the hot path).
"""

from __future__ import annotations

import re
from typing import Any

from engine.quality import _norm_title, clean_note_title
from engine.vault import SEED_NOTES

# Map messy / short aliases → canonical seed (or stable hub) titles
SEED_ALIASES: dict[str, str] = {
    "ollama": "Ollama",
    "ollamma": "Ollama",
    "come ollamma": "Ollama",
    "come ollamma knows": "Ollama",
    "local": "Local",
    "local model": "Local",
    "local model overview": "Local",
    "second brain": "Second Brain",
    "knowledge graph": "Knowledge Graph",
    "wikilinks": "Wikilinks",
    "wikilink": "Wikilinks",
    "maps of content": "Maps of Content",
    "moc": "Maps of Content",
    "voice models": "Voice Models",
    "note types": "Note Types",
    "brain visualizer": "Cypra Studio",
    "brainvisualizer": "Cypra Studio",
    "cypra": "CYPRA Core Directive",
    "cypra core directive": "CYPRA Core Directive",
    "core directive": "Core Directive",
    "quiet human partnership": "Quiet Human Partnership",
    "formal high-reliability layer": "Formal High-Reliability Layer",
    "formal high reliability layer": "Formal High-Reliability Layer",
    "autonomy first": "Autonomy First",
    "truth comfort": "Truth > Comfort",
    "truth > comfort": "Truth > Comfort",
}

# Exact junk titles from noisy auto-extract (case-insensitive via norm)
JUNK_TITLES_EXACT: set[str] = {
    "parts does orion",
    "come ollamma knows",
    "know running program",
    "memory looking",
    "explain water chemical",
    "improve yourself",
    "utilizing brainvisualizer programs",
    "chat idea",
    "chat idea notes",
    "only our shared memory",
    "using only our shared memory",
}

_JUNK_TITLE_RE = re.compile(
    r"^(how|what|why|when|where|who|which|can you|parts does)\b",
    re.I,
)


def seed_titles() -> set[str]:
    return {n["title"] for n in SEED_NOTES}


def resolve_seed_alias(title: str) -> str | None:
    """If title (or cleaned form) maps to a seed/hub, return canonical title."""
    if not title:
        return None
    raw = _norm_title(title)
    if raw in SEED_ALIASES:
        return SEED_ALIASES[raw]
    cleaned = clean_note_title(title)
    if cleaned:
        c = _norm_title(cleaned)
        if c in SEED_ALIASES:
            return SEED_ALIASES[c]
        # exact seed title match
        for st in seed_titles():
            if _norm_title(st) == c:
                return st
    for st in seed_titles():
        if _norm_title(st) == raw:
            return st
    return None


def apply_seed_merge_to_extract(extract: dict[str, Any]) -> dict[str, Any]:
    """
    Remap extract note titles/links/relations onto seed hubs when aliases match.
    Merges duplicate notes that collapse to the same canonical title.
    """
    data = dict(extract or {})
    notes_in = list(data.get("notes") or [])
    merged: dict[str, dict[str, Any]] = {}  # norm canon → note
    order: list[str] = []

    for n in notes_in:
        if not isinstance(n, dict):
            continue
        title = (n.get("title") or "").strip()
        if not title:
            continue
        canon = resolve_seed_alias(title) or title
        key = _norm_title(canon)
        if key not in merged:
            nn = {**n, "title": canon}
            # keep original type unless seed alias forces richer type from seed file
            merged[key] = nn
            order.append(key)
        else:
            # merge content lightly into existing
            prev = merged[key]
            extra = (n.get("content") or n.get("body") or "").strip()
            old = (prev.get("content") or prev.get("body") or "").strip()
            if extra and extra not in old:
                prev["content"] = (old + "\n\n---\n\n" + extra) if old else extra
            links = list(dict.fromkeys((prev.get("links") or []) + (n.get("links") or [])))
            prev["links"] = links
            tags = list(dict.fromkeys((prev.get("tags") or []) + (n.get("tags") or [])))
            prev["tags"] = tags

    # remap links on notes
    for key in order:
        n = merged[key]
        new_links: list[str] = []
        seen: set[str] = set()
        for link in n.get("links") or []:
            lt = resolve_seed_alias(str(link)) or clean_note_title(str(link)) or str(link)
            lk = _norm_title(lt)
            if not lk or lk in seen:
                continue
            seen.add(lk)
            new_links.append(lt)
        n["links"] = new_links

    notes_out = [merged[k] for k in order]
    data["notes"] = notes_out

    title_set = {_norm_title(n["title"]) for n in notes_out}
    rels = []
    for r in data.get("relations") or []:
        if not isinstance(r, dict):
            continue
        a_raw = r.get("from") or r.get("source") or ""
        b_raw = r.get("to") or r.get("target") or ""
        a = resolve_seed_alias(str(a_raw)) or clean_note_title(str(a_raw)) or str(a_raw)
        b = resolve_seed_alias(str(b_raw)) or clean_note_title(str(b_raw)) or str(b_raw)
        if not a or not b or _norm_title(a) == _norm_title(b):
            continue
        if _norm_title(a) in title_set or _norm_title(b) in title_set:
            rels.append({**r, "from": a, "to": b})
    data["relations"] = rels
    data["_seed_merged"] = True
    return data


def is_junk_note(note: dict[str, Any], *, protected: set[str] | None = None) -> tuple[bool, str]:
    """
    Heuristic junk detector. Returns (is_junk, reason).
    Never marks protected titles (seeds + sticky hubs) as junk.
    """
    title = (note.get("title") or note.get("id") or "").strip()
    if not title:
        return True, "empty_title"
    nt = _norm_title(title)
    prot = protected or {_norm_title(t) for t in seed_titles()}
    if nt in prot or resolve_seed_alias(title):
        # seed/hub: never junk-delete (merge path handles aliases)
        return False, ""

    if nt in JUNK_TITLES_EXACT:
        return True, "known_junk_title"

    tags = {str(t).lower() for t in (note.get("tags") or [])}
    ntype = str(note.get("type") or "").lower()
    body = (note.get("body") or note.get("content") or "").strip()
    desc = (note.get("description") or "").strip()

    # Question-shaped titles left by old extract
    if _JUNK_TITLE_RE.match(title) or title.rstrip().endswith("?"):
        return True, "question_title"

    # Auto session scraps with thin content
    if ntype == "session" and ("auto" in tags or "session" in tags):
        if len(body) < 200 or body.count("### User") >= 1 and len(body) < 600:
            # short session dumps
            if len(title.split()) <= 4 and not any(
                k in nt for k in ("orion", "ollama", "cypra", "brain")
            ):
                return True, "thin_session_auto"

    # Wikilink-only stubs
    if "appeared as a wikilink" in body.lower() and len(body) < 280:
        return True, "wikilink_stub"

    # Keyword auto with almost no content
    if "keyword" in tags and "auto" in tags and len(body) < 160 and len(desc) < 40:
        return True, "keyword_stub"

    return False, ""


def find_junk_candidates(vault: Any) -> list[dict[str, Any]]:
    prot = {_norm_title(t) for t in seed_titles()}
    # also protect any live note that is a seed alias target
    for t in SEED_ALIASES.values():
        prot.add(_norm_title(t))
    out: list[dict[str, Any]] = []
    for meta in vault.list_notes() or []:
        full = vault.read_note(meta.get("id") or meta.get("title") or "")
        if not full:
            continue
        junk, reason = is_junk_note(full, protected=prot)
        if junk:
            out.append(
                {
                    "id": full.get("id"),
                    "title": full.get("title"),
                    "type": full.get("type"),
                    "reason": reason,
                }
            )
    return out


def prune_junk_notes(
    vault: Any,
    memory: Any | None = None,
    embed_store: Any | None = None,
    *,
    dry_run: bool = False,
    merge_aliases: bool = True,
) -> dict[str, Any]:
    """
    Merge alias notes into seeds, delete remaining junk.
    """
    merged: list[dict[str, str]] = []
    deleted: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []

    if merge_aliases:
        for meta in list(vault.list_notes() or []):
            full = vault.read_note(meta.get("id") or "")
            if not full:
                continue
            title = full.get("title") or full.get("id") or ""
            canon = resolve_seed_alias(title)
            if not canon or _norm_title(canon) == _norm_title(title):
                continue
            target = vault.read_note(canon)
            if not target:
                # write missing seed shell? skip — user may not have restored seeds
                skipped.append({"id": full["id"], "title": title, "reason": "missing_target"})
                continue
            if dry_run:
                merged.append(
                    {
                        "from": title,
                        "from_id": full["id"],
                        "to": canon,
                        "to_id": target["id"],
                    }
                )
                continue
            vault.merge_notes(full["id"], target["id"])
            if embed_store is not None:
                try:
                    embed_store.drop(full["id"])
                except Exception:
                    pass
            if memory is not None:
                try:
                    memory.remove_doc(full["id"], save=False)
                except Exception:
                    pass
            merged.append(
                {
                    "from": title,
                    "from_id": full["id"],
                    "to": canon,
                    "to_id": target["id"],
                }
            )

    candidates = find_junk_candidates(vault)
    for c in candidates:
        # skip if we just planned merge of this id
        if any(m.get("from_id") == c["id"] for m in merged):
            continue
        if dry_run:
            deleted.append(c)
            continue
        if vault.delete_note(c["id"]):
            if embed_store is not None:
                try:
                    embed_store.drop(c["id"])
                except Exception:
                    pass
            if memory is not None:
                try:
                    memory.remove_doc(c["id"], save=False)
                except Exception:
                    pass
            deleted.append(c)

    if not dry_run and memory is not None:
        try:
            memory.rebuild_from_vault(vault)
        except Exception:
            try:
                memory.save()
            except Exception:
                pass

    return {
        "ok": True,
        "dry_run": dry_run,
        "merged": merged,
        "deleted": deleted,
        "skipped": skipped,
        "merged_count": len(merged),
        "deleted_count": len(deleted),
        "notes_now": len(list(vault.wiki.glob("*.md"))) if hasattr(vault, "wiki") else None,
    }


def vault_health(
    vault: Any,
    memory: Any | None = None,
    embed_store: Any | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    notes = list(vault.list_notes() or [])
    junk = find_junk_candidates(vault)
    orphans = 0
    dead_links = 0
    try:
        from engine.analytics import analyze_vault

        a = analyze_vault(vault, memory)
        orphans = int(a.get("orphan_count") or 0)
        dead_links = int(a.get("dead_link_count") or 0)
    except Exception:
        pass

    mem_stats = memory.stats() if memory is not None else {}
    emb_stats = embed_store.stats() if embed_store is not None else {}
    embedded = int(emb_stats.get("embedded") or 0)
    n_notes = len(notes)
    missing_embeds = max(0, n_notes - embedded) if emb_stats else None

    s = settings or {}
    chat = s.get("ollama_chat_model") or s.get("chat_model")
    extract = s.get("ollama_extract_model") or s.get("extract_model")
    same_model = str(chat or "").strip() == str(extract or "").strip()

    return {
        "notes": n_notes,
        "junk_candidates": len(junk),
        "junk_sample": junk[:12],
        "orphans": orphans,
        "dead_links": dead_links,
        "indexed": mem_stats.get("notes_indexed"),
        "embeddings": emb_stats,
        "missing_embeds": missing_embeds,
        "seed_count": len(seed_titles()),
        "seeds_present": sum(
            1 for t in seed_titles() if vault.read_note(t)
        ),
        "chat_model": chat,
        "extract_model": extract,
        "same_model_vram": same_model,
        "num_ctx": s.get("ollama_num_ctx"),
        "auto_open_new_notes": s.get("auto_open_new_notes"),
        "sticky_pins": list(s.get("sticky_pins") or []),
        "extract_growth": s.get("extract_growth"),
    }
