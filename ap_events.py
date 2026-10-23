# -*- coding: utf-8 -*-
"""事件分类与文本渲染。**纯函数**，不碰 MCP、不发消息。

⚠️ 事件形状来自 MCP 源码 `src/bot/wire-events.ts`（已核实）：

    chat        {username, message}
    whisper     {username, message}
    death       {position}        ← **不带 username**
    login       {username}
    playerJoined/playerLeft  {username}

🔴 `death` 是 `bot.on("death")` 的回调，即**它自己死了**。mineflayer 根本
没有「其他玩家死亡」这个事件 —— 把它当「有人死了」广播出去是错的。
同理 `login` 是它连上服务器的时刻；`playerJoined`/`playerLeft` 才是**别人**。
"""
from __future__ import annotations

SELF = "self"
OWN = "own"
WAKE = "wake"
AMBIENT = "ambient"
IGNORE = "ignore"

_OWN_TYPES = frozenset({"death", "login"})
_AMBIENT_TYPES = frozenset({"chat", "whisper", "playerJoined", "playerLeft"})
_KNOWN_TYPES = _OWN_TYPES | _AMBIENT_TYPES

_OWN_LABELS = {"death": "死亡", "login": "进入了服务器"}


def norm(text) -> str:
    return str(text or "").strip().casefold()


def _data(ev):
    data = ev.get("data") if isinstance(ev, dict) else None
    return data if isinstance(data, dict) else {}


def classify(ev: dict, entity_name: str, wake_words: list) -> str:
    """把一条事件归到 self / own / wake / ambient / ignore 之一。"""
    if not isinstance(ev, dict):
        return IGNORE
    etype = str(ev.get("type") or "")
    if etype not in _KNOWN_TYPES:
        return IGNORE
    # death / login 描述的是**它自己**的状态
    if etype in _OWN_TYPES:
        return OWN
    data = _data(ev)
    if etype in ("playerJoined", "playerLeft"):
        return AMBIENT if str(data.get("username") or "").strip() else IGNORE
    who = norm(data.get("username"))
    if not who:
        return IGNORE
    if who == norm(entity_name):
        return SELF
    if etype == "whisper":
        return WAKE  # 私聊天然是点名，不必再写唤醒词
    message = norm(data.get("message"))
    for word in wake_words or []:
        target = norm(word)
        if target and target in message:
            return WAKE
    return AMBIENT


def own_label(etype: str) -> str:
    return _OWN_LABELS.get(str(etype or ""), "")


def own_detail(ev) -> str:
    """只给 death 生成坐标说明；其余返回空串。"""
    if not isinstance(ev, dict) or str(ev.get("type") or "") != "death":
        return ""
    position = _data(ev).get("position")
    if not isinstance(position, dict):
        return ""
    parts = []
    for axis in ("x", "y", "z"):
        value = position.get(axis)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append("%d" % round(value))
    if len(parts) != 3:
        return ""
    return "于 (%s)" % ", ".join(parts)


def render_line(ev: dict) -> str:
    """环境缓冲用的一行。

    ⚠️ own 类（death / login）**不进环境缓冲** —— 它们走
    `own_event_template` 单独推群。混进来的话下一轮心跳它会读到「自己死了」
    当成刚发生的事.
    """
    if not isinstance(ev, dict):
        return ""
    data = _data(ev)
    etype = str(ev.get("type") or "")
    who = str(data.get("username") or "?")
    if etype == "chat":
        return "<%s> %s" % (who, str(data.get("message") or "").strip())
    if etype == "whisper":
        return "<%s> 对你说: %s" % (who, str(data.get("message") or "").strip())
    if etype == "playerJoined":
        return "%s 进入了服务器" % who
    if etype == "playerLeft":
        return "%s 离开了服务器" % who
    return ""


def render_lines(events, limit: int = 50) -> str:
    lines = []
    for ev in events or []:
        line = render_line(ev)
        if line:
            lines.append(line)
    if not lines:
        return ""
    omitted = 0
    if limit and len(lines) > limit:
        omitted = len(lines) - limit
        lines = lines[-limit:]
    out = "\n".join(lines)
    if omitted:
        out += "\n(更早的 %d 条动态已省略)" % omitted
    return out
