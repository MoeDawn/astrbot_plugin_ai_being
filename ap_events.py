# -*- coding: utf-8 -*-
"""事件渲染。**纯函数**，不碰 MCP、不发消息。

事件形状来自 `awesome-mineflayer-mcp` 的 `src/bot/wire-events.ts`（已核实）：

    chat        {username, message}
    whisper     {username, message}
    death       {position}        ← **不带 username**，是 `bot.on("death")`
    login       {username}
    playerJoined / playerLeft  {username}
    message     {text, position}  ← 系统消息，含跨服与 QQ 群转发
    health      {health, food, saturation}
    entityHurt / entityDead  {id, name}
    weatherUpdate  {isRaining, thunderState}
    experience  {level, points}

🔴 `death` 是**它自己死了**。mineflayer 没有「别的玩家死了」这个事件 ——
别人的死亡走 `message`。
"""
from __future__ import annotations

EMPTY_TEXT = "（这段时间没有什么特别的）"
DROPPED_NOTE = "（中间有一段动态因缓冲溢出丢失）"


def _data(ev):
    data = ev.get("data") if isinstance(ev, dict) else None
    return data if isinstance(data, dict) else {}


def _text(value):
    return str(value if value is not None else "").strip()


# 只有这几类事件带「谁」的语义。login / death 是**她自己的状态**，
# 即便 payload 里带 username 也必须保留 —— 否则「你进入了服务器」永远看不到。
_SPEAKER_TYPES = frozenset({"chat", "whisper", "playerJoined", "playerLeft"})


def is_self(ev: dict, entity_name: str) -> bool:
    """这条事件是不是**她自己**发的。

    只看 `_SPEAKER_TYPES` 里的类型；`login` / `death` 是自身状态事件，
    永远不排除。按用户名精确匹配，不区分大小写。
    """
    if not isinstance(ev, dict) or not _text(entity_name):
        return False
    if _text(ev.get("type")) not in _SPEAKER_TYPES:
        return False
    who = _text(_data(ev).get("username")).casefold()
    return bool(who) and who == _text(entity_name).casefold()


def _coords(position):
    if not isinstance(position, dict):
        return ""
    parts = []
    for axis in ("x", "y", "z"):
        value = position.get(axis)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append("%d" % round(value))
    return "(%s)" % ", ".join(parts) if len(parts) == 3 else ""


def render_event(ev: dict) -> str:
    """把一条事件渲染成一行。无法渲染时返回空串（调用方跳过）。"""
    if not isinstance(ev, dict):
        return ""
    etype = _text(ev.get("type"))
    data = _data(ev)
    who = _text(data.get("username")) or "?"

    if etype == "chat":
        message = _text(data.get("message"))
        return "<%s> %s" % (who, message) if message else ""
    if etype == "whisper":
        message = _text(data.get("message"))
        return "<%s> 私聊你: %s" % (who, message) if message else ""
    if etype == "playerJoined":
        return "%s 进入了服务器" % who if who != "?" else ""
    if etype == "playerLeft":
        return "%s 离开了服务器" % who if who != "?" else ""
    if etype == "death":
        coords = _coords(data.get("position"))
        return "你自己死了，位置 %s" % coords if coords else "你自己死了"
    if etype == "login":
        return "你进入了服务器"
    if etype == "message":
        text = _text(data.get("text"))
        return "系统消息: %s" % text if text else ""
    if etype == "health":
        health = data.get("health")
        food = data.get("food")
        if isinstance(health, (int, float)) and isinstance(food, (int, float)):
            return "血量 %d/20，饥饿 %d/20" % (round(health), round(food))
        return ""
    if etype == "weatherUpdate":
        return "天气变化: %s" % (
            "开始下雨" if data.get("isRaining") else "天晴了"
        )
    if etype == "experience":
        level = data.get("level")
        if isinstance(level, (int, float)):
            return "你升到了 %d 级" % round(level)
        return ""
    if etype == "entityHurt":
        name = _text(data.get("name"))
        return "%s 受伤了" % name if name else ""
    if etype == "entityDead":
        name = _text(data.get("name"))
        return "%s 死亡了" % name if name else ""
    if etype == "goal_reached":
        return "你到达了目标"
    if etype == "path_reset":
        reason = _text(data.get("reason"))
        return "寻路被重置(%s)" % reason if reason else "寻路被重置"
    if etype == "actionBar":
        text = _text(data.get("text"))
        return "动作栏: %s" % text if text else ""
    if etype == "title":
        text = _text(data.get("text"))
        return "标题: %s" % text if text else ""
    if etype:
        return "事件 %s" % etype
    return ""


def render_events(events, entity_name="", limit=60, dropped=False) -> str:
    """渲染成多行文本。**新的在后**，超出 `limit` 时丢最旧的。"""
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 0
    lines = []
    for ev in events or []:
        if is_self(ev, entity_name):
            continue
        line = render_event(ev)
        if line:
            lines.append(line)
    omitted = 0
    if limit and len(lines) > limit:
        omitted = len(lines) - limit
        lines = lines[-limit:]
    if not lines:
        out = EMPTY_TEXT
    else:
        out = "\n".join(lines)
        if omitted:
            out += "\n(更早的 %d 条动态已省略)" % omitted
    if dropped:
        out += "\n" + DROPPED_NOTE
    return out
