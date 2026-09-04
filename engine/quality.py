"""
Error reduction for assistant replies and extract payloads.

Detects and mitigates:
- hallucinated [[wikilink]] citations not in the vault / memory context
- empty / truncated replies
- overconfident claims without memory grounding
- low-quality extract notes (empty titles, giant dumps)
"""

from __future__ import annotations

import re
from typing import Any

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]")
CERTAINTY_RE = re.compile(
    r"\b(definitely|certainly|always|never|guaranteed|without (?:a )?doubt|"
    r"it is (?:a )?fact that|proven that)\b",
    re.I,
)


def error_reduction_enabled(settings: dict[str, Any] | None) -> bool:
    return (settings or {}).get("error_reduction", True) is not False


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


# Cheap (regex-only) title hygiene — no LLM, microsecond-scale.
_TITLE_JUNK_PREFIX = re.compile(
    r"^(?:"
    r"(?:please\s+)?(?:can|could)\s+(?:you|i|we)\s+|"
    r"(?:please\s+)?(?:tell\s+me|explain|describe|define|list)\s+|"
    r"how(?:\s+(?:do|does|did|can|to|come))?\s+|"
    r"what(?:\s+(?:is|are|was|were|do|does|'s))?\s+|"
    r"why(?:\s+(?:is|are|do|does|did))?\s+|"
    r"when(?:\s+(?:is|are|do|does|did))?\s+|"
    r"where(?:\s+(?:is|are|do|does|did))?\s+|"
    r"who(?:\s+(?:is|are|was|were))?\s+|"
    r"which\s+|"
    r"parts?\s+(?:does|do|of)\s+|"
    r"(?:i\s+)?(?:want|need|like|prefer)\s+(?:to\s+)?|"
    r"(?:i|we|you)\s+(?:use|using|need|want|prefer)\s+(?:to\s+)?|"
    r"(?:using|use|uses|used)\s+|"
    r"(?:conversation|discussion|notes?|information|details|overview|guide|thoughts)\s+(?:about|on|for|of)\s+|"
    r"(?:the|a|an)?\s*(?:user|assistant)\s+(?:wants?|needs?|asks?)\s+(?:to\s+)?|"
    r"(?:this|that)\s+(?:node|note|memory|conversation)\s+(?:captures?|covers?|describes?|contains?)\s+|"
    r")",
    re.I,
)
_TITLE_JUNK_SUFFIX = re.compile(
    r"\s+(?:uses|used|using|is|are|was|were|does|do|did|can|will|should|"
    r"notes?|feature|overview|looking|knows?|effectively|properly|correctly|successfully|currently|today|here|now)$",
    re.I,
)
_TITLE_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "of",
    "in",
    "on",
    "for",
    "is",
    "are",
    "was",
    "were",
    "be",
    "it",
    "this",
    "that",
    "with",
    "as",
    "at",
    "by",
    "from",
    "my",
    "your",
    "our",
    "me",
    "you",
    "we",
    "i",
    "do",
    "can",
    "how",
    "what",
    "why",
    "when",
    "where",
    "who",
    "which",
    "please",
    "hi",
    "hello",
    "thanks",
    "thank",
    "ok",
    "okay",
    "yes",
    "no",
    "cool",
    "sure",
    "user",
    "assistant",
    "chat",
    "message",
    "reply",
    "question",
    "answer",
}
# small words kept lowercase inside Title Case (except first/last)
_TITLE_SMALL = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "but",
    "of",
    "in",
    "on",
    "for",
    "to",
    "vs",
    "via",
    "with",
    "from",
}


def clean_note_title(
    raw: str,
    *,
    max_words: int = 6,
    max_chars: int = 48,
) -> str:
    """
    Turn messy extract / chat phrases into atomic vault titles.

    Pure string ops — safe on the chat hot path (no model calls).
    Examples:
      "parts does Orion" → "Orion"
      "how is your memory looking" → "Memory"
      "Project Orion uses a Pi 5" → "Project Orion"
    """
    t = (raw or "").strip()
    if not t:
        return ""
    # strip markdown / quotes / wikilink wrappers
    t = re.sub(r"^\[\[|\]\]$", "", t)
    t = t.strip(" \"'`*_~")
    t = re.sub(r"^#+\s*", "", t)
    # first line / clause only (don't split decimals like Qwen2.5)
    t = t.split("\n", 1)[0]
    t = re.split(r"[!?]+|(?<!\d)\.(?!\d)", t, maxsplit=1)[0]
    t = re.split(r"[:;—–]\s+", t, maxsplit=1)[0]
    t = re.sub(r"\s+", " ", t).strip(" .-:;,")
    # peel question / filler prefixes a few times
    for _ in range(3):
        nxt = _TITLE_JUNK_PREFIX.sub("", t).strip(" .-:;,")
        if nxt == t or not nxt:
            break
        t = nxt
    t = _TITLE_JUNK_SUFFIX.sub("", t).strip(" .-:;,")
    # drop trailing question mark residue
    t = t.rstrip("?").strip()
    if not t:
        return ""
    # Cut sentence-like tails: "Project Orion uses a Pi…" → "Project Orion"
    verb_cut = re.search(
        r"\s+(?:uses?|using|runs?|running|is|are|was|were|has|have|had|"
        r"does|do|did|knows?|looking|makes?|needs?|wants?)\s+",
        t,
        re.I,
    )
    if verb_cut and verb_cut.start() >= 2:
        left = t[: verb_cut.start()].strip()
        if len(left.split()) >= 1:
            t = left
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9_/+&.-]*", t) if w]
    if not words:
        return ""
    # drop leading stopwords
    while words and words[0].lower() in _TITLE_STOP:
        words = words[1:]
    # drop trailing stopwords
    while words and words[-1].lower() in _TITLE_STOP:
        words = words[:-1]
    if not words:
        return ""
    # Prefer short atomic titles (2–4 words) when phrase was long
    hard_max = max_words if len(words) <= max_words else min(max_words, 4)
    if len(words) > hard_max:
        words = words[:hard_max]
        # Truncation can expose a stopword at the new boundary. Trim once more.
        while words and words[-1].lower() in _TITLE_STOP:
            words.pop()
    # reject single pure stopword
    if len(words) == 1 and words[0].lower() in _TITLE_STOP:
        return ""
    # Title Case (preserve short ALLCAPS acronyms like API, GPU)
    out: list[str] = []
    last = len(words) - 1
    for i, w in enumerate(words):
        if w.isupper() and 1 < len(w) <= 6:
            out.append(w)
        elif any(ch.isdigit() for ch in w) or "." in w or "/" in w:
            out.append(w)
        elif i not in (0, last) and w.lower() in _TITLE_SMALL:
            out.append(w.lower())
        else:
            out.append(w[0].upper() + w[1:] if len(w) > 1 else w.upper())
    title = " ".join(out).strip()
    if len(title) < 2:
        return ""
    if len(title) > max_chars:
        # hard cut on word boundary
        cut = title[: max_chars].rsplit(" ", 1)[0]
        title = cut if len(cut) >= 3 else title[:max_chars].rstrip()
    # final reject: still looks like a full question
    if title.endswith("?") or len(title.split()) > max_words:
        return ""
    low = title.lower()
    if low in ("hi", "hello", "thanks", "thank you", "ok", "okay", "yes", "no", "cool"):
        return ""
    return title


def _title_quality_score(title: str) -> float:
    """Cheap deterministic score for choosing the cleanest graph-node label."""
    t = (title or "").strip()
    if not t:
        return -100.0
    words = t.split()
    low = t.lower()
    score = 0.0
    score += 2.5 if 1 <= len(words) <= 4 else -0.9 * max(0, len(words) - 4)
    if len(t) <= 36:
        score += 1.0
    elif len(t) > 48:
        score -= 1.5
    generic = {
        "conversation", "chat", "chat message", "message", "reply", "question",
        "answer", "discussion", "topic", "thing", "something", "important topic",
        "new memory", "memory note", "assistant", "user", "notes", "information",
    }
    if low in generic:
        score -= 8.0
    if t.endswith(("?", ".", ":")):
        score -= 2.0
    if any(ch.isdigit() for ch in t):
        score += 0.7
    if any(w.isupper() and len(w) >= 2 for w in words):
        score += 0.5
    return score


def refine_note_title(
    raw_title: str,
    *,
    description: str = "",
    content: str = "",
    note_type: str = "concept",
) -> str:
    """Return a concise, semantic label using deterministic local cleanup only.

    The model-supplied title is the primary source. Description/content are used
    only when the supplied title is empty or clearly generic; this avoids another
    model call and keeps graph growth off the latency-sensitive chat path.
    """
    primary = clean_note_title(raw_title)
    if primary and _title_quality_score(primary) >= 1.0:
        return primary

    candidates: list[str] = []
    for raw in (description, content):
        text = (raw or "").strip()
        if not text:
            continue
        heading = re.search(r"^#{1,3}\s+(.+?)\s*$", text, re.M)
        if heading:
            candidates.append(heading.group(1))
        first = re.split(r"[.!?]\s+", text, maxsplit=1)[0].strip()
        if first:
            candidates.append(first)

    best = primary
    best_score = _title_quality_score(primary) if primary else -1000.0
    for candidate in candidates:
        cleaned = clean_note_title(candidate)
        if not cleaned:
            continue
        score = _title_quality_score(cleaned)
        if score > best_score + 0.75:
            best, best_score = cleaned, score

    # Avoid a generic fallback being saved merely because the model omitted a title.
    if best and best.lower() in {"conversation", "discussion", "topic", "message", "notes", "information"}:
        return ""
    return best


def _title_index(titles: list[str] | None) -> dict[str, str]:
    """Map normalized title → canonical display title."""
    idx: dict[str, str] = {}
    for t in titles or []:
        if not t:
            continue
        idx[_norm_title(t)] = t.strip()
    return idx


def collect_allowed_titles(
    *,
    vault_titles: list[str] | None = None,
    memory_context: str = "",
    pinned_titles: list[str] | None = None,
) -> list[str]:
    titles: list[str] = []
    titles.extend(vault_titles or [])
    titles.extend(pinned_titles or [])
    # titles mentioned in memory context lines
    for m in WIKILINK_RE.finditer(memory_context or ""):
        titles.append(m.group(1).strip())
    # headings / bullet labels that look like note titles
    for line in (memory_context or "").splitlines():
        line = line.strip().lstrip("#*- ").strip()
        if 2 <= len(line) <= 80 and not line.endswith("."):
            # skip pure prose
            if re.match(r"^[A-Za-z0-9][\w\s\-/:&']+$", line):
                titles.append(line)
    # unique preserve order
    out: list[str] = []
    seen: set[str] = set()
    for t in titles:
        n = _norm_title(t)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(t.strip())
    return out


def sanitize_assistant_reply(
    reply: str,
    *,
    allowed_titles: list[str] | None = None,
    memory_context: str = "",
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Post-process a reply for accuracy signals.

    Returns:
      {
        text, issues: [{code, detail}],
        unknown_citations: [...],
        fixed_citations: int,
        confidence: high|medium|low
      }
    """
    text = (reply or "").strip()
    issues: list[dict[str, str]] = []
    unknown: list[str] = []
    fixed = 0

    if not text:
        return {
            "text": text,
            "issues": [{"code": "empty_reply", "detail": "Assistant returned empty text"}],
            "unknown_citations": [],
            "fixed_citations": 0,
            "confidence": "low",
            "changed": False,
        }

    enabled = error_reduction_enabled(settings)
    idx = _title_index(allowed_titles)
    # also allow fuzzy match: strip trailing plurals lightly
    def resolve(title: str) -> str | None:
        n = _norm_title(title)
        if n in idx:
            return idx[n]
        # try without trailing s
        if n.endswith("s") and n[:-1] in idx:
            return idx[n[:-1]]
        # substring match for short titles
        for k, v in idx.items():
            if n == k or (len(n) >= 4 and (n in k or k in n)):
                return v
        return None

    if enabled and idx:

        def repl(m: re.Match[str]) -> str:
            nonlocal fixed
            raw_title = (m.group(1) or "").strip()
            display = (m.group(2) or "").strip()
            hit = resolve(raw_title)
            if hit:
                if display:
                    return f"[[{hit}|{display}]]"
                if hit != raw_title:
                    fixed += 1
                    return f"[[{hit}]]"
                return m.group(0)
            unknown.append(raw_title)
            fixed += 1
            # demote hallucinated citation to plain text (keep readable)
            return display or raw_title

        text = WIKILINK_RE.sub(repl, text)
        if unknown:
            issues.append(
                {
                    "code": "unknown_citations",
                    "detail": f"Removed {len(unknown)} vault citation(s) not found in memory: "
                    + ", ".join(unknown[:6])
                    + ("…" if len(unknown) > 6 else ""),
                }
            )

    # truncation / unfinished markers
    if re.search(r"\b(as an ai|i cannot browse|i don't have access to)\b", text, re.I):
        issues.append(
            {
                "code": "boilerplate",
                "detail": "Generic AI disclaimer detected — prefer vault-grounded answers",
            }
        )

    if text.endswith(("...", "…")) and len(text) < 80:
        issues.append({"code": "truncated", "detail": "Reply looks truncated"})

    # overconfidence without memory grounding
    if CERTAINTY_RE.search(text) and not memory_context.strip():
        issues.append(
            {
                "code": "ungrounded_certainty",
                "detail": "Strong certainty language without memory context",
            }
        )

    # light whitespace polish
    polished = re.sub(r"[ \t]+\n", "\n", text)
    polished = re.sub(r"\n{3,}", "\n\n", polished).strip()
    changed = polished != (reply or "").strip()

    # confidence score
    if any(i["code"] in ("empty_reply", "truncated") for i in issues):
        conf = "low"
    elif unknown or any(i["code"] == "ungrounded_certainty" for i in issues):
        conf = "medium"
    else:
        conf = "high"

    return {
        "text": polished,
        "issues": issues,
        "unknown_citations": unknown,
        "fixed_citations": fixed,
        "confidence": conf,
        "changed": changed or bool(unknown) or fixed > 0,
    }


def sanitize_extract(
    extract: dict[str, Any],
    *,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Drop junk notes and normalize titles before vault write.

    Title cleaning always runs (regex only — does not slow chat).
    Heavier body caps respect error_reduction when disabled.
    """
    deep = error_reduction_enabled(settings)
    data = dict(extract or {})
    notes_in = list(data.get("notes") or [])
    clean: list[dict[str, Any]] = []
    dropped = 0
    seen: set[str] = set()
    title_map: dict[str, str] = {}  # original norm → cleaned display

    for n in notes_in:
        if not isinstance(n, dict):
            dropped += 1
            continue
        raw_title = (n.get("title") or "").strip()
        ntype_hint = (n.get("type") or "concept").strip().lower().split("|", 1)[0] or "concept"
        title = refine_note_title(
            raw_title,
            description=(n.get("description") or "").strip(),
            content=(n.get("content") or n.get("body") or "").strip(),
            note_type=ntype_hint,
        )
        if not title:
            dropped += 1
            continue
        key = _norm_title(title)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        if raw_title:
            title_map[_norm_title(raw_title)] = title
        title_map[key] = title

        content = (n.get("content") or n.get("body") or "").strip()
        desc = (n.get("description") or "").strip()
        # salvage notes that only have a title — still useful for the graph
        if not content and not desc:
            desc = f"Captured note: {title}."
            content = f"# {title}\n\n{desc}\n"
        elif not content and desc:
            content = f"# {title}\n\n{desc}\n"
        # keep first heading aligned with cleaned title
        if content:
            lines = content.splitlines()
            if lines and lines[0].lstrip().startswith("#"):
                rest = "\n".join(lines[1:]).lstrip()
                content = f"# {title}\n\n{rest}" if rest else f"# {title}\n"
        if deep and len(content) > 12000:
            content = content[:12000] + "\n…"
        elif not deep and len(content) > 20000:
            content = content[:20000] + "\n…"

        # clean link targets the same way
        links_out: list[str] = []
        link_seen: set[str] = set()
        for link in n.get("links") or []:
            lt = clean_note_title(str(link))
            if not lt:
                continue
            lk = _norm_title(lt)
            if lk in link_seen:
                continue
            link_seen.add(lk)
            links_out.append(lt)

        ntype = (n.get("type") or "concept").strip().lower()
        # collapse multi-type junk like "concept|entity"
        if "|" in ntype:
            ntype = ntype.split("|", 1)[0].strip() or "concept"
        allowed_types = {
            "concept",
            "entity",
            "person",
            "project",
            "decision",
            "fact",
            "preference",
            "session",
            "meta",
            "tool",
        }
        if ntype not in allowed_types:
            ntype = "concept"

        clean.append(
            {
                **n,
                "title": title,
                "type": ntype,
                "description": (desc[:240] if desc else n.get("description") or ""),
                "content": content,
                "links": links_out,
            }
        )

    data["notes"] = clean
    if dropped:
        data["_quality"] = {**(data.get("_quality") or {}), "dropped_notes": dropped}

    # relations: remap cleaned titles, drop orphans
    titles = {_norm_title(n["title"]) for n in clean}

    def _map_endpoint(raw: str) -> str | None:
        c = clean_note_title(raw)
        if not c:
            return None
        k = _norm_title(c)
        if k in titles:
            return title_map.get(k, c)
        # original string might map via title_map
        ok = _norm_title(raw)
        if ok in title_map and _norm_title(title_map[ok]) in titles:
            return title_map[ok]
        return None

    rels = []
    for r in data.get("relations") or []:
        if not isinstance(r, dict):
            continue
        a = _map_endpoint(r.get("from") or r.get("source") or "")
        b = _map_endpoint(r.get("to") or r.get("target") or "")
        if a and b and a != b:
            rels.append(
                {
                    **r,
                    "from": a,
                    "to": b,
                    "relation": (r.get("relation") or "related").strip(),
                }
            )
    data["relations"] = rels
    # Collapse aliases onto seed hubs (Ollama/Orion/…) — free, no LLM
    try:
        from engine.hygiene import apply_seed_merge_to_extract

        data = apply_seed_merge_to_extract(data)
    except Exception:
        pass
    return data


def quality_summary(result: dict[str, Any]) -> str:
    """One-line status for UI."""
    conf = result.get("confidence") or "high"
    n = len(result.get("issues") or [])
    fixed = int(result.get("fixed_citations") or 0)
    if conf == "high" and not n and not fixed:
        return "Quality check · clean"
    parts = [f"Quality · {conf}"]
    if fixed:
        parts.append(f"{fixed} citation fix(es)")
    if n:
        parts.append(f"{n} note(s)")
    return " · ".join(parts)
