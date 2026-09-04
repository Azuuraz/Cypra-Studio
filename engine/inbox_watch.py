"""Scan vault inbox for new files and queue them for ingest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class InboxWatcher:
    def __init__(self, state_path: Path) -> None:
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.seen: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self.state_path.exists():
            try:
                self.seen = json.loads(self.state_path.read_text(encoding="utf-8")).get("seen") or {}
            except (OSError, json.JSONDecodeError):
                self.seen = {}

    def save(self) -> None:
        self.state_path.write_text(json.dumps({"seen": self.seen}, indent=2), encoding="utf-8")

    def scan(self, inbox: Path) -> list[dict[str, Any]]:
        inbox = Path(inbox)
        if not inbox.is_dir():
            return []
        new_items = []
        for path in sorted(inbox.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".md", ".txt", ".json", ".csv", ".log"}:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            h = hashlib.sha256(data).hexdigest()[:16]
            key = str(path.name)
            if self.seen.get(key) == h:
                continue
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                continue
            new_items.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "hash": h,
                    "text": text,
                    "title": path.stem,
                }
            )
        return new_items

    def mark(self, name: str, file_hash: str) -> None:
        self.seen[name] = file_hash
        self.save()
