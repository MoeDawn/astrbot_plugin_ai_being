# -*- coding: utf-8 -*-
"""把一条任务提示词合成成 AstrBot 群事件并推进 pipeline。

🔴 **消息链里必须有 `At`**：`commit_event` 进来的事件仍会经过
`WakingCheckStage`，它按「消息链里有没有指向自己 `self_id` 的 At」**重新计算**
`is_wake`。直接设 `event.is_wake = True` 会被覆盖 —— 注入静默不生效。

🔴 **`self_id` 必须与 `sender_id` 不同**：否则在
`ignore_bot_self_message=True` 的部署上会被 `stop_event()`。这里把两者做成
不同值，从而**不依赖**那个全局开关。

🔴 **打 `ai_being_task` 标记**：NetherLink 的 `on_group_message` 见标记即
跳过入站转发 —— 否则这段内部提示词会被广播到游戏公屏。
（`sync_bot_msgs=true` 时它不再拦截机器人消息，`self_id==sender_id` 那招失效。）
"""
from __future__ import annotations

from astrbot.api import logger
from astrbot.api.platform import (
    AstrBotMessage,
    Group,
    MessageMember,
    MessageType,
)

SELF_ID = "ai_being_task_self"
SENDER_ID = "ai_being_task_sender"
SENDER_NAME = "事件轮询"
MARKER_KEY = "ai_being_task"


class InjectError(Exception):
    """UMO 非法、文本为空、平台构造事件失败。"""


def parse_umo(umo: str) -> tuple[str, str, str]:
    """`平台id:消息类型:会话id` → `(platform_id, message_type, session_id)`。"""
    text = str(umo or "").strip()
    parts = text.split(":", 2)
    if len(parts) != 3:
        raise InjectError("会话格式应为 平台id:消息类型:会话id，实际是 %r" % umo)
    platform_id, message_type, session_id = (p.strip() for p in parts)
    if not platform_id or not session_id:
        raise InjectError("会话缺少平台标识或会话 id: %r" % umo)
    return platform_id, message_type, session_id


def require_group_umo(umo: str) -> tuple[str, str]:
    """校验这是**群消息**的 UMO，返回 `(platform_id, session_id)`。

    🔴 必须校验：本插件合成的是群事件。投到私聊 UMO 会把群消息发到好友号；
    投到游戏平台（`netherlink_mc`）则会「成功」但 `send()` 是静默 no-op ——
    水位线照样推进，回复凭空消失。
    """
    platform_id, message_type, session_id = parse_umo(umo)
    if message_type != "GroupMessage":
        raise InjectError(
            "只能投递到群会话（形如 aiocqhttp:GroupMessage:群号），实际是 %r" % umo
        )
    return platform_id, session_id


def build_message(*, session_id, text, self_id=SELF_ID,
                  sender_id=SENDER_ID, sender_name=SENDER_NAME):
    from astrbot.core.message.components import At, Plain

    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.self_id = str(self_id)
    message.session_id = str(session_id)
    message.message_id = "ai-being-task-%d" % (
        abs(hash((str(session_id), str(text)))) % 10 ** 12
    )
    message.group = Group(group_id=str(session_id), group_name=str(session_id))
    message.sender = MessageMember(user_id=str(sender_id), nickname=str(sender_name))
    # At 必须在第一位：唤醒检查阶段只认这个
    message.message = [At(qq=str(self_id)), Plain(str(text))]
    message.message_str = str(text)
    message.raw_message = {"type": MARKER_KEY}
    return message


def commit_task(platform, umo: str, text: str) -> None:
    if not str(text or "").strip():
        raise InjectError("任务提示词为空")
    _platform_id, session_id = require_group_umo(umo)
    message = build_message(session_id=session_id, text=text)
    try:
        event = platform.create_event(message)
    except Exception as error:  # noqa: BLE001
        raise InjectError("平台适配器无法构造事件: %s" % error) from error
    try:
        event.is_wake = True
        event.is_at_or_wake_command = True
    except Exception:  # noqa: BLE001  某些事件对象是只读的，不是致命问题
        pass
    try:
        event.set_extra(MARKER_KEY, True)
    except Exception as error:  # noqa: BLE001
        raise InjectError("无法在事件上打任务标记: %s" % error) from error
    try:
        platform.commit_event(event)
    except Exception as error:  # noqa: BLE001
        raise InjectError("提交事件失败: %s" % error) from error
    logger.info("AI 实体: 已注入任务 -> %s（%d 字）" % (umo, len(str(text))))
