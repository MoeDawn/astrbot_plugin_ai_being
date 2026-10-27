# NetherLink AI 实体

让一具 mineflayer 驱动的 AI 实体在 Minecraft 里活动，按任务设定的时间把游戏内动态交给它。

本插件只做事件轮询。群服互通全部由 [NetherLink](https://github.com/MoeDawn/astrbot_plugin_netherlink) 负责：玩家聊天推群、QQ 群消息转发进游戏、跨服转发、它自己的发言进群、它在群里的回复进游戏。两个插件配套使用，单独装本插件收不到也发不出群消息。

依赖 awesome-mineflayer-mcp 服务器。

## 功能

- 每条任务有自己的时间、事件类型、提示词、投递会话与静默时段。
- 多个任务可以看同一段时间的动态。新建任务只看创建之后的动态。
- 它可以在群里给自己排任务，任务直接写进配置的 `watch_tasks`，面板上看得见。
  手填的任务它删不掉，只能停用；它自己建的任务可以删。
- 它可以让某个任务立刻执行一次，不必等时间表。

## 环境要求

| 项 | 要求 |
|---|---|
| AstrBot | 4.16 及以上 |
| MCP 服务 | 一个已连接的 mineflayer MCP 服务，能在目标服务器登录 |
| QQ 平台 | OneBot / NapCat（aiocqhttp）适配器 |
| NetherLink | 已安装，群服互通已开启 |

⚠️ AstrBot 里同时连接多个 MCP 服务时轮询不启动，日志列出候选。

## 安装

1. 在插件管理里上传 zip 并启用。
2. 填 `entity_name`、`server_address`、`server_port`。
3. 在 `watch_tasks` 里添加一条任务，填 `session`。

启动后看三行日志：

- `AI 实体: 已配置的 entity_name=… server=…:… 任务=N`
- `AI 实体: 使用 MCP 服务 '…'`
- `AI 实体: 轮询与调度已启动`

## 会话

`session` 的格式是 `平台标识:消息类型:会话id`，例如 `aiocqhttp:GroupMessage:123456789`。

| 段 | 填什么 |
|---|---|
| 平台标识 | AstrBot 面板「平台管理」里的机器人 ID（aiocqhttp 默认 `default`），不是适配器名 |
| 消息类型 | 固定 `GroupMessage` |
| 会话id | 群号 |

⚠️ 留空则该条任务永不触发。平台标识填错时注入失败，日志记 ERROR。

## 配置项

| 项 | 默认 | 说明 |
|---|---|---|
| `entity_name` | 空 | 它在游戏里的登录名，与 mineflayer 的 `MC_USERNAME` 一致。按名字精确匹配，不区分大小写。⚠️ 留空则它自己的发言也会进 `{events}` |
| `server_address` | 空 | MC 服务器地址，只写进提示词 |
| `server_port` | `25565` | MC 服务器端口，只写进提示词 |
| `watch_tasks` | 空 | 轮询任务，见下 |

### 任务字段

| 项 | 默认 | 说明 |
|---|---|---|
| `name` | `事件轮询` | 折叠时显示这一条 |
| `cron` | `*/30 * * * *` | 5 字段 cron：分 时 日 月 周 |
| `types` | 7 项，见下 | 推送哪些动态 |
| `prompt` | 见下 | 到点发给 AI 的提示词 |
| `session` | 空 | 投递会话。⚠️ 留空则不触发 |
| `enabled` | 开 | 关闭后到点也不触发 |
| `quiet_hours` | 空 | 静默时段，如 `0-7`；支持跨午夜 `22-2`。只压制定时触发 |
| `offline_policy` | `annotate` | 它不在游戏里时怎么办。`annotate` 照常触发并在提示词里写明不在线；`skip` 这一轮不触发 |
| `created_by` | 空 | 隐藏字段。`ai` = 由 AI 建立，它可以自己删；留空 = 手填，只能停用 |

`prompt` 占位符：`{events}` 期间事件、`{entity}` 它的名字、`{server}` 服务器地址、`{time}` 当前时间。要写字面量花括号时写成 `{{` 与 `}}`。

## 事件类型

面板上是 60 个勾选框，覆盖 mineflayer MCP 会推送的全部类型。

默认开 7 个：`chat`、`whisper`、`playerJoined`、`playerLeft`、`death`、`message`、`weatherUpdate`。

`death` 是它自己死亡；别人的死亡在 `message` 里。

⚠️ `health`、`entityHurt`、`entitySpawn`、`soundEffectHeard`、`entitySwingArm` 频率很高，开了会挤占其他事件。

## 它不在游戏里时

每次触发前先查一次连接状态，结果缓存 10 秒。

不在线时提示词里会写：

```
(你现在不在游戏里,连接状态:已断开,上次断开原因:connection reset。
 看不到游戏内动态,别假设自己站在游戏里)
```

`offline_policy` 设为 `skip` 时这一轮直接不触发，水位线不推进，上线后还能补上。

## 任务轮的回复去哪

| 它做什么 | QQ 群 | 游戏公屏 |
|---|---|---|
| 直接回复 | 有 | 有 |
| 用 `chat` 工具 | 有 | 有 |
| 调 `ai_being_stay_silent` | 无 | 无 |
| 只回 `[静默]` | 无 | 无 |

⚠️ 游戏公屏那列需要 NetherLink 的 `sync_bot_msgs` 打开。

## 迁移

1. 在 NetherLink 里打开 `sync_bot_msgs`。⚠️ 存量部署升级后仍是关，不开则它在群里的回复不会出现在游戏公屏。
2. 把它的游戏 ID 加进 NetherLink 的 `binding_exempt_players`。⚠️ 不加则绑定门禁会反复把它踢出服务器。
3. 把它的游戏 ID 加进 NetherLink 的 `mc_no_wake_players`。⚠️ 填进 `mc_ignored_players` 会让它的言行在群里全部消失。

## 已知限制

AI 建的任务与手填的共用一份 `watch_tasks`。面板打开着、没刷新时 AI 建了任务，
你点保存会用旧快照覆盖掉它。刷新面板即可看到最新列表。

## 可靠性

- MCP 未连上时按 10 / 30 / 60 / 120 / 240 秒退避重试，之后每 240 秒重试一次。
- 单次轮询失败不推进游标，下一轮重读。
- 服务端事件缓冲溢出时，`{events}` 里注明中间有一段动态丢失。
- 任务注入失败不推进该任务的水位线，下次触发重试同一段动态。
- 面板点「重载」后继续轮询。

## 状态

✅ 事件渲染、调度、注入、任务工具与配置解析有单元测试覆盖。
⚠️ 未在真实 AstrBot + Minecraft 服务器上完整验证。

## 许可

MIT
