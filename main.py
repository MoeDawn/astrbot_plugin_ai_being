# -*- coding: utf-8 -*-
"""AI 实体插件入口。

职责：把「MCP 事件轮询 / 定时心跳」两个入口接到同一个 QQ 群会话。
**所有判断交给提示词** —— 插件只决定「谁说的、要不要唤醒、发到哪」，
不判断「该不该回应、说什么、做不做事」。

三条不变量（改动前务必读）：
  1. 轮询**失败不写游标**（重复消费会让同一句话反复推群、反复唤醒）
  2. `self` 类事件**不进环境缓冲**（否则下一轮心跳它读到自己说的话，
     会当成别人说的）
  3. `own` 类事件（death/login）走 own_event_template **立即推群**，
     不进环境缓冲
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

try:
    from . import (
        ap_config,
        ap_cron,
        ap_cursor,
        ap_events,
        ap_inject,
        ap_mcp,
        ap_sync,
    )
except ImportError:  # 插件以顶层模块方式加载时
    import ap_config
    import ap_cron
    import ap_cursor
    import ap_events
    import ap_inject
    import ap_mcp
    import ap_sync

PLUGIN_NAME = "astrbot_plugin_ai_being"

# ⚠️ self_id 与 sender_id 必须**不同**，否则被 ignore_bot_self_message
#    判成机器人自己的消息直接 stop_event()。
SELF_ID = "ai_being"
SENDER_ID = "ai_being_context"
SENDER_NAME = "mineflayer"

AMBIENT_CAP = 50
POLL_LIMIT = 200
REPORT_AFTER_FAILURES = 3
HEARTBEAT_TICK_SECONDS = 60


class AIPlayerPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config_obj = ap_config.load(config or {})
        # ⚠️ get_astrbot_plugin_data_path() 返回的是 **str**，必须先包成 Path
        data_dir = Path(get_astrbot_plugin_data_path()) / "ai_being"
        self._cursor = ap_cursor.Cursor(data_dir / "cursor.json")
        self._ambient = []
        self._last_wake_at = 0.0
        self._last_heartbeat_minute = None
        self._fail_streak = 0
        self._reported_down = False
        self._dedup = ap_sync.Dedup(self.config_obj.self_wake_guard)
        self._client = None
        self._client_name = ""
        self._tasks = []
        self._sync_tasks = set()
        self._stopping = asyncio.Event()

        self._log_configured()
        self._start_runtime_if_enabled()

    # ------------------------------------------------------------ 配置

    def _log_configured(self) -> None:
        config = self.config_obj
        logger.info(
            "AI 实体: 已配置的 entity_name=%s group_id=%s mcp_server_name=%s "
            "poll_interval=%ss heartbeat_cron=%r quiet_hours=%r"
            % (
                config.entity_name,
                config.group_id or "(空)",
                config.mcp_server_name or "(自动)",
                config.poll_interval,
                config.heartbeat_cron,
                config.quiet_hours,
            )
        )
        if not config.enable:
            logger.info("AI 实体: enable=false,本插件不动作")
            return
        if not config.entity_name:
            logger.error(
                "AI 实体: 没有配置 entity_name,无法识别它自己的发言"
                "(自唤醒防护与自有事件播报都会失效)"
            )
        if not config.group_id:
            logger.warning("AI 实体: 没有配置 group_id,唤醒与同步都不会发生")
        if config.heartbeat_cron and not ap_cron.valid(config.heartbeat_cron):
            logger.warning(
                "AI 实体: heartbeat_cron=%r 不是合法 cron,心跳已关闭"
                % config.heartbeat_cron
            )
        if not config.wake_words:
            logger.warning("AI 实体: wake_words 为空,游戏内点名永远不会唤醒")

    def _start_runtime_if_enabled(self) -> None:
        if not self.config_obj.enable:
            return
        try:
            self._client_name, self._client = ap_mcp.resolve_client(
                self.context.provider_manager.llm_tools,
                self.config_obj.mcp_server_name,
            )
        except ap_mcp.McpError as error:
            logger.error("AI 实体: 找不到可用的 MCP 服务,轮询不会启动(%s)" % error)
            return
        except AttributeError as error:
            logger.error("AI 实体: AstrBot 内部接口与预期不符,轮询不会启动(%s)" % error)
            return
        logger.info("AI 实体: 使用 MCP 服务 %r" % self._client_name)
        self._ensure_tasks()

    def _ensure_tasks(self) -> None:
        """按当前配置创建后台任务。**可重复调用**，已在跑就不动。"""
        if self._client is None:
            return
        if any(not task.done() for task in self._tasks):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("AI 实体: 没有事件循环,后台任务未启动")
            return
        self._tasks = []
        self._stopping = asyncio.Event()
        self._tasks.append(loop.create_task(self._poll_loop()))
        if self.config_obj.heartbeat_cron and ap_cron.valid(
            self.config_obj.heartbeat_cron
        ):
            self._tasks.append(loop.create_task(self._heartbeat_loop()))

    def _start_runtime(self) -> None:
        """幂等启动入口（热重载用）。"""
        if self._client is None:
            self._start_runtime_if_enabled()
        else:
            self._ensure_tasks()

    async def _stop_runtime(self) -> None:
        self._stopping.set()
        for task in list(self._sync_tasks):
            task.cancel()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as error:  # noqa: BLE001
                logger.warning("AI 实体: 停止任务时出错(忽略): %s" % error)
        self._tasks = []
        self._sync_tasks.clear()

    async def terminate(self) -> None:
        await self._stop_runtime()
        logger.info("AI 实体: 已停止")

    # ------------------------------------------------------------ 轮询

    async def _poll_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                logger.error("AI 实体: 轮询循环异常(继续): %s" % error)
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.config_obj.poll_interval
                )
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self) -> None:
        if self._client is None:
            return
        since = self._cursor.load()
        try:
            page = await ap_mcp.get_events(
                self._client,
                since,
                self.config_obj.poll_types,
                POLL_LIMIT,
                self.config_obj.mcp_timeout,
            )
        except ap_mcp.McpError as error:
            self._note_failure(str(error))
            return
        self._note_success()
        # 成功就必须写：空返回、dropped 也要写，否则下一轮会重复消费
        self._cursor.save(page["nextSince"])
        if page.get("dropped"):
            logger.warning("AI 实体: 事件缓冲溢出,已跳过一段动态(游标不重置)")
        self._ingest(page.get("events") or [])

    def _note_failure(self, detail: str) -> None:
        self._fail_streak += 1
        logger.warning(
            "AI 实体: 读取事件失败(第 %d 次): %s" % (self._fail_streak, detail)
        )
        if self._fail_streak >= REPORT_AFTER_FAILURES:
            self._report_down(
                "连续 %d 次读不到游戏事件" % self._fail_streak
            )

    def _note_success(self) -> None:
        if self._fail_streak >= REPORT_AFTER_FAILURES:
            logger.info("AI 实体: MCP 事件读取已恢复")
        self._fail_streak = 0
        self._reported_down = False

    def _report_down(self, message: str) -> None:
        """连续失败才在群里说一句；**恢复不播报**（避免噪声）。"""
        if self._reported_down:
            return
        self._reported_down = True
        logger.error("AI 实体: %s" % message)
        self._sync_line("(%s)" % message)

    # ------------------------------------------------------------ 事件

    def _ingest(self, events) -> None:
        config = self.config_obj
        wakens = []
        for event in events:
            kind = ap_events.classify(
                event, config.entity_name, config.wake_words
            )
            if kind == ap_events.SELF:
                # ① 登记到去重表：它自己说的话若被某种方式回灌成唤醒，
                #    这张表会拦下，避免自言自语。
                # ② **不进环境缓冲**：下一轮心跳读到自己的话会当成别人说的。
                self._dedup.remember(
                    str((event.get("data") or {}).get("message") or "")
                )
                continue
            if kind == ap_events.OWN:
                self._handle_own(event)
                continue
            if kind == ap_events.WAKE:
                message = str((event.get("data") or {}).get("message") or "")
                if self._dedup.is_recent(message):
                    continue  # 自唤醒：它刚用 chat 工具说过这句
                wakens.append(event)
                continue
            if kind == ap_events.AMBIENT:
                self._collect_ambient(event)
        if wakens:
            self._handle_wake_events(wakens)

    def _handle_own(self, event) -> None:
        """它死了 / 它连上了服务器 —— 立即推群，不进环境缓冲。"""
        config = self.config_obj
        label = ap_events.own_label(str((event or {}).get("type") or ""))
        if not label:
            return
        line = ap_sync.render(
            config.own_event_template,
            entity=config.entity_name,
            event=label,
            detail=ap_events.own_detail(event),
        )
        self._sync_line(line)

    def _collect_ambient(self, event) -> None:
        line = ap_events.render_line(event)
        if not line:
            return
        self._ambient.append(line)
        if len(self._ambient) > AMBIENT_CAP:
            del self._ambient[: len(self._ambient) - AMBIENT_CAP]

    def _ambient_text(self) -> str:
        return "\n".join(self._ambient[-AMBIENT_CAP:]) or "(这段时间没有什么特别的)"

    def _handle_wake_events(self, wakens) -> None:
        config = self.config_obj
        if not config.group_id:
            return
        now = time.monotonic()
        if config.wake_cooldown and (now - self._last_wake_at) < config.wake_cooldown:
            logger.info("AI 实体: 唤醒冷却中,丢弃 %d 条点名" % len(wakens))
            return
        # 冷却时间在**注入成功之后**才更新：投递失败不该吃掉下一次机会
        text = ap_sync.render(
            config.wake_template,
            entity=config.entity_name,
            events=ap_events.render_lines(wakens, limit=20),
        )
        if self._inject_wake(text):
            self._last_wake_at = now

    # ------------------------------------------------------------ 心跳

    async def _heartbeat_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self._heartbeat_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                logger.error("AI 实体: 心跳异常(继续): %s" % error)
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=HEARTBEAT_TICK_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    def _heartbeat_once(self, now: datetime | None = None, hour: int | None = None) -> None:
        config = self.config_obj
        now = now or datetime.now()
        if not config.heartbeat_cron or not ap_cron.matches(
            config.heartbeat_cron, now
        ):
            return
        minute_key = now.strftime("%Y%m%d%H%M")
        if self._last_heartbeat_minute == minute_key:
            return  # 同一分钟只触发一次（tick 可能在一分钟内跑两遍）
        current_hour = now.hour if hour is None else hour
        if ap_config.quiet_now(config.quiet_hours, current_hour):
            logger.info("AI 实体: 静默时段,本次心跳跳过")
            return
        self._last_heartbeat_minute = minute_key
        text = ap_sync.render(
            config.heartbeat_prompt,
            entity=config.entity_name,
            events=self._ambient_text(),
            state="未知",
        )
        self._inject_heartbeat(text)

    # ------------------------------------------------------------ 注入

    def _inject(self, text: str) -> bool:
        config = self.config_obj
        if not config.group_id:
            return False
        try:
            _platform_id, adapter = ap_inject.resolve_platform(
                self.context.platform_manager, config.qq_platform_id
            )
        except ap_inject.InjectError as error:
            logger.error("AI 实体: 无法唤醒(%s)" % error)
            return False
        try:
            ap_inject.commit_wake(
                adapter,
                group_id=config.group_id,
                text=text,
                self_id=SELF_ID,
                sender_id=SENDER_ID,
                sender_name=SENDER_NAME,
            )
        except ap_inject.InjectError as error:
            logger.error("AI 实体: 注入失败(%s)" % error)
            return False
        return True

    def _inject_wake(self, text: str) -> bool:
        ok = self._inject(text)
        if ok:
            self._ambient.append("(你被点名了,正在回应)")
        return ok

    def _inject_heartbeat(self, text: str) -> bool:
        return self._inject(text)

    # ------------------------------------------------------------ 推群

    def _sync_line(self, text: str) -> None:
        """把一条文本推到群（异步、不阻塞调用方）。

        ⚠️ 任务必须登记，并在 terminate 时取消 —— 否则插件卸载后仍有任务
        往已销毁的 Context 上发消息。
        """
        config = self.config_obj
        if not config.group_id or not str(text or "").strip():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._sync_line_async(text))
        self._sync_tasks.add(task)

        def _done(finished):
            self._sync_tasks.discard(finished)
            if not finished.cancelled():
                finished.exception()  # 取走异常，避免 "never retrieved" 警告

        task.add_done_callback(_done)

    async def _sync_line_async(self, text: str) -> None:
        config = self.config_obj
        platform_id = config.qq_platform_id
        if not platform_id:
            try:
                platform_id, _adapter = ap_inject.resolve_platform(
                    self.context.platform_manager, ""
                )
            except ap_inject.InjectError as error:
                logger.warning("AI 实体: 找不到可用的平台,消息丢弃: %s" % error)
                return
        ok = await ap_sync.send_group(
            self.context, platform_id, config.group_id, text
        )
        if not ok:
            logger.warning(
                "AI 实体: 推送到群 %s 未被接受,消息已丢弃: %s"
                % (config.group_id, str(text)[:30])
            )
