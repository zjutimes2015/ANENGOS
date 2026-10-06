"""append-only 审计日志：每个动作、每次审批决策都留痕，可回放。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    """JSONL 追加写；只追加不修改，保证审计可信。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, entry: dict[str, Any]) -> None:
        entry = {"ts": _now(), **entry}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def replay(self) -> list[dict[str, Any]]:
        """回放全部审计记录。"""
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def actions_for(self, actor: str) -> list[dict[str, Any]]:
        return [e for e in self.replay() if e.get("actor") == actor]
