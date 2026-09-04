"""
CypraMatrix adapter for Cypra Studio.

Reads Modelfiles + SYSTEM directives from the project-local Matrix folder
(MatrixFiles/), never from Documents\\CypraTeam or another machine path.

Identity lives in the Modelfile SYSTEM block. BV injects that directive into
chat so any provider (local provider or host Ollama) can speak as the selected agent
without requiring the portable :11435 Ollama store.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .operational_state import performance_score, snapshot as operational_snapshot

# Folder names Steve may have used when copying the Matrix into BV.
_LOCAL_DIR_NAMES = (
    "MatrixFiles",
    "matrix",
    "Matrix",
    "matrix folder",
    "Matrix Folder",
    "Matrixfiles",
)

_LEGACY_MARKERS = (
    "documents\\cyprateam",
    "documents/cyprateam",
    "\\cyprateam\\",
    "/cyprateam/",
)

_FROM_RE = re.compile(r"^FROM\s+(\S+)", re.MULTILINE)
_SYSTEM_TRIPLE_RE = re.compile(r'^SYSTEM\s+"""(.*?)"""', re.MULTILINE | re.DOTALL)
_SYSTEM_QUOTE_RE = re.compile(r'^SYSTEM\s+"(.*?)"\s*(?:$|\r?\n)', re.MULTILINE | re.DOTALL)
_PARAM_RE = re.compile(r"^PARAMETER\s+(\S+)\s+(\S+)", re.MULTILINE)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,}", re.I)

_DEFAULT_CORE = ("cypra", "anomaly", "quantum", "nexus-prime", "chloe", "medic")

# UI categories for the full Matrix roster. Categories are derived from the
# directive slug so the interface can group every local Modelfile without
# changing the underlying Matrix directives. First matching group wins.
_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("AI & Computing", (
        "ai", "artificial", "algorithm", "algebraic", "automation", "computer",
        "computing", "code", "coding", "compiler", "cybernet", "embedded",
        "firmware", "inference", "machine", "ml", "nlp", "software", "vision",
        "robot", "developer", "programmer", "quantum",
    )),
    ("Security", (
        "security", "secure", "cyber", "threat", "forensic", "phishing",
        "malware", "incident", "auth", "penetration", "red-team", "blue-team",
        "privacy", "fraud", "compliance-security",
    )),
    ("Networking & Infrastructure", (
        "network", "cloud", "devops", "infrastructure", "operating-system",
        "distributed", "system-administrator", "site-reliability", "sre",
        "platform", "storage", "datacenter", "observability",
    )),
    ("Data & Analytics", (
        "data", "analytics", "statistic", "etl", "business-intelligence",
        "database", "information", "master-data", "data-scientist", "data-engineer", "econometric",
    )),
    ("Science & Medicine", (
        "medical", "medicine", "pediatric", "dental", "immunology", "biolog",
        "bio", "chem", "physic", "geophys", "geolog", "climat", "ecolog",
        "neurosc", "toxicolog", "patholog", "pharmac", "genetic", "astro",
        "science", "research",
    )),
    ("Engineering & Hardware", (
        "engineer", "engineering", "mechanical", "electrical", "civil", "aerospace",
        "automotive", "manufactur", "process", "materials", "power", "energy",
        "renewable", "satellite", "3d-print", "3d-model", "hardware", "robotics",
        "firmware", "embedded",
    )),
    ("Business & Operations", (
        "business", "executive", "manager", "management", "operations", "operating",
        "project", "product", "program", "workflow", "continuity", "supply",
        "procurement", "marketing", "sales", "customer", "strategy", "change-communications",
        "human-resources", "hr-", "organization",
    )),
    ("Finance & Economics", (
        "finance", "financial", "accounting", "econom", "bank", "investment",
        "trading", "quantitative-finance", "risk", "audit", "treasury", "actuar",
    )),
    ("Legal & Governance", (
        "legal", "law", "governance", "policy", "regulator", "compliance",
        "contract", "ethics", "privacy", "public-sector", "government", "civil-rights",
    )),
    ("Creative & Design", (
        "design", "designer", "art", "audio", "music", "film", "video", "visual",
        "ux", "ui", "creative", "writing", "writer", "copy", "calligraphy",
        "textile", "fashion", "illustrat", "architecture", "photo", "conservator",
    )),
    ("Education & Humanities", (
        "education", "educator", "teacher", "tutor", "training", "learning",
        "history", "historian", "lingu", "localization", "literature", "philos",
        "anthropolog", "sociolog", "oral-history", "archive",
    )),
    ("Specialized & Other", ()),
)

def agent_category(slug: str | None, summary: str | None = None) -> str:
    def score(source: str, needles: tuple[str, ...], weight: int) -> int:
        normalized = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
        tokens = set(part for part in normalized.split("-") if part)
        total = 0
        for needle in needles:
            term = needle.lower().strip("-")
            if not term:
                continue
            # Short hints must be whole tokens. Raw substring matching classified
            # words such as "email" as AI and "article" as Creative.
            matched = term in tokens if len(term) <= 3 else term in normalized
            if matched:
                total += weight + min(3, term.count("-") + (1 if len(term) >= 8 else 0))
        return total

    ranked: list[tuple[int, int, str]] = []
    for index, (category, needles) in enumerate(_CATEGORY_RULES):
        if needles:
            ranked.append((score(str(slug or ""), needles, 6) + score(str(summary or ""), needles, 1), -index, category))
    best = max(ranked, default=(0, 0, "Specialized & Other"))
    return best[2] if best[0] > 0 else "Specialized & Other"

_STOP = {
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
    "be",
    "you",
    "your",
    "with",
    "this",
    "that",
    "from",
    "into",
    "as",
    "at",
    "by",
    "it",
    "its",
    "we",
    "our",
    "me",
    "my",
    "please",
    "help",
    "need",
    "want",
    "make",
    "use",
    "using",
}

# Roster cache: keyed by resolved root string
_CACHE: dict[str, Any] = {"root": "", "mtime": 0.0, "agents": [], "by_name": {}, "config": {}}


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def is_legacy_path(path: Path | str | None) -> bool:
    if not path:
        return False
    s = str(path).replace("/", "\\").lower()
    return any(m in s for m in _LEGACY_MARKERS)


def looks_like_matrix_root(path: Path | None) -> bool:
    if not path or not path.is_dir():
        return False
    if is_legacy_path(path):
        return False
    modfiles = path / "Modfiles"
    if modfiles.is_dir() and any(modfiles.glob("Modelfile_*")):
        return True
    if any(path.glob("Modelfile_*")):
        return True
    if (path / "MatrixConfig.json").is_file() and modfiles.is_dir():
        return True
    return False


def _candidate_from_env() -> Path | None:
    raw = (os.environ.get("CYPRA_MATRIX_DIR") or os.environ.get("BRAIN_MATRIX_DIR") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if looks_like_matrix_root(p):
        return p.resolve()
    return None


def resolve_matrix_root(
    root: Path | None = None,
    settings: dict[str, Any] | None = None,
) -> Path | None:
    """
    Prefer the Matrix folder copied into this Cypra Studio project.
    Never follow Documents\\CypraTeam or other leftover host paths.
    """
    proj = Path(root) if root else project_root()

    env_p = _candidate_from_env()
    if env_p:
        return env_p

    override = str((settings or {}).get("matrix_root") or "").strip()
    if override:
        p = Path(override).expanduser()
        if not p.is_absolute():
            p = proj / p
        if looks_like_matrix_root(p):
            return p.resolve()

    for name in _LOCAL_DIR_NAMES:
        cand = proj / name
        if looks_like_matrix_root(cand):
            return cand.resolve()

    # One-level scan: any sibling/child that actually contains Modfiles
    try:
        for child in proj.iterdir():
            if child.is_dir() and looks_like_matrix_root(child):
                return child.resolve()
    except OSError:
        pass

    return None


def load_matrix_config(matrix_root: Path | None) -> dict[str, Any]:
    if not matrix_root:
        return {}
    cfg = matrix_root / "MatrixConfig.json"
    if not cfg.is_file():
        return {}
    try:
        data = json.loads(cfg.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def parse_modelfile(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    from_m = _FROM_RE.search(raw)
    sys_m = _SYSTEM_TRIPLE_RE.search(raw) or _SYSTEM_QUOTE_RE.search(raw)
    params = {k: v for k, v in _PARAM_RE.findall(raw)}
    name = path.name
    if name.lower().startswith("modelfile_"):
        name = name[10:]
    name = name.strip() or path.stem
    directive = (sys_m.group(1).strip() if sys_m else "")
    summary = ""
    if directive:
        first = directive.splitlines()[0].strip()
        summary = first[:180]
    is_custom = path.parent.name.lower() == "customagents"
    return {
        "name": name,
        "slug": name.lower(),
        "label": _pretty_label(name),
        "path": str(path),
        "relpath": f"CustomAgents/{path.name}" if is_custom else f"Modfiles/{path.name}",
        "from": (from_m.group(1).strip() if from_m else ""),
        "directive": directive,
        "summary": summary,
        "parameters": params,
        "category": "CUSTOM" if is_custom else agent_category(name, summary),
        "custom": is_custom,
    }


def _pretty_label(slug: str) -> str:
    return " ".join(p.capitalize() for p in re.split(r"[-_]+", slug) if p)


def _modelfile_paths(matrix_root: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for folder in (matrix_root / "Modfiles", matrix_root / "CustomAgents", matrix_root):
        if not folder.is_dir():
            continue
        try:
            files = folder.glob("Modelfile_*")
        except OSError:
            continue
        for p in files:
            if not p.is_file():
                continue
            key = p.name.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(p)
    found.sort(key=lambda p: p.name.lower())
    return found


def _dir_mtime(matrix_root: Path) -> float:
    stamp = 0.0
    for folder in (matrix_root, matrix_root / "Modfiles", matrix_root / "CustomAgents"):
        try:
            stamp = max(stamp, folder.stat().st_mtime)
        except OSError:
            continue
    return stamp


def _load_roster(matrix_root: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    agents: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    for path in _modelfile_paths(matrix_root):
        try:
            agent = parse_modelfile(path)
        except OSError:
            continue
        slug = agent["slug"]
        agents.append(agent)
        by_name[slug] = agent
    return agents, by_name


def get_roster(
    settings: dict[str, Any] | None = None,
    *,
    root: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    matrix_root = resolve_matrix_root(root, settings)
    if not matrix_root:
        return {
            "ok": False,
            "root": None,
            "source": "missing",
            "agents": [],
            "by_name": {},
            "config": {},
            "count": 0,
        }

    mtime = _dir_mtime(matrix_root)
    cache_key = str(matrix_root)
    if (
        not force
        and _CACHE.get("root") == cache_key
        and float(_CACHE.get("mtime") or 0) == mtime
        and _CACHE.get("agents")
    ):
        return {
            "ok": True,
            "root": cache_key,
            "source": "local",
            "agents": _CACHE["agents"],
            "by_name": _CACHE["by_name"],
            "config": _CACHE["config"],
            "count": len(_CACHE["agents"]),
        }

    agents, by_name = _load_roster(matrix_root)
    config = load_matrix_config(matrix_root)
    _CACHE.update(
        {
            "root": cache_key,
            "mtime": mtime,
            "agents": agents,
            "by_name": by_name,
            "config": config,
        }
    )
    return {
        "ok": True,
        "root": cache_key,
        "source": "local",
        "agents": agents,
        "by_name": by_name,
        "config": config,
        "count": len(agents),
    }


def core_models(settings: dict[str, Any] | None = None) -> list[str]:
    roster = get_roster(settings)
    cfg = roster.get("config") or {}
    names = cfg.get("CoreModels") or cfg.get("core_models") or list(_DEFAULT_CORE)
    out: list[str] = []
    by_name = roster.get("by_name") or {}
    for n in names:
        slug = str(n).strip().lower()
        if slug and slug in by_name and slug not in out:
            out.append(slug)
    if not out:
        out = [n for n in _DEFAULT_CORE if n in by_name]
    return out


def get_agent(name: str | None, settings: dict[str, Any] | None = None) -> dict[str, Any] | None:
    slug = (name or "").strip().lower()
    if not slug:
        return None
    roster = get_roster(settings)
    return (roster.get("by_name") or {}).get(slug)


def public_agent(agent: dict[str, Any] | None, *, include_directive: bool = False) -> dict[str, Any] | None:
    if not agent:
        return None
    out = {
        "name": agent.get("name"),
        "slug": agent.get("slug"),
        "label": agent.get("label"),
        "from": agent.get("from"),
        "summary": agent.get("summary"),
        "category": agent.get("category") or agent_category(agent.get("slug"), agent.get("summary")),
        "relpath": agent.get("relpath"),
        "parameters": agent.get("parameters") or {},
        "has_directive": bool(agent.get("directive")),
    }
    if include_directive:
        out["directive"] = agent.get("directive") or ""
    return out


def search_agents(
    query: str = "",
    *,
    settings: dict[str, Any] | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    roster = get_roster(settings)
    agents: list[dict[str, Any]] = list(roster.get("agents") or [])
    q = (query or "").strip().lower()
    if q:
        scored: list[tuple[int, dict[str, Any]]] = []
        for a in agents:
            slug = str(a.get("slug") or "")
            label = str(a.get("label") or "").lower()
            summary = str(a.get("summary") or "").lower()
            score = 0
            if slug == q:
                score = 100
            elif slug.startswith(q):
                score = 80
            elif q in slug:
                score = 60
            elif q in label:
                score = 50
            elif q in summary:
                score = 30
            if score:
                scored.append((score, a))
        scored.sort(key=lambda t: (-t[0], t[1].get("slug") or ""))
        agents = [a for _, a in scored]
    else:
        cores = set(core_models(settings))
        agents.sort(key=lambda a: (0 if a.get("slug") in cores else 1, a.get("slug") or ""))
    cap = max(1, min(1000, int(limit or 1000)))
    return [public_agent(a) for a in agents[:cap] if a]


def _tokens(text: str) -> list[str]:
    words = [w.lower() for w in _WORD_RE.findall(text or "")]
    return [w for w in words if w not in _STOP and len(w) > 1]


_ORCH_TOKENS = {
    "nexus",
    "route",
    "routing",
    "orchestrat",
    "orchestration",
    "telemetry",
    "fleet",
    "coordinate",
    "coordinator",
    "multiagent",
}

_SPECIALTY_HINTS: dict[str, str] = {
    "algebraist": "algebra algebraic polynomial quadratic equation factoring unknowns",
    "calculus-expert": "calculus derivative integral limit differential",
    "tutoring": "tutor homework teach lesson student",
}


def route_agent(
    text: str,
    *,
    settings: dict[str, Any] | None = None,
    limit: int = 5,
    team: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Score roster against a prompt. Specialists beat Nexus Prime."""
    roster = get_roster(settings)
    tokens = _tokens(text)
    if not tokens:
        cores = core_models(settings)
        out = []
        for slug in cores[:limit]:
            a = (roster.get("by_name") or {}).get(slug)
            if a:
                item = public_agent(a) or {}
                item["score"] = 1
                out.append(item)
        return out

    token_set = set(tokens)
    orch_query = bool(token_set & _ORCH_TOKENS)
    operational_data = operational_snapshot()
    scored: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for agent in roster.get("agents") or []:
        slug = str(agent.get("slug") or "")
        parts = {p for p in re.split(r"[-_]+", slug) if p} | {slug}
        hint = _SPECIALTY_HINTS.get(slug, "")
        blob = (
            f"{slug} {agent.get('label') or ''} {agent.get('summary') or ''} "
            f"{hint} {(agent.get('directive') or '')[:400]}"
        ).lower()
        score = 0
        for tok in tokens:
            if tok == slug or tok in parts:
                score += 12
            elif any(tok == p or (len(tok) > 4 and (tok in p or p in tok)) for p in parts):
                score += 8
            elif tok in blob:
                score += 4
        if slug in ("nexus-prime", "nexus") and not orch_query:
            score -= 6
        if score > 0:
            bonus, components = performance_score(slug, token_set, team=team, operational_data=operational_data)
            scored.append((score + bonus, agent, components))
    scored.sort(key=lambda t: (-t[0], t[1].get("slug") or ""))
    out: list[dict[str, Any]] = []
    for score, agent, components in scored[: max(1, min(12, int(limit or 5)))]:
        item = public_agent(agent) or {}
        item["score"] = round(score, 3)
        item["routing_components"] = components
        out.append(item)
    return out


def matrix_enabled(settings: dict[str, Any] | None = None) -> bool:
    s = settings or {}
    if s.get("matrix_enabled") is False:
        return False
    return bool(resolve_matrix_root(settings=s))


def active_agent_name(settings: dict[str, Any] | None = None) -> str:
    s = settings or {}
    # The selected/saved agent is always authoritative. There is no UI routing mode.
    name = str(s.get("matrix_agent") or "").strip().lower()
    if bool(s.get("matrix_agent_locked")) and name and get_agent(name, s):
        return name
    if name and get_agent(name, s):
        return name
    forced = str(s.get("matrix_agent_resolved") or "").strip().lower()
    if forced and get_agent(forced, s):
        return forced
    if name and get_agent(name, s):
        return name
    cores = core_models(s)
    return cores[0] if cores else "cypra"


def resolve_chat_agent(
    settings: dict[str, Any] | None,
    user_text: str = "",
) -> dict[str, Any] | None:
    if not matrix_enabled(settings):
        return None
    s = settings or {}
    # Matrix always uses the saved/selected agent.
    return get_agent(active_agent_name(s), s)


def directive_block_for_chat(
    settings: dict[str, Any] | None,
    user_text: str = "",
    *,
    compact: bool = False,
) -> tuple[str, str]:
    """
    Returns (system_block, agent_slug). Empty block when Matrix is off/missing.
    """
    agent = resolve_chat_agent(settings, user_text)
    if not agent:
        return "", ""
    directive = (agent.get("directive") or "").strip()
    if not directive:
        return "", str(agent.get("slug") or "")
    cap = 700 if compact else 2400
    if len(directive) > cap:
        directive = directive[: cap - 1].rstrip() + "…"
    slug = str(agent.get("slug") or "")
    label = str(agent.get("label") or slug)
    rules = (
        "Speak as this Matrix specialist. Follow their operating rules. "
        "MEMORY CONTEXT and real [[vault titles]] still win over invented facts."
    )
    if compact:
        rules = "Speak as this specialist. Cite only real [[notes]]."
    block = (
        f"## MATRIX AGENT: {label}\n"
        f"{rules}\n\n"
        f"{directive}"
    )
    return block, slug


def raw_agent_directive(
    settings: dict[str, Any] | None,
    user_text: str = "",
    *,
    max_chars: int = 4000,
) -> tuple[str, str]:
    """Modelfile SYSTEM only — no BV wrapper that would steer the reply."""
    agent = resolve_chat_agent(settings, user_text)
    if not agent:
        return "", ""
    directive = (agent.get("directive") or "").strip()
    slug = str(agent.get("slug") or "")
    if not directive:
        return "", slug
    if len(directive) > max_chars:
        directive = directive[: max_chars - 1].rstrip() + "…"
    return directive, slug


def sanitize_matrix_root_setting(raw: str, *, project: Path | None = None) -> str:
    """Persist only a local, non-legacy path (or empty = auto)."""
    text = (raw or "").strip()
    if not text:
        return ""
    proj = project or project_root()
    p = Path(text).expanduser()
    if not p.is_absolute():
        p = proj / p
    try:
        resolved = p.resolve()
    except OSError:
        return ""
    if is_legacy_path(resolved) or not looks_like_matrix_root(resolved):
        return ""
    # Prefer a project-relative path when the folder lives inside BV
    try:
        rel = resolved.relative_to(proj.resolve())
        return str(rel)
    except ValueError:
        return str(resolved)


def status(settings: dict[str, Any] | None = None, *, root: Path | None = None) -> dict[str, Any]:
    roster = get_roster(settings, root=root)
    enabled = matrix_enabled(settings)
    agent_name = active_agent_name(settings) if roster.get("ok") else ""
    agent = get_agent(agent_name, settings) if agent_name else None
    return {
        "ok": bool(roster.get("ok")),
        "enabled": enabled and bool(roster.get("ok")),
        "root": roster.get("root"),
        "source": "local" if roster.get("ok") else "missing",
        "legacy_rejected": True,
        "count": int(roster.get("count") or 0),
        "core": (
            [s for s in core_models(settings) if s in {a.get("slug") for a in (roster.get("agents") or [])}]
            if roster.get("ok") else []
        ),
        "agent": public_agent(agent),
        "config": {
            "model": (roster.get("config") or {}).get("Model")
            or (roster.get("config") or {}).get("DefaultBaseModel"),
            "profile": (roster.get("config") or {}).get("DefaultProfile"),
            "context": (roster.get("config") or {}).get("ContextLength")
            or (roster.get("config") or {}).get("DefaultContext"),
        },
        "scanned_at": time.time(),
    }
