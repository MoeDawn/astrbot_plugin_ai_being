# -*- coding: utf-8 -*-
"""配置读取与回退。**纯函数、不抛异常、不记日志**。

配错了不报错、只是安静地不生效 —— 与 NetherLink 同口径。所以每一个非法值
都必须在这里落到一个**确定的**默认值，且默认值集中在 `DEFAULTS`，
`_conf_schema.json` 与测试都从同一份口径出发。

⚠️ 两个例外：`heartbeat_cron` 与 `quiet_hours` **允许留空**（= 关闭），
所以它们不走「空串回退默认值」那条通用规则。
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_HEARTBEAT_PROMPT = """现在没有人叫你。先了解现场(血量、饥饿、坐标、背包、附近实体),再做你想做的事。
优先保命:饥饿值低就吃东西,血量低先回血,装备没穿齐先穿上,工具不够就想办法。
可以散步、采集、钓鱼、整理箱子、烧矿、跟人、看风景;做完就停。
只有值得说的才在群里说一句,其余保持沉默。
不要修改世界,不要使用管理指令。
最近游戏内动态:
{events}"""

DEFAULT_WAKE_TEMPLATE = """游戏里有动静,可能需要你回应:
{events}
先判断是不是在叫你。是的话用 chat 工具在游戏里回一句,必要时也在这里说一声;
不是的话保持安静,什么都不用做。"""

DEFAULTS = {
    "enable": True,
    "mcp_server_name": "",
    "entity_name": "",
    "group_id": "",
    "qq_platform_id": "",
    "poll_interval": 60,
    "poll_types": [
        "chat",
        "death",
        "login",
        "playerJoined",
        "playerLeft",
        "whisper",
    ],
    "wake_words": [],
    "wake_cooldown": 30,
    "wake_template": DEFAULT_WAKE_TEMPLATE,
    "heartbeat_cron": "*/30 * * * *",
    "heartbeat_prompt": DEFAULT_HEARTBEAT_PROMPT,
    "quiet_hours": "0-7",
    "own_event_template": "{entity} {event}",
    "self_wake_guard": 10,
    "mcp_timeout": 20,
}

# 轮询下限：再快下去只是把 MCP 打满，拿不到更快的事件。
MIN_POLL_INTERVAL = 5


@dataclass
class Config:
    enable: bool = True
    mcp_server_name: str = ""
    entity_name: str = ""
    group_id: str = ""
    qq_platform_id: str = ""
    poll_interval: int = 60
    poll_types: list = field(default_factory=list)
    wake_words: list = field(default_factory=list)
    wake_cooldown: int = 30
    wake_template: str = DEFAULT_WAKE_TEMPLATE
    heartbeat_cron: str = ""
    heartbeat_prompt: str = DEFAULT_HEARTBEAT_PROMPT
    quiet_hours: str = ""
    own_event_template: str = "{entity} {event}"
    self_wake_guard: int = 10
    mcp_timeout: int = 20


def _as_int(raw, key, minimum=None):
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return int(DEFAULTS[key])
    if minimum is not None and value < minimum:
        return minimum
    return value


def _as_str(raw, key):
    """空串回退默认值 —— 想关掉某段文本应删正文，而不是清空配置项。"""
    text = str(raw if raw is not None else "").strip()
    return text or str(DEFAULTS[key])


def _as_str_list(raw, key):
    """list 或逗号分隔串都认（旧部署拿到的是字符串）。

    全角逗号一并认：中文输入法下写「妹妹，爱莉喵」是常态，
    不认的话整串会变成一个词，静默失效。
    """
    if raw is None:
        return list(DEFAULTS[key])
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    text = str(raw).strip()
    if not text:
        return []
    out = []
    for part in text.replace("，", ",").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def load(raw: dict) -> Config:
    raw = raw or {}
    cfg = Config()
    cfg.enable = bool(raw.get("enable", DEFAULTS["enable"]))
    cfg.mcp_server_name = str(raw.get("mcp_server_name") or "").strip()
    # ⚠️ 留空 = 未配置，**不能**回退默认值 —— 这里没有「合理的默认名字」，
    # 猜一个会让插件把别人的发言当成它自己说的。
    cfg.entity_name = str(raw.get("entity_name") or "").strip()
    cfg.group_id = str(raw.get("group_id") or "").strip()
    cfg.qq_platform_id = str(raw.get("qq_platform_id") or "").strip()
    cfg.poll_interval = _as_int(
        raw.get("poll_interval"), "poll_interval", MIN_POLL_INTERVAL
    )
    # 空的 poll_types 会让轮询什么都拿不到 —— 静默失效，所以回退默认值。
    cfg.poll_types = _as_str_list(
        raw.get("poll_types"), "poll_types"
    ) or list(DEFAULTS["poll_types"])
    cfg.wake_words = _as_str_list(raw.get("wake_words"), "wake_words")
    cfg.wake_cooldown = _as_int(raw.get("wake_cooldown"), "wake_cooldown", 0)
    cfg.wake_template = _as_str(raw.get("wake_template"), "wake_template")
    # 允许留空 = 关闭心跳；只有「根本没配」才用默认值。
    if "heartbeat_cron" in raw:
        cfg.heartbeat_cron = str(raw.get("heartbeat_cron") or "").strip()
    else:
        cfg.heartbeat_cron = str(DEFAULTS["heartbeat_cron"])
    cfg.heartbeat_prompt = _as_str(
        raw.get("heartbeat_prompt"), "heartbeat_prompt"
    )
    # 允许留空 = 不静默。
    if "quiet_hours" in raw:
        cfg.quiet_hours = str(raw.get("quiet_hours") or "").strip()
    else:
        cfg.quiet_hours = str(DEFAULTS["quiet_hours"])
    cfg.own_event_template = _as_str(
        raw.get("own_event_template"), "own_event_template"
    )
    cfg.self_wake_guard = _as_int(
        raw.get("self_wake_guard"), "self_wake_guard", 0
    )
    cfg.mcp_timeout = _as_int(raw.get("mcp_timeout"), "mcp_timeout", 1)
    return cfg


def quiet_now(spec: str, hour: int) -> bool:
    """`"0-7"` 含头不含尾；支持 `"22-2"` 跨午夜。

    非法一律返回 False（**fail-open**）：静默是「少打扰」，判错方向的代价
    是「本该安静时醒了一次」；反过来写会变成「心跳静默消失」，那是本项目的
    典型静默失败。
    """
    spec = (spec or "").strip()
    if not spec:
        return False
    try:
        lo_s, hi_s = spec.split("-", 1)
        lo, hi = int(lo_s), int(hi_s)
    except (ValueError, AttributeError, TypeError):
        return False
    if not (0 <= lo <= 23 and 0 <= hi <= 23):
        return False
    if lo == hi:
        return False  # `8-8` 是空区间，不是一整天
    if lo < hi:
        return lo <= hour < hi
    return hour >= lo or hour < hi
