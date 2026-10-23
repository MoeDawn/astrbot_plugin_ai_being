# AI 实体 · AstrBot 插件

让 mineflayer 驱动的 AI 实体在游戏里的言行同步到 QQ 群，并支持游戏内点名与定时心跳唤醒。

配套 [NetherLink](https://github.com/MoeDawn/astrbot_plugin_netherlink) 使用：
NetherLink 负责真人玩家的群服互通，本插件负责游戏里那具 AI 实体。

## 效果

- 游戏里有人叫它（唤醒词，或私聊），AI 被唤醒并在群里回应
- 到设定的周期自己醒来活动，做完就停
- 群里 @ 它，它可以决定用 `chat` 工具回一句
- 它死了、上线了，群里会说一声

## 环境要求

| 项 | 要求 |
|---|---|
| AstrBot | 4.16 及以上 |
| MCP 服务 | 已在 AstrBot 里连接好的 mineflayer MCP 服务 |
| QQ 平台 | OneBot / NapCat（aiocqhttp）适配器 |
| MC 端 | 装 NetherLink 的 MC 端插件，并开启群服互通 |

## 安装

1. 在插件管理里上传 zip 并启用
2. 填 **`entity_name`**：AI 实体在游戏里的登录名（必填）
3. 填 **`group_id`**：唤醒与播报发到哪个群
4. 填 **`mcp_server_name`**：AstrBot 里那个 MCP 服务的名字（只有一个时可不填）

填完看日志里的四行「AI 实体: 已配置的…」，确认读到的值与预期一致。

## ⚠️ 装之前必须做的一件事

**停掉手写的「轮询游戏事件」定时任务。**

`get_events` 只有一个游标。两边同时读会互相吃掉对方的事件，
表现是「有时能收到、有时收不到」，而且不报错。

## 和 NetherLink 的分工

| 事件 | 由谁处理 |
|---|---|
| 它说的话推群 | **NetherLink**（用你现有的聊天模板，格式与真人一致） |
| 它的死亡 / 上线播报 | **本插件**（NetherLink 的忽略名单会把它们全丢掉） |
| 唤醒它 | **本插件** |
| 定时心跳 | **本插件** |
| 它被唤醒后说的话 | AstrBot 原生回复，进群 |

### 必须在 NetherLink 里配合的设置

把它的游戏 ID 填进 NetherLink 的 **`mc_no_wake_players`**（不唤醒名单）。

- ✅ 它的话照常进群
- ✅ 进退服、死亡、成就照常播报
- ❌ 它用唤醒词说话时不会把自己叫醒

**不要填 `mc_ignored_players`** —— 那个名单会把它的所有事件连同聊天一起丢掉，
群里什么都看不到。

## 配置项

| 项 | 默认 | 说明 |
|---|---|---|
| `enable` | 开 | 总开关。关闭后本插件完全不动作 |
| `entity_name` | 空 | **必填**。它在游戏里的登录名 |
| `group_id` | 空 | **必填**。唤醒与播报发到哪个群 |
| `mcp_server_name` | 空 | MCP 服务名。只有一个服务时可不填 |
| `qq_platform_id` | 空 | 发消息用的机器人。多机器人部署时务必填 |
| `poll_interval` | 60 | 事件轮询间隔（秒），最小 5 |
| `poll_types` | 见下 | 轮询哪些事件类型 |
| `wake_words` | 空 | 聊天里出现任一即唤醒。留空则只能靠私聊与心跳唤醒 |
| `wake_cooldown` | 30 | 两次唤醒的最小间隔（秒） |
| `wake_template` | 见下 | 被点名时发给 AI 的提示词 |
| `heartbeat_cron` | `*/30 * * * *` | 心跳周期（分 时 日 月 周）。留空关闭 |
| `heartbeat_prompt` | 见下 | 心跳时发给 AI 的提示词 |
| `quiet_hours` | `0-7` | 静默时段（整点，含头不含尾）。只压制心跳 |
| `own_event_template` | `{entity} {event}` | 它死亡 / 上线时推群的格式 |
| `self_wake_guard` | 10 | 自唤醒防护窗口（秒） |
| `mcp_timeout` | 20 | 单次 MCP 调用超时（秒） |

`poll_types` 默认：`chat`、`death`、`login`、`playerJoined`、`playerLeft`、`whisper`。
`death` 与 `login` 指**它自己**的死亡与上线，不是别人。

### 模板占位符

| 模板 | 可用占位符 |
|---|---|
| `wake_template` | `{entity}`、`{events}` |
| `heartbeat_prompt` | `{entity}`、`{events}`、`{state}` |
| `own_event_template` | `{entity}`、`{event}`、`{detail}` |

占位符写错时会原样输出那段文字，不会让消息消失。

## 行为说明

**什么时候醒**：游戏里被点名、被人私聊、心跳到点、群里 @ 它。

**什么时候不说话**：提示词让它自己判断。插件只负责把事件送到它面前，
说什么、做不做事都由提示词决定。

**静默时段**：只压制心跳。有人叫它照样会醒。

**MCP 断线**：前两次失败不说话（网络抖一下就播报是噪声），
连续三次失败在群里提一句；恢复时不播报。

## 已知限制

- 事件缓冲有限，长时间不轮询会丢事件（会记一条 warning，游标不重置）
- 同一句话在防护窗口内不会重复唤醒，超过窗口则不拦
- 群消息的上下文会随时间变长，目前没有裁剪策略

## 许可

MIT
