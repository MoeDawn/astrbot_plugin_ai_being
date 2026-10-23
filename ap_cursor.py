# -*- coding: utf-8 -*-
"""`get_events` 游标的持久化。

**每次成功轮询后立刻写**（空返回、`dropped` 也写）—— 见设计文档 §6.1。
写入走临时文件 + `os.replace`，避免断电/崩溃留下半截 JSON。

⚠️ 这是**本插件独占**的游标，与用户手写定时任务用的
`/AstrBot/data/mc_events_cursor.txt` 是两个文件。`get_events` 只有一个
`since`，两边共用会互相吃掉事件，且**完全不报错**。
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class Cursor:
    def __init__(self, path) -> None:
        self.path = Path(path)

    def load(self) -> int:
        """读游标。**任何异常都返回 0**（从头读），绝不抛出。"""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return 0
        try:
            value = int(raw.get("since", 0))
        except (AttributeError, TypeError, ValueError):
            return 0
        return max(0, value)

    def save(self, since: int) -> None:
        try:
            value = max(0, int(since))
        except (TypeError, ValueError):
            value = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps({"since": value}, ensure_ascii=False)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, self.path)
