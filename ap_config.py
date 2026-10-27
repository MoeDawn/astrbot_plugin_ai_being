# -*- coding: utf-8 -*-
"""配置解析。**纯函数、不抛异常、不记日志**。

配错了不报错、只是安静地不生效 —— 与 NetherLink 同口径。所以每一个非法值
都必须落到一个**确定的**默认值。

本插件只认 4 个配置项：`entity_name` / `server_address` / `server_port` /
`watch_tasks`。推群、唤醒、心跳全部交给 NetherLink 与 AstrBot 自己的调度，
这里不再有任何与之重复的键。
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_CRON = "*/30 * * * *"
DEFAULT_NAME = "事件轮询"
DEFAULT_PORT = 25565

# ⚠️ `death` 是**她自己**死了（mineflayer 的 bot.on("death")），不是别人。
#    `health` / `entityHurt` / `entityDead` 频率很高，刻意不放进默认值。
DEFAULT_TYPES = (
    "chat",
    "whisper",
    "playerJoined",
    "playerLeft",
    "death",
    "message",
    "weatherUpdate",
)

# 🔴 静默哨兵：AI 只回这一个词时，插件**丢弃整条回复**，不往群里发。
#    定时轮询每分钟都会触发，无事可做时若不加约束，群里会被「没有新动静」刷屏。
#    这**不是**代码替 AI 判断该不该说话 —— 判断仍在提示词里，这里只是把
#    「我决定不说话」这个结论变成一个可执行的动作。
SILENT_TOKEN = "[静默]"

DEFAULT_PROMPT = """下面是最近的游戏内动态:
{events}

说明:
- 以 <名字> 开头的是游戏内玩家发言
- 带 [服务器名] 的是其他服务器的动态,群里看不到
- 带 ⌜群名⌟ 的是 QQ 群消息转发进游戏的,群里已经发生过了,不要重复回复
- 叫的是你才回应,不确定就保持安静
- 要让游戏里的玩家看到,用 chat 工具说;想同时让群里知道,就在群里也说一句
- 没有值得说的,调用 ai_being_stay_silent 工具,本次回复不会发送
  也可以只回复 [静默] 达到同样效果
"""

TEMPLATE_KEY = "event_poll"

# 它不在游戏里时怎么处理。annotate = 照常触发但说明离线，skip = 跳过不触发。
OFFLINE_ANNOTATE = "annotate"
OFFLINE_SKIP = "skip"


@dataclass(frozen=True)
class WatchTask:
    id: str
    name: str
    cron: str
    types: tuple
    prompt: str
    session: str
    enabled: bool
    quiet_hours: str
    offline_policy: str = "annotate"   # annotate | skip
    source: str = "config"          # config | ai，由 created_by 推出
    created_by: str = ""            # 空 = 手填，ai = AI 建立


@dataclass(frozen=True)
class Config:
    entity_name: str = ""
    server_address: str = ""
    server_port: int = DEFAULT_PORT
    tasks: tuple = field(default_factory=tuple)


def _text(raw, default=""):
    return str(raw if raw is not None else "").strip() or default


def _port(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_PORT
    if not (1 <= value <= 65535):
        return DEFAULT_PORT
    return value


def _as_str_list(raw):
    """list 或逗号分隔串都认；全角逗号一并认；去重保序。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        parts = [str(x).strip() for x in raw]
    else:
        text = str(raw).strip()
        if not text:
            return []
        parts = text.replace("，", ",").split(",")
    out = []
    for part in parts:
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def _offline_policy(raw) -> str:
    """`annotate`（默认）或 `skip`；非法值一律回退 annotate。"""
    value = _text(raw).lower()
    return OFFLINE_SKIP if value == OFFLINE_SKIP else OFFLINE_ANNOTATE


def make_id(name: str, used: set) -> str:
    """任务 id 由规范化名字生成；重名追加 `#2`、`#3`。

    ⚠️ 改名字 = 换了一条任务（水位线会从当前最大 seq 重新起步）。
    """
    base = str(name or "").strip() or DEFAULT_NAME
    candidate = base
    index = 2
    while candidate in used:
        candidate = "%s#%d" % (base, index)
        index += 1
    used.add(candidate)
    return candidate


def build_task(entry, used_names) -> WatchTask | None:
    """把一条 `template_list` 条目变成 `WatchTask`；不是本模板的返回 None。"""
    if not isinstance(entry, dict):
        return None
    key = entry.get("__template_key")
    # 缺 __template_key 也接受：AI 自建任务存盘时不写这个键
    if key is not None and key != TEMPLATE_KEY:
        return None
    name = _text(entry.get("name"), DEFAULT_NAME)
    types = tuple(_as_str_list(entry.get("types"))) or DEFAULT_TYPES
    created_by = _text(entry.get("created_by")).lower()
    return WatchTask(
        id=make_id(name, used_names),
        name=name,
        cron=_text(entry.get("cron"), DEFAULT_CRON),
        types=types,
        prompt=_text(entry.get("prompt"), DEFAULT_PROMPT),
        session=_text(entry.get("session")),
        enabled=bool(entry.get("enabled", True)),
        quiet_hours=_text(entry.get("quiet_hours")),
        offline_policy=_offline_policy(entry.get("offline_policy")),
        source="ai" if created_by == "ai" else "config",
        created_by=created_by,
    )


def load(raw: dict) -> Config:
    raw = raw or {}
    entries = raw.get("watch_tasks")
    if not isinstance(entries, (list, tuple)):
        entries = []
    used = set()
    tasks = []
    for entry in entries:
        task = build_task(entry, used)
        if task is not None:
            tasks.append(task)
    return Config(
        entity_name=_text(raw.get("entity_name")),
        server_address=_text(raw.get("server_address")),
        server_port=_port(raw.get("server_port")),
        tasks=tuple(tasks),
    )


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


def to_entry(task: WatchTask) -> dict:
    """把任务序列化成 `watch_tasks` 的一个条目（写回配置用）。"""
    return {
        "__template_key": TEMPLATE_KEY,
        "name": task.name,
        "cron": task.cron,
        "types": list(task.types),
        "prompt": task.prompt,
        "session": task.session,
        "enabled": bool(task.enabled),
        "quiet_hours": task.quiet_hours,
        "offline_policy": task.offline_policy,
        "created_by": task.created_by,
    }

