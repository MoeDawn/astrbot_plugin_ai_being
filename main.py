# -*- coding: utf-8 -*-
"""AI 实体插件入口 —— NetherLink 的附属插件。

职责：**按计划把游戏内动态送进 AstrBot 会话**。推群、唤醒、同步她的发言
全部由 NetherLink 负责，这里一件都不重复。

四条不变量（改动前务必读）：
  1. **只有轮询器能调 `get_events`** —— 游标全局唯一，多读者会让同一段动态被重复唤醒
  2. **轮询失败不写游标、不推进任何水位线** —— 否则一段动态永久丢失
  3. 新建任务的水位线 = 当前最大 seq —— 不翻旧账
  4. 注入的事件带 `ai_being_task` 标记 —— 否则提示词会被 NetherLink 转发进游戏
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

try:
    from . import (
        ap_buffer,
        ap_config,
        ap_cron,
        ap_events,
        ap_inject,
        ap_mcp,
        ap_tasks,
    )
except ImportError:  # 插件以顶层模块方式加载时
    import ap_buffer
    import ap_config
    import ap_cron
    import ap_events
    import ap_inject
    import ap_mcp
    import ap_tasks

PLUGIN_NAME = "astrbot_plugin_ai_being"

# 轮询器是**唯一**读者，间隔定成常量：真实频率由服务端缓冲决定，改快只是白耗。
POLL_INTERVAL_SECONDS = 15
SCHEDULE_TICK_SECONDS = 10
POLL_LIMIT = 1000
MAX_PAGES_PER_TICK = 8
RESOLVE_BACKOFF = (10, 30, 60, 120, 240)

# 「这一轮我决定不说话」的标记键。由 ai_being_stay_silent 工具打上，
# 由 _drop_silent_reply 消费。只在本插件内部使用，不是跨插件契约。
SILENT_FLAG = "ai_being_silent"

# 连续这么多次读取失败后，丢弃 client 重新解析（MCP 可能换了连接）
CLIENT_DROP_AFTER_FAILURES = 3


def chain_is_silent(chain) -> bool:
    """整条回复只有一个纯文本组件，且内容就是静默哨兵。"""
    if not chain:
        return False
    if any(type(comp).__name__ != "Plain" for comp in chain):
        return False
    text = "".join(str(getattr(comp, "text", "") or "") for comp in chain).strip()
    return text == ap_config.SILENT_TOKEN

class AIBeingPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config_obj = ap_config.load(config or {})
        # ⚠️ get_astrbot_plugin_data_path() 返回 **str**，必须先包成 Path
        self.data_dir = Path(get_astrbot_plugin_data_path()) / "ai_being"
        self._state = ap_tasks.StateStore(self.data_dir)
        self._state.load()
        self._buffer = ap_buffer.Buffer()
        self._ai_tasks = []
        self._tasks_obj = []
        self._fired = {}
        self._client = None
        self._client_name = ""
        self._resolve_attempts = 0
        self._next_resolve_at = 0.0
        self._poll_failures = 0
        self._polled_once = False
        self._tasks = []
        self._stopping = asyncio.Event()
        self._injected = []

        self._log_configured()
        self._refresh_tasks()
        # 构造期通常没有运行中的事件循环，这里会静默返回；
        # 真正的启动点在 on_astrbot_loaded / on_plugin_loaded。
        self._start_loops("构造")

    # ------------------------------------------------------------ 配置

    def _log_configured(self) -> None:
        config = self.config_obj
        logger.info(
            "AI 实体: 已配置的 entity_name=%s server=%s:%s 任务=%d"
            % (
                config.entity_name or "(空)",
                config.server_address or "(空)",
                config.server_port,
                len(config.tasks),
            )
        )
        if not config.entity_name:
            logger.error(
                "AI 实体: 没有配置 entity_name,无法排除它自己的发言"
                "（{events} 里会出现它说的话）"
            )
        if not any(task.enabled and task.session for task in config.tasks):
            logger.warning(
                "AI 实体: 配置里的 watch_tasks 为空或全部停用"
                "（AI 自建任务启动后才加载）"
            )
        for task in config.tasks:
            if not str(task.session or "").strip():
                logger.warning("AI 实体: 任务 %r 的 session 为空，该条永不触发" % task.name)
            elif not ap_cron_valid(task.cron):
                logger.warning(
                    "AI 实体: 任务 %r 的 cron=%r 非法，该条已停用"
                    % (task.name, task.cron)
                )
        try:
            settings = self.context.get_config().get("platform_settings", {}) or {}
        except Exception:  # noqa: BLE001
            settings = {}
        if settings.get("ignore_bot_self_message"):
            logger.info("AI 实体: ignore_bot_self_message=true（不影响本插件）")

    # ------------------------------------------------------------ 生命周期

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """AstrBot 全部初始化完成后启动一次。"""
        self._start_loops("启动")

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata):
        """**每次**本插件加载完成时启动（含热重载）。

        🔴 为什么必须同时挂这个钩子：`star_manager.reload` 只做
        `terminate` + `load`，**不会**重新派发 `on_astrbot_loaded`。
        只靠启动钩子的话，重载后的新实例两条循环永远不起 —— 面板显示
        「加载成功」、日志干净、游标一动不动。2026-10-26 真机实测：
        重载后 `state.json` 停在 terminate 写下的 cursor 0。

        ⚠️ 这个钩子对**每个**插件的加载都触发，必须只认自己（与 NetherLink
        的 `_rebind_game_platform_after_reload` 同一写法）。
        """
        name = getattr(metadata, "name", "") or ""
        if name and name != PLUGIN_NAME:
            return
        self._start_loops("重载")

    def _start_loops(self, reason: str) -> None:
        """幂等启动两条循环。构造期没有事件循环，所以这里再兜一次。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 构造期：交给 on_astrbot_loaded / on_plugin_loaded
        if any(not task.done() for task in self._tasks):
            return
        self._stopping = asyncio.Event()
        self._tasks = [
            loop.create_task(self._poll_loop()),
            loop.create_task(self._schedule_loop()),
        ]
        logger.info("AI 实体: 轮询与调度已启动（%s）" % reason)

    async def terminate(self):
        """停止循环。

        🔴 **不在这里保存状态**：游标与水位线在每次轮询成功、每次注入成功后
        就已经落盘了。而重载是 `terminate` + `load` —— 在这里再写一次，
        写进去的是**旧实例**的内存现值（它已经停止工作，值不会更新）。
        2026-10-26 真机那次 `cursor 0` 正是这么来的。
        """
        self._stopping.set()
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
        logger.info("AI 实体: 已停止")

    # ------------------------------------------------------------ 任务

    def _refresh_tasks(self) -> None:
        self._ai_tasks = ap_tasks.load_ai_tasks(self.data_dir)
        merged = ap_tasks.merge(
            self.config_obj.tasks, self._ai_tasks, self._state.toggles()
        )
        self._tasks_obj = [task for task in merged if ap_cron_valid(task.cron)]
        self._state.ensure_watermarks(
            self._tasks_obj, max(self._state.cursor, self._buffer.max_seq())
        )

    # ------------------------------------------------------------ MCP

    def _resolve_client(self) -> bool:
        if self._client is not None:
            return True
        now = time.monotonic()
        if now < self._next_resolve_at:
            return False                      # 还在退避窗口里，安静跳过
        try:
            self._client_name, self._client = ap_mcp.resolve_client(
                self.context.provider_manager.llm_tools, ""
            )
        except (ap_mcp.McpError, AttributeError) as error:
            index = min(self._resolve_attempts, len(RESOLVE_BACKOFF) - 1)
            delay = RESOLVE_BACKOFF[index]
            self._resolve_attempts += 1
            self._next_resolve_at = now + delay
            # 多个 MCP 服务是配置错误（spec §4.7），不是网络抖动，记 ERROR。
            # 退避窗口（10/30/60/120/240 秒）保证不会每 15 秒刷屏。
            log = (
                logger.error
                if isinstance(error, ap_mcp.McpAmbiguousError)
                else logger.warning
            )
            log(
                "AI 实体: 无法确定可用的 MCP 服务(第 %d 次)，%d 秒后重试: %s"
                % (self._resolve_attempts, delay, error)
            )
            self._client = None
            return False
        self._resolve_attempts = 0
        self._next_resolve_at = 0.0
        self._poll_failures = 0        # 新连接要有完整的失败容忍度
        logger.info("AI 实体: 使用 MCP 服务 %r" % self._client_name)
        return True

    # ------------------------------------------------------------ 轮询

    async def _poll_loop(self):
        while not self._stopping.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                logger.error("AI 实体: 轮询循环异常(继续): %s" % error)
            await self._sleep(POLL_INTERVAL_SECONDS)

    async def _sleep(self, seconds):
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _poll_once(self):
        if self._client is None:
            if not self._resolve_client():
                return
        # 任务快照由 `__init__` 与 `_schedule_once` 刷新；轮询只消费当前快照，
        # 不在这里重建 —— 否则运行期新写入的任务列表会被冲回配置里的旧值。
        types = ap_tasks.enabled_types(self._tasks_obj)
        if not types:
            return  # 并集为空 ⇒ 不轮询，省一次 MCP 调用

        since = int(self._state.cursor)
        dropped = False
        pages = 0
        next_since = since     # 收尾的丢失提示要用最后一次的区间上界
        while pages < MAX_PAGES_PER_TICK:
            try:
                page = await ap_mcp.get_events(
                    self._client, since, types, POLL_LIMIT, 20
                )
            except ap_mcp.McpError as error:
                logger.warning("AI 实体: 读取事件失败: %s" % error)
                self._poll_failures += 1
                if self._poll_failures >= CLIENT_DROP_AFTER_FAILURES:
                    logger.warning(
                        "AI 实体: 连续 %d 次读取失败，丢弃当前 MCP 连接并重新解析"
                        % self._poll_failures
                    )
                    self._client = None
                return  # 失败不写游标、不推进水位线
            self._poll_failures = 0
            # 至少成功读完一页才允许调度触发（见 `_schedule_once` 的门槛）。
            self._polled_once = True
            dropped = dropped or bool(page.get("dropped"))
            # 服务端 `dropped` 的语义是「(since, nextSince] 之间丢了事件」：
            # 上界只能取 nextSince —— 本页可能零事件（整段被挤掉），
            # 用 buffer.max_seq() 会退化成 0，提示永远不出现。
            next_since = int(page["nextSince"])
            # 本地溢出不一样：淘汰的是缓冲里最老的事件，可能早已被任务读过，
            # 边界必须取被淘汰事件的最大 seq，否则「挤掉已读过的」会误报成丢失。
            evicted_until = self._buffer.add(page.get("events") or [])
            if evicted_until:
                logger.warning(
                    "AI 实体: 本地缓冲溢出，丢弃了截至 seq %d 的较早动态"
                    % evicted_until
                )
                self._state.mark_dropped_until(evicted_until)
            if next_since == since:
                break  # 服务端没有前进，继续翻页只会死循环
            # 🔴 判据是**游标倒退**，不是「本地缓冲为空」：
            #    重启后缓冲本来就是空的，用 max_seq() 当判据会把所有持久化
            #    水位线抹成 0，导致每个任务重读全部事件（重复注入）。
            if next_since < since:
                rebased = self._state.rebase_watermarks(next_since)
                removed = self._buffer.drop_above(next_since)
                if rebased or removed:
                    logger.warning(
                        "AI 实体: 事件序号回退到 %d，已重定基 %d 个任务的水位线，"
                        "丢弃 %d 条超前事件: %s"
                        % (next_since, len(rebased), removed,
                           ", ".join(rebased[:5]))
                    )
            since = next_since
            self._state.cursor = since
            self._state.save()
            if len(page.get("events") or []) < POLL_LIMIT:
                break
            pages += 1
        if dropped:
            logger.warning("AI 实体: 事件缓冲溢出，中间有一段动态丢失")
            self._state.mark_dropped_until(next_since)

    # ------------------------------------------------------------ 调度

    async def _schedule_loop(self):
        while not self._stopping.is_set():
            try:
                self._schedule_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                logger.error("AI 实体: 调度循环异常(继续): %s" % error)
            await self._sleep(SCHEDULE_TICK_SECONDS)

    def _schedule_once(self):
        # ⚠️ 一次都没成功轮询过就不触发 —— 否则启动竞态或 MCP 离线时
        #    会用空事件唤醒，且当分钟唯一一次触发被白白用掉。
        if not self._polled_once:
            return
        # ⚠️ 必须每轮重读 `tasks.json` —— AI 自建任务只存在这里，不刷新的
        #    表现是「AI 建了任务却永不触发」。
        self._refresh_tasks()
        for task in ap_tasks.due(self._tasks_obj, datetime.now(), self._fired):
            try:
                self._fire(task)
            except Exception as error:  # noqa: BLE001
                # 一个任务炸掉不该带走同一分钟的其余任务 —— `due()` 已经把它们
                # 全部标记为本分钟触发过，被跳过的要等下一分钟才会重来。
                logger.error("AI 实体: 任务 %r 触发失败: %s" % (task.name, error))

    def _fire(self, task) -> None:
        """取事件 → 渲染 → 拼提示词 → 注入 → 推进水位线。

        ⚠️ 注入失败时**不推进水位线** —— 这一段动态应当在下一次触发时重来。
        """
        watermark = self._state.watermark(task.id)
        # 丢失提示按 seq 区间判定：同一条丢失区间内的所有任务都该看到它
        dropped = self._state.dropped_for(watermark)
        # ⚠️ 这里取 200，截断交给 `render_events(limit=60)` —— 否则渲染层
        #    永远拿不到 >60 条，「更早的 N 条已省略」这句永不出现。
        events, new_watermark = self._buffer.take(
            watermark, list(task.types), limit=200
        )
        text = self._render_prompt(task, events, dropped)
        try:
            self._inject_task(task, text)
        except Exception as error:  # noqa: BLE001
            logger.error("AI 实体: 任务 %r 注入失败: %s" % (task.name, error))
            return
        self._state.set_watermark(task.id, new_watermark)
        # 所有启用任务都越过丢失区间后才能清标记 —— 第一个任务成功不代表别人
        self._state.clear_dropped_if_all_passed(self._live_watermarks())
        self._state.save()

    def _live_watermarks(self):
        """会真正推进水位线的任务（启用且 session 非空）。

        ⚠️ 别把空 session 的任务算进清零门槛：`due()` 永远跳过它们、
        水位线永不动，标记会被永远卡住。
        """
        return [
            self._state.watermark(t.id)
            for t in self._tasks_obj
            if t.enabled and str(t.session or "").strip()
        ]

    def _render_prompt(self, task, events, dropped) -> str:
        config = self.config_obj
        events_text = ap_events.render_events(
            events, entity_name=config.entity_name, limit=60, dropped=dropped
        )
        values = {
            "events": events_text,
            "entity": config.entity_name,
            "server": config.server_address,
            "port": config.server_port,
            "host": config.server_address,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        try:
            return str(task.prompt).format(**values)
        except (KeyError, IndexError, ValueError, AttributeError):
            return str(task.prompt) + "\n\n" + events_text

    def _inject_task(self, task, text) -> None:
        platform_id, _session_id = ap_inject.require_group_umo(task.session)
        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            raise ap_inject.InjectError(
                "会话 %r 的平台 %r 不在运行中" % (task.session, platform_id)
            )
        meta = platform.meta()
        if str(getattr(meta, "name", "") or "") != "aiocqhttp":
            raise ap_inject.InjectError(
                "只能投递到 aiocqhttp 群会话，%r 是 %r 平台"
                % (platform_id, getattr(meta, "name", "?"))
            )
        ap_inject.commit_task(platform, task.session, text)

    # ------------------------------------------------------------ 静默哨兵

    @filter.on_decorating_result()
    async def _drop_silent_reply(self, event) -> None:
        """AI 只回了静默哨兵时，丢弃整条回复，不往群里发。

        🔴 **只对自己合成的调度事件生效**（`ai_being_task` 标记）。群里正常
        对话不受影响 —— 那里用户问「在吗」她答「[静默]」是真实回复，删掉就是
        装死。

        ⚠️ 这不是代码替 AI 判断「该不该说话」：判断在提示词里，这里只是把
        「我决定不说话」这个结论变成一个可执行的动作。定时轮询每分钟都触发，
        没有这条，群里会被「没有新动静」刷屏。
        """
        if not event.get_extra(ap_inject.MARKER_KEY):
            return
        if event.get_extra(SILENT_FLAG):
            chain = getattr(event.get_result(), "chain", None)
            if chain:
                chain.clear()
            logger.info("AI 实体: 本轮静默,回复已丢弃")
            return
        result = event.get_result()
        chain = getattr(result, "chain", None) if result is not None else None
        if not chain:
            return
        # 只认「全是纯文本组件」的回复。用类名而不是 isinstance：
        #   · 真机上 Plain 是 astrbot.core.message.components.Plain；
        #   · 这里只需要「有 text、不是图片/At/引用」，用属性判断更稳。
        if any(type(comp).__name__ != "Plain" for comp in chain):
            return
        text = "".join(str(getattr(comp, "text", "") or "") for comp in chain).strip()
        if text != ap_config.SILENT_TOKEN:
            return
        chain.clear()
        logger.info("AI 实体: 本轮静默,回复已丢弃")

    # ------------------------------------------------------------ 出站

    @filter.on_llm_response()
    async def _mark_silent_at_agent_stage(self, event, resp) -> None:
        """在 **agent 结束**时就判定「本轮要不要出声」，而不是等到出站钩子。

        🔴 为什么必须这么早（2026-10-26 真机）：
        本插件与 NetherLink 都监听 `on_decorating_result`，AstrBot 按**插件加载
        顺序**（`os.listdir`）执行，谁先谁后不可控。若 NetherLink 先跑，它会先把
        「[静默]」转发进游戏并打上幂等标记；本插件再清空链，就变成「群里没了、
        游戏里留着」。

        `on_llm_response` 发生在 `on_decorating_result` **之前**，且标记挂在
        event 上，两个插件谁先谁后都能读到 —— 这才是顺序无关的位置。

        两种沉默来源都在这里归一成 `SILENT_FLAG`：
          · AI 调用了 `ai_being_stay_silent` 工具；
          · AI 只回了 `[静默]` 这个哨兵文本。
        """
        if not event.get_extra(ap_inject.MARKER_KEY):
            return
        if event.get_extra(SILENT_FLAG):
            return  # 工具已经标过
        text = str(getattr(resp, "completion_text", "") or "").strip()
        if text == ap_config.SILENT_TOKEN:
            try:
                event.set_extra(SILENT_FLAG, True)
            except Exception:  # noqa: BLE001
                return
            logger.info("AI 实体: 本轮静默,回复已丢弃")

    @filter.on_decorating_result()
    async def _drop_silent_reply(self, event) -> None:
        """本轮被判为沉默时，丢弃整条回复（群和游戏都不会出现）。

        兜底：`on_llm_response` 没跑到时（例如工具调用后还有后续轮次），
        这里按 `chain_is_silent` 再判一次。

        只对带 `ai_being_task` 标记的事件生效；群里正常对话不受影响。
        """
        if not event.get_extra(ap_inject.MARKER_KEY):
            return
        if not (event.get_extra(SILENT_FLAG) or chain_is_silent(
                getattr(event.get_result(), "chain", None))):
            return
        chain = getattr(event.get_result(), "chain", None)
        if chain:
            chain.clear()
        logger.info("AI 实体: 本轮决定沉默，已丢弃")
    # ------------------------------------------------------------ AI 工具

    @filter.llm_tool(name="ai_being_stay_silent")
    async def ai_being_stay_silent(self, event) -> str:
        """这一轮不需要说话:调用本工具后,本次回复不会发到 QQ 群。

        定时任务每轮都会把游戏内动态交给你。多数时候没有值得说的,
        这时调用本工具,再随便回一句,那句话会被丢弃。
        想对游戏里的玩家说话时用 chat 工具。

        ⚠️ 只在定时任务触发时有效;群里有人跟你说话时不要用,对方会收不到回应。
        """
        try:
            event.set_extra(SILENT_FLAG, True)
        except Exception as error:  # noqa: BLE001
            return "标记失败：%s" % error
        return "已标记为沉默，本次回复不会发送。"


    @filter.llm_tool(name="ai_being_task_list")
    async def ai_being_task_list(self, event) -> str:
        """列出所有事件轮询任务：配置里的与你自己创建的。

        用这些工具安排自己什么时候醒来了解游戏内动态。
        """
        try:
            self._refresh_tasks()
            if not self._tasks_obj:
                return "现在没有任何任务。"
            lines = []
            for task in self._tasks_obj:
                source = "配置" if task.source == "config" else "自建"
                state = "启用" if task.enabled else "停用"
                lines.append(
                    "- [%s] %s（%s，%s）cron=%s 类型=%s 会话=%s"
                    % (source, task.id, task.name, state, task.cron,
                       ",".join(task.types), task.session or "(空)")
                )
            if any(task.source == "ai" for task in self._tasks_obj):
                lines.append("要改任务只能删了重建（重建会重置水位线）。")
            return "\n".join(lines)
        except Exception as error:  # noqa: BLE001
            return "查询任务出错：%s" % error

    @filter.llm_tool(name="ai_being_task_create")
    async def ai_being_task_create(self, event, name: str, cron: str,
                                   prompt: str, session: str = "",
                                   types: str = "", quiet_hours: str = "") -> str:
        """创建一个定时任务：到点后把期间的游戏内动态交给你。

        Args:
            name(string): 任务名,也是唯一标识。改名等于重建,会重置水位线(期间的动态不再补发)
            cron(string): 5 字段 cron:分 时 日 月 周,例如 0 9 * * * = 每天 9 点
            prompt(string): 到点后发给你的提示词,用 {events} 表示期间的游戏内动态。要写字面量花括号时写成 {{ 和 }}。⚠️ 写错则 {events} 不会被替换
            session(string): 投递到哪个会话,形如 aiocqhttp:GroupMessage:123;留空 = 当前会话
            types(string): 逗号分隔的事件类型,留空用默认(chat,whisper,playerJoined,playerLeft,death,message,weatherUpdate)
            quiet_hours(string): 静默时段如 0-7,留空不静默
        """
        try:
            if not ap_cron.valid(str(cron or "").strip()):
                return "cron 表达式非法：%s（需要 5 段：分 时 日 月 周）" % cron
            target = str(session or "").strip() or str(event.unified_msg_origin)
            try:
                ap_inject.require_group_umo(target)
            except ap_inject.InjectError as error:
                return "会话格式不对：%s" % error
            name_text = str(name or "").strip()
            if not name_text:
                return "任务名不能为空 —— 它是任务的唯一标识。"
            entry = {
                "__template_key": ap_config.TEMPLATE_KEY,
                "name": name_text,
                "cron": str(cron).strip(),
                "prompt": str(prompt),
                "session": target,
                "types": [
                    item.strip()
                    for item in str(types or "").replace("，", ",").split(",")
                    if item.strip() and item.strip() not in ("none", "null")
                ],
                "enabled": True,
                "quiet_hours": str(quiet_hours or "").strip(),
            }
            task = ap_config.build_task(entry, set())
            # ⚠️ 用**规范化后**的 id 同时比对配置任务与自建任务 ——
            #    只比 name 原文会让空名字/重名绕过检查，撞出两条同 id 任务。
            existing = {t.id for t in self.config_obj.tasks} | {
                t.id for t in ap_tasks.load_ai_tasks(self.data_dir)
            }
            if task.id in existing:
                return "已经有同名任务 %r 了，换个名字或先删掉旧的。" % task.id
            task = replace(task, id=task.id, source="ai")
            self._ai_tasks.append(task)
            ap_tasks.save_ai_tasks(self.data_dir, self._ai_tasks)
            self._refresh_tasks()
            self._state.save()
            return ("已创建任务 %r（cron=%s，投到 %s）。"
                    "改名等于重建，会重置水位线。" % (task.id, task.cron, task.session))
        except Exception as error:  # noqa: BLE001
            return "创建任务出错：%s" % error

    @filter.llm_tool(name="ai_being_task_delete")
    async def ai_being_task_delete(self, event, task_id: str) -> str:
        """删除一个你自己创建的任务。配置文件里的任务删不掉,只能停用。

        Args:
            task_id(string): 任务 id,用 ai_being_task_list 查
        """
        try:
            self._refresh_tasks()
            target = str(task_id or "").strip()
            known = {t.id for t in self._tasks_obj}
            if target not in known:
                return "没有 id 为 %r 的任务。" % task_id
            self._ai_tasks = ap_tasks.load_ai_tasks(self.data_dir)
            kept = [task for task in self._ai_tasks if task.id != target]
            if len(kept) == len(self._ai_tasks):
                # 存在但不在自建列表里 ⇒ 是配置任务
                return ("%r 是配置文件里的任务，删不掉；"
                        "用 ai_being_task_toggle 停用。" % target)
            ap_tasks.save_ai_tasks(self.data_dir, kept)
            self._ai_tasks = kept
            self._state.forget_task(target)
            self._state.save()
            self._refresh_tasks()
            return "已删除任务 %r。改任务请删了重建（会重置水位线）。" % target
        except Exception as error:  # noqa: BLE001
            return "删除任务出错：%s" % error

    @filter.llm_tool(name="ai_being_task_toggle")
    async def ai_being_task_toggle(self, event, task_id: str, enabled: bool) -> str:
        """启用或停用一个任务,配置里的也可以。

        Args:
            task_id(string): 任务 id
            enabled(boolean): true 启用,false 停用
        """
        try:
            target = str(task_id or "").strip()
            if not target:
                return "任务 id 不能为空。"
            self._refresh_tasks()
            if not any(t.id == target for t in self._tasks_obj):
                return "没有 id 为 %r 的任务。用 ai_being_task_list 查一下。" % target
            value = enabled if isinstance(enabled, bool) else str(enabled).strip().lower() in ("true", "1", "yes", "开")
            self._state.set_enabled(target, value)
            self._state.save()
            self._refresh_tasks()
            return "任务 %r 已%s。" % (target, "启用" if value else "停用")
        except Exception as error:  # noqa: BLE001
            return "切换任务出错：%s" % error


def ap_cron_valid(expression) -> bool:
    return ap_cron.valid(expression)
