"""FoxZone 插件级运行时状态容器（QZoneRuntime）。

集中持有插件全部有状态对象：Cookie 服务、三份持久化状态
（reply_tracker / interaction_log / vision_cache）、LLM 内容服务
与发送串行锁。``FoxZonePlugin`` 在 ``on_plugin_loaded`` 时创建
唯一的 ``QZoneRuntime`` 并初始化，所有组件（Service / Tool /
Autopilot / ContentService）通过 ``plugin.runtime`` 共享同一份状态，
保证状态一致性与锁语义在插件内全局生效。
"""

from __future__ import annotations

import asyncio
import typing
from typing import Any, Awaitable, Callable

from src.app.plugin_system.api.adapter_api import get_bot_info_by_platform
from src.app.plugin_system.api.log_api import COLOR, get_logger

from .config import FoxZoneConfig
from .core.cookie import CookieService
from .core.http import QZoneAPIClient
from .core.interaction_log import InteractionLog
from .core.llm import ContentService
from .core.reply_tracker import ReplyTrackerService
from .core.vision_cache import ImageVisionCache

if typing.TYPE_CHECKING:
    from .plugin import FoxZonePlugin

logger = get_logger("foxzone.runtime", color=COLOR.ORANGE)


class QZoneRuntime:
    """FoxZone 插件级运行时状态容器（单例，由 FoxZonePlugin 持有）。

    Attributes:
        plugin: 宿主插件实例
        config: 插件配置
        cookie: Cookie 获取与缓存服务
        reply_tracker: 已回复评论跟踪
        interaction_log: 好友说说互动记录
        vision_cache: 图片识别结果缓存
        content: LLM 内容生成服务
        reply_send_lock: 发送串行锁（runtime 单例，故实例级锁即可全局生效）
    """

    def __init__(self, plugin: "FoxZonePlugin") -> None:
        """初始化运行时容器（不做 IO；IO 在 initialize 中执行）。

        Args:
            plugin: 宿主插件实例
        """
        self.plugin: "FoxZonePlugin" = plugin
        self.config: FoxZoneConfig = plugin.config  # type: ignore[assignment]
        self.cookie = CookieService(self.config)
        self.reply_tracker = ReplyTrackerService()
        self.interaction_log = InteractionLog()
        self.vision_cache = ImageVisionCache()
        self.content = ContentService(self)
        self.reply_send_lock: asyncio.Lock = asyncio.Lock()
        self._initialized = False
        #: Bot QQ 号缓存（首次 bot_qq() 时从适配器获取）
        self._bot_qq: str | None = None

    async def initialize(self) -> None:
        """幂等初始化：加载三份持久化状态。"""
        if self._initialized:
            return
        await self.reply_tracker.initialize()
        await self.vision_cache.initialize()
        await self.interaction_log.initialize()
        self._initialized = True
        logger.info("QZoneRuntime 初始化完成（持久化状态已加载）。")

    async def shutdown(self) -> None:
        """落盘所有 dirty 状态。"""
        try:
            await self.interaction_log.save()
            await self.vision_cache.save()
        except Exception as exc:
            logger.warning(f"QZoneRuntime 落盘失败: {exc}")

    # ------------------------------------------------------------------
    # API 客户端
    # ------------------------------------------------------------------

    def has_cookie_adapter(self) -> bool:
        """判断是否存在已就绪的 Cookie 适配器。

        Returns:
            True 表示至少有一个候选适配器已启动
        """
        return self.cookie.has_adapter()

    async def bot_qq(self) -> str:
        """获取 Bot QQ 号（优先从 QQ 适配器动态读取，带缓存）。

        仅在成功取回非空 QQ 号时缓存，失败返回空串但不缓存，
        便于后续轮询重试时再次尝试。

        Returns:
            Bot 的 QQ 号字符串；无法从适配器获取时返回空串。
        """
        if self._bot_qq is not None:
            return self._bot_qq
        try:
            bot_info = await get_bot_info_by_platform("qq")
            qq = str(bot_info.get("bot_id", "") or "") if bot_info else ""
            if qq:
                self._bot_qq = qq
                return qq
            logger.warning("从适配器获取 Bot QQ 号为空，稍后重试。")
            return ""
        except Exception as exc:
            logger.warning(f"从适配器获取 Bot QQ 号失败: {exc}")
            return ""

    async def build_client(self) -> QZoneAPIClient | None:
        """构建 QZoneAPIClient 实例（Cookie 获取 + gtk 计算）。

        Returns:
            客户端实例；Cookie 不可用时返回 None
        """
        cookies = await self.cookie.get_cookies(await self.bot_qq())
        if not cookies:
            logger.error(
                "构建 API 客户端失败：无法获取 Cookie。"
                "请确认已启动 QQ 适配器，或存在有效的本地 Cookie 缓存。"
            )
            return None

        try:
            return QZoneAPIClient.create(cookies)  # type: ignore[return-value]
        except ValueError as exc:
            logger.error(f"构建 API 客户端失败：{exc}")
            return None

    async def with_client(
        self, func: Callable[[QZoneAPIClient], Awaitable[Any]]
    ) -> Any:
        """统一处理 Cookie 失效重试（最多重试一次）。

        Args:
            func: 接收客户端实例的异步回调

        Returns:
            回调的返回值

        Raises:
            RuntimeError: 无法获取 Cookie，或重试后仍失败
        """
        for retry_count in range(2):
            client = await self.build_client()
            if client is None:
                raise RuntimeError("获取 QZone API 客户端失败：无法获取 Cookie。")

            try:
                return await func(client)
            except RuntimeError as exc:
                if "错误码: -3000" in str(exc) and retry_count == 0:
                    logger.warning("Cookie 失效（-3000），清除缓存并重试…")
                    self.cookie.clear_cache(await self.bot_qq())
                    continue
                raise

        raise RuntimeError("API 调用失败：超过最大重试次数。")
