# -*- coding: utf-8 -*-
"""MCP 连接查找与 `get_events` 调用。

⚠️ 这里用到的 AstrBot 内部接口**都不是**面向插件的稳定 API：
`context.provider_manager.llm_tools.mcp_client_dict` 是个只读视图
（`MappingProxyType` 派生的 Mapping），`MCPClient.session` 是
`mcp.ClientSession`。升级 AstrBot 后若对不上，本模块统一包成 `McpError`，
由 main 记 ERROR 并停轮询 —— 不让它变成静默失效。

⚠️ **为什么只能轮询**：AstrBot 构造 `ClientSession` 时没传 `message_handler`
（`core/agent/mcp_client.py` 的 `_do_connect`），服务端推送
（`sendLoggingMessage` / `notifications/resources/updated`）没有任何消费者。
所以 `MCP_EVENT_STREAM=push` 在本框架下收不到，`get_events` 是唯一稳定路径。
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta


class McpError(Exception):
    """MCP 调用失败：连接不在、服务端 isError、超时、结果无法解析。"""


class McpAmbiguousError(McpError):
    """有多个 MCP 服务且没有指定用哪个 —— 配置错误，不是网络抖动。"""


def resolve_client(tool_manager, wanted: str):
    """从 AstrBot 的 MCP 连接表里找一具客户端。

    返回 `(server_name, client)`。`wanted` 留空时**只有恰好一个服务**才自动
    选取 —— 有歧义就抛错，绝不「随便挑一个」（挑错就是往另一具实体发指令）。

    ⚠️ `wanted` 生产调用恒传空串；保留该形参只为测试与将来扩展，
    不代表存在可配置的 MCP 服务名。
    """
    try:
        table = tool_manager.mcp_client_dict
    except AttributeError as error:
        raise McpError("AstrBot 的 MCP 连接表不可用: %s" % error) from error
    try:
        names = list(table.keys())
    except AttributeError as error:
        raise McpError("MCP 连接表不是映射: %s" % error) from error
    if wanted:
        if wanted not in table:
            raise McpError(
                "指定的 MCP 服务 %r 不在已连接的 MCP 服务里(现有: %s)"
                % (wanted, ", ".join(names) or "无")
            )
        return wanted, table[wanted]
    if not names:
        raise McpError("AstrBot 里没有任何已连接的 MCP 服务")
    if len(names) > 1:
        raise McpAmbiguousError(
            "检测到 %d 个 MCP 服务(%s)，无法自动选取。"
            "本插件一次只能驱动一具实体，请在 AstrBot 的 MCP 配置里只保留一个连接。"
            % (len(names), ", ".join(sorted(names)))
        )
    return names[0], table[names[0]]


def _first_text(result) -> str:
    for part in getattr(result, "content", None) or []:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text.strip():
            return text[:300]
    return "(无内容)"


def _extract_payload(result):
    """优先 `structuredContent`，退化为解析第一个文本块。"""
    payload = getattr(result, "structuredContent", None)
    if isinstance(payload, dict):
        return payload
    for part in getattr(result, "content", None) or []:
        text = getattr(part, "text", None)
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            parsed = json.loads(text)
        except ValueError as error:
            raise McpError("MCP 返回的不是 JSON: %s" % text[:120]) from error
        if isinstance(parsed, dict):
            return parsed
        raise McpError("MCP 返回的 JSON 不是对象")
    raise McpError("MCP 结果既没有 structuredContent 也没有文本")


async def call_tool(client, name: str, arguments: dict, timeout: float) -> dict:
    session = getattr(client, "session", None)
    if session is None:
        raise McpError("MCP 会话未建立")
    try:
        result = await asyncio.wait_for(
            session.call_tool(
                name=name,
                arguments=arguments,
                read_timeout_seconds=timedelta(seconds=timeout),
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError as error:
        raise McpError("调用 %s 超时(%ss)" % (name, timeout)) from error
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise McpError("调用 %s 失败: %s" % (name, error)) from error
    if getattr(result, "isError", False):
        raise McpError("MCP 服务端报错: %s" % _first_text(result))
    return _extract_payload(result)


async def get_events(client, since: int, types, limit: int, timeout: float) -> dict:
    """取一批事件。

    返回 `{"nextSince": int, "dropped": bool, "events": list}`。
    ⚠️ `nextSince` 缺失一律当失败 —— 悄悄退回旧游标会**重复消费**事件，
    表现是同一句话反复推群、反复唤醒。
    """
    payload = await call_tool(
        client,
        "get_events",
        {"since": int(since), "types": list(types or []), "limit": int(limit)},
        timeout,
    )
    try:
        next_since = int(payload.get("nextSince"))
    except (TypeError, ValueError) as error:
        raise McpError("get_events 返回里没有可用的 nextSince") from error
    events = payload.get("events")
    if not isinstance(events, list):
        events = []
    return {
        "nextSince": next_since,
        "dropped": bool(payload.get("dropped", False)),
        "events": events,
    }

async def connection_status(client, timeout: float) -> dict:
    """查连接状态：`{status, username, lastEndReason, ...}`。

    服务端每次都会给完整字段（未连接时 `username` 可能是上次的配置值、
    `position` 为 null）。这里只挑我们判断要用的，其余原样留着。
    """
    return await call_tool(client, "get_connection_status", {}, timeout)
