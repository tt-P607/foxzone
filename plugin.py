"""FoxZone（墨狐空间）插件入口。

功能概览：
- 发布 QQ 空间说说（支持 AI 配图：SiliconFlow / NovelAI）
- 读取并与好友说说互动（点赞、评论）
- 自动监控好友动态
- 自动回复自己说说下的评论（通过 QZoneAdapter + QZoneChatter）

架构说明：
- `plugin.py` 仅负责插件装配与生命周期管理
- 核心 HTTP 逻辑位于 `core/api_client.py`（QZoneAPIClient）
- 框架组件位于 `components/`（QZoneService 等）
- LLM 提示词通过 PromptManager 统一管理（见 `prompts.py`）
"""

from __future__ import annotations

from typing import cast

from src.app.plugin_system.api.log_api import get_logger, COLOR
from src.app.plugin_system.base import BasePlugin, register_plugin

from .components.adapter import QZoneAdapter
from .components.chatter import QZoneChatter
from .components.commands import SendFeedCommand
from .components.service import QZoneService
from .components.tools import (
    QZoneCommentTool,
    QZoneLikeTool,
    QZoneStartComposeFeedTool,
    QZoneSubmitFeedTool,
    ReadFeedTool,
)
from .config import FoxZoneConfig
from .prompts import register_foxzone_prompts

logger = get_logger("foxzone.plugin", color=COLOR.ORANGE)


@register_plugin
class FoxZonePlugin(BasePlugin):
    """FoxZone QQ 空间助手插件。

    提供向 QQ 空间自动发送/读取说说、与好友动态互动的能力，
    整合 LLM 内容生成与 AI 图片生成（SiliconFlow / NovelAI）。
    """

    plugin_name = "foxzone"
    plugin_version = "1.0.0"
    plugin_author = "MoFox Team"
    plugin_description = "QQ 空间助手：自动发送说说、读取互动好友动态"

    # 声明插件配置类，框架会在实例化前自动加载
    configs = [FoxZoneConfig]

    # ------------------------------------------------------------------
    # 生命周期钩子
    # ------------------------------------------------------------------

    async def on_plugin_loaded(self) -> None:
        """插件加载完成后的初始化回调。

        执行顺序：
        1. 读取 ``general.enabled``，为 False 则跳过后续初始化
        2. 注册 PromptManager 提示词模板
        3. 预热 QZoneService 的持久化状态

        Raises:
            任何注册或服务初始化阶段的异常都会向上传播，
            由插件管理器记录并标记加载失败，不在此处吞异常。
        """
        cfg = cast(FoxZoneConfig, self.config)
        if not cfg.general.enabled:
            logger.warning("FoxZone 插件未启用（general.enabled=false），跳过初始化。")
            return

        # 1. 注册提示词模板（所有模板文本来自 config.prompts，由配置框架
        #    根据 PromptsSection 的 Field(default=...) 自动落盘到 config.toml）
        register_foxzone_prompts(cfg)
        logger.info("FoxZone 提示词模板注册完成。")

        # 2. 预热服务持久化状态（ReplyTracker 等）
        # 注意：直接实例化 QZoneService，避免在 on_plugin_loaded 阶段
        # 通过 service_manager.get_service() 获取（此时插件尚未被
        # plugin_manager 标记为已加载，会触发"插件未加载"警告）。
        service = QZoneService(plugin=self)
        await service.initialize()
        logger.info("FoxZone 服务初始化完成。")

        logger.info("FoxZone 插件加载完成。")

    async def on_plugin_unloaded(self) -> None:
        """插件卸载时的清理回调。"""
        logger.info("FoxZone 插件已卸载。")

    # ------------------------------------------------------------------
    # 组件注册
    # ------------------------------------------------------------------

    def get_components(self) -> list[type]:
        """返回插件内所有组件类。

        Returns:
            组件类列表，框架会自动注册到全局注册表。
            ``general.enabled=false`` 时返回空列表，所有组件不注册、
            后台循环也不会启动。
        """
        cfg = cast(FoxZoneConfig, self.config)
        if not cfg.general.enabled:
            return []
        components: list[type] = [
            ReadFeedTool,
            QZoneCommentTool,
            QZoneLikeTool,
        ]

        if cfg.general.expose_feed_write_tools:
            components.extend(
                [
                    QZoneStartComposeFeedTool,
                    QZoneSubmitFeedTool,
                ]
            )

        components.extend(
            [
                SendFeedCommand,
                QZoneService,
                QZoneAdapter,
                QZoneChatter,
            ]
        )
        return components
