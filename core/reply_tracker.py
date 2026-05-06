"""回复跟踪服务模块。

记录已回复的 QQ 空间评论，防止对同一条评论重复回复。
数据持久化使用 storage_api 的分区 JSON 存储。
"""

from __future__ import annotations

import time

from src.app.plugin_system.api.log_api import get_logger, COLOR
from src.app.plugin_system.api import storage_api

logger = get_logger("foxzone.reply_tracker_service", color=COLOR.ORANGE)

# storage_api 分区和键名
_STORE_NAMESPACE = "foxzone"
_STORE_KEY = "reply_tracker"


class ReplyTrackerService:
    """已回复评论跟踪服务。

    使用 storage_api 分区 JSON 存储持久化已回复记录，数据结构为：
    ``{"data": {feed_id: {comment_id: timestamp_float}}}``

    Attributes:
        _data: 从持久化存储加载的数据字典
    """

    def __init__(self) -> None:
        """初始化回复跟踪服务。"""
        self._data: dict[str, dict[str, dict[str, float]]] = {"data": {}}

    async def initialize(self) -> None:
        """从持久化存储加载已有回复记录。

        应在插件加载时的异步初始化流程中调用。
        若存储文件不存在，则使用空数据初始化。
        """
        loaded = await storage_api.load_json(_STORE_NAMESPACE, _STORE_KEY)
        if loaded is not None:
            if isinstance(loaded, dict) and "data" in loaded:
                self._data = loaded
                feed_count = len(self._data["data"])
                comment_count = sum(
                    len(v) for v in self._data["data"].values()
                )
                logger.info(
                    f"已加载回复跟踪数据：{feed_count} 条说说，{comment_count} 条评论记录。"
                )
            else:
                logger.warning("回复跟踪数据格式不正确，将使用空数据初始化。")
        else:
            logger.debug("回复跟踪数据文件不存在，使用空数据初始化。")

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def has_replied(self, feed_id: str, comment_id: str) -> bool:
        """判断是否已对指定评论回复过。

        Args:
            feed_id: 说说 ID
            comment_id: 评论 ID

        Returns:
            True 表示已回复，False 表示尚未回复
        """
        return comment_id in self._data["data"].get(feed_id, {})

    def get_replied_comments(self, feed_id: str) -> set[str]:
        """获取指定说说下所有已回复的评论 ID 集合。

        Args:
            feed_id: 说说 ID

        Returns:
            已回复评论 ID 的集合（如果无记录则返回空集合）
        """
        return set(self._data["data"].get(feed_id, {}).keys())

    # ------------------------------------------------------------------
    # 写入接口
    # ------------------------------------------------------------------

    async def mark_as_replied(self, feed_id: str, comment_id: str) -> None:
        """标记指定评论已回复，并持久化到存储。

        Args:
            feed_id: 说说 ID
            comment_id: 评论 ID
        """
        if feed_id not in self._data["data"]:
            self._data["data"][feed_id] = {}

        self._data["data"][feed_id][comment_id] = time.time()
        await self._persist()
        logger.debug(f"已标记评论 {comment_id}（说说 {feed_id}）为已回复。")

    async def remove_reply_record(self, feed_id: str, comment_id: str) -> None:
        """删除指定评论的回复记录。

        若记录不存在，则静默忽略（幂等操作）。

        Args:
            feed_id: 说说 ID
            comment_id: 评论 ID
        """
        feed_records = self._data["data"].get(feed_id)
        if feed_records and comment_id in feed_records:
            del feed_records[comment_id]
            # 若该说说下已无记录，则清理空字典
            if not feed_records:
                del self._data["data"][feed_id]
            await self._persist()
            logger.debug(f"已删除评论 {comment_id}（说说 {feed_id}）的回复记录。")

    async def clear_feed_records(self, feed_id: str) -> None:
        """清除指定说说下的所有回复记录。

        Args:
            feed_id: 说说 ID
        """
        if feed_id in self._data["data"]:
            count = len(self._data["data"][feed_id])
            del self._data["data"][feed_id]
            await self._persist()
            logger.info(f"已清除说说 {feed_id} 下的 {count} 条回复记录。")

    # ------------------------------------------------------------------
    # 私有辅助方法
    # ------------------------------------------------------------------

    async def _persist(self) -> None:
        """将当前数据写入 storage_api 持久化存储。

        若保存失败，仅记录错误日志，不抛出异常（回复跟踪为非关键功能）。
        """
        try:
            await storage_api.save_json(_STORE_NAMESPACE, _STORE_KEY, self._data)
        except Exception as e:
            logger.error(f"持久化回复跟踪数据失败: {e}")
