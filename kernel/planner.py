"""待办写入：TodoManager（learn-claude-code s03）。

带状态的任务清单、同时只允许一个 in_progress；连续多轮不更新计划会收到提醒，
用问责压力逼 agent 保持规划。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TodoStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


@dataclass
class Todo:
    title: str
    status: TodoStatus = TodoStatus.TODO
    note: str = ""


class TodoManager:
    def __init__(self, max_concurrent: int = 1) -> None:
        self.todos: list[Todo] = []
        self.max_concurrent = max_concurrent
        self._stall_rounds = 0

    def add(self, title: str) -> Todo:
        t = Todo(title=title)
        self.todos.append(t)
        return t

    def mark_in_progress(self, index: int) -> None:
        active = sum(1 for t in self.todos if t.status == TodoStatus.IN_PROGRESS)
        if active >= self.max_concurrent and self.todos[index].status != TodoStatus.IN_PROGRESS:
            raise ValueError(f"最多 {self.max_concurrent} 个进行中任务")
        self.todos[index].status = TodoStatus.IN_PROGRESS

    def mark_done(self, index: int) -> None:
        self.todos[index].status = TodoStatus.DONE
        self._stall_rounds = 0

    def bump_stall(self) -> str | None:
        """每轮调用；连续 3 轮无进展则返回提醒文案。"""
        self._stall_rounds += 1
        if self._stall_rounds >= 3:
            self._stall_rounds = 0
            return "提醒：你已经 3 轮没有更新任务进度，请确认计划仍然有效。"
        return None

    def summary(self) -> str:
        lines = [f"{i}. [{t.status.value}] {t.title}" for i, t in enumerate(self.todos)]
        return "\n".join(lines) or "（暂无任务）"
