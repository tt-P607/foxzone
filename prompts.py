"""墨狐空间提示词模板注册。

5 个核心 PromptTemplate（说说写作 / 评论生成 / 评论回复 / 评论批量决策 /
好友说说互动）以及统一的评论规范段，**全部模板文本**仅存在于
``config.py`` 的 ``PromptsSection`` 中（``Field(default=...)``），由配置框架
自动落盘到 ``config/plugins/foxzone/config.toml`` 的 ``[prompts]`` 节。

本文件只保留：
  * 图像引擎指引段（按 provider 静态选择，与配置无关）
  * 发说说外部指引模板（``FEED_GUIDANCE_TEMPLATE``）
  * 图片场景描述模板（``IMAGE_SCENE_DESC_TEMPLATE``）
  * ``register_foxzone_prompts(config)``：从 config 读取 6 个字段，
    把模板内 ``__GUIDELINES__`` 占位符替换成 ``comment_guidelines`` 实际文本，
    再注册到全局 ``PromptManager``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import COLOR, get_logger
from src.app.plugin_system.api.prompt_api import register_template
from src.app.plugin_system.types import PromptTemplate

if TYPE_CHECKING:
    from .config import FoxZoneConfig

logger = get_logger("foxzone.prompts", color=COLOR.ORANGE)

# =============================================================================
# 图像引擎指引段（不外置到 config.toml，仅生图路径使用，按 provider 动态选）
# =============================================================================

#: NovelAI 引擎的图像指引段。
IMAGE_GUIDANCE_NOVELAI: str = """\
（生图引擎：NovelAI）

image_info 结构：
```json
{{
  "prompt": "<英文 NovelAI tags>",
  "negative_prompt": "<只写追加负面词，可省略>",
  "aspect_ratio": "方图"
}}
```

{style_block}

{base_neg_block}

NovelAI prompt 用英文 tag，半角逗号分隔，不要写完整句子。推荐顺序：
1. 主体数量/性别：`1girl`, `1boy` 等。
2. 外貌与服装：按你自己的人设写发色、瞳色、服饰、气质；不要把画风锚点重复写进 prompt。
3. 构图：优先自然近景，如 `upper body`, `cowboy shot`, `selfie`, `candid`, `from above`。
4. 表情动作：如 `looking at viewer`, `slight smile`, `hand on cheek`, `relaxed pose`。
5. 场景光线：按正文氛围写地点、时间、天气、`soft lighting`, `golden hour`, `night lighting` 等。
6. 镜头氛围：如 `shallow depth of field`, `film grain`, `snapshot`, `slice of life`。

`selfie` 只表示自拍视角，不等于画手机；不要写 `holding phone`, `phone in hand`, `mirror selfie`，除非正文真的需要。
权重只给最关键的 2~3 个 tag，例如 `1.2::soft lighting::`、`0.8::heavy bokeh::`。
如果引用特定作品/角色风格，用 Danbooru 常见写法，如 `character name (work name)`。
"""

#: SiliconFlow（FLUX 系列）引擎的图像指引段。
IMAGE_GUIDANCE_SILICONFLOW: str = """\
（生图引擎：SiliconFlow / FLUX）

image_info 结构：
```json
{{
  "prompt": "<英文描述，可混合短句与关键词>",
  "aspect_ratio": "方图"
}}
```

SiliconFlow 走 FLUX 系列文生图模型，prompt 风格更自由：
- 推荐英文，自然语言短句 + 关键标签混合，比纯 tag 效果更稳。
- 重点描述：主体外貌/动作 → 场景环境 → 光线氛围 → 镜头视角，长度 50~100 词为宜。
- 不要写 `negative_prompt` 字段（系统已默认过滤低质量与不良内容）。
- 风格关键词放结尾，例如 `anime style`, `photo realistic`, `illustration`, `cinematic lighting`。
- 不必使用 NovelAI 的 tag 权重语法（`1.2::xxx::`），FLUX 不识别。
"""

#: OpenAI 兼容 chat-completion 协议的图像指引段。
IMAGE_GUIDANCE_OPENAI: str = """\
（生图引擎：OpenAI 兼容 chat 协议）

image_info 结构：
```json
{{
  "prompt": "<自然语言描述，中英文均可>",
  "aspect_ratio": "方图"
}}
```

OpenAI 兼容接口走自然语言：
- 直接用一段中文或英文描述画面，**不要堆砌 tag**，**不要写权重语法**。
- 推荐写法：「一张 XX 风格的图：主体是…，正在做…，场景是…，氛围/光线…，镜头/视角…」。
- 控制在 30~80 字，过长容易被忽略；过短画面会模糊。
- 不要写 `negative_prompt` 字段（OpenAI 协议不支持，写了无效）。
- 画幅由 ``aspect_ratio`` 控制：方图(1:1) / 竖图(9:16) / 横图(16:9)。
"""

#: ``qzone_start_compose_feed`` Tool 返回给外部 Chatter 的发说说指引模板。
FEED_GUIDANCE_TEMPLATE: str = """\
==== 发说说指引（请认真阅读后再调用 qzone_submit_feed）====

像平常随手发动态那样写，不要像作文、公告或剧情旁白。
- 1~3 句就够，允许碎碎念、半句话、轻轻吐槽。
- 重点是“今天/此刻我想说点什么”，不用把情绪解释完整。
- 可以参考你自己最近发过的 QQ 空间动态来延续生活感，但别重复同一题材。

【你自己最近发过的 QQ 空间动态】
{recent_block}

【配图】
{ai_image_section}

只有这条动态自然适合“人物照 / 自拍感 / 含人物生活场景”时才传 image_info。
纯风景、纯静物、纯抒情碎语，直接省略 image_info。

{provider_guidance_block}

写好后直接调用 `qzone_submit_feed(content=..., image_info=...)`；不配图就只传 content。
"""

#: 图片场景描述生成模板（用于生图路径，未外置到 config.toml）。
IMAGE_SCENE_DESC_TEMPLATE: str = (
    "根据以下说说内容，生成一段适合 AI 绘图的英文场景描述。\n\n"
    "说说内容：\n"
    "---\n"
    "{story_content}\n"
    "---\n\n"
    "要求：\n"
    "1. 纯英文，描述画面主体、环境、氛围和光线。\n"
    "2. 不超过 80 个词。\n"
    "3. 适合作为文生图模型的 prompt，内容健康积极。\n"
    "4. 只输出英文 prompt 文本，不含任何其他内容。"
)

# =============================================================================
# PromptManager 注册
# =============================================================================


def register_foxzone_prompts(config: "FoxZoneConfig") -> None:
    """从 config.prompts 读取所有提示词文本，注册到全局 PromptManager。

    所有提示词文本均来自 ``config.toml [prompts]`` 节，本函数不再持有任何
    内置 fallback。模板内 ``__GUIDELINES__`` 占位符会被
    ``config.prompts.comment_guidelines`` 实际文本一次性替换。

    Args:
        config: 当前插件配置实例。
    """
    section = config.prompts
    guidelines = section.comment_guidelines

    def _with_guidelines(text: str) -> str:
        """把模板里的 ``__GUIDELINES__`` 占位符替换为 guidelines 实际文本。"""
        return text.replace("__GUIDELINES__", guidelines)

    # 1. 说说正文生成
    register_template(
        PromptTemplate(
            name="foxzone.story.generate",
            template=section.story_generate,
        )
    )

    # 2. 图片场景描述生成（不外置到配置）
    register_template(
        PromptTemplate(
            name="foxzone.image.scene_desc",
            template=IMAGE_SCENE_DESC_TEMPLATE,
        )
    )

    # 3. 批量评论回复决策（自己说说下评论）
    register_template(
        PromptTemplate(
            name="foxzone.comment.reply.batch",
            template=_with_guidelines(section.comment_reply_batch),
        )
    )

    # 4. 好友说说互动决策（外部接力评论）
    register_template(
        PromptTemplate(
            name="foxzone.friend.feed.interact",
            template=_with_guidelines(section.friend_feed_interact),
        )
    )

