# FoxZone（墨狐空间）

将 Bot 接入 QQ 空间，实现说说自动发布、好友动态监控、评论自动互动与「他人空间下的接力回复」。基于 [Neo-MoFox](https://github.com/MoFox-Studio/Neo-MoFox) 框架的插件层 API（`src.app.plugin_system.api.*`）实现，不直接依赖框架内部模块。

> 此插件需配合 [napcat](https://github.com/NapNeko/NapCatQQ) 或同类提供 Cookie 抓取能力的 QQ 协议端使用。

---

## 功能概览

| 模块 | 说明 |
|------|------|
| **发布说说** | LLM 生成正文，可选 AI 配图（SiliconFlow / NovelAI / OpenAI 兼容端点） |
| **读取动态** | 拉取自己/好友的最近说说，提取文本与图片描述供 LLM 使用 |
| **评论与点赞** | 提供 `qzone_post_comment` / `qzone_like_feed` 工具供 LLM 自主调用 |
| **自动回复** | 监听自己说说下的评论，由 `QZoneChatter` 决策回复内容并发送 |
| **好友动态监控** | 周期性扫描好友 timeline，决策点赞/评论/跳过 |
| **外部接力回复** | 检查 Bot 在他人说说下评论后，是否有人回复 Bot 的评论；命中则继续接力 |
| **记忆集成** | 默认接入 `booku_memory` 暴露的 Tool/Agent，写说说与评论决策时可读写长期记忆 |

---

## 组件清单

| 类型 | 名称 | 说明 |
|------|------|------|
| `service` | `qzone_service` | QQ 空间统一服务出口（封装 HTTP 调用、互动日志、回复跟踪） |
| `adapter` | `qzone_adapter` | 调度自动监控/外部回查循环，向框架投递事件与消息 |
| `chatter` | `qzone_chatter` | 自有 Chatter，处理评论回复与好友动态决策 |
| `command` | `foxzone` | 管理与调试命令（包含 `/foxzone send_feed` 等） |
| `tool` | `qzone_read_feed` | 读取说说列表 |
| `tool` | `qzone_post_comment` | 发表评论 |
| `tool` | `qzone_like_feed` | 点赞 |
| `tool` | `qzone_start_compose_feed` | 开始撰写说说（生成草稿） |
| `tool` | `qzone_submit_feed` | 提交已撰写好的说说 |
| `config` | `config` | 插件配置（见下方「配置」） |

---

## 安装与启用

1. 把本目录放入主程序的 `plugins/foxzone/`
2. 依赖声明在 `manifest.json` 中，系统将自动尝试安装，或可手动运行安装：
   ```bash
   uv pip install aiohttp beautifulsoup4 json5 orjson Pillow
   ```
3. 在 `config/plugins/foxzone/config.toml` 中至少填写：
   ```toml
   [general]
   enabled = true
   bot_qq = "你的 QQ 号"
   ```
4. 在 `config/model.toml` 中确认 `actor`、`vlm` 等任务已注册（任务名可在配置中切换）
5. 启动主程序：`uv run main.py`

---

## 配置（节选）

完整字段见 [`config.py`](config.py)，每节都带 `description`，TOML 支持热重载。

```toml
[general]
enabled = true
bot_qq = ""                       # 必填：Bot 自身 QQ
expose_feed_write_tools = true    # 是否给 LLM 暴露写说说 Tool

[llm]
story_model_task = "actor"        # 生成说说正文用的模型任务
comment_model_task = "actor"      # 生成评论/回复用的模型任务
vision_model_task = "vlm"         # 识图模型任务，置空则跳过识图

[memory]
enable_memory_integration = true  # 默认接入 booku_memory 的 Tool/Agent

[monitor]
enable_auto_monitor = true        # 评论自动回复轮询
interval_minutes = 10
max_comment_age_hours = 72.0
enable_friend_monitor = false     # 好友动态主动互动
friend_monitor_interval_minutes = 30
external_followup_minutes = 20    # 外部接力回查间隔
external_followup_batch = 2       # 每轮最多检查多少个 (qq, feed)
external_followup_max_replies_per_feed = 5  # 同 feed 接力上限，防双 bot 死循环
dnd_enabled = false
dnd_start_hour = 23
dnd_end_hour = 7

[image]
enabled = false                   # 是否给说说配图
provider = "siliconflow"          # 或 "novelai" / "openai"
# ... 各 provider 子节见 config.py
```

---

## 关键文件

```
foxzone/
├── plugin.py              插件入口与生命周期
├── config.py              配置定义（ConfigBase / SectionBase）
├── prompts.py             LLM 提示词模板，向 PromptManager 注册
├── manifest.json          插件元数据
├── components/
│   ├── service.py         QZoneService：发说说、评论、回复、互动日志
│   ├── adapter.py         QZoneAdapter：自动监控与外部回查循环
│   ├── chatter.py         QZoneChatter：评论回复与好友动态决策
│   ├── commands/          管理命令（如 /foxzone send_feed）
│   └── tools/             暴露给 LLM 的工具组件
├── core/
│   ├── api_client.py      QZone HTTP API 封装
│   ├── content.py         LLM 调用封装（含 booku_memory 工具循环）
│   ├── cookie.py          Cookie 获取与持久化
│   ├── interaction_log.py 互动日志（评论过的说说 / 回查时间戳）
│   ├── reply_tracker.py   评论已回复跟踪（避免重复回复）
│   ├── vision_cache.py    识图结果缓存
│   └── image/             AI 配图 provider 集合
├── QZONE_API.md           QZone API 文档与实现要点
├── REFACTOR_DESIGN.md     架构演进记录
└── CHANGELOG.md           开发日志
```

---

## 开发与测试

```bash
# 单元测试（覆盖 components / tools / API client / chatter / service）
uv run pytest test/plugins/foxzone -q

# 类型与风格检查
uv tool run ruff check plugins/foxzone
```

测试位于仓库根 [`test/plugins/foxzone/`](../../test/plugins/foxzone)。

### 文档索引

- [`QZONE_API.md`](QZONE_API.md) — 接口端点、参数、Cookie 流程、外部回查实现要点（msgdetail_v6 严格参数、`-10049` 限流处理、模块级锁串行化等）
- [`REFACTOR_DESIGN.md`](REFACTOR_DESIGN.md) — 当前模块边界与重构思路
- [`CHANGELOG.md`](CHANGELOG.md) — 关键功能演进记录

---

## 已知限制

- `booku_memory` 的 `MemoryFlashbackInjector` 仅识别 `default_chatter_user_prompt`，FoxZone 模板不在闪回范围内（不影响主动调用记忆 Tool）
- `QZoneService` 是非单例（每次 `get_service` 新建实例），`interaction_log.json` 在并发回查间存在 race，已用 reply 成功路径上的 `mark_comment_replied` 作最终一致性兜底
- 外部接力回复路径不经过 `ON_MESSAGE_RECEIVED` 事件分发，其他依赖该事件的插件无法介入

---

## 协议

随主仓库 LICENSE (AGPL-v3.0) 发布。
