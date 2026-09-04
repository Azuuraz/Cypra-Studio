"""Legacy Memory v1 operation log: undo, session-to-note timeline, and growth history."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class OpsLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ops: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.ops = data.get("ops") or []
        except (OSError, json.JSONDecodeError):
            self.ops = []

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"ops": self.ops[-500:], "saved_at": _now()}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def record(
        self,
        kind: str,
        *,
        session_id: str | None = None,
        note_ids: list[str] | None = None,
        note_titles: list[str] | None = None,
        meta: dict[str, Any] | None = None,
        undoable: bool = False,
        snapshot: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        op = {
            "id": uuid.uuid4().hex[:12],
            "kind": kind,
            "at": _now(),
            "session_id": session_id,
            "note_ids": note_ids or [],
            "note_titles": note_titles or [],
            "meta": meta or {},
            "undoable": undoable,
            "snapshot": snapshot or [],
            "undone": False,
        }
        self.ops.append(op)
        self.save()
        return op

    def last_undoable(self) -> dict[str, Any] | None:
        for op in reversed(self.ops):
            if op.get("undoable") and not op.get("undone") and op.get("kind") in (
                "extract",
                "ingest",
                "capture",
            ):
                return op
        return None

    def mark_undone(self, op_id: str) -> None:
        for op in self.ops:
            if op.get("id") == op_id:
                op["undone"] = True
                break
        self.save()

    def active_memory_ops(self) -> list[dict[str, Any]]:
        """Return operations from the current legacy-memory lifetime.

        Reset markers preserve append-only history while preventing pre-reset
        note activity from feeding a new memory epoch or its rollups.
        Historical ``brain_reset`` markers remain accepted for compatibility.
        """
        start = 0
        for index, op in enumerate(self.ops):
            if op.get("kind") in ("memory_reset", "brain_reset"):
                start = index + 1
        return self.ops[start:]

    def timeline(self, limit: int = 40) -> list[dict[str, Any]]:
        out = []
        active_ops = self.active_memory_ops()
        for op in reversed(active_ops[-limit * 2 :]):
            if op.get("undone"):
                continue
            if not op.get("note_ids") and op.get("kind") not in ("rollup", "import"):
                continue
            out.append(
                {
                    "id": op["id"],
                    "kind": op["kind"],
                    "at": op["at"],
                    "session_id": op.get("session_id"),
                    "note_ids": op.get("note_ids") or [],
                    "note_titles": op.get("note_titles") or [],
                    "meta": op.get("meta") or {},
                }
            )
            if len(out) >= limit:
                break
        return out

    def growth(self, limit: int = 100) -> list[dict[str, Any]]:
        """Flatten note births for growth replay."""
        events = []
        for op in self.active_memory_ops():
            if op.get("undone"):
                continue
            titles = op.get("note_titles") or []
            ids = op.get("note_ids") or []
            for i, nid in enumerate(ids):
                events.append(
                    {
                        "at": op.get("at"),
                        "note_id": nid,
                        "title": titles[i] if i < len(titles) else nid,
                        "kind": op.get("kind"),
                        "op_id": op.get("id"),
                    }
                )
        return events[-limit:]
