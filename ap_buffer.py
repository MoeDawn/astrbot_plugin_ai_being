# -*- coding: utf-8 -*-
"""本地事件缓冲 + 每任务水位线。

**为什么仍然只留一个读者**：`get_events` 的 `since` 是全局唯一游标。
服务端实现（`awesome-mineflayer-mcp` 的 `dist/bot/events.js`）里 `drain()`
只按 `since` 过滤、**不删除**事件，所以多读者不会互相吃事件 —— 但每个读者
都得各自维护游标、各自处理 `types` 过滤与分页，多一处就多一处漂移。
让唯一轮询器读一次、事件落到这里，各任务按自己的 `seq` 水位线来取：
**同一事件可以被多个任务各读一次**，这是本设计最大的收益。
"""
from __future__ import annotations

import time

DEFAULT_MAX_ENTRIES = 500


def _seq_of(event):
    if not isinstance(event, dict):
        return None
    value = event.get("seq")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


class Buffer:
    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self.max_entries = max(1, int(max_entries))
        self._events = []
        self.evicted = 0

    def add(self, events) -> int:
        """追加一批事件，返回**本次被淘汰事件的最大 seq**（没淘汰返回 0）。

        调用方用它作为本地丢失区间的上界 —— 用 `nextSince` 会把「淘汰了
        早已读过的事件」误报成丢失。

        ⚠️ 存的是事件的**浅拷贝**并附本地接收时刻；下游只读，不得原地修改。
        """
        now = time.time()
        for event in events or []:
            seq = _seq_of(event)
            if seq is None:
                continue
            item = dict(event)
            item["received_at"] = now
            self._events.append(item)
        self._events.sort(key=lambda item: item["seq"])
        if len(self._events) > self.max_entries:
            removed = len(self._events) - self.max_entries
            dropped_items = self._events[:removed]
            del self._events[:removed]
            self.evicted += removed
            return max(item["seq"] for item in dropped_items)
        return 0

    def max_seq(self) -> int:
        return self._events[-1]["seq"] if self._events else 0

    def drop_above(self, seq) -> int:
        """丢弃 `seq` 大于给定值的本地事件（服务端序号回退时用）。返回丢弃条数。"""
        try:
            limit = int(seq)
        except (TypeError, ValueError):
            return 0
        before = len(self._events)
        self._events = [e for e in self._events if e["seq"] <= limit]
        return before - len(self._events)

    def take(self, watermark: int, types, limit: int = 60):
        """取 `seq > watermark` 且类型匹配的事件。

        返回 `(事件列表, 新水位线)`。新水位线是**缓冲里最大 seq**，不是最后
        返回那条 —— 被 `limit` 截掉的是更早的事件，不该下次再读一遍。
        水位线只进不退。返回的是缓冲内事件的**引用**，下游只读。
        """
        try:
            watermark = int(watermark)
        except (TypeError, ValueError):
            watermark = 0
        if not isinstance(limit, int) or isinstance(limit, bool):
            limit = 0
        if limit < 0:
            limit = 0                      # 与 render_events 同口径：非正数 = 不限制
        wanted = {str(t) for t in (types or [])}
        picked = [
            event
            for event in self._events
            if event["seq"] > watermark
            and (not wanted or str(event["type"]) in wanted)
        ]
        if limit and len(picked) > limit:
            picked = picked[-limit:]
        new_watermark = max(watermark, self.max_seq())
        return picked, new_watermark

    def clear(self) -> None:
        self._events.clear()
