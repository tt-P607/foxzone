"""FoxZone（墨狐空间）插件入口。

功能概览：
- 发布 QQ 空间说说
- 读取并与好友说说互动（点赞、评论）
- 自动监控好友动态、自动回复自己说说下的评论、外部空间接力回复

架构说明（详见 plans/refactor_foxzone.md）：
- `plugin.py` 负责装配与生命周期：持有 QZoneRuntime（插件级状态单例）
  并启停 Autopilot（三条定时循环）
- `runtime.py` 集中持有全部持久化状态与发送串行锁
- `components/` 为对外契约层（Service 原子门面 / 3 个 Tool / Command）
- `autopilot/` 为自治层（调度 + BatchSendEngine + 三条流程）
- `core/` 为能力层（http 客户端 / llm 生成 / 评论树等）
"""

from __future__ import annotations

from typing import cast

from src.app.plugin_system.api.log_api import get_logger, COLOR
from src.app.plugin_system.base import BasePlugin, register_plugin

from .autopilot import Autopilot
from .components.commands import SendFeedCommand
from .components.service import QZoneService
from .components.tools import QZoneCommentTool, QZoneLikeTool, ReadFeedTool
from .config import FoxZoneConfig
from .prompts import register_foxzone_prompts
from .runtime import QZoneRuntime

logger = get_logger("foxzone.plugin", color=COLOR.ORANGE)


@register_plugin
class FoxZonePlugin(BasePlugin):
    """FoxZone QQ 空间助手插件。

    提供向 QQ 空间自动发送/读取说说、与好友动态互动的能力，
    整合 LLM 内容生成（说说/评论/互动决策）。

    Attributes:
        runtime: 插件级运行时状态容器（on_plugin_loaded 时创建）
        autopilot: 自治调度器（on_plugin_loaded 时创建并启动）
    """

    plugin_name = "foxzone"
    plugin_author = "言柒"
    plugin_description = "QQ 空间助手：自动发送说说、读取互动好友动态"

    # 声明插件配置类，框架会在实例化前自动加载
    configs = [FoxZoneConfig]

    runtime: QZoneRuntime
    autopilot: Autopilot

    # ------------------------------------------------------------------
    # 生命周期钩子
    # ------------------------------------------------------------------

    async def on_plugin_loaded(self) -> None:
        """插件加载完成后的初始化回调。

        执行顺序：
        1. 读取 ``general.enabled``，为 False 则跳过后续初始化
        2. 注册 PromptManager 提示词模板
        3. 创建并初始化 QZoneRuntime（加载持久化状态，全插件唯一）
        4. 启动 Autopilot 三条定时循环

        Raises:
            任何注册或初始化阶段的异常都会向上传播，
            由插件管理器记录并标记加载失败，不在此处吞异常。
        """
        cfg = cast(FoxZoneConfig, self.config)
        if not cfg.general.enabled:
            logger.warning("FoxZone 插件未启用（general.enabled=false），跳过初始化。")
            return

        # 1. 注册提示词模板（模板文本来自 config.prompts，由配置框架
        #    根据 PromptsSection 的 Field(default=...) 自动落盘到 config.toml）
        register_foxzone_prompts(cfg)
        logger.info("FoxZone 提示词模板注册完成。")

        # 2. 创建插件级运行时单例并加载持久化状态
        self.runtime = QZoneRuntime(self)
        await self.runtime.initialize()

        # 3. 启动自治调度循环
        self.autopilot = Autopilot(self.runtime)
        await self.autopilot.start()

        logger.info("FoxZone 插件加载完成。")

    async def on_plugin_unloaded(self) -> None:
        """插件卸载时的清理回调：停止循环并落盘状态。"""
        cfg = cast(FoxZoneConfig, self.config)
        if cfg.general.enabled:
            await self.autopilot.stop()
            await self.runtime.shutdown()
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
            SendFeedCommand,
            QZoneService,
        ]
        return components
