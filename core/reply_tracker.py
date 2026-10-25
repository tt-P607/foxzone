"""回复跟踪服务模块。

记录已回复的 QQ 空间评论，防止对同一条评论重复回复。
数据持久化使用 storage_api 的分区 JSON 存储。
"""

from __future__ import annotations

import json
import time
from typing import Any

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
        self._data: dict[str, Any] = {"data": {}, "scoped": {}, "pending_comments": {}}

    async def initialize(self) -> None:
        """从持久化存储加载已有回复记录。

        对加载数据做类型校验：``data`` 必须是 dict 且每个 feed 值必须是
        dict（``{comment_id: timestamp}``），异常条目丢弃，避免后续
        ``has_replied`` 因类型异常而崩溃。应在插件加载时调用。

        """
        loaded = await storage_api.load_json(_STORE_NAMESPACE, _STORE_KEY)
        if loaded is None or not isinstance(loaded, dict):
            logger.debug("回复跟踪数据文件不存在或格式异常，使用空数据初始化。")
            self._data = {"data": {}, "scoped": {}, "pending_comments": {}}
            return
        raw_data = loaded.get("data", {})
        if not isinstance(raw_data, dict):
            logger.warning("回复跟踪数据 data 字段非 dict，使用空数据初始化。")
            self._data = {"data": {}, "scoped": {}, "pending_comments": {}}
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
        raw_scoped = loaded.get("scoped", {})
        scoped = (
            {
                key: entry
                for key, entry in raw_scoped.items()
                if isinstance(key, str)
                and isinstance(entry, dict)
                and entry.get("status") in {"replied", "pending", "skipped"}
            }
            if isinstance(raw_scoped, dict)
            else {}
        )
        raw_pending = loaded.get("pending_comments", {})
        pending = (
            {
                key: timestamp
                for key, timestamp in raw_pending.items()
                if isinstance(key, str) and isinstance(timestamp, (int, float))
            }
            if isinstance(raw_pending, dict)
            else {}
        )
        self._data = {"data": cleaned, "scoped": scoped, "pending_comments": pending}
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
        return self.get_status(feed_id, comment_id) in {"replied", "legacy"}

    def _scope_key(
        self,
        feed_id: str,
        comment_id: str,
        *,
        account_qq: str,
        host_qq: str,
        parent_comment_id: str,
        parent_comment_uin: str,
        comment_uin: str,
    ) -> str | None:
        """构建完整回复作用域键；缺少任一身份字段时返回 None。"""
        scope = (
            account_qq,
            host_qq,
            feed_id,
            parent_comment_id,
            parent_comment_uin,
            comment_id,
            comment_uin,
        )
        if not all(str(value) for value in scope):
            return None
        return json.dumps([str(value) for value in scope], separators=(",", ":"))

    def get_status(
        self,
        feed_id: str,
        comment_id: str,
        *,
        account_qq: str = "",
        host_qq: str = "",
        parent_comment_id: str = "",
        parent_comment_uin: str = "",
        comment_uin: str = "",
    ) -> str | None:
        """读取精确作用域状态，或返回无法归属的 legacy 状态。"""
        key = self._scope_key(
            feed_id,
            comment_id,
            account_qq=account_qq,
            host_qq=host_qq,
            parent_comment_id=parent_comment_id,
            parent_comment_uin=parent_comment_uin,
            comment_uin=comment_uin,
        )
        if key is not None:
            entry = self._data["scoped"].get(key)
            if isinstance(entry, dict):
                return str(entry["status"])
        if comment_id in self._data["data"].get(feed_id, {}):
            return "legacy"
        return None

    def is_blocked(
        self,
        feed_id: str,
        comment_id: str,
        *,
        account_qq: str = "",
        host_qq: str = "",
        parent_comment_id: str = "",
        parent_comment_uin: str = "",
        comment_uin: str = "",
    ) -> bool:
        """判断该作用域是否应从自动处理队列排除。"""
        return self.get_status(
            feed_id,
            comment_id,
            account_qq=account_qq,
            host_qq=host_qq,
            parent_comment_id=parent_comment_id,
            parent_comment_uin=parent_comment_uin,
            comment_uin=comment_uin,
        ) in {"replied", "pending", "skipped", "legacy"}

    # ------------------------------------------------------------------
    # 写入接口
    # ------------------------------------------------------------------

    def is_comment_pending(self, account_qq: str, host_qq: str, feed_id: str) -> bool:
        """判断该账号在指定说说下是否存在待核实的普通评论。"""
        key = json.dumps([str(account_qq), str(host_qq), str(feed_id)], separators=(",", ":"))
        return key in self._data["pending_comments"]

    async def mark_comment_pending(self, account_qq: str, host_qq: str, feed_id: str) -> None:
        """记录普通评论投递待核实，保留内存阻止状态并持久化。"""
        if not all((account_qq, host_qq, feed_id)):
            raise ValueError("普通评论待核实状态必须提供账号、主人和说说 ID")
        key = json.dumps([str(account_qq), str(host_qq), str(feed_id)], separators=(",", ":"))
        self._data["pending_comments"][key] = time.time()
        await self._persist()

    async def mark_as_replied(
        self,
        feed_id: str,
        comment_id: str,
        *,
        account_qq: str = "",
        host_qq: str = "",
        parent_comment_id: str = "",
        parent_comment_uin: str = "",
        comment_uin: str = "",
    ) -> None:
        """标记指定评论已回复，并持久化到存储。

        Args:
            feed_id: 说说 ID
            comment_id: 评论 ID
        """
        await self._mark(
            "replied", feed_id, comment_id, account_qq=account_qq,
            host_qq=host_qq, parent_comment_id=parent_comment_id,
            parent_comment_uin=parent_comment_uin, comment_uin=comment_uin,
        )

    async def mark_pending(
        self,
        feed_id: str,
        comment_id: str,
        *,
        account_qq: str = "",
        host_qq: str = "",
        parent_comment_id: str = "",
        parent_comment_uin: str = "",
        comment_uin: str = "",
    ) -> None:
        """记录写入结果未知；该状态阻止自动重发但不表示成功。"""
        await self._mark(
            "pending", feed_id, comment_id, account_qq=account_qq,
            host_qq=host_qq, parent_comment_id=parent_comment_id,
            parent_comment_uin=parent_comment_uin, comment_uin=comment_uin,
        )

    async def mark_skipped(
        self,
        feed_id: str,
        comment_id: str,
        *,
        account_qq: str = "",
        host_qq: str = "",
        parent_comment_id: str = "",
        parent_comment_uin: str = "",
        comment_uin: str = "",
    ) -> None:
        """记录已明确跳过的评论，不将其视为已回复。"""
        await self._mark(
            "skipped", feed_id, comment_id, account_qq=account_qq,
            host_qq=host_qq, parent_comment_id=parent_comment_id,
            parent_comment_uin=parent_comment_uin, comment_uin=comment_uin,
        )

    async def _mark(
        self,
        status: str,
        feed_id: str,
        comment_id: str,
        *,
        account_qq: str,
        host_qq: str,
        parent_comment_id: str,
        parent_comment_uin: str,
        comment_uin: str,
    ) -> None:
        """写入作用域状态；持久化失败时保留内存记录并向上传播。"""
        key = self._scope_key(
            feed_id, comment_id, account_qq=account_qq, host_qq=host_qq,
            parent_comment_id=parent_comment_id,
            parent_comment_uin=parent_comment_uin, comment_uin=comment_uin,
        )
        if key is None:
            if status != "replied":
                raise ValueError("pending/skipped 状态必须提供完整评论作用域")
            bucket = self._data["data"].setdefault(feed_id, {})
            previous = bucket.get(comment_id)
            bucket[comment_id] = time.time()
            try:
                await self._persist()
            except Exception:
                if previous is None:
                    del bucket[comment_id]
                    if not bucket:
                        del self._data["data"][feed_id]
                else:
                    bucket[comment_id] = previous
                raise
            return

        scoped = self._data["scoped"]
        scoped[key] = {"status": status, "timestamp": time.time()}
        await self._persist()

    # ------------------------------------------------------------------
    # 私有辅助方法
    # ------------------------------------------------------------------

    async def _persist(self) -> None:
        """将当前数据写入 storage_api 持久化存储。

        保存失败时向调用方传播，避免下一轮误以为状态已落盘。
        """
        await storage_api.save_json(_STORE_NAMESPACE, _STORE_KEY, self._data)
