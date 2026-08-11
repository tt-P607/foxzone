"""墨狐空间提示词模板注册。

3 个核心 PromptTemplate（说说写作 / 评论批量决策 / 好友说说互动）
以及统一的评论规范段，**全部模板文本**仅存在于 ``config.py`` 的
``PromptsSection`` 中（``Field(default=...)``），由配置框架自动落盘到
``config/plugins/foxzone/config.toml`` 的 ``[prompts]`` 节。

本文件只保留：
  * ``register_foxzone_prompts(config)``：从 config 读取 4 个字段，
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

    # 2. 批量评论回复决策（自己说说下评论）
    register_template(
        PromptTemplate(
            name="foxzone.comment.reply.batch",
            template=_with_guidelines(section.comment_reply_batch),
        )
    )

    # 3. 好友说说互动决策（外部接力评论）
    register_template(
        PromptTemplate(
            name="foxzone.friend.feed.interact",
            template=_with_guidelines(section.friend_feed_interact),
        )
    )
