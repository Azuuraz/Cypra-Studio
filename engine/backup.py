"""
Backup / restore helpers for Cypra Studio program state.

Default destination: ~/Documents/CypraStudio/backups
(Windows: D:\\Data\\Documents\\… when that is the user Documents folder).
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_NAME = "CypraStudio"

# What to copy from the app data root
DATA_INCLUDE = (
    "settings.json",
    "vaults.json",
    "ops.json",
    "memory",
    "sessions",
    "vault",
    "vaults",
    "inbox_seen.json",
)

# Optional extras if present
DATA_OPTIONAL = (
    "launch.log",
    "backups",  # nested older backups (skipped when nesting)
)


def documents_dir() -> Path:
    """User Documents folder (cross-platform), honoring Windows folder redirection."""
    # Explicit override (full backups root OR Documents parent)
    env = os.environ.get("CYPRA_BACKUP_DIR") or os.environ.get("BRAIN_BACKUP_DIR")
    if env:
        p = Path(env)
        if p.name.lower() == "backups" and p.parent.name == APP_NAME:
            return p.parent.parent
        if p.name == APP_NAME:
            return p.parent
        return p

    # Windows: Known Folder FOLDERID_Documents (handles D:\Data\Documents redirection)
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            # SHGetKnownFolderPath
            _SHGetKnownFolderPath = ctypes.windll.shell32.SHGetKnownFolderPath
            _SHGetKnownFolderPath.argtypes = [
                ctypes.c_void_p,
                wintypes.DWORD,
                wintypes.HANDLE,
                ctypes.POINTER(ctypes.c_wchar_p),
            ]
            # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", wintypes.BYTE * 8),
                ]

            folderid = GUID(
                0xFDD39AD0,
                0x238F,
                0x46AF,
                (wintypes.BYTE * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7),
            )
            path_ptr = ctypes.c_wchar_p()
            hr = _SHGetKnownFolderPath(ctypes.byref(folderid), 0, None, ctypes.byref(path_ptr))
            if hr == 0 and path_ptr.value:
                out = Path(path_ptr.value)
                ctypes.windll.ole32.CoTaskMemFree(path_ptr)
                if out.exists():
                    return out
        except Exception:
            pass
        try:
            import ctypes
            from ctypes import wintypes

            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            # CSIDL_PERSONAL = 5
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
                out = Path(buf.value)
                if out.exists():
                    return out
        except Exception:
            pass

    home = Path.home()
    for candidate in (
        Path("D:/Data/Documents"),
        home / "Documents",
        home / "My Documents",
        Path(os.environ.get("USERPROFILE", "")) / "Documents",
    ):
        try:
            if candidate and Path(candidate).exists():
                return Path(candidate)
        except OSError:
            continue
    return home / "Documents"


def backup_root() -> Path:
    env = os.environ.get("CYPRA_BACKUP_DIR") or os.environ.get("BRAIN_BACKUP_DIR")
    if env:
        root = Path(env)
        root.mkdir(parents=True, exist_ok=True)
        return root
    root = documents_dir() / APP_NAME / "backups"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _copy_tree(src: Path, dst: Path, *, skip_names: set[str] | None = None) -> None:
    skip = skip_names or set()
    if not src.exists():
        return
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return
    for root, dirs, files in os.walk(src):
        root_p = Path(root)
        # prune heavy/cache dirs
        dirs[:] = [d for d in dirs if d not in skip and d != "__pycache__"]
        rel = root_p.relative_to(src)
        out_dir = dst / rel
        out_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.endswith(".pyc"):
                continue
            shutil.copy2(root_p / f, out_dir / f)


def save_program_state(
    data_root: Path,
    *,
    project_root: Path | None = None,
    include_project_snapshot: bool = False,
    also_local: bool = True,
) -> dict[str, Any]:
    """
    Full program-state backup into Documents/CypraStudio/backups.

    Includes: settings, vaults, memory indexes, chat sessions, ops log.
    Optionally snapshots app code (engine/static/templates).
    """
    data_root = Path(data_root)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    stamp_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = backup_root()
    folder = root / f"state_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)

    data_out = folder / "data"
    data_out.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for name in DATA_INCLUDE:
        src = data_root / name
        if not src.exists():
            continue
        dst = data_out / name
        if src.is_dir():
            _copy_tree(src, dst, skip_names={"backups", "__pycache__"})
        else:
            shutil.copy2(src, dst)
        copied.append(name)

    for name in DATA_OPTIONAL:
        if name == "backups":
            continue  # avoid recursive backup bloat
        src = data_root / name
        if src.exists() and src.is_file():
            shutil.copy2(src, data_out / name)
            copied.append(name)

    # Project code snapshot (optional — larger)
    if include_project_snapshot and project_root:
        proj = Path(project_root)
        snap = folder / "project_snapshot"
        snap.mkdir(parents=True, exist_ok=True)
        for rel in (
            "server.py",
            "app.py",
            "requirements.txt",
            "README.md",
            "engine",
            "templates",
            "static",
            "MatrixFiles",
        ):
            src = proj / rel
            if not src.exists():
                continue
            dst = snap / rel
            if src.is_dir():
                _copy_tree(
                    src,
                    dst,
                    skip_names={
                        "__pycache__",
                        "node_modules",
                        "OllamaModels",
                        "Logs",
                        "Tasks",
                        "Backups",
                    },
                )
            else:
                shutil.copy2(src, dst)
        copied.append("project_snapshot")

    # Manifest
    settings_preview: dict[str, Any] = {}
    settings_path = data_out / "settings.json"
    if settings_path.exists():
        try:
            settings_preview = json.loads(settings_path.read_text(encoding="utf-8"))
            # mask key in manifest only (full key stays in settings.json for restore)
            if settings_preview.get("xai_api_key"):
                k = str(settings_preview["xai_api_key"])
                settings_preview["xai_api_key"] = ("••••" + k[-4:]) if len(k) > 4 else "••••"
        except (OSError, json.JSONDecodeError):
            settings_preview = {}

    manifest = {
        "app": APP_NAME,
        "kind": "program_state",
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
        "stamp": stamp,
        "stamp_utc": stamp_utc,
        "source_data": str(data_root.resolve()),
        "destination": str(folder.resolve()),
        "includes": copied,
        "settings_summary": {
            "llm_provider": settings_preview.get("llm_provider"),
            "theme_preset": settings_preview.get("theme_preset"),
            "ollama_chat_model": settings_preview.get("ollama_chat_model"),
            "onboarding_done": settings_preview.get("onboarding_done"),
            "settings_schema": settings_preview.get("settings_schema"),
        },
        "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME"),
        "user": os.environ.get("USERNAME") or os.environ.get("USER"),
    }
    (folder / "BACKUP_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (folder / "README.txt").write_text(
        "\n".join(
            [
                "Cypra Studio — program state backup",
                f"Created: {manifest['backed_up_at']}",
                f"Folder:  {folder}",
                "",
                "Contents:",
                "  data/settings.json  — preferences + provider",
                "  data/vaults/        — all vaults & wiki notes",
                "  data/memory/        — search index + embeddings",
                "  data/sessions/      — chat sessions",
                "  data/ops.json       — activity log",
                "",
                "Restore: copy data/* back into the app's data/ folder",
                "(close the app first), or use a future Restore UI.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    # Zip for single-file restore
    zip_path = root / f"CypraStudio_state_{stamp}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in folder.rglob("*"):
            if path.is_file():
                zf.write(path, arcname=str(path.relative_to(folder)))

    # Latest pointer
    (root / "LATEST.txt").write_text(
        f"folder={folder}\nzip={zip_path}\nat={stamp}\n", encoding="utf-8"
    )
    (root / "LATEST.json").write_text(
        json.dumps(
            {
                "folder": str(folder),
                "zip": str(zip_path),
                "at": stamp,
                "manifest": manifest,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Optional mirror under app data/backups (small pointer + zip copy if small)
    local_zip = None
    if also_local:
        local_dir = data_root / "backups"
        local_dir.mkdir(parents=True, exist_ok=True)
        local_zip = local_dir / zip_path.name
        try:
            shutil.copy2(zip_path, local_zip)
            (local_dir / "LATEST_DOCUMENTS.txt").write_text(
                f"Primary backup is in Documents:\n{zip_path}\n{folder}\n",
                encoding="utf-8",
            )
        except OSError:
            local_zip = None

    folder_size = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
    zip_size = zip_path.stat().st_size if zip_path.exists() else 0

    return {
        "ok": True,
        "folder": str(folder),
        "zip": str(zip_path),
        "local_zip": str(local_zip) if local_zip else None,
        "path": str(zip_path),
        "size": zip_size,
        "folder_size": folder_size,
        "stamp": stamp,
        "documents_root": str(root),
        "includes": copied,
        "message": f"Program state saved to Documents · {zip_path.name}",
    }
