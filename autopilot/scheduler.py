"""Autopilot：FoxZone 自治调度器。

管理三条定时循环（自己说说评论轮询 / 好友动态监控 / 外部空间接力回查），
含勿扰时段（DND）判定。取代旧版继承 ``BaseAdapter`` 的 QZoneAdapter——
它唯一的实职就是定时器宿主，不需要 transport 语义。

通过 :class:`~plugins.foxzone.runtime.QZoneRuntime` 注入依赖，
不经过 ``get_service()`` 反查。
"""

from __future__ import annotations

import asyncio
import datetime
import typing
from typing import Awaitable, Callable

from src.app.plugin_system.api.log_api import COLOR, get_logger
from src.kernel.concurrency import get_task_manager

from .external import external_followup_once
from .friend_feeds import friend_monitor_once
from .self_comments import poll_self_comments_once

if typing.TYPE_CHECKING:
    from ..runtime import QZoneRuntime

logger = get_logger("foxzone.autopilot", color=COLOR.CYAN)


class Autopilot:
    """FoxZone 自治调度器：管理三条定时循环。

    Attributes:
        _runtime: 插件运行时状态容器
        _task_ids: 已启动的后台任务 ID 列表
    """

    def __init__(self, runtime: "QZoneRuntime") -> None:
        """初始化调度器。

        Args:
            runtime: 插件运行时状态容器
        """
        self._runtime = runtime
        self._task_ids: list[str] = []

    async def start(self) -> None:
        """按配置启动 0~3 条 daemon 循环（经 task_manager）。"""
        cfg = self._runtime.config
        if not cfg.monitor.enable_auto_monitor:
            logger.info("自动监控未启用（enable_auto_monitor=False），跳过启动所有轮询任务。")
            return

        tm = get_task_manager()

        # 评论回复轮询：受 enable_auto_reply 控制
        if cfg.monitor.enable_auto_reply:
            logger.info("启动自己说说评论轮询任务…")
            info = tm.create_task(
                self._loop(
                    "评论轮询",
                    cfg.monitor.interval_minutes,
                    lambda: self._once_with_bot_qq(
                        lambda bq: poll_self_comments_once(
                            self._runtime,
                            bq,
                            5,
                            cfg.monitor.max_comment_age_hours,
                        )
                    ),
                ),
                name="foxzone_qzone_poll",
                daemon=True,
            )
            self._task_ids.append(info.task_id)
        else:
            logger.info("自动回复未启用（enable_auto_reply=False），跳过评论轮询任务。")

        # 外部空间评论回查：受 enable_external_followup 独立控制（默认关闭，防风控）
        if cfg.monitor.enable_external_followup:
            logger.info("启动外部空间评论回查轮询任务…")
            info = tm.create_task(
                self._loop(
                    "外部回查",
                    cfg.monitor.external_followup_minutes,
                    lambda: self._once_with_bot_qq(
                        lambda bq: external_followup_once(
                            self._runtime,
                            bq,
                            cfg.monitor.max_comment_age_hours,
                            max(0, int(cfg.monitor.external_followup_batch)),
                            float(cfg.monitor.external_followup_max_feed_age_hours),
                        )
                    ),
                ),
                name="foxzone_qzone_external_followup",
                daemon=True,
            )
            self._task_ids.append(info.task_id)
        else:
            logger.info("外部空间评论回查未启用（enable_external_followup=False），跳过。")

        # 好友说说监控轮询：受 enable_friend_monitor 独立控制
        if cfg.monitor.enable_friend_monitor:
            logger.info("启动好友说说动态轮询任务…")
            info = tm.create_task(
                self._loop(
                    "好友说说监控",
                    cfg.monitor.friend_monitor_interval_minutes,
                    lambda: friend_monitor_once(
                        self._runtime, cfg.monitor.friend_monitor_num_feeds
                    ),
                ),
                name="foxzone_friend_monitor",
                daemon=True,
            )
            self._task_ids.append(info.task_id)
        else:
            logger.info("好友说说监控未启用（enable_friend_monitor=False），跳过好友动态轮询任务。")

    async def stop(self) -> None:
        """取消全部循环任务。"""
        tm = get_task_manager()
        for task_id in self._task_ids:
            tm.cancel_task(task_id)
        self._task_ids.clear()
        logger.info("Autopilot 已停止所有轮询任务。")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _is_dnd_active(self) -> bool:
        """检查当前是否处于勿扰时间段。

        支持跨午夜的时间段（如 23 → 7 表示晚 11 点到早 7 点）。

        Returns:
            True 表示当前处于勿扰时间，应跳过轮询
        """
        cfg = self._runtime.config
        if not cfg.monitor.dnd_enabled:
            return False

        current_hour = datetime.datetime.now().hour
        start = cfg.monitor.dnd_start_hour
        end = cfg.monitor.dnd_end_hour

        if start <= end:
            # 例如 9 → 17（同一天内）
            return start <= current_hour < end
        # 跨午夜，例如 23 → 7
        return current_hour >= start or current_hour < end

    async def _once_with_bot_qq(
        self, once: Callable[[str], Awaitable[None]]
    ) -> None:
        """动态获取 Bot QQ 号后执行单次轮询。

        适配器启动完成后 ``bot_qq`` 才可用，故在每次执行时获取，
        避免启动阶段取到空串并长期失效。

        Args:
            once: 接收 Bot QQ 号的单次执行回调
        """
        bot_qq = await self._runtime.bot_qq()
        await once(bot_qq)

    async def _wait_for_adapters(self, timeout: float = 60.0) -> bool:
        """等待 QQ 适配器启动就绪后再执行轮询。

        适配器由框架在 ``ON_ALL_PLUGIN_LOADED`` 事件后于后台启动，
        首次轮询前先等待其就绪，避免适配器未就绪时白跑一次。

        Args:
            timeout: 最长等待秒数

        Returns:
            True 表示已检测到可用适配器；超时返回 False
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self._runtime.has_cookie_adapter():
                return True
            await asyncio.sleep(2.0)
        return False

    async def _loop(
        self,
        name: str,
        interval_minutes: float,
        once: Callable[[], Awaitable[None]],
    ) -> None:
        """通用循环骨架：等待适配器就绪 + DND 检查 + 异常隔离 + sleep。

        Args:
            name: 循环名称（用于日志）
            interval_minutes: 轮询间隔（分钟）
            once: 单次执行回调
        """
        logger.info(f"{name}任务启动（间隔 {interval_minutes} 分钟）。")
        if not await self._wait_for_adapters():
            logger.warning(
                f"{name}：等待 QQ 适配器就绪超时，仍尝试执行（需依赖本地 Cookie 缓存）。"
            )
        while True:
            if self._is_dnd_active():
                logger.debug(f"{name}：当前处于勿扰时间段，跳过本次扫描。")
            else:
                try:
                    await once()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"{name}出现异常，将在下次间隔后重试: {e}")

            await asyncio.sleep(interval_minutes * 60)
