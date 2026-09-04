"""
Heuristic memory extraction when the LLM returns empty / invalid JSON.

Keeps long-term memory useful on small local models (e.g. llama3.2:3b) by
mining wikilinks, preferences, noun phrases, and session topics from the
user ↔ assistant exchange.
"""

from __future__ import annotations

import re
from typing import Any

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]")
PREFER_RE = re.compile(
    r"\b(?:i\s+(?:prefer|like|love|hate|want|need|use|am using|always|never)|"
    r"my\s+(?:favorite|preferred|default))\b\s+(.{8,80}?)(?:[.!?\n]|$)",
    re.I,
)
PROPER_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z0-9]+){0,3})\b")
BULLET_RE = re.compile(r"^\s*[-*•]\s+(.{8,80})$", re.M)

FACT_RE = re.compile(
    r"\b(?:the|my|your|our|this)?\s*([A-Za-z][A-Za-z0-9'_-]*(?:\s+[A-Za-z][A-Za-z0-9'_-]*){0,8})\s+(?:is|are|was|were)\s+(.{2,180}?)(?=[.!?\n]|$)",
    re.I,
)

_STOP_TITLES = {
    "the",
    "this",
    "that",
    "with",
    "from",
    "your",
    "have",
    "will",
    "what",
    "when",
    "where",
    "which",
    "there",
    "here",
    "user",
    "assistant",
    "cypra",
    "brain",
    "okay",
    "thanks",
    "hello",
    "hi",
    "yes",
    "no",
    "ok",
    "prefer",
    "using",
    "want",
    "need",
    "like",
    "love",
    "hate",
    "always",
    "never",
    "make",
    "more",
    "less",
    "very",
    "also",
    "just",
    "really",
    "thing",
    "things",
    "stuff",
    "something",
    "anything",
}

_TOPIC_STOP = {
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
    "just",
    "about",
    "what",
    "how",
    "why",
    "please",
    "also",
    "into",
    "than",
    "then",
    "them",
    "they",
    "not",
    "but",
    "so",
    "if",
}


def growth_mode(settings: dict[str, Any] | None) -> str:
    """sparse | balanced | dense — how hungry extract should be."""
    m = str((settings or {}).get("extract_growth") or "dense").strip().lower()
    if m in ("sparse", "balanced", "dense", "aggressive"):
        return "dense" if m == "aggressive" else m
    return "dense"


def max_notes_for(settings: dict[str, Any] | None) -> int:
    s = settings or {}
    mode = growth_mode(s)
    default = 8 if mode == "dense" else 5 if mode == "balanced" else 3
    n = int(s.get("ollama_max_notes") or s.get("extract_max_notes") or default)
    hard_cap = 80 if bool(s.get("explicit_memory_growth")) else 12
    return max(1, min(hard_cap, n))


def _clean_title(t: str) -> str:
    """Delegate to shared atomic-title cleaner (regex only, free)."""
    try:
        from engine.quality import clean_note_title

        return clean_note_title(t)
    except Exception:
        t = re.sub(r"\s+", " ", (t or "").strip(" .-:;,"))
        t = t[:48]
        if len(t) < 3 or t.lower() in _STOP_TITLES:
            return ""
        if t.islower() and " " in t:
            t = t.title()
        elif t.islower():
            t = t[:1].upper() + t[1:]
        return t


def _keywords(text: str, limit: int = 10) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text or "")
    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        low = w.lower()
        if low in _TOPIC_STOP or low in seen:
            continue
        seen.add(low)
        out.append(w)
        if len(out) >= limit:
            break
    return out


def _note(
    title: str,
    *,
    ntype: str = "concept",
    description: str = "",
    content: str = "",
    tags: list[str] | None = None,
    links: list[str] | None = None,
) -> dict[str, Any]:
    title = _clean_title(title)
    if not title:
        return {}
    desc = (description or f"Topic from conversation: {title}.").strip()
    body = (content or desc).strip()
    if not body.startswith("#"):
        body = f"# {title}\n\n{body}"
    return {
        "title": title,
        "type": ntype,
        "description": desc[:240],
        "content": body[:2000],
        "tags": tags or ["auto"],
        "links": links or [],
    }


def heuristic_extract(
    user_text: str,
    assistant_text: str = "",
    *,
    existing_titles: list[str] | None = None,
    settings: dict[str, Any] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """
    Build atomic notes without an LLM.
    Always returns at least one note for non-trivial user turns when growth=dense.
    """
    mode = growth_mode(settings)
    cap = limit or max_notes_for(settings)
    user = (user_text or "").strip()
    asst = (assistant_text or "").strip()
    bundle = f"{user}\n{asst}"
    existing = {re.sub(r"\s+", " ", t.strip().lower()) for t in (existing_titles or []) if t}

    notes: list[dict[str, Any]] = []
    relations: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(n: dict[str, Any]) -> None:
        if not n or not n.get("title"):
            return
        key = n["title"].strip().lower()
        if key in seen or key in existing:
            # still allow merge-friendly update of existing by including once
            if key in seen:
                return
        if len(notes) >= cap:
            return
        seen.add(key)
        notes.append(n)

    # 1) Explicit wikilinks (highest signal)
    for m in WIKILINK_RE.finditer(bundle):
        title = m.group(1).strip()
        add(
            _note(
                title,
                ntype="concept",
                description=f"Referenced in conversation as [[{title}]].",
                content=f"Appeared as a [[wikilink]] in chat.\n\nContext:\n> {user[:280]}",
                tags=["linked", "auto"],
            )
        )

    # 2) Explicit user-stated facts. This runs before loose keyword/proper-name
    # extraction so durable statements such as "project codename is ORBIT-7429"
    # become factual stored-memory evidence rather than generic heuristic topics.
    for m in FACT_RE.finditer(user):
        subject = re.sub(r"\s+", " ", m.group(1).strip())
        value = re.sub(r"\s+", " ", m.group(2).strip(" .,:;"))
        if not subject or not value:
            continue
        subject = re.sub(r"^(?:the|my|your|our|this)\s+", "", subject, flags=re.I).strip()
        # Compact title while preserving the exact full statement in the body.
        words = [w for w in subject.split() if w.lower() not in _STOP_TITLES]
        title = " ".join(words[:6]).strip() or "User Stated Fact"
        add(
            _note(
                title,
                ntype="fact",
                description=f"User-stated fact: {subject} is {value}.",
                content=(
                    f"# {title}\n\n"
                    f"USER-STATED FACT\n"
                    f"{subject} is {value}.\n\n"
                    f"Exact source message:\n> {m.group(0).strip()}\n"
                ),
                tags=["fact", "user-stated", "verified"],
                links=[],
            )
        )

    # 3) Preferences / decisions
    for m in PREFER_RE.finditer(user):
        phrase = m.group(1).strip()
        # drop leading filler verbs, keep meaningful object phrase
        phrase = re.sub(
            r"^(?:to\s+)?(?:using|use|have|get|make|see|try)\s+",
            "",
            phrase,
            flags=re.I,
        ).strip()
        words = [w for w in phrase.split()[:7] if w.lower() not in _STOP_TITLES]
        title = " ".join(words)
        if len(title) < 4 or title.lower() in _STOP_TITLES:
            continue
        add(
            _note(
                title,
                ntype="preference",
                description=f"User preference: {phrase[:160]}",
                content=f"Captured from user message:\n\n> {m.group(0).strip()}\n",
                tags=["preference", "auto"],
            )
        )

    # 4) Proper-noun-ish phrases from user message
    if mode in ("balanced", "dense"):
        for m in PROPER_RE.finditer(user):
            title = m.group(1).strip()
            if len(title) < 4 or title.lower() in _STOP_TITLES:
                continue
            if title.split()[0].lower() in _TOPIC_STOP and len(title.split()) == 1:
                continue
            add(
                _note(
                    title,
                    ntype="entity" if title[:1].isupper() else "concept",
                    description=f"Mentioned by the user: {title}.",
                    content=f"## {title}\n\nSurfaced from user chat.\n\n> {user[:320]}\n",
                    tags=["entity", "auto"],
                )
            )

    # 5) Bullet ideas from assistant (dense mode)
    if mode == "dense" and asst:
        for m in BULLET_RE.finditer(asst):
            line = m.group(1).strip().rstrip(".")
            # skip questions and long prose
            if "?" in line or len(line) > 70:
                continue
            # use leading phrase as title
            title = re.split(r"[:—–-]", line, maxsplit=1)[0].strip()
            title = re.sub(r"^\*+|\*+$", "", title).strip()
            if len(title.split()) > 6:
                title = " ".join(title.split()[:5])
            add(
                _note(
                    title,
                    ntype="concept",
                    description=line[:200],
                    content=f"{line}\n\nFrom assistant reply linked to user topic.\n",
                    tags=["idea", "auto"],
                    links=[],
                )
            )

    # 6) Keyword concepts from user (skip filler / already-seen)
    kws = [
        k
        for k in _keywords(user, 10)
        if k.lower() not in _STOP_TITLES and len(k) >= 4
    ]
    if mode == "dense":
        for kw in kws[:5]:
            add(
                _note(
                    kw,
                    ntype="concept",
                    description=f"Key term from the conversation: {kw}.",
                    content=(
                        f"## {kw}\n\n"
                        f"Auto-captured keyword from user message.\n\n"
                        f"> {user[:360]}\n"
                    ),
                    tags=["keyword", "auto"],
                )
            )

    # 7) Always keep a session turn note in dense/balanced if still empty-ish
    min_want = 1 if mode == "sparse" else 2 if mode == "balanced" else 3
    if user and len(user) >= 8 and len(notes) < min_want:
        # Prefer keywords — never raw question text as a title
        topic = _clean_title(" ".join(kws[:3])) if kws else ""
        if not topic:
            topic = _clean_title(user) or "Chat Idea"
        session_title = topic if len(notes) == 0 else f"{topic} Notes"
        session_title = _clean_title(session_title) or topic
        add(
            _note(
                session_title,
                ntype="session",
                description=f"Session capture: {user[:120]}",
                content=(
                    f"## {topic}\n\n"
                    f"### User\n{user[:900]}\n\n"
                    f"### Assistant\n{(asst[:900] if asst else '_(no reply)_')}\n"
                ),
                tags=["session", "auto"],
                links=[n["title"] for n in notes[:4]],
            )
        )

    # Link notes together lightly
    titles = [n["title"] for n in notes]
    for i, a in enumerate(titles):
        for b in titles[i + 1 : i + 3]:
            relations.append({"from": a, "to": b, "relation": "related"})
            # inject links into note objects
            for n in notes:
                if n["title"] == a and b not in n["links"]:
                    n["links"].append(b)

    # Cap
    notes = notes[:cap]
    return {
        "summary": (
            f"Heuristic extract · {len(notes)} node(s) from conversation"
            if notes
            else "Nothing durable found"
        ),
        "notes": notes,
        "relations": relations[:20],
        "source": "heuristic",
    }


def merge_extracts(
    primary: dict[str, Any] | None,
    fallback: dict[str, Any] | None,
    *,
    limit: int = 8,
) -> dict[str, Any]:
    """Prefer LLM notes; fill up to limit with heuristic notes."""
    primary = primary or {"summary": "", "notes": [], "relations": []}
    fallback = fallback or {"summary": "", "notes": [], "relations": []}
    seen: set[str] = set()
    notes: list[dict[str, Any]] = []
    for n in list(primary.get("notes") or []) + list(fallback.get("notes") or []):
        if not isinstance(n, dict):
            continue
        title = (n.get("title") or "").strip()
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        notes.append(n)
        if len(notes) >= limit:
            break
    relations = list(primary.get("relations") or []) + list(fallback.get("relations") or [])
    summary = (primary.get("summary") or "").strip() or (fallback.get("summary") or "")
    sources = []
    if primary.get("notes"):
        sources.append("llm")
    if fallback.get("source") == "heuristic" and fallback.get("notes"):
        sources.append("heuristic")
    return {
        "summary": summary,
        "notes": notes,
        "relations": relations[:40],
        "source": "+".join(sources) or "none",
        "parse_error": bool(primary.get("parse_error")),
    }
