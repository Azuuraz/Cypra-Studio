"""Per-agent sandboxed workplace. Read/write only under MatrixFiles/Workplaces/<slug>/."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = ROOT / "MatrixFiles" / "Workplaces"
MAX_FILE_BYTES = 400_000
MAX_INJECT_CHARS = 16000
MAX_LIST = 200

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_OP_LIST = re.compile(r"(?:^|\n)\s*#{0,3}\s*FILE\s+LIST\b", re.I)
_OP_READ = re.compile(r"(?:^|\n)\s*#{0,3}\s*FILE\s+READ\s+path\s*=\s*([^\n]+)", re.I)
_OP_CREATE = re.compile(r"(?:^|\n)\s*#{0,3}\s*FILE\s+CREATE\s+path\s*=\s*([^\n]+)\s*\n(?:```[\w+-]*\n)?(.*?)(?:\n```)?(?=\n\s*#{0,3}\s*FILE\s|\Z)", re.I | re.S)
_OP_WRITE = re.compile(
    r"(?:^|\n)\s*#{0,3}\s*FILE\s+(?:WRITE|SAVE|EDIT)\s+path\s*=\s*([^\n]+)\s*\n(?:```[\w+-]*\n)?(.*?)(?:\n```)?(?=\n\s*#{0,3}\s*FILE\s|\Z)",
    re.I | re.S,
)
_OP_APPEND = re.compile(
    r"(?:^|\n)\s*#{0,3}\s*FILE\s+APPEND\s+path\s*=\s*([^\n]+)\s*\n(?:```[\w+-]*\n)?(.*?)(?:\n```)?(?=\n\s*#{0,3}\s*FILE\s|\Z)",
    re.I | re.S,
)
_OP_DELETE = re.compile(r"(?:^|\n)\s*#{0,3}\s*FILE\s+DELETE\s+path\s*=\s*([^\n]+)", re.I)
_OP_MKDIR = re.compile(r"(?:^|\n)\s*#{0,3}\s*FILE\s+MKDIR\s+path\s*=\s*([^\n]+)", re.I)
_OP_RENAME = re.compile(
    r"(?:^|\n)\s*#{0,3}\s*FILE\s+RENAME\s+from\s*=\s*([^\s]+)\s+to\s*=\s*([^\n]+)",
    re.I,
)
_STRIP = re.compile(
    r"\n?\s*#{0,3}\s*FILE\s+(?:LIST\b[^\n]*|READ[^\n]*|WRITE[^\n]*|SAVE[^\n]*|EDIT[^\n]*|CREATE[^\n]*|APPEND[^\n]*|DELETE[^\n]*|MKDIR[^\n]*|RENAME[^\n]*)\n?(?:```[\w+-]*\n.*?```)?",
    re.I | re.S,
)
_LIST_ASK = re.compile(
    r"\b(list|show|what(?:'s| is)|ls|dir)\b.*\b(file|workplace|folder|director)",
    re.I,
)
_WRITE_ASK = re.compile(
    r"\b(write|create|save|make|add|put|edit|update|modify|change|fix|patch|rewrite|replace)\b.*\b(file|script|code|\.py|\.js|\.md|\.txt|\.json|\.html|\.css)\b|"
    r"\b(write|create|save|edit|update|modify|change|fix|patch|rewrite|replace)\s+[`'\"]?[A-Za-z0-9._/-]+\.[A-Za-z0-9]+",
    re.I,
)
_READ_ASK = re.compile(
    r"\b(read|open|show|cat|print|inspect|examine|check)\b.*\b(file|contents?|code|[A-Za-z0-9._/-]+\.[A-Za-z0-9]+)",
    re.I,
)
_FENCE = re.compile(r"```(?:([\w.+-]+))?(?:[ \t]+([^\s`]+))?\n(.*?)```", re.S)
_NAMED = re.compile(
    r"(?:write|create|save|make|file|edit|update|modify|change|put|fix|patch|rewrite|replace)\s+(?:a\s+)?(?:file\s+)?[`'\"]?([A-Za-z0-9._/-]+\.[A-Za-z0-9]{1,8})",
    re.I,
)
_DELETE_ASK = re.compile(
    r"\b(delete|remove)\s+(?:the\s+)?(?:file\s+)?[`'\"]?([A-Za-z0-9._/-]+\.[A-Za-z0-9]{1,8})",
    re.I,
)
_RENAME_ASK = re.compile(
    r"\brename\s+[`'\"]?([A-Za-z0-9._/-]+\.[A-Za-z0-9]{1,8})[`'\"]?\s+to\s+[`'\"]?([A-Za-z0-9._/-]+\.[A-Za-z0-9]{1,8})",
    re.I,
)
_APPEND_ASK = re.compile(r"\b(append|add to)\b", re.I)
_MKDIR_ASK = re.compile(
    r"\b(?:mkdir|create\s+folder|make\s+(?:a\s+)?(?:folder|directory))\s+[`'\"]?([A-Za-z0-9._/-]+)",
    re.I,
)



# Explicit target extraction is intentionally conservative. The filesystem layer
# must never invent a filename when the user did not provide one.
_TARGET_RE = re.compile(r"(?<![A-Za-z0-9_])(?:[`\"'])([^`\"']+)(?:[`\"'])|(?<![A-Za-z0-9_])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9]{1,16})\b")
_FOLDER_RE = re.compile(r"(?:folder|directory)\s+[`\"']?([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)", re.I)


def _clean_target(value: str) -> str:
    return str(value or "").strip().strip('`\"\'').replace('\\', '/')


def explicit_targets(user_text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _TARGET_RE.finditer(user_text or ""):
        quoted = bool(m.group(1))
        raw = _clean_target(m.group(1) or m.group(2) or "")
        # Quoted/backticked prose is not a filename. For a quoted target,
        # require a filename extension or a path separator so phrases such as
        # `SECOND LINE` cannot steal the operation target from `note.txt`.
        looks_like_file = bool(re.search(r"(?:[/\\]|\.[A-Za-z0-9]{1,16})", raw))
        if quoted and not looks_like_file:
            continue
        if raw and raw not in seen and not raw.lower().startswith(("http://", "https://")):
            seen.add(raw)
            out.append(raw)
    return out


def _user_target_for_op(user_text: str, op: str) -> str | None:
    text = user_text or ""
    targets = explicit_targets(text)
    lower = text.lower()
    if op == "rename":
        m = _RENAME_ASK.search(text)
        if m:
            return _clean_target(m.group(1)), _clean_target(m.group(2))
        if len(targets) >= 2 and "rename" in lower:
            return targets[0], targets[1]
        return None
    if op == "mkdir":
        m = _MKDIR_ASK.search(text)
        if m:
            return _clean_target(m.group(1))
        fm = _FOLDER_RE.search(text)
        if fm:
            return _clean_target(fm.group(1))
        return None
    if op in {"read", "write", "edit", "save", "create", "append", "delete"}:
        # FILE WRITE is the model's generic full-file mutation primitive, so an
        # explicit user request to create/edit/update/save the exact target may
        # legitimately authorize it. Merely mentioning a filename never does.
        mutate_verbs = ("write", "create", "save", "make", "edit", "update", "modify", "change", "put", "fix", "patch", "rewrite", "replace")
        verb_map = {
            "read": ("read", "open", "show", "cat", "print", "inspect", "examine", "check"),
            "append": ("append", "add to"),
            "delete": ("delete", "remove"),
            "edit": mutate_verbs,
            "create": mutate_verbs,
            "save": mutate_verbs,
            "write": mutate_verbs,
        }
        if any(v in lower for v in verb_map.get(op, ())):
            return targets[0] if targets else None
    return None


def _action_pattern(verbs: tuple[str, ...]) -> str:
    parts = [re.escape(v).replace(r"\ ", r"\s+") for v in verbs]
    return r"(?:" + "|".join(parts) + r")"


def _negates_action(user_text: str, verbs: tuple[str, ...]) -> bool:
    action = _action_pattern(verbs)
    text = user_text or ""
    return bool(
        re.search(rf"\b(?:do\s+not|don't|dont|never)\b[^.!?\n]{{0,48}}\b{action}\b", text, re.I)
        or re.search(rf"\bwithout\b[^.!?\n]{{0,32}}\b{action}(?:ing)?\b", text, re.I)
    )


def _explicit_action_request(user_text: str, verbs: tuple[str, ...]) -> bool:
    """Recognize command-like user authorization, not hypothetical discussion."""
    text = (user_text or "").strip()
    if not text:
        return False
    action = _action_pattern(verbs)
    clauses = [c.strip() for c in re.split(r"(?<=[.!?])\s+|\n+", text) if c.strip()]
    for clause in clauses:
        if _negates_action(clause, verbs):
            continue
        # Questions about what/how one *would* modify are advisory, not consent.
        if re.match(r"^(?:how|what|why|where|when|which)\s+(?:would|could|can|should|will|do|does|did)\b", clause, re.I):
            continue
        if re.match(r"^(?:should|can|could|would)\s+i\b", clause, re.I):
            continue
        patterns = (
            rf"^(?:please\s+|kindly\s+)?(?:go\s+ahead\s+and\s+)?{action}\b",
            rf"\b(?:please|kindly)\s+{action}\b",
            rf"^(?:can|could|would|will)\s+you\s+(?:please\s+)?{action}\b",
            rf"^(?:i\s+(?:want|need)\s+you\s+to|i(?:'d|\s+would)\s+like\s+you\s+to|go\s+ahead\s+and|feel\s+free\s+to)\s+{action}\b",
            rf"^(?:then|and\s+then)\s+{action}\b",
        )
        if any(re.search(pattern, clause, re.I) for pattern in patterns):
            return True
    return False


def _operation_intent_allowed(user_text: str, kind: str) -> bool:
    mutate = ("write", "create", "save", "make", "edit", "update", "modify", "change", "put", "fix", "patch", "rewrite", "replace")
    verbs = {
        "read": ("read", "open", "show", "cat", "print", "inspect", "examine", "check"),
        "write": mutate,
        "save": mutate,
        "edit": mutate,
        "create": mutate,
        "append": ("append", "add to"),
        "delete": ("delete", "remove"),
        "rename": ("rename",),
        "mkdir": ("mkdir", "create folder", "create directory", "make folder", "make directory"),
    }.get(kind, ())
    return bool(verbs and _explicit_action_request(user_text, verbs))


def authorize_operation(user_text: str, op: dict[str, Any]) -> dict[str, Any]:
    """Require current-turn user intent before any model-emitted FILE operation.

    The model may propose an operation, but it cannot grant itself authority.
    Authorization comes only from the current user message and is checked again
    in Python before touching the workplace.
    """
    kind = str(op.get("op") or "").lower()
    text = user_text or ""
    out = dict(op)

    if kind == "list":
        list_verbs = ("list", "show", "ls", "dir")
        if _negates_action(text, list_verbs):
            return {"op": "list", "ok": False, "error": "FILE LIST was explicitly denied by the user."}
        if user_wants_list(text):
            return out
        return {"op": "list", "ok": False, "error": "FILE LIST was not explicitly requested by the user."}

    if kind not in {"read", "write", "save", "edit", "create", "append", "delete", "rename", "mkdir"}:
        return {"op": kind or "unknown", "ok": False, "error": "Unknown or unauthorized file operation."}

    if not _operation_intent_allowed(text, kind):
        labels = {
            "read": "Read/open/inspect",
            "write": "Write/edit/save",
            "save": "Write/edit/save",
            "edit": "Write/edit/save",
            "create": "Create/write",
            "append": "Append",
            "delete": "Delete",
            "rename": "Rename",
            "mkdir": "Folder creation",
        }
        return {
            "op": kind,
            "path": out.get("path"),
            "from": out.get("from") if kind == "rename" else None,
            "to": out.get("to") if kind == "rename" else None,
            "ok": False,
            "error": f"{labels.get(kind, 'File operation')} was not explicitly requested by the user.",
        }

    # Intent and exact targets are intentionally separate checks. The next
    # validation step binds the operation to the exact user-named path(s).
    return out


def validate_target_exact(slug: str | None, user_text: str, op: dict[str, Any]) -> dict[str, Any]:
    """Resolve an operation against the exact user-mentioned target when possible.

    This never fabricates a target. If an operation has an explicit path that
    conflicts with the user's exact target, reject it instead of silently
    redirecting the write.
    """
    kind = str(op.get("op") or "").lower()
    out = dict(op)
    if kind == "rename":
        requested = _user_target_for_op(user_text, "rename")
        if requested:
            src, dst = requested
            if _clean_target(out.get("from")) != src or _clean_target(out.get("to")) != dst:
                return {"op": "rename", "ok": False, "from": out.get("from"), "to": out.get("to"), "error": f"Exact rename required: `{src}` → `{dst}`."}
        return out
    if kind == "mkdir":
        requested = _user_target_for_op(user_text, "mkdir")
        if requested and _clean_target(out.get("path")) != requested:
            return {"op": "mkdir", "path": out.get("path"), "ok": False, "error": f"Exact folder required: `{requested}`."}
        return out
    requested = _user_target_for_op(user_text, kind)
    supplied = _clean_target(out.get("path"))
    if requested:
        if supplied != requested:
            return {"op": kind, "path": supplied, "ok": False, "error": f"Exact filename required: `{requested}`."}
    elif not supplied:
        return {"op": kind, "ok": False, "error": "No exact filename/path was provided; refusing to guess."}
    else:
        return {"op": kind, "path": supplied, "ok": False, "error": "No exact user-named filename/path was found; refusing to guess."}
    return out


def agent_slug(raw: str | None) -> str:
    s = _SLUG_RE.sub("-", str(raw or "cypra").strip().lower()).strip("-._")
    return s or "cypra"


def workplace_dir(slug: str | None) -> Path:
    d = (WORK_ROOT / agent_slug(slug)).resolve()
    d.mkdir(parents=True, exist_ok=True)
    (d / "README.txt").write_text(
        "This folder is this agent's workplace. The agent may read and write files here when Files mode is on.\n",
        encoding="utf-8",
    ) if not (d / "README.txt").exists() else None
    return d


def _safe(slug: str | None, rel: str) -> Path:
    root = workplace_dir(slug)
    rel_n = str(rel or "").replace("\\", "/").lstrip("/")
    if not rel_n or rel_n.endswith("/"):
        raise ValueError("Path required")
    if ".." in Path(rel_n).parts or rel_n.startswith("/") or (len(rel_n) > 1 and rel_n[1] == ":"):
        raise ValueError("Path must stay inside the agent workplace")
    out = (root / rel_n).resolve()
    try:
        out.relative_to(root)
    except ValueError as e:
        raise ValueError("Path must stay inside the agent workplace") from e
    return out


def list_files(slug: str | None) -> list[dict[str, Any]]:
    root = workplace_dir(slug)
    items: list[dict[str, Any]] = []
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(root).as_posix()
        try:
            st = p.stat()
        except OSError:
            continue
        items.append({"path": rel, "bytes": int(st.st_size)})
        if len(items) >= MAX_LIST:
            break
    return items


def read_file(slug: str | None, rel: str) -> dict[str, Any]:
    path = _safe(slug, rel)
    if not path.is_file():
        return {"ok": False, "path": rel, "error": "not found"}
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        return {"ok": False, "path": rel, "error": f"file too large ({len(raw)} bytes)"}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    return {"ok": True, "path": rel, "text": text, "bytes": len(raw)}


def create_file(slug: str | None, rel: str, content: str) -> dict[str, Any]:
    path = _safe(slug, rel)
    if path.exists():
        return {"ok": False, "path": rel, "error": "already exists; use EDIT/WRITE to overwrite or RENAME to move it"}
    data = (content or "").encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"Write too large ({len(data)} bytes)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"ok": True, "path": rel, "bytes": len(data), "created": True}


def edit_file(slug: str | None, rel: str, content: str) -> dict[str, Any]:
    path = _safe(slug, rel)
    if not path.is_file():
        return {"ok": False, "path": rel, "error": "not found; EDIT requires an existing file"}
    data = (content or "").encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"Write too large ({len(data)} bytes)")
    path.write_bytes(data)
    return {"ok": True, "path": rel, "bytes": len(data), "edited": True}


def write_file(slug: str | None, rel: str, content: str) -> dict[str, Any]:
    path = _safe(slug, rel)
    data = (content or "").encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"Write too large ({len(data)} bytes)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"ok": True, "path": rel, "bytes": len(data)}


def append_file(slug: str | None, rel: str, content: str) -> dict[str, Any]:
    path = _safe(slug, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        return {"ok": False, "path": rel, "error": "not found; APPEND requires an existing file"}
    existing = path.read_bytes()
    extra = (content or "").encode("utf-8")
    if len(existing) + len(extra) > MAX_FILE_BYTES:
        raise ValueError("Append would exceed size limit")
    if existing and not existing.endswith(b"\n") and extra:
        extra = b"\n" + extra
    path.write_bytes(existing + extra)
    return {"ok": True, "path": rel, "bytes": path.stat().st_size, "appended": len(extra)}


def delete_file(slug: str | None, rel: str) -> dict[str, Any]:
    path = _safe(slug, rel)
    if not path.exists():
        return {"ok": False, "path": rel, "error": "not found"}
    if path.is_dir():
        import shutil
        shutil.rmtree(path)
    else:
        path.unlink()
    return {"ok": True, "path": rel, "deleted": True}


def rename_file(slug: str | None, src: str, dst: str) -> dict[str, Any]:
    a = _safe(slug, src)
    b = _safe(slug, dst)
    if not a.exists():
        return {"ok": False, "path": src, "error": "not found"}
    if b.exists():
        return {"ok": False, "path": dst, "error": "destination already exists; refusing to overwrite"}
    b.parent.mkdir(parents=True, exist_ok=True)
    a.rename(b)
    return {"ok": True, "path": dst, "from": src, "to": dst}


def mkdir_path(slug: str | None, rel: str) -> dict[str, Any]:
    rel_n = str(rel or "").replace("\\", "/").strip("/") + "/.keep"
    path = _safe(slug, rel_n)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    folder = str(rel or "").replace("\\", "/").strip("/")
    return {"ok": True, "path": folder, "mkdir": True}


def user_wants_list(user_text: str) -> bool:
    text = user_text or ""
    if _negates_action(text, ("list", "show", "ls", "dir")):
        return False
    if _explicit_action_request(text, ("list", "show", "ls", "dir")):
        return True
    # Direct information request for the workplace inventory is also explicit
    # list intent, even though it is phrased as a question.
    return bool(re.search(r"\b(?:what|which)\b[^?!.\n]{0,80}\bfiles?\b[^?!.\n]{0,80}\b(?:workplace|folder|directory)\b", text, re.I))


def user_wants_write(user_text: str) -> bool:
    return _operation_intent_allowed(user_text, "write")


def user_wants_read(user_text: str) -> bool:
    return _operation_intent_allowed(user_text, "read")


def format_ops_for_user(slug: str | None, results: list[dict[str, Any]]) -> str:
    bits: list[str] = []
    for r in results:
        op = r.get("op")
        if op == "create" and r.get("ok"):
            bits.append(f"Created `{r.get('path')}` ({r.get('bytes')} bytes) in the workplace.")
        elif op == "create":
            bits.append(f"Could not create `{r.get('path')}`: {r.get('error')}")
        elif op in ("write", "save", "edit") and r.get("ok"):
            bits.append(f"Saved `{r.get('path')}` ({r.get('bytes')} bytes) in the workplace.")
        elif op in ("write", "save", "edit"):
            bits.append(f"Could not write `{r.get('path')}`: {r.get('error')}")
        elif op == "append" and r.get("ok"):
            bits.append(f"Appended to `{r.get('path')}` (now {r.get('bytes')} bytes).")
        elif op == "append":
            bits.append(f"Could not append `{r.get('path')}`: {r.get('error')}")
        elif op == "read" and r.get("ok"):
            bits.append(f"Read `{r.get('path')}` ({r.get('bytes')} bytes).")
        elif op == "read":
            bits.append(f"Could not read `{r.get('path')}`: {r.get('error')}")
        elif op == "delete" and r.get("ok"):
            bits.append(f"Deleted `{r.get('path')}`.")
        elif op == "delete":
            bits.append(f"Could not delete `{r.get('path')}`: {r.get('error')}")
        elif op == "rename" and r.get("ok"):
            bits.append(f"Renamed `{r.get('from')}` → `{r.get('to')}`.")
        elif op == "rename":
            bits.append(f"Could not rename: {r.get('error')}")
        elif op == "mkdir" and r.get("ok"):
            bits.append(f"Created folder `{r.get('path')}`.")
        elif op == "list" and r.get("ok"):
            bits.append(format_list_for_user(slug, r.get("files") or []))
        elif op == "list":
            bits.append(f"Could not list workplace: {r.get('error')}")
    return "\n".join(bits).strip()


def infer_ops(user_text: str, assistant_text: str) -> list[dict[str, Any]]:
    """Parse explicit FILE tags, then infer writes from fenced code + a filename."""
    ops = parse_file_ops(assistant_text)
    if any(o.get("op") == "write" for o in ops):
        return ops
    name = None
    m = _NAMED.search(user_text or "")
    if m:
        name = m.group(1).strip()
    fences = list(_FENCE.finditer(assistant_text or ""))
    if fences and user_wants_write(user_text):
        lang, fname, body = fences[0].group(1), fences[0].group(2), fences[0].group(3)
        path = (fname or name or "").replace("\\", "/").lstrip("/")
        if not path:
            return ops
        # When the user explicitly names a file, that exact target wins.
        user_targets = explicit_targets(user_text or "")
        if user_targets:
            path = user_targets[0]
        verb_text = (user_text or "").lower()
        op_kind = "create" if any(v in verb_text for v in ("create", "make a", "make the")) else "write"
        if any(v in verb_text for v in ("edit", "update", "modify", "change")):
            op_kind = "edit"
        ops.append({"op": op_kind, "path": path, "content": (body or "").replace("\r\n", "\n")})
    if not any(o.get("op") in {"write", "edit", "create", "append"} for o in ops) and fences and _APPEND_ASK.search(user_text or ""):
        append_target = _user_target_for_op(user_text or "", "append") or name
        if append_target:
            ops.append({"op": "append", "path": append_target, "content": (fences[0].group(3) or "").replace("\r\n", "\n")})
    dm = _DELETE_ASK.search(user_text or "")
    if dm and not any(o.get("op") == "delete" for o in ops):
        ops.append({"op": "delete", "path": dm.group(2)})
    rm = _RENAME_ASK.search(user_text or "")
    if rm and not any(o.get("op") == "rename" for o in ops):
        ops.append({"op": "rename", "from": rm.group(1), "to": rm.group(2)})
    mm = _MKDIR_ASK.search(user_text or "")
    if mm and not any(o.get("op") == "mkdir" for o in ops):
        ops.append({"op": "mkdir", "path": mm.group(1)})
    if not ops and user_wants_list(user_text):
        ops.append({"op": "list"})
    if not ops and user_wants_read(user_text) and name:
        ops.append({"op": "read", "path": name})
    return ops


def format_list_for_user(slug: str | None, files: list[dict[str, Any]] | None = None) -> str:
    items = files if files is not None else list_files(slug)
    name = agent_slug(slug)
    if not items:
        return f"Workplace for {name} only (MatrixFiles/Workplaces/{name}/) is empty besides README.txt."
    lines = [f"Workplace files for {name}:"]
    for f in items:
        lines.append(f"- {f.get('path')} ({f.get('bytes')} bytes)")
    return "\n".join(lines)


def parse_file_ops(text: str) -> list[dict[str, Any]]:
    src = text or ""
    ops: list[dict[str, Any]] = []
    for m in _OP_LIST.finditer(src):
        ops.append({"op": "list", "at": m.start()})
    for m in _OP_CREATE.finditer(src):
        body = m.group(2) or ""
        if body.endswith("\n```"):
            body = body[:-4]
        ops.append({"op": "create", "path": m.group(1).strip().strip('"'), "content": body.replace("\r\n", "\n"), "at": m.start()})
    for m in _OP_READ.finditer(src):
        ops.append({"op": "read", "path": m.group(1).strip().strip('"'), "at": m.start()})
    for m in _OP_WRITE.finditer(src):
        body = m.group(2) or ""
        if body.endswith("\n```"):
            body = body[: -4]
        ops.append({"op": "write", "path": m.group(1).strip().strip('"'), "content": body.replace("\r\n", "\n"), "at": m.start()})
    for m in _OP_APPEND.finditer(src):
        body = m.group(2) or ""
        if body.endswith("\n```"):
            body = body[: -4]
        ops.append({"op": "append", "path": m.group(1).strip().strip('"'), "content": body.replace("\r\n", "\n"), "at": m.start()})
    for m in _OP_DELETE.finditer(src):
        ops.append({"op": "delete", "path": m.group(1).strip().strip('"'), "at": m.start()})
    for m in _OP_MKDIR.finditer(src):
        ops.append({"op": "mkdir", "path": m.group(1).strip().strip('"'), "at": m.start()})
    for m in _OP_RENAME.finditer(src):
        ops.append({"op": "rename", "from": m.group(1).strip().strip('"'), "to": m.group(2).strip().strip('"'), "at": m.start()})
    ops.sort(key=lambda o: int(o.get("at") or 0))
    for o in ops:
        o.pop("at", None)
    return ops


def strip_file_ops(text: str) -> str:
    out = _STRIP.sub("\n", text or "")
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def run_ops(slug: str | None, ops: list[dict[str, Any]], user_text: str = "") -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for op in ops:
        kind = str(op.get("op") or "")
        try:
            authorized = authorize_operation(user_text, op)
            if authorized.get("ok") is False:
                results.append(authorized)
                continue
            checked = validate_target_exact(slug, user_text, authorized) if kind != "list" else authorized
            if checked.get("ok") is False:
                results.append(checked)
                continue
            op = checked
            if kind == "list":
                results.append({"op": "list", "ok": True, "files": list_files(slug)})
            elif kind == "read":
                results.append({"op": "read", **read_file(slug, str(op.get("path") or ""))})
            elif kind == "create":
                results.append({"op": "create", **create_file(slug, str(op.get("path") or ""), str(op.get("content") or ""))})
            elif kind in ("write", "save"):
                results.append({"op": kind, **write_file(slug, str(op.get("path") or ""), str(op.get("content") or ""))})
            elif kind == "edit":
                results.append({"op": "edit", **edit_file(slug, str(op.get("path") or ""), str(op.get("content") or ""))})
            elif kind == "append":
                results.append({"op": "append", **append_file(slug, str(op.get("path") or ""), str(op.get("content") or ""))})
            elif kind == "delete":
                results.append({"op": "delete", **delete_file(slug, str(op.get("path") or ""))})
            elif kind == "rename":
                results.append({"op": "rename", **rename_file(slug, str(op.get("from") or op.get("path") or ""), str(op.get("to") or ""))})
            elif kind == "mkdir":
                results.append({"op": "mkdir", **mkdir_path(slug, str(op.get("path") or ""))})
            else:
                results.append({"op": kind or "unknown", "ok": False, "error": "unknown op"})
        except Exception as e:
            results.append({"op": kind, "ok": False, "path": op.get("path"), "error": str(e)})
    return results


def results_for_model(results: list[dict[str, Any]]) -> str:
    lines = ["FILE RESULTS (from your workplace — use these, then answer the user):"]
    for r in results:
        op = r.get("op")
        if op == "list":
            if not r.get("ok"):
                lines.append(f"LIST: ERROR {r.get('error')}")
                continue
            files = r.get("files") or []
            lines.append("LIST:")
            if not files:
                lines.append("  (empty workplace)")
            for f in files:
                lines.append(f"  - {f.get('path')} ({f.get('bytes')} bytes)")
        elif op == "read":
            if not r.get("ok"):
                lines.append(f"READ {r.get('path')}: ERROR {r.get('error')}")
                continue
            body = str(r.get("text") or "")
            if len(body) > MAX_INJECT_CHARS:
                body = body[:MAX_INJECT_CHARS] + "\n[TRIMMED]"
            lines.append(f"READ {r.get('path')}:\n```\n{body}\n```")
        elif op in ("create", "write", "append", "delete", "rename", "mkdir"):
            if r.get("ok"):
                lines.append(f"{op.upper()} {r.get('path')}: ok")
            else:
                lines.append(f"{op.upper()} {r.get('path')}: ERROR {r.get('error')}")
    return "\n".join(lines)


WORKPLACE_OVERLAY = """## AGENT WORKPLACE FILES
You have a private folder for THIS agent only. Other agents cannot see it. Use these operations; they are executed locally.

### FILE LIST
### FILE READ path=relative/file.py
### FILE CREATE path=relative/file.py
````
full file contents (create only; fails if it already exists)
````
### FILE WRITE path=relative/file.py
```
full file contents (create or overwrite / save / edit)
```
### FILE APPEND path=relative/file.py
```
text to add at end
```
### FILE DELETE path=relative/file.py
### FILE RENAME from=old.py to=new.py
### FILE MKDIR path=src/utils

Rules:
- Paths stay inside your workplace. No .., no absolute paths, no other agents' folders.
- CREATE = create exact new file and fail if it already exists. WRITE/SAVE = exact full-file replace. EDIT = exact existing-file replace and fails if the target is missing. APPEND = exact existing file only. DELETE/RENAME/MKDIR require exact targets.
- FILES mode grants access to the sandbox, not permission to act autonomously. Every LIST/READ/WRITE/CREATE/EDIT/APPEND/DELETE/RENAME/MKDIR must be explicitly requested by the CURRENT user message. Mentioning a filename alone is not authorization.
- Never invent filenames, extensions, destinations, or default names. If the user did not specify the target, do not perform the filesystem operation.
- If the user named a target, it must match exactly; never silently redirect to a similar filename.
- After READ you will get FILE RESULTS; then answer the user in plain language.
- Do not claim a file changed unless you emitted the matching FILE op.
"""
