"""Multi-vault registry under data/vaults/ + import helpers."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.vault import Vault, slugify


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class VaultManager:
    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.vaults_root = self.data_root / "vaults"
        self.vaults_root.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.data_root / "vaults.json"
        self._ensure_default()

    def _ensure_default(self) -> None:
        reg = self.registry()
        # migrate legacy data/vault into vaults/default if needed
        legacy = self.data_root / "vault"
        default_path = self.vaults_root / "default"
        if not reg.get("vaults"):
            if legacy.exists() and any(legacy.rglob("*.md")):
                if not default_path.exists():
                    shutil.copytree(legacy, default_path, dirs_exist_ok=True)
            default_path.mkdir(parents=True, exist_ok=True)
            (default_path / "wiki").mkdir(exist_ok=True)
            (default_path / "inbox").mkdir(exist_ok=True)
            reg = {
                "active": "default",
                "vaults": [
                    {
                        "id": "default",
                        "name": "Default",
                        "path": "vaults/default",
                        "created": _now(),
                    }
                ],
            }
            self._save(reg)

    def registry(self) -> dict[str, Any]:
        if self.registry_path.exists():
            try:
                return json.loads(self.registry_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return {"active": "default", "vaults": []}

    def _save(self, reg: dict[str, Any]) -> None:
        self.registry_path.write_text(json.dumps(reg, indent=2), encoding="utf-8")

    def active_id(self) -> str:
        return self.registry().get("active") or "default"

    def active_path(self) -> Path:
        reg = self.registry()
        aid = reg.get("active") or "default"
        for v in reg.get("vaults") or []:
            if v.get("id") == aid:
                return self.data_root / v["path"]
        return self.vaults_root / "default"

    def list_vaults(self) -> list[dict[str, Any]]:
        return list(self.registry().get("vaults") or [])

    def create(self, name: str) -> dict[str, Any]:
        reg = self.registry()
        vid = slugify(name).replace(" ", "-").lower() or f"vault-{len(reg.get('vaults') or [])+1}"
        # unique
        existing = {v["id"] for v in reg.get("vaults") or []}
        base = vid
        i = 2
        while vid in existing:
            vid = f"{base}-{i}"
            i += 1
        path = self.vaults_root / vid
        path.mkdir(parents=True, exist_ok=True)
        (path / "wiki").mkdir(exist_ok=True)
        (path / "inbox").mkdir(exist_ok=True)
        entry = {"id": vid, "name": name.strip() or vid, "path": f"vaults/{vid}", "created": _now()}
        reg.setdefault("vaults", []).append(entry)
        self._save(reg)
        Vault(path)  # seed
        return entry

    def switch(self, vault_id: str) -> dict[str, Any]:
        reg = self.registry()
        for v in reg.get("vaults") or []:
            if v.get("id") == vault_id:
                reg["active"] = vault_id
                self._save(reg)
                return v
        raise KeyError(vault_id)

    def import_markdown_folder(self, source: Path, vault: Vault) -> dict[str, Any]:
        source = Path(source)
        if not source.is_dir():
            raise FileNotFoundError(str(source))
        imported = 0
        for path in source.rglob("*.md"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            title = path.stem
            # strip frontmatter title if present
            m = re.match(r"^---\s*\n.*?\n---\s*\n", text, re.DOTALL)
            body = text[m.end() :] if m else text
            for line in body.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip() or title
                    break
            vault.upsert_note(title, text if text.startswith("---") else body, merge=True)
            imported += 1
        return {"imported": imported, "source": str(source)}
