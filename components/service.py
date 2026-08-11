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

from ..core.interaction_log import SOURCE_AGENT
from ..core.llm.vision import describe_images

if typing.TYPE_CHECKING:
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
        return self._plugin.runtime

    @property
    def _cfg(self):
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
        """读取指定 QQ 的说说列表（含全部评论，自动分页补全）。

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
        """读取自己的说说及全部评论。"""
        return await self.list_feeds(await self._rt.bot_qq(), num=num, skip_commented=False)

    async def get_feed_detail(
        self, host_qq: str, feed_id: str
    ) -> dict[str, Any] | None:
        """精确拉取单条说说完整详情（正文+图片+评论）。

        基于 ``emotion_cgi_msgdetail_v6``，一次请求拿到 feed 全貌；
        用于「外部空间评论回查」按 InteractionLog 标记精准命中。
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
        """精确拉取单条说说的评论（含 list_3 楼中楼回复）。

        使用 ``emotion_cgi_msgdetail_v6`` 接口按 tid 精准查询，
        1 个请求即可获得完整评论区。

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
        只返回未点赞的说说（monitor_list_feeds 内部已过滤已点赞项）。

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
        """评论指定说说。"""
        if not text.strip():
            return False

        try:
            return await self._rt.with_client(
                lambda client: client.comment(str(target_qq), str(feed_id), text.strip())
            )
        except Exception as exc:
            logger.error(f"评论说说失败: {exc}")
            return False

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
        target_name: str,
        reply_text: str,
        comment_tid: str,
        commenter_qq: str = "",
    ) -> bool:
        """回复指定评论。

        Raises:
            RuntimeError: 不可重试错误（cookie 失效 / -10049 限流），
                向上传播供批量处理器停止重试。
        """
        if not reply_text.strip():
            return False

        try:
            return await self._rt.with_client(
                lambda client: client.reply(
                    str(feed_id),
                    str(host_qq),
                    target_name,
                    reply_text.strip(),
                    str(comment_tid),
                    str(commenter_qq),
                )
            )
        except RuntimeError:
            raise
        except Exception as exc:
            logger.error(f"回复评论失败: {exc}")
            return False

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
