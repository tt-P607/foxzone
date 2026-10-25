"""QZoneService 组件：作为 BaseService 暴露 QQ 空间原子能力。

自身**无状态**——所有持久化状态与串行锁由插件级单例
:class:`~plugins.foxzone.runtime.QZoneRuntime` 持有（``plugin.runtime``）。
框架的 ``ServiceManager.get_service()`` 每次创建新 Service 实例，
但它们全部指向同一个 runtime，因此状态一致性与锁语义不受影响。

批量编排逻辑（外部接力 / 好友监控）位于 ``autopilot/`` 包，
本 Service 只保留可被 Tool / Command / 其他插件调用的原子操作。
"""

from __future__ import annotations

import time
import typing
from typing import Any

from src.app.plugin_system.api import storage_api
from src.app.plugin_system.api.log_api import COLOR, get_logger
from src.app.plugin_system.base import BaseService

from ..core.comment_tree import opaque_id, reply_target, resolve_comment
from ..core.http.comments import QZoneUnknownResultError
from ..core.interaction_log import ACTION_COMMENT, SOURCE_AGENT
from ..core.llm.vision import describe_images

if typing.TYPE_CHECKING:
    from ..config import FoxZoneConfig
    from ..plugin import FoxZonePlugin
    from ..runtime import QZoneRuntime

logger = get_logger("foxzone.service", color=COLOR.ORANGE)


class QZoneService(BaseService):
    """QQ 空间统一服务出口（原子能力门面）。"""

    name = "qzone_service"
    description = "QQ 空间 API 服务（发布说说、读取动态、互动等）"

    def __init__(self, plugin: "FoxZonePlugin") -> None:  # type: ignore[override]
        """初始化 QZoneService（不创建任何状态，全部取自 runtime）。"""
        super().__init__(plugin)
        self._plugin: "FoxZonePlugin" = plugin  # type: ignore[assignment]

    @property
    def _rt(self) -> "QZoneRuntime":
        """插件级运行时单例。"""
        return self._plugin.runtime  # type: ignore[attr-defined]

    @property
    def _cfg(self) -> "FoxZoneConfig":
        """插件配置。"""
        return self._rt.config

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------

    async def publish_feed(
        self,
        content: str,
        images: list[bytes] | None = None,
    ) -> bool:
        """发布一条说说。"""
        clean_content = content.strip()
        if not clean_content:
            logger.warning("说说内容为空，已拒绝发布。")
            return False

        try:
            success = await self._rt.with_client(
                lambda client: client.publish(clean_content, images or [])
            )
        except Exception as exc:
            logger.error(f"发布说说时发生异常: {exc}")
            return False

        if success:
            await self._save_send_history(clean_content)
        return success

    async def publish_generated_feed(
        self,
        topic: str = "",
        context: str | None = None,
    ) -> dict[str, Any]:
        """按主题生成内容并发布说说。

        Args:
            topic: 说说主题
            context: 附加的聊天上下文

        Returns:
            ``{"success": bool, "message": str}``
        """
        story = await self._rt.content.generate_story(topic, context=context)
        if not story:
            return {"success": False, "message": "生成说说内容失败"}

        success = await self.publish_feed(story)
        if success:
            return {"success": True, "message": story}
        return {"success": False, "message": "发布说说至 QQ 空间失败"}

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def list_feeds(
        self,
        target_qq: str,
        num: int = 5,
        skip_commented: bool = True,
        paginate_comments: bool = True,
    ) -> list[dict[str, Any]]:
        """读取指定 QQ 的说说和有界评论页，保留完整性状态。

        Args:
            target_qq: 目标 QQ 号
            num: 读取条数
            skip_commented: 是否跳过 Bot 已评论的说说（监控互动场景传 True，纯读取展示传 False）
            paginate_comments: 是否对每条说说调用评论详情接口补全长评论区。
        """
        target = str(target_qq).strip()
        if not target:
            return []

        try:
            feeds = await self._rt.with_client(
                lambda client: client.list_feeds(
                    target,
                    max(1, num),
                    skip_commented=skip_commented,
                    paginate_comments=paginate_comments,
                )
            )
        except Exception as exc:
            logger.error(f"读取 QQ {target} 的说说失败: {exc}")
            return []

        return feeds or []

    async def list_own_feeds_with_comments(self, num: int = 5) -> list[dict[str, Any]]:
        """读取自己的说说及可见评论，保留完整性状态。"""
        return await self.list_feeds(await self._rt.bot_qq(), num=num, skip_commented=False)

    async def get_feed_detail(
        self, host_qq: str, feed_id: str
    ) -> dict[str, Any] | None:
        """精确读取单条说说的正文、图片和有界评论页。

        基于 ``emotion_cgi_msgdetail_v6``，分页接续一级评论；
        ``list_3`` 只包含服务端内联返回的楼中楼。
        """
        try:
            return await self._rt.with_client(
                lambda client: client.fetch_feed_detail(
                    host_qq=str(host_qq), tid=str(feed_id)
                )
            )
        except Exception as exc:
            logger.error(f"读取说说 {feed_id}（host={host_qq}）详情失败: {exc}")
            return None

    async def get_feed_comments(
        self, host_qq: str, feed_id: str
    ) -> list[dict[str, Any]]:
        """兼容接口：投影单条说说详情中当前可见的评论。

        评论分页可能部分完成；需要完整性状态的消费者应调用
        :meth:`get_feed_detail`。

        Args:
            host_qq: 说说主人 QQ 号
            feed_id: 说说 tid

        Returns:
            评论字典列表；接口失败或未命中返回空。
        """
        detail = await self.get_feed_detail(host_qq, feed_id)
        if not detail:
            return []
        return list(detail.get("comments", []) or [])

    async def get_monitor_feeds(self, num: int = 10) -> list[dict[str, Any]]:
        """获取好友动态流（用于好友说说自动监控）。

        返回的每项包含 target_qq, tid, content, images, comments 等字段。
        ``liked`` 保留实际点赞状态，由上层过滤已处理动态。

        Args:
            num: 最多获取的好友说说数量

        Returns:
            好友说说数据字典列表
        """
        try:
            feeds = await self._rt.with_client(
                lambda client: client.monitor_list_feeds(max(1, num))
            )
            return feeds or []
        except Exception as exc:
            logger.error(f"获取好友动态失败: {exc}")
            return []

    # ------------------------------------------------------------------
    # 互动
    # ------------------------------------------------------------------

    async def comment(self, target_qq: str, feed_id: str, text: str) -> bool:
        """串行发表评论，并记录明确成功或待核实状态。"""
        if not text.strip():
            return False

        host_qq = str(target_qq)
        post_id = str(feed_id)
        account_qq = await self._rt.bot_qq()
        if not account_qq or not host_qq or not post_id:
            return False
        tracker = self._rt.reply_tracker
        async with self._rt.reply_send_lock:
            if tracker.is_comment_pending(account_qq, host_qq, post_id):
                raise QZoneUnknownResultError("该说说已有待核实评论，禁止再次发送")
            try:
                success = await self._rt.with_client(
                    lambda client: client.comment(host_qq, post_id, text.strip())
                )
            except QZoneUnknownResultError:
                await tracker.mark_comment_pending(account_qq, host_qq, post_id)
                raise
            except RuntimeError as exc:
                logger.error(f"评论说说失败: {exc}")
                return False

            if success:
                await self.mark_interaction(host_qq, post_id, ACTION_COMMENT, SOURCE_AGENT)
            return bool(success)

    async def like(self, target_qq: str, feed_id: str) -> bool:
        """点赞指定说说。"""
        try:
            return await self._rt.with_client(
                lambda client: client.like(str(target_qq), str(feed_id))
            )
        except Exception as exc:
            logger.error(f"点赞说说失败: {exc}")
            return False

    async def reply_comment(
        self,
        feed_id: str,
        host_qq: str,
        target_name: str = "",
        reply_text: str = "",
        comment_tid: str = "",
        commenter_qq: str = "",
        *,
        comment_uin: str = "",
        parent_comment_id: str = "",
        parent_comment_uin: str = "",
    ) -> bool:
        """唯一核验目标评论后，串行回复并记录作用域状态。

        ``target_name`` 保留位置参数契约；实际提及昵称从核验后的详情读取。
        ``commenter_qq`` 与关键字 ``comment_uin`` 均表示实际目标作者。

        Raises:
            QZoneUnknownResultError: 写入结果未知或目标已有待核实写入
            RuntimeError: 明确服务端拒绝
        """
        if not reply_text.strip():
            return False

        post_id = str(feed_id)
        host = str(host_qq)
        target_id = opaque_id(comment_tid)
        target_author = opaque_id(comment_uin or commenter_qq)
        if comment_uin and commenter_qq and str(comment_uin) != str(commenter_qq):
            return False
        parent_id = opaque_id(parent_comment_id)
        parent_author = opaque_id(parent_comment_uin)
        if not target_id or not target_author or bool(parent_id) != bool(parent_author):
            return False

        account_qq = await self._rt.bot_qq()
        if not account_qq or not host or not post_id:
            return False
        tracker = self._rt.reply_tracker
        scope = {
            "account_qq": account_qq,
            "host_qq": host,
            "parent_comment_id": parent_id or target_id,
            "parent_comment_uin": parent_author or target_author,
            "comment_uin": target_author,
        }
        async with self._rt.reply_send_lock:
            status = tracker.get_status(post_id, target_id, **scope)
            if status == "pending":
                raise QZoneUnknownResultError("该评论已有待核实回复，禁止再次发送")
            if status in {"replied", "skipped", "legacy"}:
                return False

            detail = await self.get_feed_detail(host, post_id)
            comments = detail.get("comments", []) if detail else []
            if not isinstance(comments, list):
                return False
            target = resolve_comment(
                comments,
                target_id,
                parent_comment_id=parent_id,
                comment_uin=target_author,
                parent_comment_uin=parent_author,
            )
            if target is None:
                return False
            raw_parent_id = target.get("parent_comment_id")
            if raw_parent_id is None:
                raw_parent_id = target.get("parent_tid")
            if opaque_id(raw_parent_id) and not parent_id:
                return False
            actual_parent = reply_target(target, comments)
            if actual_parent is None:
                return False
            actual_parent_id, actual_parent_author = actual_parent
            if (parent_id and parent_id != actual_parent_id) or (
                parent_author and parent_author != actual_parent_author
            ):
                return False

            actual_scope = {
                **scope,
                "parent_comment_id": actual_parent_id,
                "parent_comment_uin": actual_parent_author,
            }
            actual_status = tracker.get_status(post_id, target_id, **actual_scope)
            if actual_status == "pending":
                raise QZoneUnknownResultError("该评论已有待核实回复，禁止再次发送")
            if actual_status is not None:
                return False

            try:
                result = await self._rt.with_client(
                    lambda client: client.reply(
                        post_id,
                        host,
                        str(target.get("nickname") or ""),
                        reply_text.strip(),
                        actual_parent_id,
                        target_author,
                        parent_comment_uin=actual_parent_author,
                    )
                )
            except QZoneUnknownResultError:
                await tracker.mark_pending(post_id, target_id, **actual_scope)
                raise
            except RuntimeError:
                raise

            reply_id = result.get("comment_id") if isinstance(result, dict) else None
            valid_reply_id = (
                isinstance(reply_id, int) and not isinstance(reply_id, bool) and reply_id >= 0
                or isinstance(reply_id, str) and bool(reply_id.strip())
            )
            if not valid_reply_id:
                await tracker.mark_pending(post_id, target_id, **actual_scope)
                raise QZoneUnknownResultError("回复回执缺少新回复 ID，需先核实投递状态")
            await tracker.mark_as_replied(post_id, target_id, **actual_scope)
            await self.mark_interaction(host, post_id, ACTION_COMMENT, SOURCE_AGENT)
            return True

    # ------------------------------------------------------------------
    # 状态代理（转发到 runtime）
    # ------------------------------------------------------------------

    async def has_replied_comment(self, feed_id: str, comment_tid: str) -> bool:
        """检查是否已回复过指定评论。"""
        return self._rt.reply_tracker.has_replied(feed_id, comment_tid)

    async def mark_comment_replied(self, feed_id: str, comment_tid: str) -> None:
        """标记评论已回复。"""
        await self._rt.reply_tracker.mark_as_replied(feed_id, comment_tid)

    def has_interacted(self, target_qq: str, feed_id: str) -> bool:
        """判断是否已与该说说有过任何互动（点赞或评论）。"""
        return self._rt.interaction_log.has_interacted(target_qq, feed_id)

    async def mark_interaction(
        self,
        target_qq: str,
        feed_id: str,
        action: str,
        source: str = SOURCE_AGENT,
    ) -> None:
        """记录一次对好友说说的互动。

        Args:
            target_qq: 说说主人 QQ 号
            feed_id: 说说 tid
            action: 互动类型，使用 ``ACTION_LIKE`` / ``ACTION_COMMENT``
            source: 来源标识，``SOURCE_AGENT`` 或 ``SOURCE_POLL``
        """
        self._rt.interaction_log.mark(target_qq, feed_id, action, source)
        await self._rt.interaction_log.save()

    async def mark_followup_checked(self, target_qq: str, feed_id: str) -> None:
        """更新某 (target_qq, feed_id) 的最近回查时间戳并落盘。"""
        self._rt.interaction_log.mark_followup_checked(target_qq, feed_id)
        await self._rt.interaction_log.save()

    async def iter_followup_qqs(
        self, exclude_qq: str = "", limit: int = 0,
        max_feed_age_hours: float = 0,
    ) -> list[tuple[str, list[str]]]:
        """按「最久未回查」聚合返回需回查的 (target_qq, [feed_ids…])。

        Args:
            exclude_qq: 排除该 QQ（通常是 bot 自己）
            limit: 本轮最多回查多少个 QQ；<= 0 表示不限
            max_feed_age_hours: 评论过的说说超过该时长（小时）则不再回查；
                <= 0 表示不限。
        """
        return self._rt.interaction_log.iter_followup_qqs(
            exclude_target_qq=exclude_qq, limit=limit,
            max_feed_age_hours=max_feed_age_hours,
        )

    async def iter_followup_feeds(
        self, exclude_qq: str = "", limit: int = 0,
        max_feed_age_hours: float = 0, only_target_qq: str = "",
    ) -> list[tuple[str, str]]:
        """按「最久未回查」返回需回查的 (target_qq, feed_id) 列表（feed 粒度）。

        Args:
            exclude_qq: 排除该 QQ（通常是 bot 自己）
            limit: 本轮最多回查多少条 feed；<= 0 表示不限
            max_feed_age_hours: 评论过的说说超过该时长（小时）则不再回查；
                <= 0 表示不限。
            only_target_qq: 仅返回该 QQ 下的 feed（非空时生效）
        """
        return self._rt.interaction_log.iter_followup_feeds(
            exclude_target_qq=exclude_qq, limit=limit,
            max_feed_age_hours=max_feed_age_hours, only_target_qq=only_target_qq,
        )

    # ------------------------------------------------------------------
    # 识图 / LLM 决策 / 文本块（转发到 core.llm）
    # ------------------------------------------------------------------

    async def describe_images(self, urls: list[str]) -> dict[str, str]:
        """批量获取图片的视觉识别描述（有缓存则复用，否则调用 vision LLM）。

        若 config.llm.vision_model_task 为空，则跳过识图，返回空字典。

        Args:
            urls: 图片 URL 列表

        Returns:
            ``{url: description}`` 字典，识别失败或未识别的 URL 不在结果中
        """
        return await describe_images(
            urls, self._cfg.llm.vision_model_task, self._rt.vision_cache
        )

    async def generate_batch_replies(
        self,
        comment_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """批量生成评论回复决策（转发 ContentService）。"""
        return await self._rt.content.generate_batch_replies(comment_items)

    async def generate_feed_decisions(
        self,
        feed_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """批量生成好友说说评论决策（转发 ContentService）。"""
        return await self._rt.content.generate_feed_decisions(feed_items)

    async def get_recent_self_feeds_block(self, num: int = 3) -> str:
        """生成「自己最近 N 条说说」的完整上下文文本块（转发 ContentService）。"""
        return await self._rt.content.get_recent_self_feeds_block(num=num)

    # ------------------------------------------------------------------
    # 私有辅助
    # ------------------------------------------------------------------

    async def _save_send_history(self, story: str) -> None:
        """将刚发送的说说内容追加到发送历史（最多保留 20 条）。"""
        try:
            data = await storage_api.load_json("foxzone", "send_history")
            if data is None:
                data = {"records": []}

            records: list[dict[str, Any]] = data.get("records", [])
            records.append(
                {
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "text": story,
                }
            )
            data["records"] = records[-20:]
            await storage_api.save_json("foxzone", "send_history", data)
        except Exception as exc:
            logger.warning(f"保存发送历史失败: {exc}")
