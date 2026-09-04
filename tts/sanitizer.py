"""Presentation-only cleanup for text sent to speech synthesis."""

from __future__ import annotations

import re


_FENCED_CODE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~", re.MULTILINE)
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MARKDOWN_LINK = re.compile(r"!?\[([^\]]+)\]\([^)]*\)")
_WIKI_LINK = re.compile(r"\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]")
_META_LINE = re.compile(
    r"^\s*(?:\[?(?:debug|trace|tool|brain|tokens?|tok/s|speed|metadata|system)\]?\s*[:=]|"
    r"(?:prompt|completion|total)[ _-]?tokens?\s*[:=]|generation\s+(?:stats?|speed)\s*[:=]).*$",
    re.IGNORECASE,
)
_JSON_LINE = re.compile(r'^\s*[{}\[\],]|^\s*"[^"\n]+"\s*:', re.MULTILINE)
_PRIVATE_PATH = re.compile(r"(?:\b[A-Za-z]:\\[^\s]+|/(?:home|Users|var|tmp)/[^\s]+)", re.IGNORECASE)
_PRIVATE_LINE = re.compile(
    r"^\s*(?:internal|directive|tool\s*call|environment|env|filesystem|file\s*path|working\s*directory|cwd)\s*[:=].*$",
    re.IGNORECASE,
)

# Edge/online-only deterministic secret redaction. These patterns intentionally
# operate independently from the general speech cleanup so local Piper can keep
# speaking user-requested private material without transmitting it externally.
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN\s+[A-Z0-9 -]*PRIVATE\s+KEY(?:\s+BLOCK)?-----[\s\S]*?"
    r"-----END\s+[A-Z0-9 -]*PRIVATE\s+KEY(?:\s+BLOCK)?-----",
    re.IGNORECASE,
)
_INTERNAL_ONLINE_LINE = re.compile(
    r"^\s*(?:SYSTEM[ _-]?PROMPT|DEVELOPER[ _-]?PROMPT|BRAIN[ _-]?CONTEXT|"
    r"TOOL[ _-]?(?:OUTPUT|CALL)|AGENT[ _-]?CONTEXT|INTERNAL[ _-]?CONTEXT|"
    r"HIDDEN[ _-]?CONTEXT)\s*[:=].*$",
    re.IGNORECASE | re.MULTILINE,
)
_CREDENTIAL_URL = re.compile(
    r"(?:https?://|www\.)\S*[?&](?:token|api[_-]?key|key|auth|authorization|"
    r"access[_-]?token|refresh[_-]?token|secret|client[_-]?secret)=[^\s&#]+\S*",
    re.IGNORECASE,
)
_JSON_SECRET_FIELD = re.compile(
    r'''(["']?(?:api[ _-]?key|apikey|token|access[ _-]?token|refresh[ _-]?token|"
    r"auth[ _-]?token|password|passwd|pwd|secret|client[ _-]?secret|authorization)["']?\s*:\s*)'''
    r'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^,}\]\r\n]+)''',
    re.IGNORECASE,
)
_ASSIGNMENT_SECRET = re.compile(
    r"\b(?:[A-Z0-9.-]+[_-])*(?:API[ _-]?KEY|APIKEY|TOKEN|ACCESS[ _-]?TOKEN|"
    r"REFRESH[ _-]?TOKEN|AUTH[ _-]?TOKEN|PASSWORD|PASSWD|PWD|"
    r"SECRET(?:[ _-]?ACCESS[ _-]?KEY)?|CLIENT[ _-]?SECRET|AUTHORIZATION)\b\s*[:=]\s*"
    r'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}\]\)]+)''',
    re.IGNORECASE,
)
_AUTH_BEARER = re.compile(
    r"\b(?:authorization\s*[:=]\s*)?bearer\s+[A-Za-z0-9._~+\-/=]{6,}",
    re.IGNORECASE,
)
_AUTHORIZATION_CREDENTIAL = re.compile(
    r"\bauthorization\s*[:=]\s*(?:(?:basic|token|apikey|api-key)\s+)?"
    r"[A-Za-z0-9._~+\-/=]{4,}",
    re.IGNORECASE,
)
_COMMON_SECRET_PREFIX = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|"
    r"AKIA[0-9A-Z]{12,})\b",
    re.IGNORECASE,
)

# Defense-in-depth patterns for text after markdown cleanup. The normal speech
# sanitizer turns underscores into spaces, so common secret shapes may become
# space-separated. These expressions remove the whole credential-bearing clause
# conservatively rather than attempting to reconstruct or log the value.
_POST_INTERNAL_CLAUSE = re.compile(
    r"\b(?:SYSTEM[ -]?PROMPT|DEVELOPER[ -]?PROMPT|BRAIN[ -]?CONTEXT|"
    r"TOOL[ -]?(?:OUTPUT|CALL)|AGENT[ -]?CONTEXT|INTERNAL[ -]?CONTEXT|HIDDEN[ -]?CONTEXT)"
    r"\s*[:=]\s*.*?(?=(?:\b(?:SYSTEM[ -]?PROMPT|DEVELOPER[ -]?PROMPT|BRAIN[ -]?CONTEXT|"
    r"TOOL[ -]?(?:OUTPUT|CALL)|AGENT[ -]?CONTEXT|INTERNAL[ -]?CONTEXT|HIDDEN[ -]?CONTEXT)\s*[:=])|[.!?](?:\s|$)|$)",
    re.IGNORECASE,
)
_POST_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN\s+[A-Z0-9 -]*PRIVATE\s+KEY(?:\s+BLOCK)?-----.*?"
    r"-----END\s+[A-Z0-9 -]*PRIVATE\s+KEY(?:\s+BLOCK)?-----",
    re.IGNORECASE | re.DOTALL,
)


def _remove_tables(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        looks_like_header = "|" in line and re.match(r"^\s*\|?\s*:?-{3,}", next_line)
        if looks_like_header:
            index += 2
            while index < len(lines) and "|" in lines[index]:
                index += 1
            output.append("Table omitted.")
            continue
        output.append(line)
        index += 1
    return "\n".join(output)


def _remove_json_dumps(text: str) -> str:
    lines = text.splitlines()
    jsonish = sum(1 for line in lines if _JSON_LINE.search(line))
    if len(lines) >= 4 and jsonish >= max(3, len(lines) // 2):
        return "Structured data omitted."
    return text


def _truncate_sentence(text: str, maximum: int) -> str:
    if len(text) <= maximum:
        return text
    clipped = text[:maximum].rstrip()
    boundaries = [match.end() for match in re.finditer(r"[.!?](?:\s|$)", clipped)]
    if boundaries:
        clipped = clipped[: boundaries[-1]].rstrip()
    elif " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0].rstrip()
    return clipped or text[:maximum].strip()


def sanitize_for_online_tts(text: str) -> str:
    """Remove credential-shaped/private material before any online TTS call.

    This function is deliberately deterministic and side-effect free. It never
    logs input, output, or matched values. Callers must fail closed if it raises.
    """

    value = str(text or "").replace("\x00", " ")

    # Remove whole structured blocks/lines first while original boundaries still
    # exist. This is especially important before the normal speech sanitizer
    # flattens markdown and whitespace.
    value = _PRIVATE_KEY_BLOCK.sub(" Private key omitted. ", value)
    value = _INTERNAL_ONLINE_LINE.sub("", value)
    value = _CREDENTIAL_URL.sub(" online link omitted ", value)

    # Remove credentials without ever preserving the matched value.
    value = _AUTH_BEARER.sub("authentication credential", value)
    value = _AUTHORIZATION_CREDENTIAL.sub("authentication credential", value)
    value = _JSON_SECRET_FIELD.sub(lambda match: f"{match.group(1)}[redacted]", value)
    value = _ASSIGNMENT_SECRET.sub("credential", value)
    value = _COMMON_SECRET_PREFIX.sub("credential", value)

    # Run post-format defenses too; these are useful when this function receives
    # already-normalized speech text immediately before Edge synthesis.
    value = _POST_PRIVATE_KEY_BLOCK.sub(" Private key omitted. ", value)
    value = _POST_INTERNAL_CLAUSE.sub("", value)

    value = re.sub(r"\s+", " ", value).strip()
    return value


def sanitize_for_speech(
    text: str,
    *,
    maximum: int = 1000,
    skip_code: bool = True,
    skip_urls: bool = True,
    privacy_harden: bool = False,
) -> str:
    """Return a safe-to-speak copy without changing the displayed response."""

    value = str(text or "").replace("\x00", " ")
    if skip_code:
        value = _FENCED_CODE.sub(" Code block omitted. ", value)
        value = _INLINE_CODE.sub(lambda match: match.group(1), value)
    if skip_urls:
        value = _URL.sub("", value)
    if privacy_harden:
        value = _PRIVATE_PATH.sub("", value)
    value = _MARKDOWN_LINK.sub(lambda match: match.group(1), value)
    value = _WIKI_LINK.sub(lambda match: match.group(2) or match.group(1), value)
    value = _remove_tables(value)
    value = _remove_json_dumps(value)

    cleaned_lines: list[str] = []
    for line in value.splitlines():
        if _META_LINE.match(line):
            continue
        if privacy_harden and _PRIVATE_LINE.match(line):
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        line = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", line)
        line = re.sub(r"[*_~>|]+", " ", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            cleaned_lines.append(line)

    value = " ".join(cleaned_lines)
    value = re.sub(r"\s+", " ", value).strip()
    return _truncate_sentence(value, max(1, min(10000, int(maximum))))
