"""
Cypra Studio plugin manager.

Install plugins from GitHub (zip download), enable/disable, remove.
Plugins live under data/plugins/<id>/ with a plugin.json manifest.

Manifest example:
{
  "id": "hello-status",
  "name": "Hello Status",
  "version": "1.0.0",
  "description": "Adds a status chip",
  "author": "you",
  "homepage": "https://github.com/you/hello-status",
  "main": "plugin.py",
  "js": "static/plugin.js",
  "css": "static/plugin.css",
  "permissions": ["ui"]
}
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

MANIFEST_NAME = "plugin.json"
REGISTRY_NAME = "registry.json"
GITHUB_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com[/:](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?(?:/|$)",
    re.I,
)
OWNER_REPO_RE = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_github_source(source: str) -> dict[str, str]:
    """
    Accept:
      owner/repo
      owner/repo@branch
      https://github.com/owner/repo
      https://github.com/owner/repo/tree/branch
    """
    raw = (source or "").strip()
    if not raw:
        raise ValueError("Empty GitHub source")

    ref = "main"
    # owner/repo@ref
    if "@" in raw and "github.com" not in raw.lower():
        left, right = raw.rsplit("@", 1)
        raw, ref = left.strip(), right.strip() or "main"

    m = OWNER_REPO_RE.match(raw)
    if m:
        return {"owner": m.group("owner"), "repo": m.group("repo"), "ref": ref}

    m = GITHUB_RE.search(raw)
    if not m:
        raise ValueError(
            "Invalid GitHub source. Use owner/repo or https://github.com/owner/repo"
        )
    owner, repo = m.group("owner"), m.group("repo")
    # /tree/<ref> or /commit/<sha>
    tree = re.search(r"github\.com/[^/]+/[^/]+/(?:tree|commit)/([^/?#]+)", source, re.I)
    if tree:
        ref = tree.group(1)
    return {"owner": owner, "repo": repo, "ref": ref}


def slug_id(raw: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (raw or "").strip()).strip("-._").lower()
    return s[:64] or f"plugin-{abs(hash(raw)) % 10**8}"


class PluginManager:
    """Install / enable / disable / remove plugins under data/plugins."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root / REGISTRY_NAME
        self._registry: dict[str, Any] = {"plugins": {}, "updated": None}
        self._loaded: dict[str, Any] = {}  # id -> module
        self._hooks: dict[str, list[Callable[..., Any]]] = {}
        self._load_registry()

    # ── registry ─────────────────────────────────────────────────────

    def _load_registry(self) -> None:
        if self.registry_path.exists():
            try:
                self._registry = json.loads(
                    self.registry_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                self._registry = {"plugins": {}, "updated": None}
        if "plugins" not in self._registry:
            self._registry["plugins"] = {}
        # reconcile disk folders
        for path in self.root.iterdir():
            if not path.is_dir() or path.name.startswith("."):
                continue
            manifest = self.read_manifest(path)
            if not manifest:
                continue
            pid = manifest.get("id") or path.name
            entry = self._registry["plugins"].get(pid) or {}
            entry.update(
                {
                    "id": pid,
                    "path": path.name,
                    "name": manifest.get("name") or pid,
                    "version": manifest.get("version") or "0.0.0",
                    "description": manifest.get("description") or "",
                    "author": manifest.get("author") or "",
                    "homepage": manifest.get("homepage") or "",
                    "source": entry.get("source") or "",
                    "enabled": bool(entry.get("enabled", True)),
                    "installed_at": entry.get("installed_at") or _now_iso(),
                }
            )
            self._registry["plugins"][pid] = entry
        self.save_registry()

    def save_registry(self) -> None:
        self._registry["updated"] = _now_iso()
        tmp = self.registry_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._registry, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.registry_path)

    def list_plugins(self) -> list[dict[str, Any]]:
        out = []
        for pid, entry in sorted(
            (self._registry.get("plugins") or {}).items(),
            key=lambda kv: (kv[1].get("name") or kv[0]).lower(),
        ):
            info = dict(entry)
            info["loaded"] = pid in self._loaded
            info["has_js"] = bool(self._asset_rel(pid, "js"))
            info["has_css"] = bool(self._asset_rel(pid, "css"))
            info["has_python"] = bool(self._asset_rel(pid, "main") or self._asset_rel(pid, "python"))
            out.append(info)
        return out

    def get(self, plugin_id: str) -> dict[str, Any] | None:
        return (self._registry.get("plugins") or {}).get(plugin_id)

    def plugin_dir(self, plugin_id: str) -> Path | None:
        entry = self.get(plugin_id)
        if not entry:
            return None
        path = self.root / (entry.get("path") or plugin_id)
        return path if path.is_dir() else None

    def read_manifest(self, folder: Path) -> dict[str, Any] | None:
        mf = folder / MANIFEST_NAME
        if not mf.exists():
            return None
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            if not data.get("id"):
                data["id"] = slug_id(folder.name)
            return data
        except (OSError, json.JSONDecodeError):
            return None

    def _asset_rel(self, plugin_id: str, key: str) -> str | None:
        folder = self.plugin_dir(plugin_id)
        if not folder:
            return None
        manifest = self.read_manifest(folder) or {}
        # aliases
        if key == "python":
            key = "main"
        rel = manifest.get(key) or (manifest.get("entry") or {}).get(key)
        if not rel:
            # conventions
            defaults = {
                "main": "plugin.py",
                "js": "static/plugin.js",
                "css": "static/plugin.css",
            }
            rel = defaults.get(key)
        if not rel:
            return None
        p = folder / rel
        return rel if p.is_file() else None

    # ── install ──────────────────────────────────────────────────────

    def install_from_github(
        self,
        source: str,
        *,
        ref: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        meta = parse_github_source(source)
        if ref:
            meta["ref"] = ref
        owner, repo, branch = meta["owner"], meta["repo"], meta["ref"]
        # try requested ref, then main, then master
        refs = [branch]
        for fallback in ("main", "master"):
            if fallback not in refs:
                refs.append(fallback)

        last_err = ""
        zip_bytes: bytes | None = None
        used_ref = branch
        for r in refs:
            url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{r}.zip"
            # tags
            tag_url = f"https://github.com/{owner}/{repo}/archive/refs/tags/{r}.zip"
            for try_url in (url, tag_url):
                try:
                    resp = requests.get(
                        try_url,
                        timeout=60,
                        headers={"User-Agent": "CypraStudio-PluginManager/1.0"},
                        allow_redirects=True,
                    )
                    if resp.ok and resp.content[:2] == b"PK":
                        zip_bytes = resp.content
                        used_ref = r
                        break
                    last_err = f"HTTP {resp.status_code} for {try_url}"
                except requests.RequestException as e:
                    last_err = str(e)
            if zip_bytes:
                break
        if not zip_bytes:
            raise RuntimeError(
                f"Could not download {owner}/{repo} ({last_err}). "
                "Check the repo URL/branch and network access."
            )

        return self._install_from_zip_bytes(
            zip_bytes,
            source=f"github:{owner}/{repo}@{used_ref}",
            preferred_id=slug_id(repo),
            force=force,
        )

    def install_from_zip_path(
        self, zip_path: Path, *, force: bool = False, source: str = ""
    ) -> dict[str, Any]:
        data = Path(zip_path).read_bytes()
        return self._install_from_zip_bytes(
            data, source=source or f"zip:{zip_path.name}", force=force
        )

    def install_from_folder(
        self, folder: Path, *, force: bool = False, source: str = ""
    ) -> dict[str, Any]:
        folder = Path(folder)
        if not folder.is_dir():
            raise ValueError(f"Not a folder: {folder}")
        # find manifest
        manifest_dir = self._find_manifest_dir(folder)
        if not manifest_dir:
            raise ValueError("No plugin.json found in folder (depth ≤ 2)")
        return self._install_from_dir(
            manifest_dir,
            source=source or f"folder:{folder}",
            force=force,
        )

    def _find_manifest_dir(self, root: Path) -> Path | None:
        if (root / MANIFEST_NAME).is_file():
            return root
        # one level
        for child in root.iterdir():
            if child.is_dir() and (child / MANIFEST_NAME).is_file():
                return child
        # two levels (github zip has repo-branch/ prefix)
        for child in root.iterdir():
            if not child.is_dir():
                continue
            for sub in child.iterdir():
                if sub.is_dir() and (sub / MANIFEST_NAME).is_file():
                    return sub
                if sub.name == MANIFEST_NAME and sub.is_file():
                    return child
        return None

    def _install_from_zip_bytes(
        self,
        data: bytes,
        *,
        source: str,
        preferred_id: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="bv-plugin-") as tmp:
            tmp_path = Path(tmp)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    name = Path(info.filename)
                    if name.is_absolute() or ".." in name.parts:
                        raise ValueError(f"Unsafe plugin archive path: {info.filename}")
                    target = (tmp_path / info.filename).resolve()
                    try:
                        target.relative_to(tmp_path.resolve())
                    except ValueError:
                        raise ValueError(f"Unsafe plugin archive path: {info.filename}") from None
                zf.extractall(tmp_path)
            manifest_dir = self._find_manifest_dir(tmp_path)
            if not manifest_dir:
                raise ValueError(
                    "Downloaded archive has no plugin.json. "
                    "Repo root (or one subfolder) must include plugin.json."
                )
            return self._install_from_dir(
                manifest_dir,
                source=source,
                preferred_id=preferred_id,
                force=force,
            )

    def _install_from_dir(
        self,
        src: Path,
        *,
        source: str,
        preferred_id: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        manifest = self.read_manifest(src)
        if not manifest:
            raise ValueError("Invalid plugin.json")
        pid = slug_id(str(manifest.get("id") or preferred_id or src.name))
        manifest["id"] = pid
        dest = self.root / pid
        if dest.exists():
            if not force:
                raise FileExistsError(
                    f"Plugin '{pid}' already installed. Remove it first or pass force=true."
                )
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
        # rewrite manifest with normalized id
        (dest / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        entry = {
            "id": pid,
            "path": pid,
            "name": manifest.get("name") or pid,
            "version": manifest.get("version") or "0.0.0",
            "description": manifest.get("description") or "",
            "author": manifest.get("author") or "",
            "homepage": manifest.get("homepage") or "",
            "source": source,
            "enabled": True,
            "installed_at": _now_iso(),
        }
        self._registry.setdefault("plugins", {})[pid] = entry
        self.save_registry()
        return {"ok": True, "plugin": entry, "manifest": manifest}

    # ── enable / disable / remove ────────────────────────────────────

    def set_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        entry = self.get(plugin_id)
        if not entry:
            raise KeyError(f"Plugin not found: {plugin_id}")
        entry["enabled"] = bool(enabled)
        self._registry["plugins"][plugin_id] = entry
        self.save_registry()
        if not enabled and plugin_id in self._loaded:
            del self._loaded[plugin_id]
        return entry

    def remove(self, plugin_id: str) -> dict[str, Any]:
        entry = self.get(plugin_id)
        if not entry:
            raise KeyError(f"Plugin not found: {plugin_id}")
        folder = self.plugin_dir(plugin_id)
        if folder and folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        self._registry.get("plugins", {}).pop(plugin_id, None)
        self._loaded.pop(plugin_id, None)
        self.save_registry()
        return {"ok": True, "removed": plugin_id, "was": entry}

    # ── assets for UI ────────────────────────────────────────────────

    def client_assets(self) -> list[dict[str, str]]:
        """Enabled plugins' JS/CSS URLs for the frontend to load."""
        assets = []
        for p in self.list_plugins():
            if not p.get("enabled"):
                continue
            pid = p["id"]
            item: dict[str, str] = {"id": pid, "name": p.get("name") or pid}
            js = self._asset_rel(pid, "js")
            css = self._asset_rel(pid, "css")
            if js:
                item["js"] = f"/api/plugins/{pid}/file/{js}"
            if css:
                item["css"] = f"/api/plugins/{pid}/file/{css}"
            if js or css:
                assets.append(item)
        return assets

    def resolve_file(self, plugin_id: str, rel: str) -> Path | None:
        folder = self.plugin_dir(plugin_id)
        if not folder:
            return None
        # no path escape
        rel = rel.replace("\\", "/").lstrip("/")
        if ".." in rel.split("/"):
            return None
        path = (folder / rel).resolve()
        try:
            path.relative_to(folder.resolve())
        except ValueError:
            return None
        return path if path.is_file() else None

    # ── python load / hooks ──────────────────────────────────────────

    def load_enabled(self, api_ctx: dict[str, Any] | None = None) -> dict[str, Any]:
        """Import enabled plugin.py modules and call register(api) if present."""
        loaded = []
        errors = []
        for p in self.list_plugins():
            if not p.get("enabled"):
                continue
            pid = p["id"]
            rel = self._asset_rel(pid, "main")
            if not rel:
                continue
            folder = self.plugin_dir(pid)
            if not folder:
                continue
            py_path = folder / rel
            try:
                mod = self._import_plugin_module(pid, py_path)
                self._loaded[pid] = mod
                register = getattr(mod, "register", None)
                if callable(register):
                    api = PluginAPI(self, pid, api_ctx or {})
                    register(api)
                loaded.append(pid)
            except Exception as e:
                errors.append({"id": pid, "error": str(e)})
        return {"loaded": loaded, "errors": errors}

    def _import_plugin_module(self, plugin_id: str, path: Path) -> Any:
        name = f"bv_plugin_{slug_id(plugin_id).replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(name, path)
        if not spec or not spec.loader:
            raise ImportError(f"Cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def emit(self, hook: str, *args: Any, **kwargs: Any) -> list[Any]:
        results = []
        for fn in list(self._hooks.get(hook) or []):
            try:
                results.append(fn(*args, **kwargs))
            except Exception as e:
                results.append({"error": str(e), "hook": hook})
        return results

    def on(self, hook: str, fn: Callable[..., Any]) -> None:
        self._hooks.setdefault(hook, []).append(fn)


class PluginAPI:
    """Surface passed to plugin register(api)."""

    def __init__(
        self, manager: PluginManager, plugin_id: str, ctx: dict[str, Any]
    ) -> None:
        self.manager = manager
        self.plugin_id = plugin_id
        self.ctx = ctx

    def on(self, hook: str, fn: Callable[..., Any]) -> None:
        self.manager.on(hook, fn)

    def log(self, msg: str) -> None:
        # lightweight — plugins can print; server may capture later
        print(f"[plugin:{self.plugin_id}] {msg}")
