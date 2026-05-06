"""QZoneAdapter：QQ 空间适配器。

轮询自己的 QQ 空间说说评论，将未回复的评论包装为 MessageEnvelope
投递给框架核心，由 QZoneChatter 负责回复。
"""

from __future__ import annotations

import asyncio
import random
import typing
from datetime import datetime
from typing import Any

from mofox_wire import CoreSink, MessageEnvelope
from mofox_wire.types import UserRole

from src.app.plugin_system.base import BaseAdapter
from src.app.plugin_system.api.log_api import get_logger, COLOR
from src.app.plugin_system.api.service_api import get_service
from src.kernel.concurrency import get_task_manager

if typing.TYPE_CHECKING:
    from ..plugin import FoxZonePlugin

from ..config import FoxZoneConfig
from ..core.interaction_log import ACTION_LIKE, SOURCE_POLL
from . import SERVICE_SIG

logger = get_logger("foxzone.adapter", color=COLOR.CYAN)

_SERVICE_SIG = SERVICE_SIG


class QZoneAdapter(BaseAdapter):
    """QQ 空间评论适配器。

    定期轮询自己的 QQ 空间说说，发现未回复的评论后，构建
    MessageEnvelope 并通过 core_sink 投递给框架，触发 QZoneChatter 回复。

    Class Attributes:
        adapter_name: 适配器注册名称
        adapter_version: 适配器版本
        platform: 平台标识
    """

    adapter_name = "qzone_adapter"
    adapter_version = "1.0.0"
    adapter_description = "QQ 空间评论轮询适配器"
    platform = "qzone"

    def __init__(self, core_sink: CoreSink, plugin: "FoxZonePlugin | None", **kwargs: Any) -> None:  # type: ignore[override]
        """初始化适配器。

        Args:
            core_sink: 框架核心消息接收端
            plugin: 宿主插件实例
            **kwargs: 额外参数（由框架传入）
        """
        super().__init__(core_sink, plugin, **kwargs)
        self._plugin: "FoxZonePlugin | None" = plugin  # type: ignore[assignment]
        self._poll_task_id: str | None = None
        self._friend_monitor_task_id: str | None = None
        self._external_followup_task_id: str | None = None

    @property
    def _cfg(self) -> "FoxZoneConfig | None":
        """返回插件配置（类型安全访问）。

        Returns:
            FoxZoneConfig 实例；插件为 None 时返回 None
        """
        if self._plugin is None:
            return None
        return self._plugin.config  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # 生命周期钩子
    # ------------------------------------------------------------------

    async def on_adapter_loaded(self) -> None:
        """适配器加载时按配置启动两条轮询任务（互不依赖）。"""
        if self._plugin is None:
            logger.error("QZoneAdapter 初始化失败：宿主插件为 None。")
            return

        cfg = self._cfg
        if cfg is None or not cfg.monitor.enable_auto_monitor:
            logger.info("自动监控未启用（enable_auto_monitor=False），跳过启动所有轮询任务。")
            return

        tm = get_task_manager()

        # 评论回复轮询：受 enable_auto_reply 控制
        if cfg.monitor.enable_auto_reply:
            logger.info("启动自己说说评论轮询任务…")
            task_info = tm.create_task(
                self._poll_loop(),
                name="foxzone_qzone_poll",
                daemon=True,
            )
            self._poll_task_id = task_info.task_id

            logger.info("启动外部空间评论回查轮询任务…")
            external_task_info = tm.create_task(
                self._external_followup_loop(),
                name="foxzone_qzone_external_followup",
                daemon=True,
            )
            self._external_followup_task_id = external_task_info.task_id
        else:
            logger.info("自动回复未启用（enable_auto_reply=False），跳过评论轮询任务。")

        # 好友说说监控轮询：受 enable_friend_monitor 独立控制
        if cfg.monitor.enable_friend_monitor:
            logger.info("启动好友说说动态轮询任务…")
            friend_task_info = tm.create_task(
                self._friend_monitor_loop(),
                name="foxzone_friend_monitor",
                daemon=True,
            )
            self._friend_monitor_task_id = friend_task_info.task_id
        else:
            logger.info("好友说说监控未启用（enable_friend_monitor=False），跳过好友动态轮询任务。")

    async def health_check(self) -> bool:
        """健康检查：QZoneAdapter 是纯轮询型适配器，无常驻连接。

        框架默认 ``is_connected()`` 仅适用于 WebSocket / HTTP transport，
        会让本 Adapter 持续被视为不健康并触发 reconnect → stop，
        因此始终返回 True 跳过自动重连。

        Returns:
            True（恒定）
        """
        return True

    async def on_adapter_unloaded(self) -> None:
        """适配器卸载时取消轮询任务。"""
        logger.info("QZoneAdapter 卸载，停止评论轮询任务。")
        tm = get_task_manager()
        if self._poll_task_id is not None:
            tm.cancel_task(self._poll_task_id)
            self._poll_task_id = None
        if self._friend_monitor_task_id is not None:
            tm.cancel_task(self._friend_monitor_task_id)
            self._friend_monitor_task_id = None
        if self._external_followup_task_id is not None:
            tm.cancel_task(self._external_followup_task_id)
            self._external_followup_task_id = None

    # ------------------------------------------------------------------
    # 勿扰检测
    # ------------------------------------------------------------------

    def _is_dnd_active(self) -> bool:
        """检查当前是否处于勿扰时间段。

        支持跨午夜的时间段（如 23 → 7 表示晚 11 点到早 7 点）。

        Returns:
            True 表示当前处于勿扰时间，应跳过轮询
        """
        cfg = self._cfg
        if cfg is None or not cfg.monitor.dnd_enabled:
            return False

        import datetime as _dt

        current_hour = _dt.datetime.now().hour
        start = cfg.monitor.dnd_start_hour
        end = cfg.monitor.dnd_end_hour

        if start <= end:
            # 例如 9 → 17（同一天内）
            return start <= current_hour < end
        else:
            # 跨午夜，例如 23 → 7
            return current_hour >= start or current_hour < end

    # ------------------------------------------------------------------
    # 轮询循环
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """评论轮询主循环。

        每隔 interval_minutes 分钟读取一次自己的说说，
        对发现的未回复评论逐一构建 MessageEnvelope 并投递给核心。
        """
        if self._plugin is None:
            return

        cfg = self._cfg
        if cfg is None:
            return

        interval_minutes: float = cfg.monitor.interval_minutes
        num_feeds: int = 5
        bot_qq: str = cfg.general.bot_qq
        max_age_hours: float = cfg.monitor.max_comment_age_hours

        logger.info(f"评论轮询任务启动（间隔 {interval_minutes} 分钟，检查最新 {num_feeds} 条说说）。")

        while True:
            if self._is_dnd_active():
                logger.debug("评论轮询：当前处于勿扰时间段，跳过本次扫描。")
            else:
                try:
                    await self._poll_once(bot_qq, num_feeds, max_age_hours)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"评论轮询出现异常，将在下次间隔后重试: {e}")

            await asyncio.sleep(interval_minutes * 60)

    async def _friend_monitor_loop(self) -> None:
        """好友说说监控主循环。

        每隔 friend_monitor_interval_minutes 分钟获取一次好友动态，
        对尚未互动过的说说投递为 wire 消息交给 QZoneChatter 决策互动。
        """
        if self._plugin is None:
            return

        cfg = self._cfg
        if cfg is None:
            return

        interval_minutes: float = cfg.monitor.friend_monitor_interval_minutes
        num_feeds: int = cfg.monitor.friend_monitor_num_feeds

        logger.info(
            f"好友说说监控任务启动（间隔 {interval_minutes} 分钟，"
            f"每次检查 {num_feeds} 条好友说说）。"
        )

        while True:
            if self._is_dnd_active():
                logger.debug("好友说说监控：当前处于勿扰时间段，跳过本次扫描。")
            else:
                try:
                    await self._friend_monitor_once(num_feeds)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"好友说说监控出现异常，将在下次间隔后重试: {e}")

            await asyncio.sleep(interval_minutes * 60)

    async def _friend_monitor_once(self, num_feeds: int) -> None:
        """执行一次好友说说监控。

        获取好友动态流，将未处理的说说打包成 MessageEnvelope 投递给 QZoneChatter，
        由 Chatter 调用 LLM 批量决策互动（点赞/评论/忽略）。

        Args:
            num_feeds: 最多检查的好友说说数量
        """
        from .service import QZoneService

        service: QZoneService | None = get_service(_SERVICE_SIG)  # type: ignore[assignment]
        if service is None:
            logger.error("无法获取 QZoneService，跳过本次好友说说监控。")
            return

        try:
            feeds = await service.get_monitor_feeds(num_feeds)
        except Exception as e:
            logger.error(f"获取好友动态失败: {e}")
            return

        if not feeds:
            logger.debug("本次好友说说监控未获取到动态。")
            return

        logger.info(f"好友说说监控：获取到 {len(feeds)} 条候选动态，开始逐条过滤…")
        candidate_feeds: list[dict[str, Any]] = []
        for feed in feeds:
            target_qq: str = str(feed.get("target_qq", "")).strip()
            tid: str = str(feed.get("tid", "")).strip()

            if not target_qq or not tid:
                continue

            # 已互动（点赞或评论）则跳过
            if service.has_interacted(target_qq, tid):
                logger.info(f"跳过已互动说说 (qq={target_qq}, tid={tid})")
                continue

            candidate_feeds.append(feed)

        if not candidate_feeds:
            logger.debug("本次好友说说监控无未处理说说。")
            return

        # ── Plan D：先批量点赞 + 标记 LIKE，仅成功者再交给 Chatter 决策评论 ──
        feed_items: list[dict[str, Any]] = []
        for feed in candidate_feeds:
            target_qq = str(feed.get("target_qq", "")).strip()
            tid = str(feed.get("tid", "")).strip()
            try:
                liked = await service.like(target_qq=target_qq, feed_id=tid)
            except Exception as exc:
                logger.error(f"自动点赞异常 (qq={target_qq}, tid={tid}): {exc}")
                continue

            if not liked:
                # 点赞失败：留待下一轮重试，不进入 LLM 评论决策
                logger.warning(f"自动点赞失败，本轮不交给 LLM 决策评论 (qq={target_qq}, tid={tid})")
                continue

            await service.mark_interaction(target_qq, tid, ACTION_LIKE, SOURCE_POLL)
            logger.info(f"自动点赞成功 (qq={target_qq}, tid={tid})")

            # 仅点赞成功的说说才进入 LLM 评论决策
            content_text = str(feed.get("content") or feed.get("rt_con") or "（无正文）").strip()
            created_time = str(feed.get("created_time", "")).strip()
            images: list[str] = [str(u) for u in feed.get("images", []) if u]
            comments: list[dict[str, Any]] = feed.get("comments", [])

            image_lines: list[str] = [f"图片{j}：[待识别]" for j in range(1, len(images) + 1)]

            feed_items.append({
                "tid": tid,
                "target_qq": target_qq,
                "content": content_text,
                "created_time": created_time,
                "image_text": "\n".join(image_lines),
                "images": images,
                "comment_count": len(comments),
            })

        logger.info(
            f"好友说说监控：候选 {len(candidate_feeds)} 条，已点赞 {len(feed_items)} 条，"
            f"准备投递评论决策。"
        )
        if not feed_items:
            return

        # 直接由 service 闭环处理（不走 ChatStream/ON_MESSAGE_RECEIVED，避免 EventBus 5s 超时）
        get_task_manager().create_task(
            service.process_feed_monitor_batch(feed_items),
            name=f"foxzone:feed_monitor:{len(feed_items)}",
        )
        logger.info(
            f"本次好友说说监控，已投递 {len(feed_items)} 条已点赞说说到后台评论决策。"
        )

    # ------------------------------------------------------------------
    # 外部空间评论回查（bot 评论过别人的说说后，回查别人是否回复了 bot）
    # ------------------------------------------------------------------

    async def _external_followup_loop(self) -> None:
        """外部空间评论回查主循环。

        定期遍历 bot 评论过的「他人空间说说」，**精准**拉取这些说说本身的评论区，
        发现别人在 bot 评论下的二级回复时，将这些二级回复投递给 Chatter。

        请求量控制：
        - 间隔由 ``monitor.external_followup_minutes`` 控制（默认 20 分钟）
        - 每轮最多 ``monitor.external_followup_batch`` 个 QQ（默认 2，0 表示不限制）
        - 按「最久未检测优先」轮转，避免同一条反复拉

        以 QQ 为粒度调度：每个 QQ 一次 ``list_feeds(num=20, paginate_comments=False)``
        即可一并检查该 QQ 名下全部 bot 评论过的 feed，单 QQ 仅 1 次请求。
        """
        if self._plugin is None:
            return

        cfg = self._cfg
        if cfg is None:
            return

        interval_minutes: float = cfg.monitor.external_followup_minutes
        # batch_size <= 0 表示不限制，一轮内扫完所有标记 QQ
        batch_size: int = max(0, int(cfg.monitor.external_followup_batch))
        bot_qq: str = cfg.general.bot_qq
        max_age_hours: float = cfg.monitor.max_comment_age_hours
        max_feed_age_hours: float = float(cfg.monitor.external_followup_max_feed_age_hours)

        batch_desc = "不限制" if batch_size == 0 else f"最多 {batch_size} 个 QQ"
        feed_age_desc = (
            "不限制" if max_feed_age_hours <= 0 else f"{max_feed_age_hours}h 内"
        )
        logger.info(
            f"外部空间评论回查任务启动（间隔 {interval_minutes} 分钟，"
            f"每轮{batch_desc}，max_comment_age_hours={max_age_hours}，"
            f"feed 时效={feed_age_desc}）。"
        )

        while True:
            if self._is_dnd_active():
                logger.debug("外部回查：当前处于勿扰时间段，跳过本次扫描。")
            else:
                try:
                    await self._external_followup_once(
                        bot_qq, max_age_hours, batch_size, max_feed_age_hours
                    )
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"外部空间评论回查出现异常，将在下次间隔后重试: {e}")

            await asyncio.sleep(interval_minutes * 60)

    async def _external_followup_once(
        self, bot_qq: str, max_age_hours: float, batch_size: int,
        max_feed_age_hours: float = 0,
    ) -> None:
        """执行一次外部空间评论回查（QQ 聚合版）。

        采用「时间线扫描 + 精准详情」二段式：先用 msglist_v6 快速判断哪些已记录的
        feed 还在时间线最新 20 条内（cheap），命中后再对每条单独调 msgdetail_v6
        拿 hex 全局 tid 的完整评论列表（精准），用于可靠地匹配 parent_tid 并提交回复。

        逻辑：
        1. ``iter_followup_qqs`` 按「最久未回查」排序聚合
           ``(target_qq, [feed_ids])``，挑 ``batch_size`` 个 QQ（0 表示不限）。
        2. 每个 QQ 调一次 ``service.list_feeds(num=20, paginate_comments=False)``
           做时间线命中判断（不分页，仅用 msglist_v6 自带的 tid 列表）。
        3. 命中的 feed_id 调 ``service.get_feed_comments`` 单独拉 msgdetail_v6
           完整评论（含 list_3 楼中楼，hex 全局 tid）；未命中的（已超出 20 条范围）
           记日志跳过。
        4. 无论是否找到新回复，对所有标记 (qq, feed) 都调
           ``mark_followup_checked`` 推后时间戳，避免轮转死锁。

        Args:
            bot_qq: Bot 自己的 QQ
            max_age_hours: 评论过期阈值（小时），0 表示不限制
            batch_size: 本轮最多检查多少个 QQ；0 表示不限
        """
        from .service import QZoneService

        service: QZoneService | None = get_service(_SERVICE_SIG)  # type: ignore[assignment]
        if service is None:
            logger.error("无法获取 QZoneService，跳过本次外部回查。")
            return

        qq_targets: list[tuple[str, list[str]]] = await service.iter_followup_qqs(
            exclude_qq=bot_qq,
            limit=batch_size,
            max_feed_age_hours=max_feed_age_hours,
        )
        if not qq_targets:
            logger.debug("外部回查：本轮没有待检查的 QQ。")
            return

        total_feeds = sum(len(fids) for _, fids in qq_targets)
        logger.info(
            f"外部回查：本轮检查 {len(qq_targets)} 个 QQ 共 {total_feeds} 条 feed"
            f"（最久未回查优先）。"
        )

        new_items: list[dict[str, Any]] = []
        success_qq = 0
        fail_qq = 0
        miss_count = 0  # feed 已超出最新 20 条范围，本轮无法命中

        for idx, (target_qq, expected_feed_ids) in enumerate(qq_targets):
            # QQ 之间加 3-8s 随机抖动，避免短时间集中调用触发风控
            if idx > 0:
                jitter = random.uniform(3.0, 8.0)
                logger.debug(f"外部回查：QQ 间隔抖动 {jitter:.1f}s")
                await asyncio.sleep(jitter)

            try:
                feeds = await service.list_feeds(
                    target_qq=target_qq,
                    num=20,
                    skip_commented=False,
                    paginate_comments=False,
                )
            except Exception as e:
                logger.warning(
                    f"外部回查：拉取 QQ {target_qq} 时间线失败: {e}"
                )
                for fid in expected_feed_ids:
                    await service.mark_followup_checked(target_qq, fid)
                fail_qq += 1
                continue

            success_qq += 1
            feeds_by_tid: dict[str, dict[str, Any]] = {
                str(f.get("tid", "")): f for f in feeds if f.get("tid")
            }

            # 无论是否命中，本轮已检查过这些 feed → 更新时间戳
            for fid in expected_feed_ids:
                await service.mark_followup_checked(target_qq, fid)

            for feed_id in expected_feed_ids:
                feed = feeds_by_tid.get(str(feed_id))
                if feed is None:
                    miss_count += 1
                    continue

                # 精准模式：命中 feed 后单独调 msgdetail_v6 拿 hex 全局 tid 的完整评论列表，
                # 替换 msglist_v6 内嵌的局部序号 commentlist。这样下方 parent_tid 关联可信，
                # service.submit_reply 能用 hex commentId 提交（不会触发 -10049）。
                detailed = await service.get_feed_comments(
                    host_qq=target_qq, feed_id=str(feed_id)
                )
                comments: list[dict[str, Any]] = (
                    list(detailed) if detailed else list(feed.get("comments", []) or [])
                )
                if not comments:
                    continue

                bot_comment_tids: set[str] = {
                    str(c.get("comment_tid", ""))
                    for c in comments
                    if str(c.get("qq_account", "")) == bot_qq
                    and c.get("comment_tid")
                }
                if not bot_comment_tids:
                    continue

                for comment in comments:
                    commenter_qq: str = str(comment.get("qq_account", ""))
                    if commenter_qq == bot_qq:
                        continue

                    comment_tid: str = str(comment.get("comment_tid", ""))
                    if not comment_tid:
                        continue

                    parent_tid_raw = comment.get("parent_tid")
                    parent_tid: str = (
                        str(parent_tid_raw).strip() if parent_tid_raw else ""
                    )
                    if parent_tid not in bot_comment_tids:
                        continue

                    if max_age_hours > 0:
                        create_time_str = str(comment.get("create_time", ""))
                        if create_time_str:
                            try:
                                comment_dt = datetime.strptime(
                                    create_time_str, "%Y-%m-%d %H:%M:%S"
                                )
                                age_hours = (
                                    datetime.now() - comment_dt
                                ).total_seconds() / 3600
                                if age_hours > max_age_hours:
                                    logger.debug(
                                        f"外部回查跳过过期回复 {comment_tid}"
                                        f"（{create_time_str}，"
                                        f"{age_hours:.1f}h > {max_age_hours}h）"
                                    )
                                    continue
                            except ValueError:
                                pass

                    if await service.has_replied_comment(feed_id, comment_tid):
                        continue

                    parent_content: str = ""
                    parent_commenter_name: str = ""
                    for _c in comments:
                        if str(_c.get("comment_tid", "")) == parent_tid:
                            parent_content = str(_c.get("content", "") or "")
                            parent_commenter_name = str(_c.get("nickname", "") or "")
                            break

                    new_items.append({
                        "feed_id": feed_id,
                        "feed_content": str(feed.get("content", "") or ""),
                        "feed_images": list(feed.get("images", []) or []),
                        "story_time": str(feed.get("created_time", "") or ""),
                        "all_comments": comments,
                        "comment_tid": comment_tid,
                        "comment_content": comment.get("content", ""),
                        "comment_time": comment.get("create_time", ""),
                        "commenter_name": comment.get("nickname", ""),
                        "commenter_qq": commenter_qq,
                        "parent_tid": parent_tid,
                        "parent_content": parent_content,
                        "parent_commenter_qq": bot_qq,
                        "parent_commenter_name": parent_commenter_name,
                        "is_reply_to_bot": True,
                        "host_qq": target_qq,
                    })

        summary = (
            f"成功 {success_qq}/失败 {fail_qq} QQ，"
            f"miss {miss_count}（超 20 条范围）"
        )
        if not new_items:
            logger.info(f"外部回查：{summary}，本轮没有新发现的接力回复。")
            return

        # 外部接力回复路径不走 ON_MESSAGE_RECEIVED 事件分发，由 service 闭环处理：
        # 决策 + 发送 + 标记已处理。投递到 task_manager 后台执行，避免阻塞回查循环本身。
        get_task_manager().create_task(
            service.process_external_followup_batch(new_items),
            name=f"foxzone:followup:{bot_qq}:{len(new_items)}",
        )
        logger.info(
            f"外部回查：{summary}，提交 {len(new_items)} 条"
            f"「别人回复 bot 评论」的接力回复到后台处理。"
        )

    def _build_feed_monitor_envelope(
        self,
        feed_items: list[dict[str, Any]],
    ) -> "MessageEnvelope | None":
        """将本次好友说说监控的新说说打包成 MessageEnvelope。

        Args:
            feed_items: 格式化后的说说项列表

        Returns:
            MessageEnvelope；feed_items 为空时返回 None
        """
        if not feed_items:
            return None

        import time as _time

        batch_message_id = f"qzone_friend_batch_{int(_time.time())}_{len(feed_items)}"

        envelope: MessageEnvelope = {
            "direction": "incoming",
            "message_info": {
                "platform": self.platform,
                "message_id": batch_message_id,
                "user_info": {
                    "platform": self.platform,
                    "user_id": "foxzone_friend_monitor",
                    "role": UserRole.MEMBER,
                    "user_nickname": "QZone 好友动态监控",
                },
                "group_info": {
                    "platform": self.platform,
                    "group_id": "qzone_friend_monitor",
                    "group_name": "QZone 好友动态监控",
                },
            },
            "message_segment": {
                "type": "text",
                "data": f"[QZone 好友动态] 共 {len(feed_items)} 条新说说待处理",
            },
            "raw_message": {
                "batch_mode": True,
                "friend_feed_items": feed_items,
            },
        }
        return envelope

    async def _poll_once(self, bot_qq: str, num_feeds: int, max_age_hours: float = 0.0) -> None:
        """执行一次轮询并以批量方式投递新评论。

        收集本次轮询中所有未处理的评论，打包成单个 MessageEnvelope 投递，
        由 QZoneChatter 批量决策是否回复、如何回复。

        Args:
            bot_qq: Bot 的 QQ 号
            num_feeds: 检查的说说数量
            max_age_hours: 忽略超过此时间（小时）的评论，0 表示不限制
        """
        from .service import QZoneService

        service: QZoneService | None = get_service(_SERVICE_SIG)  # type: ignore[assignment]
        if service is None:
            logger.error("无法获取 QZoneService，跳过本次评论轮询。")
            return

        try:
            feeds = await service.list_own_feeds_with_comments(num_feeds)
        except Exception as e:
            logger.error(f"获取说说列表失败: {e}")
            return

        if not feeds:
            logger.debug("本次轮询未获取到说说。")
            return

        new_items: list[dict[str, Any]] = []
        for feed in feeds:
            feed_id: str = str(feed.get("tid", ""))
            feed_content: str = feed.get("content", "")
            comments: list[dict[str, Any]] = feed.get("comments", [])

            if not feed_id or not comments:
                continue

            for comment in comments:
                commenter_qq: str = str(comment.get("qq_account", ""))
                # 过滤掉 bot 自己的评论
                if commenter_qq == bot_qq:
                    continue

                comment_tid: str = str(comment.get("comment_tid", ""))
                if not comment_tid:
                    continue

                # 过滤过期评论（max_age_hours > 0 时生效）
                if max_age_hours > 0:
                    create_time_str = str(comment.get("create_time", ""))
                    if create_time_str:
                        try:
                            comment_dt = datetime.strptime(create_time_str, "%Y-%m-%d %H:%M:%S")
                            age_hours = (datetime.now() - comment_dt).total_seconds() / 3600
                            if age_hours > max_age_hours:
                                logger.debug(
                                    f"跳过过期评论 {comment_tid}（{create_time_str}，"
                                    f"{age_hours:.1f}h > {max_age_hours}h）"
                                )
                                continue
                        except ValueError:
                            pass  # 格式无法解析时不过滤

                if await service.has_replied_comment(feed_id, comment_tid):
                    continue

                # 解析父评论上下文（用于支持楼中楼对话链）
                parent_tid_raw = comment.get("parent_tid")
                parent_tid: str = str(parent_tid_raw).strip() if parent_tid_raw else ""
                parent_content: str = ""
                parent_commenter_qq: str = ""
                parent_commenter_name: str = ""
                if parent_tid:
                    for _c in comments:
                        if str(_c.get("comment_tid", "")) == parent_tid:
                            parent_content = str(_c.get("content", "") or "")
                            parent_commenter_qq = str(_c.get("qq_account", "") or "")
                            parent_commenter_name = str(_c.get("nickname", "") or "")
                            break
                is_reply_to_bot: bool = bool(parent_tid) and parent_commenter_qq == bot_qq

                new_items.append({
                    "feed_id": feed_id,
                    "feed_content": feed_content,
                    "feed_images": feed.get("images", []),
                    "story_time": feed.get("created_time", ""),
                    "all_comments": comments,
                    "comment_tid": comment_tid,
                    "comment_content": comment.get("content", ""),
                    "comment_time": comment.get("create_time", ""),
                    "commenter_name": comment.get("nickname", ""),
                    "commenter_qq": commenter_qq,
                    "parent_tid": parent_tid,
                    "parent_content": parent_content,
                    "parent_commenter_qq": parent_commenter_qq,
                    "parent_commenter_name": parent_commenter_name,
                    "is_reply_to_bot": is_reply_to_bot,
                    "host_qq": bot_qq,
                })

        if not new_items:
            logger.debug("本次轮询无新评论需要处理。")
            return

        # 直接由 service 闭环处理（不走 ChatStream/ON_MESSAGE_RECEIVED，避免 EventBus 5s 超时）
        # process_external_followup_batch 已能处理任意 host_qq 的回复场景，这里复用。
        get_task_manager().create_task(
            service.process_external_followup_batch(new_items),
            name=f"foxzone:self_feed_replies:{bot_qq}:{len(new_items)}",
        )
        logger.info(f"本次轮询批量投递 {len(new_items)} 条新评论到后台处理。")

    # ------------------------------------------------------------------
    # 消息转换
    # ------------------------------------------------------------------

    def _build_batch_envelope(
        self,
        bot_qq: str,
        items: list[dict[str, Any]],
    ) -> MessageEnvelope | None:
        """将本次轮询的所有新评论打包成一个批量 MessageEnvelope。

        Args:
            bot_qq: Bot 的 QQ 号
            items: 评论项列表，每项需含 comment_tid、feed_id 等字段

        Returns:
            MessageEnvelope；items 为空时返回 None
        """
        if not items:
            return None

        import time as _time

        batch_message_id = f"qzone_batch_{int(_time.time())}_{len(items)}"

        envelope: MessageEnvelope = {
            "direction": "incoming",
            "message_info": {
                "platform": self.platform,
                "message_id": batch_message_id,
                "user_info": {
                    "platform": self.platform,
                    "user_id": bot_qq,
                    "role": UserRole.MEMBER,
                    "user_nickname": "QZone 评论监控",
                },
                "group_info": {
                    "platform": self.platform,
                    "group_id": "qzone_monitor",
                    "group_name": "QZone 评论监控",
                },
            },
            "message_segment": {
                "type": "text",
                "data": f"[QZone 批量评论] 共 {len(items)} 条新评论待处理",
            },
            "raw_message": {
                "batch_mode": True,
                "comment_items": items,
            },
        }
        return envelope

    async def from_platform_message(self, raw: dict[str, Any]) -> MessageEnvelope | None:  # type: ignore[override]
        """将 QZone 评论数据转换为 MessageEnvelope。

        Args:
            raw: 包含评论信息的字典，需含 feed_id、comment_tid、
                 commenter_qq、commenter_name、comment_content、feed_content 字段

        Returns:
            MessageEnvelope；必填字段缺失时返回 None
        """
        feed_id: str = str(raw.get("feed_id", ""))
        comment_tid: str = str(raw.get("comment_tid", ""))
        commenter_qq: str = str(raw.get("commenter_qq", ""))
        comment_content: str = raw.get("comment_content", "")

        if not (feed_id and comment_tid and commenter_qq):
            logger.warning(f"评论数据字段不完整，已跳过: {raw}")
            return None

        envelope: MessageEnvelope = {
            "direction": "incoming",
            "message_info": {
                "platform": self.platform,
                "message_id": comment_tid,
                "user_info": {
                    "platform": self.platform,
                    "user_id": commenter_qq,
                    "role": UserRole.MEMBER,
                    "user_nickname": raw.get("commenter_name", ""),
                },
                "group_info": {
                    "platform": self.platform,
                    "group_id": f"qzone_feed_{feed_id}",
                    "group_name": f"qzone_feed_{feed_id}",
                },
            },
            "message_segment": {
                "type": "text",
                "data": comment_content,
            },
            "raw_message": {
                "feed_id": feed_id,
                "comment_tid": comment_tid,
                "feed_content": raw.get("feed_content", ""),
                "feed_images": raw.get("feed_images", []),
                "story_time": raw.get("story_time", ""),
                "comment_time": raw.get("comment_time", ""),
                "all_comments": raw.get("all_comments", []),
                "commenter_name": raw.get("commenter_name", ""),
                "commenter_qq": commenter_qq,
            },
        }
        return envelope

    async def _send_platform_message(self, envelope: MessageEnvelope) -> None:  # type: ignore[override]
        """发送消息到 QQ 空间（空实现）。

        QZoneChatter 直接通过 QZoneService.reply_comment() 回复评论，
        不经过 Adapter 发送，因此此处为空实现。

        Args:
            envelope: 消息信封（未使用）
        """

    async def get_bot_info(self) -> dict[str, Any]:
        """返回 Bot 基本信息。

        Returns:
            包含 platform 和 bot_id 的字典
        """
        cfg = self._cfg
        bot_qq = cfg.general.bot_qq if cfg is not None else ""
        return {
            "platform": self.platform,
            "bot_id": bot_qq,
        }
