# -*- coding: utf-8 -*-
"""把一条「唤醒」合成成 AstrBot 事件并推进 pipeline。

🔴 **为什么消息链里必须有 `At`**：`commit_event` 进来的事件仍会经过
`WakingCheckStage`，它按「消息链里有没有指向自己 `self_id` 的 At」**重新计算**
`is_wake`（`core/pipeline/waking_check/stage.py`）。直接把 `event.is_wake`
设成 True 会被它覆盖 —— 注入静默不生效。所以判据只能是 At 组件，
`is_wake` / `is_at_or_wake_command` 只是**补充**（早于框架重算时的兜底）。

🔴 **`self_id` 必须与 `sender_id` 不同**：否则被 `ignore_bot_self_message`
判成机器人自己的消息，直接 `stop_event()`。NetherLink 的 `mc_platform.py`
踩过同一个坑。
"""
from __future__ import annotations

from astrbot.api import logger
from astrbot.api.platform import (
    AstrBotMessage,
    Group,
    MessageMember,
    MessageType,
)


class InjectError(Exception):
    """找不到平台、或参数不足以合成事件。"""


def resolve_platform(platform_manager, wanted: str):
    """返回 `(platform_id, adapter)`。

    显式指定但找不到时**不静默回退** —— 回退到「随便挑一个」会让用户以为
    配置生效了，而消息其实投到了别处。
    """
    try:
        instances = list(platform_manager.get_insts())
    except AttributeError as error:
        raise InjectError("取不到平台实例列表: %s" % error) from error
    by_id = {}
    for instance in instances:
        try:
            meta = instance.meta()
        except Exception:  # noqa: BLE001  某个适配器 meta 坏了不该连累全部
            continue
        platform_id = str(getattr(meta, "id", "") or "")
        if platform_id:
            by_id[platform_id] = (instance, meta)
    if wanted:
        if wanted not in by_id:
            raise InjectError(
                "配置的 qq_platform_id=%r 不在运行中的平台里(现有: %s)"
                % (wanted, ", ".join(sorted(by_id)) or "无")
            )
        return wanted, by_id[wanted][0]
    for platform_id, (instance, meta) in by_id.items():
        if str(getattr(meta, "name", "") or "") == "aiocqhttp":
            return platform_id, instance
    raise InjectError("没有找到 aiocqhttp 平台实例(OneBot/NapCat 适配器未启用?)")


def build_message(*, group_id, text, self_id, sender_id, sender_name):
    from astrbot.core.message.components import At, Plain

    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.self_id = str(self_id)
    message.session_id = str(group_id)
    message.message_id = "ai-being-%d" % (
        abs(hash((str(group_id), str(text)))) % 10 ** 12
    )
    message.group = Group(group_id=str(group_id), group_name=str(group_id))
    message.sender = MessageMember(user_id=str(sender_id), nickname=str(sender_name))
    # At 必须在第一位：唤醒检查阶段只认这个
    message.message = [At(qq=str(self_id)), Plain(str(text))]
    message.message_str = str(text)
    message.raw_message = {"type": "ai_being_wake"}
    return message


def commit_wake(adapter, *, group_id, text, self_id, sender_id, sender_name) -> None:
    if not str(group_id or "").strip():
        raise InjectError("没有配置 group_id,唤醒无处投递")
    if not str(text or "").strip():
        raise InjectError("唤醒文本为空")
    message = build_message(
        group_id=group_id,
        text=text,
        self_id=self_id,
        sender_id=sender_id,
        sender_name=sender_name,
    )
    try:
        event = adapter.create_event(message)
    except Exception as error:  # noqa: BLE001
        raise InjectError("平台适配器无法构造事件: %s" % error) from error
    try:
        event.is_wake = True
        event.is_at_or_wake_command = True
    except Exception:  # noqa: BLE001  某些事件对象是只读的，不是致命问题
        pass
    try:
        adapter.commit_event(event)
    except Exception as error:  # noqa: BLE001
        raise InjectError("提交事件失败: %s" % error) from error
    logger.info(
        "AI 实体: 已注入唤醒 -> 群 %s(%d 字)" % (group_id, len(str(text)))
    )
