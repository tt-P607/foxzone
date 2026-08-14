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

        对加载数据做类型校验：``data`` 必须是 dict 且每个 feed 值必须是
        dict（``{comment_id: timestamp}``），异常条目丢弃，避免后续
        ``has_replied`` 因类型异常而崩溃。应在插件加载时调用。

        """
        loaded = await storage_api.load_json(_STORE_NAMESPACE, _STORE_KEY)
        if loaded is None or not isinstance(loaded, dict):
            logger.debug("回复跟踪数据文件不存在或格式异常，使用空数据初始化。")
            self._data = {"data": {}}
            return
        raw_data = loaded.get("data", {})
        if not isinstance(raw_data, dict):
            logger.warning("回复跟踪数据 data 字段非 dict，使用空数据初始化。")
            self._data = {"data": {}}
            return

        cleaned: dict[str, dict[str, float]] = {}
        dropped = 0
        for feed_id, comments in raw_data.items():
            if not isinstance(comments, dict):
                dropped += 1
                continue
            cleaned[feed_id] = {
                str(cid): float(ts)
                for cid, ts in comments.items()
                if isinstance(ts, (int, float))
            }
        self._data = {"data": cleaned}
        if dropped:
            logger.warning(f"回复跟踪数据加载时丢弃 {dropped} 条格式异常 feed 条目")
        feed_count = len(cleaned)
        comment_count = sum(len(v) for v in cleaned.values())
        logger.info(
            f"已加载回复跟踪数据：{feed_count} 条说说，{comment_count} 条评论记录。"
        )

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

    # ------------------------------------------------------------------
    # 私有辅助方法
    # ------------------------------------------------------------------

    async def _persist(self) -> None:
        """将当前数据写入 storage_api 持久化存储。

        若保存失败，仅记录错误日志，不抛出异常。
        """
        try:
            await storage_api.save_json(_STORE_NAMESPACE, _STORE_KEY, self._data)
        except Exception as e:
            logger.error(f"持久化回复跟踪数据失败: {e}")
