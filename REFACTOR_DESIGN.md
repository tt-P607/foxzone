# FoxZone 插件重构设计文档

> 版本：v2.0 重构方案  
> 生成时间：2026-04-20  
> 状态：待实施

---

## 一、为什么要重构

### 1.1 现存根本问题

**当前的 foxzone 存在"人格分裂"问题**：

- Bot 发了什么说说 → LLM 不知道（没写入任何记忆）
- 有人评论说说 → Bot 用独立的内容生成模块生成回复，与 Bot 当前的对话上下文完全割裂
- 监控好友动态 → 结果只记录在内部日志，Bot 的 LLM 管道根本没有参与
- 用户和 Bot 聊天时，Bot 不知道自己刚才在空间里说了什么

**根本原因**：foxzone 把所有逻辑都放在独立的 Service 内部，没有经过 Chatter（也就是没有经过 LLM 的对话管道），所以和 Bot 的整体认知是断开的。

### 1.2 正确的设计思路

参考 astrbook 插件（论坛通知集成）的架构：

```
外部事件（空间评论/通知）
        ↓
   [Adapter] 轮询并转换为 MessageEnvelope（统一消息格式）
        ↓
   [core_sink] 发送到框架核心管道
        ↓
   [Chatter] 用完整的 LLM 流水线处理（有人格、有历史、有记忆）
        ↓
   [Service] 通过 QZone API 把回复发出去
```

这样，Bot "看到"别人的评论，就和看到聊天消息一样，拥有完整的人格和上下文。

---

## 二、新架构总览

### 2.1 目录结构

```
plugins/foxzone/
├── plugin.py                # 插件入口（精简）
├── manifest.json            # 插件清单
├── config.py                # 配置定义（删除废弃配置节）
├── prompts.py               # 提示词注册
│
├── components/              # 框架可识别的组件（对外暴露）
│   ├── __init__.py
│   ├── adapter.py           # QZoneAdapter：轮询评论 + 好友动态 → 投递到框架管道
│   ├── chatter.py           # QZoneChatter：处理评论/好友动态消息，LLM 决策
│   ├── service.py           # QZoneService（BaseService）：暴露 QZone API
│   ├── actions/
│   │   ├── __init__.py
│   │   ├── send_feed.py     # 发说说（LLM Tool Calling 触发）
│   │   └── read_feed.py     # 读好友说说（LLM Tool Calling 触发）
│   └── commands/
│       ├── __init__.py
│       └── send_feed.py     # 手动发说说命令

└── core/                    # 内部业务逻辑（不是框架组件）
    ├── __init__.py
    ├── api_client.py        # QZoneAPIClient 类（封装所有 QQ 空间 HTTP 请求）
    ├── cookie.py            # CookieManager（Cookie 获取与缓存）
    ├── content.py           # ContentGenerator（LLM 生成说说/提示词）
    ├── reply_tracker.py     # ReplyTracker（已回复评论追踪）
    ├── interaction_log.py   # InteractionLog（好友说说三态追踪：visited/liked/commented）
    └── image/
        ├── __init__.py
        ├── siliconflow.py   # SiliconFlow 图片生成
        └── novelai.py       # NovelAI 图片生成
```

**为什么这样分层？**

- `components/` 放的是框架能"认识"的组件（继承 BaseAdapter / BaseChatter / BaseService 等），框架会自动注册它们
- `core/` 放的是纯粹的内部业务逻辑，不涉及框架，只是普通 Python 类，单元测试更容易

### 2.2 组件关系图

```
plugin.py
  └── FoxZonePlugin（BasePlugin）
       ├── configs: [FoxZoneConfig]
       └── get_components() → [
               QZoneAdapter,      ← 被动监控（评论 + 好友动态）
               QZoneChatter,      ← 评论回复 + 好友动态互动决策
               QZoneService,      ← QZone API 封装
               SendFeedAction,
               ReadFeedAction,
               SendFeedCommand,
           ]

  注：早期设计中存在的 ``QZoneInteractionAgent`` 已被废弃删除，
  外部 chatter 现统一通过 ``qzone_post_comment`` / ``qzone_like`` 等 Tool 直接互动。

QZoneAdapter（双路轮询）
  ├── _poll_loop() 每隔 N 分钟（评论监控）
  │    └── _poll_once()
  │         ├── 检查自己说说下的新评论 → 过滤已回复/过期/bot自身
  │         └── _build_batch_envelope({comment_items}) → core_sink.send()
  │                                                    → QZoneChatter [comment_items 模式]
  └── _friend_monitor_loop() 每隔 M 分钟（好友动态监控）
       └── _friend_monitor_once()
            ├── get_monitor_feeds() → 好友动态
            ├── describe_images()   → 图片视觉描述
            ├── mark_visited()      → 预标记防重入
            └── _build_feed_monitor_envelope({friend_feed_items}) → core_sink.send()
                                                                  → QZoneChatter [friend_feed_items 模式]

两条循环均在每次迭代前检查 _is_dnd_active()（DND 勿扰时间段）

QZoneChatter（双模式处理）
  ├── associated_platforms = ["qzone"]
  ├── 模式 A — comment_items：
  │    ├── service.generate_batch_replies(comment_items)  (LLM)
  │    ├── service.reply_comment()  (逐条)
  │    └── service.mark_comment_replied()
  └── 模式 B — friend_feed_items：
       ├── service.generate_feed_decisions(feed_items)  (LLM)
       ├── service.like(target_qq, tid)   (按决策)
       └── service.comment(target_qq, tid, text)  (按决策)

QZoneService（BaseService）
  ├── service_name = "qzone_service"
  ├── publish_feed(content, images) → bool
  ├── list_feeds(target_qq, num) → list[FeedItem]
  ├── get_monitor_feeds(num) → list[dict]
  ├── has_visited(target_qq, tid) → bool
  ├── mark_visited(target_qq, tid) → None
  ├── comment(target_qq, feed_id, text) → bool
  ├── like(target_qq, feed_id) → bool
  ├── reply_comment(feed_id, commenter_qq, commenter_name, text, comment_tid) → bool
  ├── generate_batch_replies(comment_items) → list[dict]
  ├── generate_feed_decisions(feed_items) → list[dict]
  └── describe_images(image_urls) → str
```

---

## 三、各模块详细设计

### 3.1 `config.py` — 配置

**删除以下废弃配置节**（对应功能已移除）：

- `[schedule]`（ScheduleSection）：所有字段（`enable_schedule`、`random_interval_min/max_minutes`、`forbidden_hours_start/end`）

**保留/修改配置节**：

```toml
[general]
enabled = true
bot_qq = ""                   # Bot 的 QQ 号，用于识别自己的说说

[ai_image]
enable_ai_image = false
provider = "siliconflow"      # siliconflow 或 novelai
image_number = 1

[siliconflow]
api_key = ""
model = "black-forest-labs/FLUX.1-schnell"

[novelai]
api_key = ""
model = "nai-diffusion-4-5-full"
character_prompt = ""
base_negative_prompt = "..."
proxy_host = ""
proxy_port = 0

[monitor]
enable_auto_monitor = true    # 是否启用 Adapter 自动轮询
interval_minutes = 10         # 轮询间隔（分钟）
enable_auto_reply = true      # 是否自动回复自己说说下的评论（由 Chatter 处理）

[cookie]
http_fallback_host = "127.0.0.1"
http_fallback_port = 9999
napcat_token = ""

[llm]
story_model_task = "actor"    # 生成说说用的模型任务（对应 model.toml 中的任务名）
comment_model_task = "actor"  # 评论/回复用的模型任务
```

**导入规范**：

```python
# 正确
from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section

# 错误（旧代码）
from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section
```

---

### 3.2 `components/adapter.py` — QZoneAdapter（新建）

**职责**：定期轮询 QQ 空间，把新评论转换成 `MessageEnvelope` 投递给框架，让 Chatter 处理。

**关键设计点**：

```python
class QZoneAdapter(BaseAdapter):
    adapter_name = "qzone_adapter"
    platform = "qzone"
    
    async def on_adapter_loaded(self) -> None:
        """Adapter 加载时启动轮询任务"""
        # 从 service_manager 获取 QZoneService（依赖注入）
        # 使用 get_task_manager() 创建 daemon 任务
        # 注意：get_task_manager 来自 src.kernel.concurrency（框架没有 API 层封装，这是例外）
    
    async def on_adapter_unloaded(self) -> None:
        """Adapter 卸载时取消轮询任务"""
    
    async def _poll_loop(self) -> None:
        """轮询主循环（在 daemon 任务中运行）"""
        # 1. 若 enable_auto_reply 开启，检查 bot 自己说说下的新评论
        #    已回复的通过 ReplyTracker 过滤
        # 2. 把每条未回复的评论转换为 MessageEnvelope
        # 3. await self.core_sink.send(envelope)
        # 4. asyncio.sleep(interval_seconds)
    
    async def from_platform_message(self, raw: dict) -> MessageEnvelope:
        """将一条评论数据转换为 MessageEnvelope"""
        # group_id = f"qzone_feed_{feed_id}"（把每篇说说当作一个"群组"）
        # user_id = str(commenter_qq)
        # message content = 评论内容（纯文本）
        # extra 字段存放：feed_id、comment_tid、feed_content（说说原文）、commenter_name
    
    async def get_bot_info(self) -> dict:
        """返回 Bot 自身信息（Adapter 协议要求实现）"""
```

**MessageEnvelope 结构**（评论到达时的"消息"）：

```python
# 一条评论 → 一个 MessageEnvelope
# platform = "qzone"
# group_id = "qzone_feed_{feed_id}"    ← 把每篇说说当作一个聊天室
# user_id = str(commenter_qq)
# user_nickname = commenter_name
# text = 评论内容
# extra = {
#     "feed_id": ...,
#     "comment_tid": ...,
#     "feed_content": "说说原文",        ← 供 Chatter 生成回复时参考
#     "commenter_name": ...,
# }
```

**注意**：只有 `enable_auto_reply = True` 时，Adapter 才把评论投递到 Chatter。监控好友动态的点赞/评论操作，暂时仍在 `ReadFeedAction` 中处理（不经过 Chatter），本次重构先不动这部分。

---

### 3.3 `components/chatter.py` — QZoneChatter（新建）

**职责**：处理来自 QZoneAdapter 的评论消息，用 LLM 生成回复，通过 QZoneService 发出。

**Chatter 路由机制（为什么不会和其他 Chatter 冲突）**：

框架的 ChatterManager 根据消息的 `platform` 字段选择 Chatter：
- `QZoneAdapter` 投递的消息带有 `platform = "qzone"`
- `QZoneChatter` 声明 `associated_platforms = ["qzone"]`，所以**只有它**会处理这类消息
- 普通聊天消息（如 `platform = "onebot"`）不会进入 `QZoneChatter`，仍由现有的 Chatter 处理
- 两个 Chatter 完全隔离，互不干扰

**关键设计点**：

```python
class QZoneChatter(BaseChatter):
    chatter_name = "qzone_chatter"
    chatter_description = "QQ 空间评论智能回复 Chatter"
    associated_platforms = ["qzone"]    # 只处理来自 qzone 平台的消息
    
    async def execute(self) -> AsyncGenerator[ChatterResult, None]:
        """主流程"""
        # 1. 从 self.stream_id 获取当前消息
        #    - message = 评论内容
        #    - extra = 说说原文、feed_id、comment_tid 等
        
        # 2. 检查 ReplyTracker，是否已回复过该 comment_tid
        #    如果已回复 → yield Stop()
        
        # 3. 从 model_config 取 comment_model_task 对应的 ModelSet
        
        # 4. 构建 LLM 提示词：
        #    - 人格描述（从 core_config.personality 读取）
        #    - 说说原文（从 extra 获取）
        #    - 评论内容（message 内容）
        #    - 评论者昵称
        
        # 5. 调用 LLM，得到回复文本
        
        # 6. 调用 QZoneService.reply_comment() 发出回复
        
        # 7. 调用 ReplyTracker 标记已回复
        
        # 8. yield Success(message="已回复...")
```

**为什么这是正确的做法**：

- Chatter 在框架内运行，自然拥有完整的 LLM 调用能力
- 未来若要集成记忆插件（如 booku_memory），只需把 Chatter 对应的 stream 纳入记忆范围即可
- 回复逻辑和人格一致性由 LLM pipeline 保证，而不是靠手工拼 prompt

**导入规范**：

```python
# 正确
from src.app.plugin_system.base import BaseChatter, Success, Failure, Stop, Wait
from src.app.plugin_system.api.llm_api import create_llm_request
from src.app.plugin_system.api.log_api import get_logger, COLOR
# LLMPayload, ROLE, Text 等在 llm_api 中透传导出，使用前确认
```

---

### 3.4 `components/service.py` — QZoneService（BaseService 重构）

**职责**：封装所有对 QQ 空间的 HTTP 操作，作为 `BaseService` 供其他组件调用。

**对比旧设计**：

| 旧设计 | 新设计 |
|-------|-------|
| `QZoneService` 是普通类，在 `plugin.py` 手动实例化 | `QZoneService` 继承 `BaseService`，由框架自动注册，其他组件通过 `service_manager.get_service()` 获取 |
| `_get_api_client()` 返回闭包字典，类型不安全 | 内部依赖 `QZoneAPIClient` 类，类型明确 |
| Cookie 失效重试逻辑分散在各处 | 统一在 `QZoneService._with_client()` 中处理 |

**接口设计**：

```python
class QZoneService(BaseService):
    service_name = "qzone_service"
    service_description = "QQ 空间 API 服务"
    
    # ── 给 Action/Chatter/Command 调用的公开方法 ──
    
    async def publish_feed(self, content: str, images: list[bytes]) -> bool:
        """发布一条说说（含图片可选）"""
    
    async def list_feeds(self, target_qq: str, num: int) -> list[dict]:
        """获取指定 QQ 用户的说说列表"""
    
    async def comment(self, target_qq: str, feed_id: str, text: str) -> bool:
        """评论指定说说"""
    
    async def like(self, target_qq: str, feed_id: str) -> bool:
        """点赞指定说说"""
    
    async def reply_comment(
        self,
        feed_id: str,
        owner_qq: str,
        commenter_name: str,
        text: str,
        comment_tid: str,
    ) -> bool:
        """回复说说下的评论"""
    
    async def list_own_feeds_with_comments(self, num: int) -> list[dict]:
        """获取自己最近 N 条说说及其评论（供 Adapter 轮询用）"""
    
    # ── 私有辅助 ──
    
    async def _with_client(self, func, *args, **kwargs):
        """统一处理 Cookie 失效重试（最多重试 1 次）"""
```

**导入规范**：

```python
# 正确
from src.app.plugin_system.base import BaseService
from src.app.plugin_system.api.log_api import get_logger, COLOR
```

---

### 3.5 `core/api_client.py` — QZoneAPIClient（重构）

**职责**：替代旧的闭包字典，封装所有 QQ 空间 HTTP 请求，类型安全，可测试。

**设计**：

```python
class QZoneAPIClient:
    """QQ 空间 HTTP API 客户端（有状态，持有 Cookie/gtk/uin 上下文）"""
    
    def __init__(self, cookies: dict[str, str], gtk: str, uin: str) -> None:
        self._cookies = cookies
        self._gtk = gtk
        self._uin = uin
    
    @staticmethod
    def create(cookies: dict[str, str]) -> "QZoneAPIClient":
        """从 Cookie 字典创建客户端（自动计算 gtk/uin）"""
        p_skey = cookies.get("p_skey") or cookies.get("P_SKEY", "")
        gtk = QZoneAPIClient._generate_gtk(p_skey)
        uin = cookies.get("uin", "").lstrip("o")
        return QZoneAPIClient(cookies, gtk, uin)
    
    @staticmethod
    def _generate_gtk(skey: str) -> str:
        """从 p_skey 计算 gtk 参数"""
    
    async def _request(self, method: str, url: str, ...) -> str:
        """发送 HTTP 请求"""
    
    async def publish(self, content: str, images: list[bytes]) -> bool: ...
    async def list_feeds(self, target_qq: str, num: int) -> list[dict]: ...
    async def comment(self, target_qq: str, feed_id: str, text: str) -> bool: ...
    async def like(self, target_qq: str, feed_id: str) -> bool: ...
    async def reply(self, feed_id: str, owner_qq: str, commenter_name: str, text: str, comment_tid: str) -> bool: ...
    async def list_own_feeds(self, num: int) -> list[dict]: ...
```

**与旧代码的核心区别**：

- 旧代码：`_get_api_client()` 在每次调用时动态生成一堆闭包，打包成 `dict`，类型标注无从写起
- 新代码：`QZoneAPIClient` 是普通类，实例化一次，方法调用清晰，IDE 可以类型推断，单元测试可以 mock

---

### 3.6 `core/cookie.py` — CookieManager（简化）

**职责**：Cookie 的获取、本地缓存和失效清理。

**与旧代码的区别**：

- 删除空桩方法 `_get_from_adapter()`（该方法函数体只有一行日志和 `return None`）
- 调用链变为：本地缓存 → HTTP Napcat 端点（两步，去掉不可用的第三步）
- 存储路径保持不变：`data/foxzone/cookies/cookies-{qq}.json`

---

### 3.7 `core/content.py` — ContentGenerator（简化）

**职责**：通过 LLM 生成说说正文和配图提示词。

**与旧代码的区别**：

- 删除评论/回复生成方法（这部分逻辑移入 `QZoneChatter`，由 Chatter 直接调用 LLM）
- 只保留：
  - `generate_story(topic)` → 生成纯文字说说
  - `generate_story_with_image_info(topic)` → 生成说说+配图提示词

**导入规范**：

```python
# 正确（通过插件传递配置，不直接调全局 get_model_config）
# ContentGenerator.__init__(plugin: FoxZonePlugin)
# 通过 plugin.config.llm.story_model_task 取任务名
# 仍需 from src.core.config import get_model_config（这是目前 API 层的空缺，保持现状，注释标明待改）

# 要修复的
from src.core.prompt import get_prompt_manager    → from src.app.plugin_system.api.prompt_api import _get_prompt_manager
from src.kernel.llm import LLMPayload, ...         → 待确认是否从 llm_api 导出

# 对于 get_core_config().personality
# 可以通过插件配置中加一个 personality_desc 字段（手动配置）替代
# 或者保留 from src.core.config import get_core_config（注释为 "框架内部 API，待 API 层补充封装后更新"）
```

**待解决的 API 层空缺**（不影响本次重构推进，记录在案）：

| 功能 | 当前内部调用 | API 层状态 |
|-----|------------|-----------|
| 获取 LLM ModelSet | `from src.core.config import get_model_config` | 无封装，暂保留内部导入并注释 |
| 获取 Bot 人格配置 | `from src.core.config import get_core_config` | 无封装，暂保留内部导入并注释 |
| 创建 task | `from src.kernel.concurrency import get_task_manager` | 无封装，约定允许 Adapter 使用 |

---

### 3.8 `core/reply_tracker.py` — ReplyTracker（改造存储）

**职责**：记录已回复的评论，防止重复。

**改造点**：

- 旧代码使用 `from src.kernel.storage import json_store`（全局单例，命名空间不隔离）
- 新代码改用 `storage_api.save_json("foxzone", "reply_tracker", data)` / `storage_api.load_json("foxzone", "reply_tracker")`

```python
# 旧（错误）
from src.kernel.storage import json_store
await json_store.save("foxzone_reply_tracker", self._data)

# 新（正确）
from src.app.plugin_system.api import storage_api
await storage_api.save_json("foxzone", "reply_tracker", self._data)
```

---

### 3.9 `components/actions/send_feed.py`

**改造点**：

```python
# 旧（错误）
from src.core.components.base.action import BaseAction
stream_id: str | None = getattr(self, "stream_id", None)

# 新（正确）
from src.app.plugin_system.base import BaseAction
stream_id: str | None = self.stream_id    # BaseAction 上已有此属性，直接访问

# 获取 QZoneService：通过 service_manager 而非 plugin 属性
from src.app.plugin_system.api.service_api import get_service
service = get_service("foxzone:service:qzone_service")
```

---

### 3.10 `components/commands/send_feed.py`

**改造点**：

```python
# 旧（错误）
from src.core.components.base.command import BaseCommand, cmd_route
from src.core.components.types import PermissionLevel

# 新（正确）
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel
```

---

### 3.11 `prompts.py`

**改造点**：

```python
# 旧（错误）
from src.core.prompt import PromptTemplate, get_prompt_manager

# 新（正确）
from src.app.plugin_system.api.prompt_api import register_template
from src.core.prompt import PromptTemplate    # PromptTemplate 类本身暂无 API 封装，保留此导入并注释
```

---

### 3.12 `plugin.py` — 大幅简化

**删除**：

- `_sync_database_schema()`（数据库表已废弃，改用 JSON 存储）
- `_register_scheduled_tasks()`（监控任务移到 QZoneAdapter 管理）
- 所有服务实例属性（`cookie_service`、`reply_tracker` 等）

**保留**：

```python
async def on_plugin_loaded(self) -> None:
    """插件加载完成后的初始化回调"""
    # 1. 注册提示词模板
    register_foxzone_prompts()
    
    # 2. 初始化 ReplyTracker（QZoneAdapter 启动前需要先加载持久化数据）
    #    新方式：ReplyTracker 变成插件属性，供 Adapter 和 Chatter 共用
    self.reply_tracker = ReplyTracker()
    await self.reply_tracker.initialize()
    
    logger.info("FoxZone 插件加载完成")

def get_components(self) -> list[type]:
    return [
        QZoneAdapter,
        QZoneChatter,
        QZoneService,
        SendFeedAction,
        ReadFeedAction,
        SendFeedCommand,
    ]
```

---

## 四、删除清单（死代码）

重构时需要**完全删除**的内容：

| 位置 | 内容 | 原因 |
|-----|-----|-----|
| `models.py` | 整个文件（`FoxZoneScheduleStatus`） | 定时发说说功能已移除，表不再使用 |
| `config.py` | `ScheduleSection` 及 `schedule` 字段声明 | 对应功能已移除 |
| `services/cookie_service.py` | `_get_from_adapter()` 方法 | 空桩，函数体只有 debug 日志和 return None |
| `services/image_service.py` | `generate_images_for_story()` 方法 | 整个插件中无调用，孤立死代码 |
| `plugin.py` | `_sync_database_schema()` 方法 | 数据库表已废弃 |
| `plugin.py` | `_register_scheduled_tasks()` 方法 | 监控任务移入 Adapter |
| `plugin.py` | 所有 `service` 实例属性声明 | 改由 service_manager 管理 |

---

## 五、数据流对比

### 旧流程（当前）：空间评论回复

```
[定时器 while loop（plugin.py）]
        ↓ 每 N 分钟
[QZoneService.monitor_feeds()]
        ↓ 轮询 API
[QZoneService._reply_to_own_feed_comments()]
        ↓ 调用
[ContentService.generate_comment_reply()]
        ↓ 调用 LLM（独立上下文，无人格，无历史）
[QZoneService._api_client["reply"]()]
        ↓
    回复发出

❌ LLM 调用时没有人格注入
❌ Bot 的其他对话不知道这件事发生过
❌ 无法集成记忆插件
```

### 新流程：空间评论回复

```
[QZoneAdapter._poll_loop()]
        ↓ 每 N 分钟，检查自己说说下的新评论
[ReplyTracker.has_replied()] ← 过滤已回复
        ↓ 有新评论
[QZoneAdapter.from_platform_message()]
        ↓ 转换为 MessageEnvelope（platform="qzone"）
[core_sink.send(envelope)]
        ↓ 进入框架管道
[QZoneChatter.execute()]
        ↓ 有完整 LLM 上下文（人格、历史、可接入记忆）
[QZoneService.reply_comment()]
        ↓
    回复发出
[ReplyTracker.mark_as_replied()]

✅ 人格由 LLM pipeline 保证
✅ 未来可以接入记忆插件（booku_memory 等）
✅ Adapter 的 stream 可以像聊天会话一样被管理
```

## 六、对外暴露的接口

这是本次重构的重要设计目标之一：**foxzone 不是一个封闭的孤岛，它需要向整个框架暴露出完整的操作接口**，让任何 Chatter（比如默认聊天 Chatter）都能通过 Tool Calling / Service 调用来操控 QQ 空间。

### 6.1 三种接口层次

| 接口层次 | 使用方 | 调用方式 |
|--------|--------|--------|
| **Action（LLM 工具）** | 任意 Chatter 的 LLM | Tool Calling 自动调用 |
| **Command（指令）** | 用户直接在聊天框输入命令 | 输入 `/foxzone send <主题>` |
| **Service（服务接口）** | 其他插件的代码 | `service_api.get_service("foxzone:service:qzone_service")` |

### 6.2 Action 接口（LLM Tool Calling）

任何 Chatter 在 LLM 响应需要操作 QQ 空间时，都可以调用以下 Action：

| Action | 功能 | 参数 |
|--------|-----|------|
| `SendFeedAction` | 发布一条说说（可带 AI 配图） | `topic: str`（说说主题）, `with_image: bool`（是否生成图） |
| `ReadFeedAction` | 读取好友说说列表 | `target_qq: str`（目标 QQ）, `num: int`（读几条）|

**使用场景举例**：

用户在聊天框对 Bot 说：
- "去发一条关于樱花的说说" → 默认 Chatter 的 LLM 判断应调用 `SendFeedAction(topic="樱花")`
- "帮我看看 xxx 最近发了什么说说" → LLM 调用 `ReadFeedAction(target_qq="xxx", num=5)`

这就是为什么需要 Action 而不是只靠 Chatter：**Action 是暴露给 LLM 的"工具箱"**，任何 Chatter 都能使用，不限于 foxzone 自己的 Chatter。

### 6.3 Command 接口（用户手动命令）

| 命令 | 功能 | 权限 |
|-----|-----|------|
| `/foxzone send <主题>` | 立即发布一条说说 | 管理员 |

这是给管理员手动触发用的，绕过 LLM，直接调用 `QZoneService.publish_feed()`。

### 6.4 Service 接口（插件间通信）

`QZoneService` 作为 `BaseService` 注册后，其他插件可以通过以下方式调用：

```python
# 其他插件的代码中
from src.app.plugin_system.api import service_api
qzone = service_api.get_service("foxzone:service:qzone_service")

# 读取说说
feeds = await qzone.list_feeds(target_qq="12345678", num=10)

# 发布说说
success = await qzone.publish_feed(content="今天天气真好", images=[])

# 评论别人的说说
await qzone.comment(target_qq="12345678", feed_id="abc123", text="好棒！")

# 点赞
await qzone.like(target_qq="12345678", feed_id="abc123")

# 回复评论（QZoneChatter 内部调用）
await qzone.reply_comment(
    feed_id="abc123",
    owner_qq="99999999",
    commenter_name="张三",
    text="谢谢你的评论！",
    comment_tid="tid_xyz",
)

# 获取自己说说及其评论（Adapter 轮询用）
own_feeds = await qzone.list_own_feeds_with_comments(num=5)
```

**`QZoneService` 是整个 foxzone 插件的对外统一出口**，无论是 Action、Chatter 还是外部插件，都通过 Service 与 QQ 空间交互，Service 内部统一处理 Cookie 失效重试、错误日志等。

### 6.5 整体接口架构图

```
其他 Chatter（普通聊天）
  └─ LLM Tool Calling ──→ SendFeedAction / ReadFeedAction
                                      │
                                      ↓
用户 → /foxzone send 命令 ──→ SendFeedCommand
                                      │
                                      ↓
QZoneChatter（回复评论）──────→ QZoneService（统一对外接口）
                                      │
外部插件（插件间通信）────────────────┘
                                      │
                                      ↓
                              QZoneAPIClient（底层 HTTP 客户端）
                                      │
                                      ↓
                              QQ 空间接口（外部）
```

所有操作都汇聚到 `QZoneService`，这样：
- Cookie 失效只在一处处理
- 日志只在一处输出
- 其他插件不需要关心 Cookie/gtk 等底层细节
- 单元测试时只需 mock `QZoneService`

---

## 七、实施步骤（分阶段）

### 第一阶段：修复导入和删除死代码（改动最小，风险最低）

1. 修复所有文件的 `from src.core.*` / `from src.kernel.*` 违规导入
2. 删除 `models.py`（及对应的 `_sync_database_schema` 调用）
3. 删除 `config.py` 中的 `ScheduleSection`
4. 删除 `cookie_service.py` 中的 `_get_from_adapter()`
5. 删除 `image_service.py` 中的 `generate_images_for_story()`
6. 修复 `getattr(self, "stream_id", None)` → `self.stream_id`
7. 修复 JSON 存储使用 `storage_api.save_json/load_json` 替代 `json_store`

### 第二阶段：重构 QZoneAPIClient 和 QZoneService

1. 新建 `core/api_client.py`，将 `_get_api_client()` 闭包字典改为 `QZoneAPIClient` 类
2. 新建 `components/service.py`，实现 `QZoneService(BaseService)`，内部使用 `QZoneAPIClient`
3. 更新 `actions/` 和 `commands/` 通过 `service_manager` 获取 `QZoneService`

### 第三阶段：新增 Adapter 和 Chatter

1. 新建 `components/adapter.py`：`QZoneAdapter` 实现轮询和消息投递
2. 新建 `components/chatter.py`：`QZoneChatter` 实现 LLM 评论回复
3. 简化 `plugin.py`，删除旧的监控逻辑
4. 重组目录结构（`services/` → `core/`，`actions/` 移入 `components/`）

### 第四阶段：整体验证

1. 运行 ruff 检查：`ruff check plugins/foxzone/`
2. 手动测试：发说说、收评论、自动回复
3. 检查 Bot 日志，确认 Chatter 正确接收并处理空间评论

---

## 八、待确认的框架 API 空缺

以下功能暂无 `src.app.plugin_system.api.*` 封装，重构时临时保留内部导入，并在代码注释中标注 `# TODO: 待 API 层提供封装后更新`：

| 功能 | 临时内部导入 | 备注 |
|-----|-----------|-----|
| 获取 LLM 模型任务配置 | `from src.core.config import get_model_config` | 需要 `ModelSet` 对象才能创建 `LLMRequest` |
| 获取 Bot 人格配置 | `from src.core.config import get_core_config` | 用于构建说说/评论的人格提示词 |
| 创建后台任务 | `from src.kernel.concurrency import get_task_manager` | Adapter 轮询任务，约定在 Adapter 中允许使用 |
| PromptTemplate 类型 | `from src.core.prompt import PromptTemplate` | 仅用于 TYPE_CHECKING，不影响运行时 |
