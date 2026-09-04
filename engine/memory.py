"""
Shared local memory index for durable notes.

Everything lives in project-local files plus a JSON search index. Stored notes can
be ranked lexically, semantically, by usage, and by explicit note relationships.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-]{1,40}", re.I)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]")
TITLE_NORM_RE = re.compile(r"[^a-z0-9]+", re.I)
STOP = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of",
    "is", "are", "was", "were", "be", "been", "being", "it", "this", "that",
    "with", "as", "by", "from", "you", "i", "we", "they", "he", "she", "my",
    "your", "our", "their", "not", "no", "yes", "do", "does", "did", "have",
    "has", "had", "will", "would", "could", "should", "can", "may", "if",
    "then", "than", "so", "just", "about", "into", "over", "also", "how",
    "what", "when", "where", "who", "why", "which", "there", "here", "its",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for m in TOKEN_RE.finditer(text or ""):
        t = m.group(0).lower()
        if t in STOP or t.isdigit():
            continue
        out.append(t)
    return out


class MemoryIndex:
    """
    Local inverted index and usage statistics over the shared memory vault.

    Persisted at data/memory/index.json so cold starts stay fast on D:.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "index.json"
        self.usage_path = self.root / "usage.json"
        # doc_id -> {tokens: Counter-like dict, title, type, updated}
        self.docs: dict[str, dict[str, Any]] = {}
        # token -> {doc_id: tf}
        self.inv: dict[str, dict[str, int]] = {}
        # usage: doc_id -> {hits, last_used, strength}
        self.usage: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.docs = raw.get("docs") or {}
                self.inv = raw.get("inv") or {}
            except (OSError, json.JSONDecodeError):
                self.docs, self.inv = {}, {}
        if self.usage_path.exists():
            try:
                self.usage = json.loads(self.usage_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.usage = {}

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"docs": self.docs, "inv": self.inv, "saved_at": _now_iso()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.path)
        utmp = self.usage_path.with_suffix(".tmp")
        utmp.write_text(json.dumps(self.usage, indent=2), encoding="utf-8")
        utmp.replace(self.usage_path)

    def rebuild_from_vault(self, vault: Any) -> dict[str, int]:
        """Full reindex from markdown vault (shared across all chats)."""
        self.docs.clear()
        self.inv.clear()
        count = 0
        live_ids: set[str] = set()
        for meta in vault.list_notes():
            full = vault.read_note(meta["id"])
            if not full:
                continue
            nid = full["id"]
            live_ids.add(nid)
            self._index_doc(
                nid,
                full.get("title") or nid,
                full.get("type") or "concept",
                full.get("body") or "",
                full.get("tags") or [],
                full.get("links") or [],
            )
            count += 1
        # Drop usage for notes that no longer exist on disk
        usage_removed = 0
        for did in list(self.usage.keys()):
            if did not in live_ids:
                del self.usage[did]
                usage_removed += 1
        self.save()
        return {"indexed": count, "usage_pruned": usage_removed}

    def remove_doc(self, doc_id: str, *, save: bool = True) -> bool:
        """Remove one doc from index + inverted postings + usage (ghost cleanup)."""
        if not doc_id:
            return False
        removed = False
        old = self.docs.pop(doc_id, None)
        if old:
            removed = True
            for tok in old.get("tf") or {}:
                bucket = self.inv.get(tok)
                if not bucket:
                    continue
                if doc_id in bucket:
                    del bucket[doc_id]
                if not bucket:
                    self.inv.pop(tok, None)
        if doc_id in self.usage:
            del self.usage[doc_id]
            removed = True
        if removed and save:
            self.save()
        return removed

    def live_note_ids(self, vault: Any) -> set[str]:
        """Ids that still resolve via vault.list_notes / read_note."""
        live: set[str] = set()
        for meta in vault.list_notes() or []:
            mid = meta.get("id") or meta.get("title")
            if not mid:
                continue
            # Prefer list_notes ids; verify unreadable files as missing
            note = vault.read_note(mid)
            if note and note.get("id"):
                live.add(note["id"])
            elif mid:
                # listed but unreadable → not live
                pass
        return live

    def prune_missing(
        self,
        vault: Any,
        *,
        save: bool = True,
        also_unreadable: bool = True,
    ) -> dict[str, int]:
        """
        Clear shared-memory entries that are not retrievable:
        - index docs with no vault note
        - usage stats for deleted / missing notes
        - inverted-index postings left orphaned
        """
        live = self.live_note_ids(vault) if also_unreadable else {
            (m.get("id") or m.get("title"))
            for m in (vault.list_notes() or [])
            if m.get("id") or m.get("title")
        }
        live = {x for x in live if x}

        docs_removed = 0
        for did in list(self.docs.keys()):
            if did not in live:
                if self.remove_doc(did, save=False):
                    docs_removed += 1

        usage_removed = 0
        for did in list(self.usage.keys()):
            if did not in live:
                del self.usage[did]
                usage_removed += 1

        # Sweep empty inv buckets / postings to missing docs
        inv_cleaned = 0
        for tok in list(self.inv.keys()):
            bucket = self.inv.get(tok) or {}
            for did in list(bucket.keys()):
                if did not in live:
                    del bucket[did]
                    inv_cleaned += 1
            if not bucket:
                del self.inv[tok]

        if save and (docs_removed or usage_removed or inv_cleaned):
            self.save()

        return {
            "docs_removed": docs_removed,
            "usage_removed": usage_removed,
            "inv_postings_removed": inv_cleaned,
            "live_notes": len(live),
            "docs_remaining": len(self.docs),
        }

    def upsert_note(self, note: dict[str, Any]) -> None:
        nid = note.get("id") or note.get("title")
        if not nid:
            return
        self._index_doc(
            nid,
            note.get("title") or nid,
            note.get("type") or "concept",
            note.get("body") or note.get("content") or "",
            note.get("tags") or [],
            note.get("links") or [],
        )

    def _index_doc(
        self,
        doc_id: str,
        title: str,
        ntype: str,
        body: str,
        tags: list[str],
        links: list[str],
    ) -> None:
        # remove old postings
        old = self.docs.get(doc_id)
        if old:
            for tok in old.get("tf") or {}:
                bucket = self.inv.get(tok)
                if bucket and doc_id in bucket:
                    del bucket[doc_id]
                    if not bucket:
                        del self.inv[tok]

        text = f"{title}\n{title}\n{' '.join(tags)}\n{' '.join(links)}\n{body}"
        tokens = tokenize(text)
        tf: dict[str, int] = defaultdict(int)
        for t in tokens:
            tf[t] += 1
        # title boost stored as extra tf
        for t in tokenize(title):
            tf[t] += 3

        self.docs[doc_id] = {
            "title": title,
            "type": ntype,
            "tf": dict(tf),
            "len": max(1, len(tokens)),
            "tags": tags,
            "links": [str(x) for x in links],
            "updated": _now_iso(),
        }
        for tok, c in tf.items():
            self.inv.setdefault(tok, {})[doc_id] = c

    def touch(self, doc_ids: list[str], *, amount: float = 1.0) -> None:
        """Strengthen memories that were retrieved or opened (improves future rank)."""
        now = _now_iso()
        for did in doc_ids:
            if not did:
                continue
            u = self.usage.setdefault(
                did, {"hits": 0, "last_used": now, "strength": 0.0}
            )
            u["hits"] = int(u.get("hits") or 0) + 1
            u["last_used"] = now
            # diminishing returns so popular nodes don't dominate forever
            prev = float(u.get("strength") or 0)
            u["strength"] = prev + amount / (1.0 + prev * 0.15)
        # cheap persist of usage only
        try:
            self.usage_path.write_text(json.dumps(self.usage, indent=2), encoding="utf-8")
        except OSError:
            pass

    def search(
        self,
        query: str,
        *,
        limit: int = 12,
        expand_neighbors: int = 2,
    ) -> list[dict[str, Any]]:
        """BM25-ish rank + usage boost + optional linked-note neighbor expansion."""
        q_tokens = tokenize(query)
        if not q_tokens and not query.strip():
            # default: strongest / most used memories
            ranked = sorted(
                self.docs.keys(),
                key=lambda d: float(self.usage.get(d, {}).get("strength") or 0),
                reverse=True,
            )
            return [self._hit(d, 0.0) for d in ranked[:limit]]

        N = max(1, len(self.docs))
        avgdl = sum(int(d.get("len") or 1) for d in self.docs.values()) / N
        k1, b = 1.4, 0.75
        scores: dict[str, float] = defaultdict(float)

        for tok in set(q_tokens):
            postings = self.inv.get(tok) or {}
            df = len(postings) or 1
            idf = math.log(1 + (N - df + 0.5) / (df + 0.5))
            for doc_id, tf in postings.items():
                dl = int(self.docs[doc_id].get("len") or 1)
                denom = tf + k1 * (1 - b + b * dl / avgdl)
                scores[doc_id] += idf * (tf * (k1 + 1)) / denom

        # Intent-aware boosts for short identity/fact questions.
        # Generic questions like "project name?" or "what is the codename?"
        # often contain only one useful keyword, so a plain BM25 rank can
        # select a recently-created side node instead of the canonical
        # project/fact node. Prefer project/entity nodes for identity queries
        # without adding extra LLM/embedding work.
        q_low = (query or "").strip().lower()
        identity_q = any(k in q_low for k in (
            "project name", "project title", "app name", "application name",
            "codename", "code name", "what is the project",
        ))
        if identity_q:
            for doc_id, doc in self.docs.items():
                dtype = str(doc.get("type") or "").lower()
                title = str(doc.get("title") or "").lower()
                boost = 0.0
                if dtype in ("project", "entity"):
                    boost += 1.8
                # Stronger preference when the title itself is name-like.
                if any(k in title for k in ("memory", "project", "app", "application", "codename")):
                    boost += 0.8
                if boost and doc_id in scores:
                    scores[doc_id] *= 1.0 + boost

        # usage / recency boost — recent + strong memories rank higher
        now = time.time()
        for doc_id in list(scores.keys()):
            u = self.usage.get(doc_id) or {}
            strength = float(u.get("strength") or 0)
            hits = int(u.get("hits") or 0)
            scores[doc_id] *= 1.0 + 0.1 * math.log1p(hits) + 0.07 * strength
            last = u.get("last_used")
            if last:
                try:
                    # ISO timestamps → age decay (fresher = higher)
                    ts = str(last).replace("Z", "+00:00")
                    age_h = max(
                        0.0,
                        (now - datetime.fromisoformat(ts).timestamp()) / 3600.0,
                    )
                    # full boost within ~6h, fades over ~7 days
                    recency = math.exp(-age_h / (24 * 3.5))
                    scores[doc_id] *= 1.0 + 0.35 * recency
                except Exception:
                    scores[doc_id] *= 1.05

        ordered = sorted(scores.items(), key=lambda x: -x[1])
        top = ordered[:limit]
        hits = [self._hit(d, s) for d, s in top]

        # Expand through explicit wikilinks from top-ranked notes.
        if expand_neighbors and hits:
            seen = {h["id"] for h in hits}
            extras: list[dict[str, Any]] = []
            for h in hits[: max(1, limit // 2)]:
                doc = self.docs.get(h["id"]) or {}
                for link in (doc.get("links") or [])[:expand_neighbors]:
                    # links may be titles; try id match
                    lid = link
                    if lid not in self.docs:
                        # fuzzy: match by title
                        for did, d in self.docs.items():
                            if (d.get("title") or "").lower() == str(link).lower() or did.lower() == str(link).lower():
                                lid = did
                                break
                    if lid in seen or lid not in self.docs:
                        continue
                    seen.add(lid)
                    extras.append(self._hit(lid, (scores.get(h["id"]) or 0) * 0.35))
            hits.extend(extras[:expand_neighbors * 2])

        return hits[: limit + expand_neighbors * 2]

    def _hit(self, doc_id: str, score: float) -> dict[str, Any]:
        d = self.docs.get(doc_id) or {}
        u = self.usage.get(doc_id) or {}
        return {
            "id": doc_id,
            "title": d.get("title") or doc_id,
            "type": d.get("type") or "concept",
            "score": round(float(score), 4),
            "hits": int(u.get("hits") or 0),
            "strength": round(float(u.get("strength") or 0), 3),
            "tags": d.get("tags") or [],
            "links": d.get("links") or [],
        }

    def context_for_chat(
        self,
        vault: Any,
        query: str,
        *,
        limit: int = 10,
        pinned_ids: list[str] | None = None,
        settings: dict[str, Any] | None = None,
        embed_store: Any | None = None,
    ) -> tuple[str, list[str], list[dict[str, Any]]]:
        """Retrieve a bounded, relationship-aware slice of long-term memory.

        Strategy:
        1. normal keyword/semantic recall
        2. exact [[wikilink]] / exact-title anchors get a very large boost
        3. traverse linked neighbors in both directions for a few hops
        4. pack anchor note bodies + compact neighbor evidence into context

        The entire vault is never dumped into the model prompt, keeping local generation
        responsive while preserving useful relationship traversal.
        """
        settings = settings or {}
        pinned_ids = pinned_ids or []
        local = str(settings.get("llm_provider") or "").lower() in ("ollama", "local", "hybrid")

        # ── baseline lexical recall ──────────────────────────────────
        hits = self.search(query, limit=max(limit, 8))
        score_map: dict[str, float] = {h["id"]: float(h.get("score") or 0) for h in hits}
        bm25_ids = {h["id"] for h in hits if float(h.get("score") or 0) > 0}
        embed_ids: set[str] = set()

        # Semantic search stays off the hot local path: live embeddings can
        # evict the chat model and create the exact lag we are avoiding.
        local_chat = str(settings.get("llm_provider") or "").lower() in ("ollama", "local", "hybrid")
        if (
            embed_store is not None
            and settings.get("use_embeddings", True)
            and not local_chat
        ):
            try:
                sem = embed_store.search(
                    query,
                    settings=settings,
                    limit=min(max(limit, 8), 8),
                    live_ids=set(self.docs.keys()),
                )
                for did, sc in sem:
                    score_map[did] = max(score_map.get(did, 0.0), 0.0) + float(sc) * 8.0
                    if float(sc) > 0.05:
                        embed_ids.add(did)
            except Exception:
                pass

        # ── exact memory anchors ────────────────────────────────────
        def norm_title(value: str) -> str:
            return " ".join(TITLE_NORM_RE.sub(" ", str(value or "").lower()).split())

        title_to_id = {norm_title((d.get("title") or did)): did for did, d in self.docs.items()}
        query_links = [m.group(1).strip() for m in WIKILINK_RE.finditer(query or "") if m.group(1).strip()]
        anchor_ids: list[str] = []

        def add_anchor(did: str) -> None:
            if did and did in self.docs and did not in anchor_ids:
                anchor_ids.append(did)

        for label in query_links:
            add_anchor(title_to_id.get(norm_title(label), label if label in self.docs else ""))

        # Also support natural-language exact title references without [[ ]].
        qnorm = norm_title(query)
        if qnorm:
            for nt, did in title_to_id.items():
                if nt and (f" {nt} " in f" {qnorm} " or nt == qnorm):
                    add_anchor(did)

        # Favor all explicit anchors. This is critical for relation questions:
        # both endpoints must be present even if BM25 dislikes one of them.
        for idx, did in enumerate(anchor_ids):
            score_map[did] = max(score_map.get(did, 0.0), 100.0 - idx)

        # ── relationship traversal (forward + reverse) ───────────────
        reverse: dict[str, set[str]] = defaultdict(set)
        title_lookup = {norm_title((d.get("title") or did)): did for did, d in self.docs.items()}
        for did, doc in self.docs.items():
            for link in doc.get("links") or []:
                target = title_lookup.get(norm_title(link), link if link in self.docs else "")
                if target and target in self.docs and target != did:
                    reverse[target].add(did)

        nav_hops = 3 if len(anchor_ids) else 2
        frontier = list(anchor_ids)
        visited = set(anchor_ids)
        distance = {did: 0 for did in anchor_ids}
        while frontier and max(distance.values(), default=0) < nav_hops and len(visited) < 36:
            current = frontier.pop(0)
            cur_doc = self.docs.get(current) or {}
            neighbors: list[str] = []
            for link in cur_doc.get("links") or []:
                target = title_lookup.get(norm_title(link), link if link in self.docs else "")
                if target:
                    neighbors.append(target)
            neighbors.extend(sorted(reverse.get(current, set())))
            for nxt in neighbors:
                if nxt in visited or nxt not in self.docs:
                    continue
                d = distance[current] + 1
                distance[nxt] = d
                visited.add(nxt)
                frontier.append(nxt)
                # Navigation evidence is useful, but query matches and exact
                # anchors remain dominant.
                score_map[nxt] = max(score_map.get(nxt, 0.0), 28.0 / d)

        # If there are multiple anchors, add a compact shortest-path trace.
        relation_paths: list[list[str]] = []
        if len(anchor_ids) >= 2:
            from collections import deque
            for target in anchor_ids[1:3]:
                q = deque([anchor_ids[0]])
                prev = {anchor_ids[0]: None}
                while q and target not in prev:
                    cur = q.popleft()
                    neigh = set()
                    doc = self.docs.get(cur) or {}
                    for link in doc.get("links") or []:
                        x = title_lookup.get(norm_title(link), link if link in self.docs else "")
                        if x:
                            neigh.add(x)
                    neigh.update(reverse.get(cur, set()))
                    for nxt in neigh:
                        if nxt not in prev:
                            prev[nxt] = cur
                            q.append(nxt)
                if target in prev:
                    path: list[str] = []
                    cur = target
                    while cur is not None:
                        path.append(cur)
                        cur = prev[cur]
                    relation_paths.append(list(reversed(path)))

        # sticky pins
        sticky_raw = list(settings.get("sticky_pins") or [])
        sticky_ids: list[str] = []
        for sp in sticky_raw:
            if not sp:
                continue
            note = vault.read_note(str(sp))
            if note and note.get("id"):
                sticky_ids.append(note["id"])
            elif str(sp) in self.docs:
                sticky_ids.append(str(sp))
        pin_set = list(dict.fromkeys(list(pinned_ids) + sticky_ids))
        for pid in pin_set:
            if pid:
                score_map[pid] = max(score_map.get(pid, 0.0), 50.0)

        # Exact anchors first, then pins, then ranked search/navigation.
        ordered: list[str] = []
        for did in anchor_ids + pin_set:
            if did and did in self.docs and did not in ordered:
                ordered.append(did)
        for did, _score in sorted(score_map.items(), key=lambda x: -x[1]):
            if did not in ordered and did in self.docs:
                ordered.append(did)

        # Local prompt budget: enough for a few strong nodes without pushing
        # Ollama beyond its small context window.
        context_char_cap = int(settings.get("ollama_memory_chars") or (2600 if local else 12000))
        context_char_cap = max(1200, min(8000 if local else 24000, context_char_cap))
        anchor_set = set(anchor_ids)
        sticky_set = set(sticky_ids)
        pin_only = set(pinned_ids)

        chunks: list[str] = []
        used: list[str] = []
        recall_meta: list[dict[str, Any]] = []
        ghosts: list[str] = []
        char_count = 0

        for did in ordered:
            note = vault.read_note(did)
            if not note:
                ghosts.append(did)
                continue
            depth = distance.get(did)
            is_anchor = did in anchor_set
            is_pin = did in pin_set
            desc = (note.get("description") or note.get("summary") or "").strip()
            body = (note.get("body") or "").strip()
            if is_anchor:
                body_cap = 700 if local else 1400
            elif is_pin:
                body_cap = 500 if local else 1000
            else:
                body_cap = 180 if local else 500
            body = body[:body_cap]
            links = note.get("links") or []
            u = self.usage.get(did) or {}
            strength = float(u.get("strength") or 0)
            tags = ", ".join(str(x) for x in (note.get("tags") or [])[:6])

            if is_anchor:
                why = "exact-anchor"
            elif did in bm25_ids:
                why = "keyword"
            elif did in embed_ids:
                why = "semantic"
            elif depth is not None:
                why = f"relationship-hop-{depth}"
            elif did in sticky_set:
                why = "sticky"
            elif did in pin_only:
                why = "pinned"
            else:
                why = "rank"

            head = f"### [[{note.get('title') or did}]] ({note.get('type') or 'concept'}; {why})"
            if tags:
                head += f"\nTags: {tags}"
            if desc:
                head += f"\n> {desc[:260 if is_anchor else 170]}"
            if links:
                head += "\nLinks: " + ", ".join(f"[[{x}]]" for x in links[:8 if is_anchor else 4])
            piece = head + (f"\n{body}" if body else "")
            if char_count and char_count + len(piece) + 2 > context_char_cap:
                # Never cut an exact anchor because of later relationship hops.
                if not is_anchor:
                    continue
                if len(piece) > context_char_cap - char_count:
                    remaining = max(500, context_char_cap - char_count - 2)
                    piece = piece[:remaining]
            chunks.append(piece)
            char_count += len(piece) + 2
            used.append(did)
            recall_meta.append({
                "id": did,
                "title": note.get("title") or did,
                "why": why,
                "score": round(float(score_map.get(did) or 0), 3),
            })
            if char_count >= context_char_cap:
                break

        if ghosts:
            for gid in ghosts:
                self.remove_doc(gid, save=False)
            self.save()

        if relation_paths:
            paths = []
            for path in relation_paths:
                paths.append(" → ".join(f"[[{self.docs[x].get('title') or x}]]" for x in path if x in self.docs))
            relation_block = "## MEMORY RELATIONSHIPS\n" + "\n".join(f"- {p}" for p in paths if p)
            chunks.insert(0, relation_block)
            char_count += len(relation_block) + 2
            # Meta for the UI — not a model-facing hallucination hint.
            for path in relation_paths:
                if len(path) >= 2:
                    for did in path[1:-1]:
                        if did in self.docs and did not in used:
                            used.append(did)
                            recall_meta.append({
                                "id": did,
                                "title": self.docs[did].get("title") or did,
                                "why": "related-note",
                                "score": 18.0,
                            })

        if used:
            # Compact linked-note evidence for relationship reasoning.
            neighborhood_lines: list[str] = []
            budget = 1200 if local else 2200
            for did in used[:8]:
                doc = self.docs.get(did) or {}
                title = doc.get("title") or did
                outgoing: list[str] = []
                for raw in doc.get("links") or []:
                    target = title_lookup.get(norm_title(raw), raw if raw in self.docs else "")
                    if target and target in self.docs:
                        outgoing.append(str(self.docs[target].get("title") or target))
                incoming = [
                    str(self.docs[x].get("title") or x)
                    for x in sorted(reverse.get(did, set())) if x in self.docs
                ]
                outgoing = list(dict.fromkeys(outgoing))[:4]
                incoming = list(dict.fromkeys(incoming))[:4]
                if not outgoing and not incoming:
                    continue
                bits: list[str] = []
                if outgoing:
                    bits.append("→ " + ", ".join(f"[[{x}]]" for x in outgoing))
                if incoming:
                    bits.append("← " + ", ".join(f"[[{x}]]" for x in incoming))
                neighborhood_lines.append(f"- [[{title}]]: " + " | ".join(bits))
                if len("\n".join(neighborhood_lines)) >= budget:
                    break
            if neighborhood_lines:
                neighborhood_block = "## RELATED MEMORY\n" + "\n".join(neighborhood_lines)
                chunks.insert(0, neighborhood_block[:budget + 24])
                char_count += len(neighborhood_block) + 2

            self.touch(used, amount=0.6)
        if not chunks:
            return "(memory is empty — talk to grow it)", [], []

        # Strong evidence header for local agents. This deliberately avoids
        # telling the model to become a canned answerer; it only defines how to
        # treat retrieved memory evidence.
        evidence_header = (
            "## RETRIEVED MEMORY\n"
            "The entries below are retrieved from the user's stored memory. Use them as evidence. "
            "When a question names [[notes]], answer from those notes and their stored links. "
            "Do not replace missing memory evidence with an unrelated generic explanation. "
            "If memory does not contain the requested fact, say that it is not stored or verified.\n\n"
        )
        return evidence_header + "\n\n".join(chunks), used, recall_meta

    def stats(self) -> dict[str, Any]:
        return {
            "notes_indexed": len(self.docs),
            "terms": len(self.inv),
            "touched_notes": sum(1 for u in self.usage.values() if int(u.get("hits") or 0) > 0),
            "total_hits": sum(int(u.get("hits") or 0) for u in self.usage.values()),
            "usage_entries": len(self.usage),
            "path": str(self.root),
        }
