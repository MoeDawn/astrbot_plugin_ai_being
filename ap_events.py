# -*- coding: utf-8 -*-
"""事件渲染。**纯函数**，不碰 MCP、不发消息。

事件形状来自 `awesome-mineflayer-mcp` 的 `src/bot/wire-events.ts`（已核实）：

    chat / whisper            {username, message}
    message                   {text, position}
    actionBar / title         {text} / {text, type}
    login                     {username}
    spawn / death / forcedMove  {position}
    respawn                   {dimension}
    health                    {health, food, saturation}
    breath                    {oxygen}
    experience                {level, points}
    heldItemChanged           {name}
    rain / weatherUpdate      {isRaining[, thunderState]}
    playerJoined / Left       {username}
    playerCollect             {collector, collected, byBot}
    entitySpawn / itemDrop    {id, type, name, username, position}
    entityGone                {id}
    entityHurt / entityDead   {id, name}
    entityEffect              {id, effect}
    entity*（一组反应类）      {id, name}
    entityAttach / Detach     {id, vehicle}
    diggingCompleted/Aborted  {position, name}
    blockPlaced               {position, name}
    windowOpen                {id, type}   windowClose  null
    mount {vehicle}  dismount / sleep / wake / usedFirework / bossBar* / spawnReset  null
    soundEffectHeard          {soundName, position}
    autoeat_*                 null / {message}

🔴 `death` 是**它自己死了**。mineflayer 没有「别的玩家死了」这个事件 ——
别人的死亡走 `message`。

🔴 未知类型**不能只给类型名**：payload 里带着实体、坐标、数值，丢掉等于让
AI 看不见现场。兜底把 JSON 附上。
"""
from __future__ import annotations

import json

EMPTY_TEXT = "（这段时间没有什么特别的）"
DROPPED_NOTE = "（中间有一段动态因缓冲溢出丢失）"


def _data(ev):
    data = ev.get("data") if isinstance(ev, dict) else None
    return data if isinstance(data, dict) else {}


def _text(value):
    return str(value if value is not None else "").strip()


# 只有这几类事件带「谁」的语义。login / death 是**它自己的状态**，
# 即便 payload 里带 username 也必须保留 —— 否则「你进入了服务器」永远看不到。
_SPEAKER_TYPES = frozenset({"chat", "whisper", "playerJoined", "playerLeft"})


def is_self(ev: dict, entity_name: str) -> bool:
    """这条事件是不是**它自己**发的。

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


def _number(value):
    return round(value) if isinstance(value, (int, float)) \
        and not isinstance(value, bool) else None


def _entity(data):
    """实体类事件的公共描述：名字 + id + 坐标，缺哪个省哪个。"""
    parts = []
    name = _text(data.get("name"))
    if name:
        parts.append(name)
    eid = data.get("id")
    if isinstance(eid, int) and not isinstance(eid, bool):
        parts.append("id %d" % eid)
    coords = _coords(data.get("position"))
    if coords:
        parts.append(coords)
    return " ".join(parts)


def _compact(data) -> str:
    """把 payload 压成一行 JSON（兜底用，不丢字段）。"""
    if not isinstance(data, dict) or not data:
        return ""
    try:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    return text[:200]


_ENTITY_REACTIONS = {
    "entityEquip": "换了装备",
    "entitySwingArm": "挥了下手臂",
    "entitySleep": "睡着了",
    "entityWake": "醒了",
    "entityCrouch": "蹲下",
    "entityUncrouch": "站起",
    "entityHandSwap": "换手",
    "entityElytraFlew": "用鞘翅飞了起来",
    "entityTaming": "正在被驯服",
    "entityTamed": "被驯服了",
}


def render_event(ev: dict) -> str:
    """把一条事件渲染成一行。无法渲染时返回空串（调用方跳过）。"""
    if not isinstance(ev, dict):
        return ""
    etype = _text(ev.get("type"))
    data = _data(ev)
    who = _text(data.get("username")) or "?"

    # ---------------------------------------------------------------- 聊天
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
    if etype == "message":
        text = _text(data.get("text"))
        if not text:
            return ""
        where = _text(data.get("position"))
        return "系统消息(%s): %s" % (where, text) if where else "系统消息: %s" % text

    # ---------------------------------------------------------------- 自身状态
    if etype == "death":
        coords = _coords(data.get("position"))
        return "你自己死了,位置 %s" % coords if coords else "你自己死了"
    if etype == "login":
        return "你进入了服务器"
    if etype == "spawn":
        coords = _coords(data.get("position"))
        return "你出生在 %s" % coords if coords else "你出生了"
    if etype == "respawn":
        dim = _text(data.get("dimension"))
        return "你重生在 %s" % dim if dim else "你重生了"
    if etype == "spawnReset":
        return "重生点已重置"
    if etype == "forcedMove":
        coords = _coords(data.get("position"))
        return "你被强制移动到 %s" % coords if coords else "你被强制移动了"

    health = _number(data.get("health"))
    if etype == "health":
        food = _number(data.get("food"))
        if health is None or food is None:
            return ""
        line = "血量 %d/20，饥饿 %d/20" % (health, food)
        saturation = _number(data.get("saturation"))
        if saturation is not None:
            line += "，饱和度 %d" % saturation
        return line
    if etype == "breath":
        oxygen = _number(data.get("oxygen"))
        return "氧气 %d" % oxygen if oxygen is not None else "氧气变化"

    if etype in ("rain", "weatherUpdate"):
        raining = bool(data.get("isRaining"))
        line = "天气变化: %s" % ("开始下雨" if raining else "天晴了")
        thunder = data.get("thunderState")
        if raining and isinstance(thunder, int) and thunder > 0:
            line += ",有雷暴"
        return line

    if etype == "experience":
        level = _number(data.get("level"))
        if level is None:
            return ""
        line = "你升到了 %d 级" % level
        points = _number(data.get("points"))
        if points is not None:
            line += "(经验值 %d)" % points
        return line

    if etype == "heldItemChanged":
        name = _text(data.get("name"))
        return "手持物变成 %s" % name if name else "手持物变了"
    if etype == "error":
        message = _text(data.get("message"))
        return "客户端错误: %s" % message if message else "客户端错误"
    if etype == "actionBar":
        text = _text(data.get("text"))
        return "动作栏: %s" % text if text else ""
    if etype == "title":
        text = _text(data.get("text"))
        if not text:
            return ""
        kind = _text(data.get("type"))
        return "标题(%s): %s" % (kind, text) if kind else "标题: %s" % text
    if etype == "resourcePack":
        url = _text(data.get("url"))
        return "收到资源包: %s" % url if url else "收到资源包"

    # ---------------------------------------------------------------- 自身行动
    if etype == "goal_reached":
        return "你到达了目标"
    if etype == "goal_updated":
        return "行动目标更新了"
    if etype == "path_reset":
        reason = _text(data.get("reason"))
        return "寻路被重置(%s)" % reason if reason else "寻路被重置"
    if etype == "pvp_stopped":
        return "战斗结束"
    if etype == "sleep":
        return "你上床了"
    if etype == "wake":
        return "你起床了"
    if etype == "mount":
        vehicle = data.get("vehicle")
        return "你骑上了实体 %s" % vehicle if vehicle is not None else "你骑上了载具"
    if etype == "dismount":
        return "你离开了载具"
    if etype == "usedFirework":
        return "你用了烟花"
    if etype == "windowOpen":
        kind = _text(data.get("type"))
        return "你打开了容器(%s)" % kind if kind else "你打开了容器"
    if etype == "windowClose":
        return "你关闭了容器"
    if etype == "bossBarCreated":
        return "出现了 Boss 血条"
    if etype == "bossBarDeleted":
        return "Boss 血条消失了"

    # ---------------------------------------------------------------- 挖掘与放置
    if etype in ("diggingCompleted", "diggingAborted"):
        verb = "挖掉了" if etype == "diggingCompleted" else "挖掘中断"
        bits = [b for b in (_text(data.get("name")),
                            _coords(data.get("position"))) if b]
        return "%s %s" % (verb, " ".join(bits)) if bits else verb
    if etype == "blockPlaced":
        bits = [b for b in (_text(data.get("name")),
                            _coords(data.get("position"))) if b]
        return "你放下了 %s" % " ".join(bits) if bits else "你放下了方块"

    # ---------------------------------------------------------------- 其他实体
    if etype == "entitySpawn":
        desc = _entity(data)
        return "附近出现 %s" % desc if desc else "附近出现了实体"
    if etype == "itemDrop":
        desc = _entity(data)
        return "附近掉落物 %s" % desc if desc else "附近有掉落物"
    if etype == "entityGone":
        eid = data.get("id")
        return "实体 %s 消失了" % eid if eid is not None else "附近实体消失了"
    if etype == "entityHurt":
        desc = _entity(data)
        return "%s 受伤了" % desc if desc else ""
    if etype == "entityDead":
        desc = _entity(data)
        return "%s 死亡了" % desc if desc else ""
    if etype == "entityEffect":
        eid = data.get("id")
        effect = _compact(data.get("effect"))
        if eid is None or not effect:
            return ""
        return "实体 %s 获得效果 %s" % (eid, effect)
    if etype in _ENTITY_REACTIONS:
        desc = _entity(data)
        return "%s %s" % (desc, _ENTITY_REACTIONS[etype]) if desc else ""
    if etype in ("entityAttach", "entityDetach"):
        eid = data.get("id")
        if eid is None:
            return ""
        verb = "上了载具" if etype == "entityAttach" else "离开了载具"
        vehicle = data.get("vehicle")
        return "实体 %s %s %s" % (eid, verb, vehicle) if vehicle is not None \
            else "实体 %s %s" % (eid, verb)
    if etype == "playerCollect":
        if data.get("byBot"):
            return "你捡起了实体 %s" % data.get("collected")
        return "实体 %s 捡起了实体 %s" % (data.get("collector"),
                                          data.get("collected"))
    if etype == "soundEffectHeard":
        bits = [b for b in (_text(data.get("soundName")),
                            _coords(data.get("position"))) if b]
        return "附近的声音: %s" % " ".join(bits) if bits else "附近有声音"

    # ---------------------------------------------------------------- 自动进食
    if etype == "autoeat_start":
        return "开始自动进食"
    if etype == "autoeat_finish":
        return "自动进食完成"
    if etype == "autoeat_fail":
        message = _text(data.get("message"))
        return "自动进食失败: %s" % message if message else "自动进食失败"

    # ---------------------------------------------------------------- 兜底
    if etype:
        blob = _compact(data)
        return "事件 %s %s" % (etype, blob) if blob else "事件 %s" % etype
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
