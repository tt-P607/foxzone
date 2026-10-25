# FoxZone（墨狐空间）

将 Bot 接入 QQ 空间，实现说说自动发布、好友动态监控、评论自动互动与「他人空间下的接力回复」。基于 [Neo-MoFox](https://github.com/MoFox-Studio/Neo-MoFox) 框架的插件层 API（`src.app.plugin_system.api.*`）实现。

> 此插件需配合已启动的 QQ 适配器使用——通过其透传的 `get_cookies` action 获取 QQ 空间 Cookie，无需额外开启 HTTP 服务器。

---

## 功能概览

| 模块 | 说明 |
|------|------|
| **发布说说** | LLM 生成正文并发布 |
| **读取动态** | 拉取自己/好友的最近说说，提取文本与图片描述供 LLM 使用；评论与内联楼中楼可能不完整 |
| **评论与点赞** | 提供 `qzone_post_comment` / `qzone_like_feed` 工具供 LLM 自主调用；评论工具也支持带完整父级身份的精确回复 |
| **自动回复** | 轮询自己说说下的评论，LLM 批量决策回复内容并发送 |
| **好友动态监控** | 周期性扫描好友 timeline，由 LLM 逐条决策是否点赞/评论（默认 AI 自主决定，可开启始终点赞） |
| **外部接力回复** | 检查 Bot 在他人说说下评论后，是否有人回复 Bot 的评论；命中则继续接力（默认关闭） |

---

## 架构定位

> **FoxZone 是一个与主 Bot 共享「人格」数据源、但拥有独立执行管道的自治子系统。**

- 人格来自 `core.toml` 的 personality 节（QZone 场景特化注入）
- 三条自治闭环（评论回复 / 好友监控 / 外部接力）由插件自己的定时循环驱动，
  不经过框架的 Chatter 消息管道（批量决策语义与「一个 stream 一次对话」不匹配）
- 对外的价值载体是 3 个 Tool——任何 Chatter 都可以通过 Tool Calling 操控 QQ 空间

详细设计见仓库根 `plans/refactor_foxzone.md`。

---

## 组件清单

| 类型 | 名称 | 说明 |
|------|------|------|
| `service` | `qzone_service` | QQ 空间原子能力出口（发布 / 读取 / 评论 / 点赞 / 回复 / 识图） |
| `command` | `foxzone` | 管理命令（`/foxzone send [主题]`） |
| `tool` | `qzone_read_feed` | 读取说说列表 |
| `tool` | `qzone_post_comment` | 发表评论或精确回复评论 |
| `tool` | `qzone_like_feed` | 点赞 |
| `config` | `config` | 插件配置（见下方「配置」） |

自治循环（非框架组件）：`autopilot/` 包由 `plugin.on_plugin_loaded` 启动、
`on_plugin_unloaded` 停止；持久化状态由插件级单例 `QZoneRuntime` 统一持有。

---

## 安装与启用

1. 把本目录放入主程序的 `plugins/foxzone/`
2. 依赖声明在 `manifest.json` 中，系统将自动尝试安装，或可手动运行安装：
   ```bash
   uv pip install aiohttp beautifulsoup4 json5 orjson
   ```
3. 在 `config/plugins/foxzone/config.toml` 中启用插件：
   ```toml
   [general]
   enabled = true
   ```
   Bot 自身 QQ 号会自动从已启动的 QQ 适配器获取，无需重复配置。
4. 在 `config/model.toml` 中确认 `actor`、`vlm` 等任务已注册（任务名可在配置中切换）
5. 启动主程序：`uv run main.py`

---

## 配置（节选）

完整字段见 [`config.py`](config.py)，每节都带 `description`，TOML 支持热重载。

```toml
[general]
enabled = true

[llm]
story_model_task = "actor"        # 生成说说正文用的模型任务
comment_model_task = "actor"      # 生成评论/回复用的模型任务
vision_model_task = "vlm"         # 识图模型任务，置空则跳过识图
multimodal_mode = false           # 多模态模式：说说图片直接传模型（需视觉模型）

[monitor]
enable_auto_monitor = true        # 总开关
interval_minutes = 10             # 评论自动回复轮询间隔
enable_auto_reply = true
max_comment_age_hours = 72.0
enable_external_followup = false  # 外部接力回查独立开关（默认关，防风控）
external_followup_minutes = 60    # 外部回查间隔
external_followup_batch = 1       # 每轮最多检查多少个 QQ
enable_friend_monitor = false     # 好友动态主动互动
always_like = false               # 始终点赞开关（false 时由 LLM 逐条决策点赞/评论）
friend_monitor_interval_minutes = 30
dnd_enabled = false
dnd_start_hour = 23
dnd_end_hour = 7

```

---

## 关键文件

```
foxzone/
├── plugin.py              插件入口：装配 + 持有 Runtime + 启停 Autopilot
├── runtime.py             QZoneRuntime：插件级状态单例（cookie / 三份持久化状态
│                          / ContentService / 发送串行锁）
├── config.py              配置定义（ConfigBase / SectionBase）
├── prompts.py             LLM 提示词模板，向 PromptManager 注册
├── manifest.json          插件元数据
├── components/            对外契约层
│   ├── service.py         QZoneService：原子能力门面
│   ├── commands/          管理命令（/foxzone send）
│   └── tools/             暴露给 LLM 的 3 个工具组件
├── autopilot/             自治层（三条定时闭环）
│   ├── scheduler.py       DND 判定 + 循环调度
│   ├── engine.py          BatchSendEngine：抖动/重试/限流/标记
│   ├── self_comments.py   自己说说评论回复
│   ├── friend_feeds.py    好友动态监控
│   └── external.py        外部空间接力回查
├── core/                  能力层
│   ├── http/              QZone HTTP 客户端（client/feeds/comments/publish）
│   ├── llm/               LLM 能力（personality/formatters/parsers
│   │                      /vision/generators）
│   ├── comment_tree.py    评论树溯源与楼中楼渲染（纯函数）
│   ├── cookie.py          Cookie 获取与持久化
│   ├── interaction_log.py 互动日志（评论过的说说 / 回查时间戳 / 接力计数）
│   ├── reply_tracker.py   评论已回复跟踪（避免重复回复）
│   └── vision_cache.py    识图结果缓存
├── QZONE_API.md           QZone API 文档与实现要点
└── CHANGELOG.md           开发日志
```

---

## 开发与测试

```bash
# 从 Neo-MoFox 仓库根目录运行 FoxZone 离线测试
uv run --no-sync python -m pytest -o addopts= -p no:cacheprovider plugins/foxzone/test -q

# 类型与风格检查
uv tool run ruff check plugins/foxzone
```

测试位于插件目录 `plugins/foxzone/test/`。这些测试使用替身验证请求构造与处理逻辑，不代表真实 QQ 投递或服务端展示结果。

### 读取与回复边界

- 说说详情按 `pos` / `num` 有界分页一级评论并去重；短页不代表结束。`comment_total` / `comment_count` 是服务端统计，`comments` 是当前读到的扁平项数。`comments_complete` 只描述一级评论是否达到服务端总数，不代表 `list_3` 楼中楼完整；`inline_replies_complete` 当前为 `false`。
- LLM 的 `comment_index` 与好友动态的 `reply_comment_index` 都是本次输入列表中从 1 开始的序号，并映射回本次评论身份。好友动态选择评论回复目标后，会在写入前重新读取详情并核实身份唯一；目标不唯一或父级身份不足时不会发送。
- `qzone_post_comment` 的精确回复参数为 `comment_id`、`comment_uin`；二级回复还需提供 `parent_comment_id`、`parent_comment_uin`。参数必须成对且与当前说说评论身份匹配。
- 网络断线、无效回执或缺少新回复 ID 时结果按待核实处理，不报告成功或普通失败来诱发重发；待核实状态按账号、说说主人、父评论及作者作用域保存在 `reply_tracker`。旧版无作用域记录保留为 `legacy`，不推断其账号或评论归属。
- 好友动态每轮从首页重新读取，不复用长游标；图片只收集当前响应中按原图优先的合法 HTTP(S) URL，不保证完整相册或超过 9 张图片的覆盖。长正文及评论树完整性以返回状态为准。

### 文档索引

- [`QZONE_API.md`](QZONE_API.md) — 接口端点、参数、Cookie 流程、详情分页与评论身份、待核实回执及限流处理
- `plans/refactor_foxzone.md`（仓库根）— 架构设计与重构记录
- [`CHANGELOG.md`](CHANGELOG.md) — 关键功能演进记录

---

## 已知限制

- 三条自治闭环不经过 `ON_MESSAGE_RECEIVED` 事件分发，其他依赖该事件的插件无法介入；若有此需求，后续可通过 `event_api` 发布自定义事件实现
- `send_history` 为 FoxZone 私有存储，主 chatter 暂无法直接感知 bot 发过的说说

---

## 协议

随主仓库 LICENSE (AGPL-v3.0) 发布。
