# -*- coding: utf-8 -*-
"""把实体的言行推送到 QQ 群。

⚠️ `context.send_message` 在找不到平台时**只记 warning、返回 False、
不抛异常**（`core/star/context.py`）。所以这里必须把 False 带出去，
由调用方补一条带群号的 warning —— 否则就是静默丢弃。
"""
from __future__ import annotations

import time


def render(template: str, **values) -> str:
    """渲染模板。占位符缺失或格式非法时**原样返回**，不抛。"""
    try:
        return str(template).format(**values)
    except (KeyError, IndexError, ValueError, AttributeError):
        return str(template)


class Dedup:
    """同一句话的短时去重。

    用途有二：
      ① 工具钩子与事件流各抓到一次时，群里只出现一条；
      ② **防自唤醒** —— 它用 chat 工具说的那句若被 MCP 回灌成 chat 事件，
         登记表会把待注入的唤醒拦下。
    """

    MAX_ENTRIES = 200

    def __init__(self, window_seconds: float, clock=None) -> None:
        self.window = max(0.0, float(window_seconds))
        self._clock = clock or time.monotonic
        self._seen = {}

    def remember(self, text) -> None:
        key = str(text or "").strip().casefold()
        if not key:
            return
        now = self._clock()
        self._seen[key] = now + self.window
        if len(self._seen) > self.MAX_ENTRIES:
            self._seen = {
                item: expiry for item, expiry in self._seen.items() if expiry > now
            }

    def is_recent(self, text) -> bool:
        key = str(text or "").strip().casefold()
        if not key:
            return False
        expiry = self._seen.get(key)
        if expiry is None:
            return False
        if self._clock() >= expiry:
            self._seen.pop(key, None)
            return False
        return True


async def send_group(context, platform_id: str, group_id: str, text: str) -> bool:
    """发一条群消息。异常吞掉并返回 False（由调用方记日志）。"""
    if not platform_id or not group_id or not str(text or "").strip():
        return False
    umo = "%s:GroupMessage:%s" % (platform_id, group_id)
    try:
        from astrbot.core.message.message_event_result import MessageChain

        return bool(
            await context.send_message(umo, MessageChain().message(str(text)))
        )
    except Exception:  # noqa: BLE001  调用方负责记日志
        return False
