"""
Conversational flow optimization.

Improves naturalness and continuity of chat by:
- style-aware system guidance (natural / concise / socratic / technical)
- thread continuity from recent turns
- open-question awareness
- lightweight follow-up suggestions for the UI
"""

from __future__ import annotations

import re
from typing import Any

STYLE_GUIDES: dict[str, str] = {
    "natural": (
        "Conversational flow: sound like a sharp, friendly thinking partner. "
        "Acknowledge what the user just said, then advance the thread. "
        "Use short paragraphs. Prefer one clear next step when helpful. "
        "Avoid robotic lists unless the user asked for structure."
    ),
    "concise": (
        "Conversational flow: be brief and high-signal. "
        "Lead with the answer, then at most 2–4 supporting points. "
        "Skip filler openers. Offer one optional next step."
    ),
    "socratic": (
        "Conversational flow: help the user think. "
        "Answer clearly, then ask 1 thoughtful question that deepens the topic. "
        "Connect to MEMORY CONTEXT when relevant. Never interrogate."
    ),
    "technical": (
        "Conversational flow: precise and structured. "
        "State assumptions, give the answer, then details or caveats. "
        "Use compact bullets for multi-part answers. Prefer exact names and [[links]]."
    ),
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
    "been",
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
    "does",
    "did",
    "can",
    "could",
    "would",
    "should",
    "will",
    "just",
    "about",
    "what",
    "how",
    "why",
    "when",
    "where",
    "who",
    "which",
    "please",
    "thanks",
    "thank",
    "hey",
    "hi",
    "hello",
}


def conversation_style(settings: dict[str, Any] | None) -> str:
    s = (settings or {}).get("conversation_style") or "natural"
    s = str(s).strip().lower()
    return s if s in STYLE_GUIDES else "natural"


def flow_enabled(settings: dict[str, Any] | None) -> bool:
    return (settings or {}).get("conversation_flow", True) is not False


def _keywords(text: str, limit: int = 8) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text or "")
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


def _open_questions(history: list[dict[str, str]], limit: int = 3) -> list[str]:
    qs: list[str] = []
    for m in reversed(history or []):
        if m.get("role") != "assistant":
            continue
        content = (m.get("content") or "").strip()
        for line in content.splitlines():
            line = line.strip()
            if line.endswith("?") and 12 <= len(line) <= 180:
                qs.append(line.lstrip("-* ").strip())
                if len(qs) >= limit:
                    return qs
        # sentence-level questions
        for part in re.split(r"(?<=[.!])\s+", content):
            part = part.strip()
            if part.endswith("?") and 12 <= len(part) <= 180 and part not in qs:
                qs.append(part)
                if len(qs) >= limit:
                    return qs
    return qs


def build_continuity_block(
    history: list[dict[str, str]],
    user_text: str,
    *,
    settings: dict[str, Any] | None = None,
    compact: bool = False,
) -> str:
    """Short block injected into system prompt for smoother multi-turn flow."""
    if not flow_enabled(settings):
        return ""

    style = conversation_style(settings)
    parts = [STYLE_GUIDES[style]]

    # Compact (local Ollama): style only — history already carries the thread
    if compact:
        return "\n\n" + parts[0]

    recent = [
        m
        for m in (history or [])[-6:]
        if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
    ]
    if recent:
        # compact thread sketch (not full replay — history already has turns)
        sketch_lines = []
        for m in recent[-4:]:
            role = "User" if m["role"] == "user" else "You"
            snippet = re.sub(r"\s+", " ", (m.get("content") or "").strip())[:120]
            sketch_lines.append(f"- {role}: {snippet}")
        parts.append("Recent thread (continuity):\n" + "\n".join(sketch_lines))

    topics = _keywords(user_text, 6)
    if topics:
        parts.append("Current turn focus: " + ", ".join(topics))

    opens = _open_questions(history, 2)
    if opens:
        parts.append(
            "Open threads you may gently close or advance if still relevant:\n"
            + "\n".join(f"- {q}" for q in opens)
        )

    parts.append(
        "Turn craft: respond to the latest user message first; "
        "carry forward unresolved threads only when useful; "
        "end with a natural close or one optional path forward — not both a lecture and a quiz."
    )
    return "\n\n## CONVERSATION FLOW\n" + "\n\n".join(parts)


def suggest_followups(
    user_text: str,
    reply: str,
    *,
    memory_titles: list[str] | None = None,
    limit: int = 3,
) -> list[str]:
    """Heuristic follow-up chips — no extra LLM call (works offline / Ollama)."""
    suggestions: list[str] = []
    ut = (user_text or "").strip()
    rt = (reply or "").strip()
    titles = [t for t in (memory_titles or []) if t][:12]

    # From cited nodes in the reply
    for m in re.finditer(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]", rt):
        title = m.group(1).strip()
        if title and title not in titles:
            titles.insert(0, title)
    titles = list(dict.fromkeys(titles))

    if titles:
        suggestions.append(f"Go deeper on [[{titles[0]}]]")
        if len(titles) > 1:
            suggestions.append(f"How does [[{titles[0]}]] relate to [[{titles[1]}]]?")

    # From open questions the model already asked — turn into user actions
    for line in rt.splitlines():
        line = line.strip().lstrip("-* ")
        if line.endswith("?") and 20 <= len(line) <= 100:
            # invite user to answer — not a good chip; invert
            continue

    low = ut.lower()
    if any(w in low for w in ("plan", "roadmap", "next", "how do i", "how to")):
        suggestions.append("Break that into a short action checklist")
    if any(w in low for w in ("remember", "note", "save", "prefer")):
        suggestions.append("What else should go into the memory graph?")
    if any(w in low for w in ("compare", "vs", "versus", "difference")):
        suggestions.append("Summarize the trade-offs in a table")
    if "?" in ut and not any("checklist" in s.lower() for s in suggestions):
        suggestions.append("Give a concrete example")

    # Topic continuation
    kws = _keywords(ut, 3)
    if kws and len(suggestions) < limit:
        suggestions.append(f"What should I remember long-term about {kws[0]}?")

    # Dedupe and cap
    out: list[str] = []
    seen: set[str] = set()
    for s in suggestions:
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def temperature_for_style(
    settings: dict[str, Any] | None,
    base: float | None = None,
) -> float:
    """Slight style-based temperature nudge (still clamped by caller)."""
    s = settings or {}
    t = float(base if base is not None else s.get("chat_temperature") or 0.7)
    style = conversation_style(s)
    if not flow_enabled(s):
        return t
    if style == "concise":
        t = min(t, 0.55)
    elif style == "socratic":
        t = max(t, 0.65)
    elif style == "technical":
        t = min(t, 0.5)
    elif style == "natural":
        t = max(0.55, min(0.9, t))
    return max(0.0, min(1.5, t))
