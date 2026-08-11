# FoxZone 开发日志

## 2026-08 减负与适配器化

### Cookie 获取改走适配器

- `core/cookie.py`：删除 Napcat HTTP 端点逻辑，改为经已启动适配器透传 `get_cookies` action（OneBot / SnowLuma 同命令名）
- 新增 `parse_cookie_string()` 纯函数；`adapter_signature` 留空自动探测（优先 snowluma → onebot），或显式指定
- `config.py [cookie]`：移除 `http_fallback_host` / `http_fallback_port` / `napcat_token`，新增 `adapter_signature` / `domain` / `request_timeout`
- `manifest.json` 新增 `adapter_api` 声明；`README.md` / `QZONE_API.md` Cookie 章节同步

### 删除生图模块（大幅减负）

- 删除 `core/image/`（dispatcher / novelai / siliconflow / openai / provider 共约 1150 行）
- 删除 `qzone_start_compose_feed` / `qzone_submit_feed` 两个 Tool 与 `FEED_GUIDANCE_TEMPLATE`、三个 `IMAGE_GUIDANCE_*`
- `components/service.py`：移除 `publish_feed_with_image_info` / `compose_feed_guidance` / `_build_generated_feed` / `_generate_image_from_info` / `_load_image_bytes`；`publish_generated_feed` 简化为纯文本生成
- `core/llm/generators.py`：移除 `generate_story_with_image_info`；`core/llm/parsers.py`：移除 `parse_story_with_image_json`
- `config.py` / `config.toml`：移除 `[ai_image]` / `[siliconflow]` / `[novelai]` / `[openai]` 与 `general.expose_feed_write_tools`
- `manifest.json` 移除两个 Tool 的 include 声明
- 底部 `publish()` 仍保留 `images` 参数（底层带图能力不变，仅上层不再自动生图）

### 删除记忆集成（去耦合）

- 删除 `core/llm/memory_tools.py` 与 `MemorySection` / `enable_memory_integration` 配置
- `core/llm/generators.py`：移除 `_send_prompt_with_memory_tools`，三个 LLM 入口改回 `_send_prompt`
- 同步清理 `README.md` / `config.toml` 记忆相关段落

### 外部回查独立开关（防风控）

- 新增 `monitor.enable_external_followup`（默认 `false`），把外部空间评论回查从 `enable_auto_reply` 中剥离独立控制
- `autopilot/scheduler.py`：外部回查改为独立开关驱动；开启时默认降频（间隔 60 分钟、每轮 1 个 QQ）
- 越层导入清理：`personality.py` 改用 `config_api.get_core_config()`；`manifest.json` 移除无用 `media_api` 与 `Pillow` 依赖

### 消除 bot_qq 配置冗余

- 删除 `general.bot_qq` 配置字段（core.toml 不含 bot QQ 号，其真源在适配器配置）
- `runtime.bot_qq()` 改为异步，经 `adapter_api.get_bot_info_by_platform("qq")` 从已启动的 QQ 适配器动态获取并缓存；全部 7 处调用点同步

### 多模态模式

- 新增 `llm.multimodal_mode`（默认 `false`）：开启后，评论回复 / 好友互动决策时把说说图片直接作为图像 payload 传给模型，不再先经 VLM 识图生成文本描述
- `core/llm/vision.py`：新增 `download_images()`（URL → base64 data）
- `core/llm/generators.py`：`_send_prompt` 支持 `images` 参数；`generate_batch_replies` / `generate_feed_decisions` 按开关分流
- `autopilot/friend_feeds.py`：多模态模式下跳过 `fill_image_text`

### 修复轮询启动过早（适配器未就绪时误跑）

- `autopilot/scheduler.py`：三条循环首次执行前先等待 QQ 适配器启动就绪（`_wait_for_adapters`，最长 60s），避免适配器尚未就绪时白跑一次并误报 Cookie 失败
- `autopilot/scheduler.py`：Bot QQ 号改为每次轮询时动态获取（`_once_with_bot_qq`），不再启动阶段预取，避免适配器未就绪时取到空串并长期失效
- `runtime.py`：`bot_qq()` 仅在取回非空 QQ 号时缓存，失败不缓存空串，便于后续重试
- `core/cookie.py`：新增 `has_adapter()` 供就绪判定；日志泛化，不再出现具体适配器名，统一「通过适配器获取 Cookie」，并明确打印成功 / 失败结果
- `core/cookie.py`：Cookie 适配器探测改为遍历所有已启动且支持 API 透传的适配器，首个成功即返回，移除固定签名顺序（snowluma → onebot）
- `runtime.py` / `config.py` / `config.toml` / `README.md`：同步泛化适配器描述文案

### Cookie 获取退避重试（解决适配器 WS 未就绪）

- 根因：适配器虽已启动并进入活跃列表，但 WebSocket 长连接可能尚未建立，`get_cookies` 透传会抛「WebSocket 连接未建立」
- `core/cookie.py`：`_get_from_adapter` 增加退避重试，默认首次尝试 + 重试 3 次，等待 3/6/9 秒递增；每次重试前重新探测已启动适配器
- `config.py [cookie]`：新增 `retry_times`（默认 3）、`retry_base_delay`（默认 3.0），`config.toml` 同步
- 空 QQ 号时不再读写本地文件缓存，避免生成 `cookies-.json`

### 补测试

- 新建 `test/plugins/foxzone/test_cookie_service.py`（13 个用例：解析 / 缓存 / 适配器多源回退 / 签名解析 / 失效清理）
- 新建 `test/plugins/foxzone/test_scheduler.py`（5 个用例：外部回查独立开关 / 默认关 / 停启）
- 新建 `test/plugins/foxzone/test_vision.py`（4 个用例：图片下载 / 失败跳过）

## 2026-07 v3.0 完全重构（自治闭环架构）

### 背景

插件内并存两套架构：`REFACTOR_DESIGN.md`（v2.0）设计的 Adapter→Envelope→Chatter 链路自 2026-03 迁移后已 100% 死代码，与 `service.process_*_batch` 闭环逐行重复且开始漂移；`QZoneService` 1301 行承担 7 类职责；框架 `ServiceManager.get_service()` 非单例导致持久化状态重复读盘、`InteractionLog` 并发写覆盖；模块级 `_REPLY_SEND_LOCK` 缺 `try/finally` 存在锁泄漏。详细审查与设计见仓库根 `plans/refactor_foxzone.md`。

### 变更

**新架构四层**：`components/`（对外契约）→ `runtime.py`（状态单例）→ `autopilot/`（自治闭环）→ `core/`（纯能力）。

- 新增 `runtime.py`：`QZoneRuntime` 插件级单例，统一持有 cookie / reply_tracker / interaction_log / vision_cache / image_dispatcher / content / 发送串行锁；`_with_client` Cookie 重试迁入
- 新增 `autopilot/`：`Autopilot` 调度器（DND + 三条循环，脱离 `BaseAdapter`）+ `BatchSendEngine`（抖动/重试/限流分类，`async with` 持锁修复锁泄漏）+ 三条流程模块
- `core/api_client.py`(1090行) 拆分为 `core/http/`（client/feeds/comments/publish，mixin 组合，对外仍是单一 `QZoneAPIClient`）
- `core/content.py`(1180行) 拆分为 `core/llm/`（personality/formatters/parsers/memory_tools/vision/generators）
- 新增 `core/comment_tree.py`：合并 service 与 content 两份评论树溯源实现
- `components/service.py` 瘦身为原子门面（1301→约 470 行），批量编排全部迁出
- 两套识图实现（service 自建 aiohttp 版 / content media_api 版）统一为 `core/llm/vision.py`（保留 vision_cache 持久化缓存）
- `ContentService` 改为持有 runtime，消除 `get_service()` 反查循环依赖

**删除**：`components/chatter.py`（368 行死代码）、`components/adapter.py`（envelope 链路死代码，调度部分迁入 autopilot）、`REFACTOR_DESIGN.md`（被 plans/refactor_foxzone.md 取代）、`IMAGE_SCENE_DESC_TEMPLATE` 与 `foxzone.image.scene_desc` 模板、全部无调用者方法（`has_visited`/`mark_visited`/`has_liked`/`has_commented`/`iter_commented*`/`get_replied_comments`/`remove_reply_record`/`clear_feed_records` 等）、`dist/` 构建产物

**修复**：
- 发送锁泄漏（模块级锁 + 手动 acquire/release → runtime 实例锁 + `async with`）
- `InteractionLog` 并发写覆盖 race（状态单例化后从根上消除）
- `plugin.py` 无效预热（旧实例初始化后即被丢弃 → runtime 真正全程复用）
- 3 处 `getattr` 滥用改为直接属性访问
- `_COMMENT_URL`/`_REPLY_URL` 重复常量与错误注释（两接口本就同端点）
- 越层导入（`ToolRegistry` 改经 `llm_api.create_tool_registry`；`ComponentType` 改经 `plugin_system.types`）

**行为变化**：
- 好友监控评论路径的限流处理与外部接力路径统一由 `BatchPolicy.stop_batch_on_rate_limit` 区分（前者终止整批、后者单条跳过），语义与旧实现一致
- `qzone` 平台注册随 Adapter 删除而消失（原唯一消费者是死代码 Chatter）；若将来需要让其他插件感知 QZone 事件，应经 `event_api` 发布自定义事件

## 2026-05 booku_memory 组件集成（单一开关版）

### 背景

之前讨论的"做成 Chatter 是为了能用记忆插件"这一假设需要纠偏：booku_memory 的 `MemoryFlashbackInjector` 通过 `on_prompt_build` 事件硬编码只识别 `default_chatter_user_prompt` 一个目标模板，所以 FoxZone 即便走 Chatter 链路也拿不到自动闪回。真正能让"说说写记忆 / 评论前读记忆"落地的路径，是在 FoxZone 自己的 LLM 调用里把 booku_memory 注册的组件暴露给 LLM，由 LLM 自主决定何时读写。

### 设计原则

**FoxZone 不僭越 booku_memory 的模式选择。** 记忆插件用轻量 Tool（`memory_read` / `memory_write`）还是 Agent（`memory_read_agent` / `memory_write_agent`），完全由其自身 `enable_lite_mode` 配置决定。FoxZone 只提供"是 / 否暴露"一个开关：

- 配置仅暴露一个布尔字段 `memory.enable_memory_integration`（**默认 `true`**）
- 通过 `ComponentRegistry.get_by_plugin_and_type("booku_memory", ComponentType.TOOL/AGENT)` 自动扫描 booku_memory 当前注册的所有 Tool 与 Agent 组件，无需用户在 FoxZone 这边维护签名列表
- 用 `get_plugin("booku_memory")` 拿到目标插件实例，再 `cls(owner_plugin)` 实例化组件 —— 这样 `self.plugin.config` 拿到的是 booku_memory 真实配置而非 FoxZone 配置
- LLM 多轮 tool call 循环手写在 `ContentService._send_prompt_with_memory_tools`，最大轮次为内部常量 `_MEMORY_MAX_TOOL_CALL_ROUNDS = 3`，不暴露给配置；不复用框架 `run_tool_call`（后者强依赖 `trigger_msg`，FoxZone 写说说/评论决策没有触发消息）
- 任何前置条件不满足（开关关闭 / 插件未加载 / 未注册任何 Tool 与 Agent）一律静默回退到原 `_send_prompt`，保证零破坏

### 涉及文件

- `plugins/foxzone/config.py`：新增 `MemorySection`，**仅一个字段** `enable_memory_integration: bool = True`
- `plugins/foxzone/core/content.py`：
  - 新增 imports：`get_plugin`、`get_global_registry`、`ToolRegistry`、`ToolResult`、`ClassVar`、`ComponentType`（lazy）
  - 新增类常量 `_MEMORY_PLUGIN_NAME` / `_MEMORY_MAX_TOOL_CALL_ROUNDS`
  - 新增 `_resolve_memory_tools()` / `_stringify_tool_result()` / `_send_prompt_with_memory_tools()`
  - 4 个 LLM 入口（`generate_story` / `generate_with_image` / `generate_batch_replies` / `generate_feed_decisions`）切到带记忆组件版本
- `test/plugins/foxzone/test_smoke.py`：补 4 个测试（开关关闭 / 插件未加载 / 序列化 / fallback）

### 已知限制

- `MemoryFlashbackInjector` 闪回机制只对 `default_chatter_user_prompt` 生效，对 FoxZone 模板无效——这是 booku_memory 自身实现的限制，本次集成不解决（用户明确要求不改记忆插件）
- 外部接力评论路径（`adapter._external_followup_once`）仍走 `task_manager.create_task` 后台闭环，不经过 envelope→Chatter；其他基于 `ON_MESSAGE_RECEIVED` 的插件介入这条路径仍需后续单独评估
- `memory_read` 的检索质量取决于 booku_memory 配置的检索任务模型与历史数据完整度

## 2026-04 提示词外置到 config.toml

### 背景

`prompts.py` 内含 5 个核心 LLM 提示词模板（说说 / 评论 / 回复 / 批量决策 / 接力评论）+ 1 个共用评论规范段，全部硬编码在代码里。用户想直接在配置文件里调整人设语气、规则措辞，不必改代码。

### 设计

**配置文件是唯一真源，代码内零提示词字符串。** 采用 KFC 模式：

- 模板默认值**仅**写在 `config.py` 的 `PromptsSection` 字段 `Field(default="""...""")` 中，由 `ConfigBase` 框架在首次加载/字段缺失时自动落盘到 `config.toml [prompts]` 节
- `prompts.py` 内**不再**存有任何说说相关提示词字符串；只保留图像引擎指引段（`IMAGE_GUIDANCE_*`）、发说说外部指引模板（`FEED_GUIDANCE_TEMPLATE`）、图片场景描述模板（`IMAGE_SCENE_DESC_TEMPLATE`）—— 这些与生图引擎/Tool schema 强绑定，不属于"用户可调提示词"
- `register_foxzone_prompts(config)` 直接从 `config.prompts.*` 读取并注册到 PromptManager，**无 fallback**
- 共用规范段（`comment_guidelines`）通过模板内 `__GUIDELINES__` 占位符替换，改一处全部生效
- `QZoneCommentTool.tool_description` 中的"评论规范段"是 Tool 的硬编码 schema 合同（给 LLM 看的调用约束），与用户可调提示词分离，字面写在 `comment.py` 的 `_TOOL_COMMENT_GUIDELINES`

### 涉及文件

- `plugins/foxzone/config.py`：新增 `PromptsSection`，6 个字段 `Field(default="""完整模板""")`
- `plugins/foxzone/prompts.py`：删除所有 `_DEFAULT_*` / `QZONE_COMMENT_GUIDELINES` / `ensure_prompts_in_toml` / `_resolve` fallback；`register_foxzone_prompts(config)` 改为强制依赖 config
- `plugins/foxzone/plugin.py`：移除 `ensure_prompts_in_toml` 调用，只保留 `register_foxzone_prompts(self.config)`
- `plugins/foxzone/components/tools/comment.py`：`_TOOL_COMMENT_GUIDELINES` 字面拼到 `tool_description`（与 config 解耦）

### 用法

首次启动后 `config/plugins/foxzone/config.toml` 由配置框架自动写入：

```toml
[prompts]
comment_guidelines = """..."""
story_generate = """..."""
comment_generate = """..."""
comment_reply = """..."""
comment_reply_batch = """..."""
friend_feed_interact = """..."""
```

直接修改任何字段，重启插件即生效。

### 注意

- 模板里的 `{xxx}` 是 PromptTemplate 渲染时由代码注入的变量名（不要改）
- `{{ }}` 表示字面 `{ / }`（在 JSON 示例里常用，不要改）
- `__GUIDELINES__` 占位符会在 register 时被替换成 `comment_guidelines` 的实际文本

---

## 2026-04 防双 bot 死循环：同 feed 接力上限

### 背景

外部接力闭环上线后存在风险：若两个 bot 都安装本插件并互为好友，对同一条说说会无限"左脚踩右脚"对答下去（A 评论 → B 接力 → A 接力 → B 接力 …），既消耗 token 也容易触发 QZone 风控。

### 方案

新增 `monitor.external_followup_max_replies_per_feed` 配置（默认 5，0 为不限），表示 bot 在**同一条好友说说**下累计接力 reply 次数上限。

实现要点：

- `core/interaction_log.py`：在每条 `(target_qq, feed_id)` 记录上新增 `external_reply_count` 字段，提供 `get_external_reply_count` / `increment_external_reply_count` 接口
- `components/service.py::process_external_followup_batch`：
  - **LLM 决策前**先按 `external_reply_count >= max` 过滤掉已达上限的 comment（避免无谓的 LLM 调用）
  - 每次成功 reply 后调用 `increment_external_reply_count` 并立即持久化
  - 达到上限后日志 WARNING 提示，下一轮回查就不再向 LLM 提交
- `config.toml` 同步默认配置注释

### 注意

- 上限按 (host_qq, feed_id) 维度计算，**不区分对方是谁**——任何一条评论占一次配额
- 达到上限的 feed 仍会被回查并 `mark_followup_checked`（避免轮转死锁），只是不会再走决策/发送
- 新增的 `external_reply_count` 字段对老数据兼容（缺失视为 0）

---

## 2026-04 修复 reply 接口 -10049 反爬触发

### 现象

`emotion_cgi_re_feeds` reply 接口持续返回：

```json
{"code": -10049, "message": "使用人数过多，请稍后再试", "subcode": 1012}
```

- 多账号、多场景复现，朋友账号**首次使用**就 -10049（不是累积限流）
- 浏览器/QQ 客户端手动回复同一条评论 100% 成功
- cookies 完整、g_tk 计算正确、URL 路径正确
- 错误响应里 `feeds` 字段回声了我们传的 `t1_uin/t1_tid/t2_uin/t2_tid`，证明**协议层字段值都对**

### 真因

通过浏览器 F12 DevTools 抓一次成功的 reply 请求 cURL，与我们的请求逐字段对照，发现 6 处差异，其中前 2 项致命：

| 字段 | 浏览器 | 我们（错） | 影响 |
|---|---|---|---|
| URL 子域 | `user.qzone.qq.com` | `h5.qzone.qq.com` | 反爬触发 |
| `commentUin` | **bot 自身 QQ（操作者）** | 评论作者 QQ | **核心字段语义错** |
| `paramstr` | `1` | `2` | 反爬触发 |
| `isSignIn` | 空字符串 | 缺失 | 字段缺失 |
| `content` | `@{uin:xxx,nick:xxx,auto:1} <内容>` | 纯文本 | 缺 @ 提及格式 |
| `qzreferrer` | `.../<uin>` | `.../<uin>/main` | 路径错 |

**关键认知更正**：之前以为 `commentUin` 是"被回复的评论作者 QQ"，并且因为这个误解走了一段弯路（一度怀疑是顶层评论作者 uin）。实际上它是**操作者 uin**，永远等于 `self._uin`。被回复者的身份信息通过 `content` 里的 `@{uin:...,nick:...,auto:1}` 提及前缀传递。

QZone 的 `-10049` 错误是反爬伪装：当请求字段组合与浏览器实际请求不一致时，服务端不会明示是哪个字段错，而是统一返回 `-10049` 让爬虫无从下手。

### 修复

- `plugins/foxzone/core/api_client.py`：
  - `_REPLY_URL` 子域 `h5.qzone.qq.com` → `user.qzone.qq.com`
  - `commentUin` 改为 `self._uin`
  - `paramstr` `2` → `1`
  - 新增 `isSignIn=""`
  - `content` 改为 `f"@{{uin:{commenter_qq},nick:{target_name},auto:1}} {content}"`
  - `qzreferrer` 去掉 `/main`
  - headers 补齐 `Accept`、`Accept-Language`、`Sec-CH-UA-*`、`Sec-Fetch-Site: same-origin`

详见 [QZONE_API.md](QZONE_API.md) §3.5。

### 验证

线上重启后两条接力回复连续 `code=0` 成功，QZone 说说下确实出现 bot 回复。

### 经验

- QZone 的 `-10049` 不一定是限流，**首先怀疑请求特征不像浏览器**
- 字段语义不能靠"猜+复用 astrbot"，**必须以浏览器抓包为权威**
- 接口字段名（`commentUin`）有歧义时，从字面意思推断很容易错；以 cURL 实测为准

---

## 2026-04 外部回查微调

- 删除临时调试白名单 `_DEBUG_ONLY_HOST_QQ`，回查全量 QQ
- QQ 之间调用 `list_feeds` 加 3-8s 随机抖动，避免短时间集中调用
- **接力 reply 成功后续期 `last_ts`**：让 `external_followup_max_feed_age_hours`（默认 72h）从"最后一次互动"算起，避免持续对话中的 feed 被过滤掉

---

## 2026-03 外部接力闭环迁移

将"外部说说接力评论 / 好友说说监控 / 自己说说评论轮询"三条路径全部迁移到 `service.task_manager` 闭环，不再走 ChatStream + EventBus（之前 5s 超时频繁触发）。

详见 `plugins/foxzone/components/service.py` 中：
- `process_external_followup_batch`
- `process_feed_monitor_batch`
