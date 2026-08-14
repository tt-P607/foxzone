"""互动记录持久化模块。

统一跟踪 bot 对好友 QQ 空间说说的互动历史（点赞、评论），
无论来源是自动轮询还是模型主动调用 Agent，均写入同一数据源。

存储结构：
``{"log": {"{target_qq}:{feed_id}": {"liked": bool, "commented": bool,
                                     "source": "poll"|"agent"|"both",
                                     "last_ts": float}}}``
"""

from __future__ import annotations

import time

from src.app.plugin_system.api import storage_api
from src.app.plugin_system.api.log_api import COLOR, get_logger

logger = get_logger("foxzone.interaction_log", color=COLOR.ORANGE)

_STORE_NAMESPACE = "foxzone"
_STORE_KEY = "interaction_log"

# 互动来源标识
SOURCE_POLL = "poll"
SOURCE_AGENT = "agent"
SOURCE_BOTH = "both"

# 互动类型
ACTION_LIKE = "liked"
ACTION_COMMENT = "commented"
ACTION_READ = "read"
#: 真实点赞状态标记（外部观察到的 bot 账号是否已点赞，不表示 bot 程序执行了点赞）
LIKED_STATE = "liked_state"


def _make_key(target_qq: str, feed_id: str) -> str:
    """生成统一记录键。"""
    return f"{target_qq}:{feed_id}"


class InteractionLog:
    """好友说说互动记录服务。

    同时被自动轮询（poll）和 Agent 主动调用两条路径使用，
    记录结构相同，通过 source 字段区分来源。

    Attributes:
        _data: 内存中的日志字典
        _dirty: 是否有未保存的更改
    """

    def __init__(self) -> None:
        """初始化互动记录（未加载）。"""
        self._data: dict[str, dict[str, object]] = {}
        self._dirty: bool = False

    async def initialize(self) -> None:
        """从持久化存储加载互动记录。

        对加载数据做类型与键格式校验：仅保留 ``{target_qq}:{feed_id}``
        形式且值为 dict 的条目，其余视为脏数据丢弃，保证后续查询
        （``entry.get(...)``）不会因类型异常而崩溃。
        """
        loaded = await storage_api.load_json(_STORE_NAMESPACE, _STORE_KEY)
        if loaded is None or not isinstance(loaded, dict):
            logger.debug("互动记录文件不存在或格式异常，使用空记录初始化")
            self._data = {}
            return
        log_data = loaded.get("log", {})
        if not isinstance(log_data, dict):
            logger.warning("互动记录 log 字段非 dict，使用空记录初始化")
            self._data = {}
            return

        cleaned: dict[str, dict[str, object]] = {}
        dropped = 0
        for key, entry in log_data.items():
            if not isinstance(key, str) or ":" not in key:
                dropped += 1
                continue
            if not isinstance(entry, dict):
                dropped += 1
                continue
            cleaned[key] = entry
        self._data = cleaned
        if dropped:
            logger.warning(f"互动记录加载时丢弃 {dropped} 条格式异常条目")
        logger.debug(f"已加载互动记录：{len(self._data)} 条")

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def has_liked(self, target_qq: str, feed_id: str) -> bool:
        """bot 账号对该说说是否处于已点赞状态。

        同时考虑外部同步的真实状态（``LIKED_STATE``，如用户手动用 bot 账号
        点的赞）与 bot 程序执行过点赞（``ACTION_LIKE``）。

        Args:
            target_qq: 说说主人的 QQ 号
            feed_id: 说说 tid

        Returns:
            True 表示已点赞
        """
        entry = self._data.get(_make_key(target_qq, feed_id), {})
        return bool(entry.get(LIKED_STATE)) or bool(entry.get(ACTION_LIKE))

    def has_commented(self, target_qq: str, feed_id: str) -> bool:
        """是否已对该说说发过评论。

        Args:
            target_qq: 说说主人的 QQ 号
            feed_id: 说说 tid

        Returns:
            True 表示已评论
        """
        return bool(self._data.get(_make_key(target_qq, feed_id), {}).get(ACTION_COMMENT))

    def has_interacted(self, target_qq: str, feed_id: str) -> bool:
        """是否对该说说有任何互动（点赞或评论）。

        Args:
            target_qq: 说说主人的 QQ 号
            feed_id: 说说 tid

        Returns:
            True 表示有任何互动
        """
        key = _make_key(target_qq, feed_id)
        entry = self._data.get(key, {})
        return bool(entry.get(ACTION_LIKE)) or bool(entry.get(ACTION_COMMENT))

    def has_seen(self, target_qq: str, feed_id: str) -> bool:
        """是否已处理过该说说（点赞、评论或仅读）。

        好友监控关闭强制点赞时，未点赞但已读取的说说通过
        ``ACTION_READ`` 记录，避免下轮被重复读取。
        已有互动（点赞或评论）但缺少 ``read`` 标记的旧记录，
        会在本次判定时自动补全 ``read`` 字段并标记 dirty，由后续
        ``save`` 统一落盘。

        Args:
            target_qq: 说说主人的 QQ 号
            feed_id: 说说 tid

        Returns:
            True 表示已处理过（点赞 / 评论 / 仅读任一）
        """
        key = _make_key(target_qq, feed_id)
        entry = self._data.get(key, {})
        seen = bool(entry.get(ACTION_LIKE)) or bool(entry.get(ACTION_COMMENT)) or bool(
            entry.get(ACTION_READ)
        )
        if seen and not entry.get(ACTION_READ):
            entry[ACTION_READ] = True
            self._dirty = True
        return seen

    def iter_followup_qqs(
        self, exclude_target_qq: str = "", limit: int = 0,
        max_feed_age_hours: float = 0, only_target_qq: str = "",
    ) -> list[tuple[str, list[str]]]:
        """按「最久未回查」聚合返回需要回查的 (target_qq, [feed_ids…])。

        策略：以 QQ 为粒度选取本轮回查目标。每个 QQ 的优先级 =
        其名下所有 bot 已评论 feed 的 ``last_followup_check`` 最小值
        （即"最久未被检查的那条"代表整个 QQ）。
        每轮挑 ``limit`` 个 QQ，对每个 QQ 一次 ``list_feeds`` 即可一并检查
        该 QQ 名下全部 bot 评论过的 feed，请求量 = limit。

        Args:
            exclude_target_qq: 排除该 QQ
            limit: 最多返回多少个 QQ；<= 0 表示不限制
            only_target_qq: 仅返回该 QQ（非空时生效），用于限定回查范围
            max_feed_age_hours: 评论过的说说超过该时长（小时）后不再回查；
                <= 0 表示不限制。基于 ``last_ts``（最近一次互动时间）判定。

        Returns:
            ``[(target_qq, [feed_id, …]), …]``，按 QQ 优先级升序。
        """
        bucket: dict[str, list[tuple[float, str]]] = {}
        exclude = str(exclude_target_qq).strip()
        only = str(only_target_qq).strip()
        now = time.time()
        max_age_secs = (
            max_feed_age_hours * 3600.0 if max_feed_age_hours and max_feed_age_hours > 0 else 0.0
        )
        for key, entry in self._data.items():
            if not entry.get(ACTION_COMMENT):
                continue
            if ":" not in key:
                continue
            target_qq, _, feed_id = key.partition(":")
            if not target_qq or not feed_id:
                continue
            if exclude and target_qq == exclude:
                continue
            if only and target_qq != only:
                continue
            if max_age_secs > 0:
                last_ts_raw = entry.get("last_ts", 0)
                last_ts = (
                    float(last_ts_raw)
                    if isinstance(last_ts_raw, (int, float))
                    else 0.0
                )
                if last_ts > 0 and (now - last_ts) > max_age_secs:
                    continue
            ts_raw = entry.get("last_followup_check", 0)
            ts = float(ts_raw) if isinstance(ts_raw, (int, float)) else 0.0
            bucket.setdefault(target_qq, []).append((ts, feed_id))

        # 每个 QQ 用其名下最小 ts 作为优先级（最久未查者优先）
        qq_with_priority: list[tuple[float, str, list[str]]] = []
        for qq, entries in bucket.items():
            entries.sort(key=lambda x: x[0])
            min_ts = entries[0][0]
            feed_ids = [fid for _, fid in entries]
            qq_with_priority.append((min_ts, qq, feed_ids))
        qq_with_priority.sort(key=lambda x: x[0])
        if limit and limit > 0:
            qq_with_priority = qq_with_priority[:limit]
        return [(qq, fids) for _, qq, fids in qq_with_priority]

    def iter_followup_feeds(
        self, exclude_target_qq: str = "", limit: int = 0,
        max_feed_age_hours: float = 0, only_target_qq: str = "",
    ) -> list[tuple[str, str]]:
        """按「最久未回查」返回需要回查的 (target_qq, feed_id) 列表。

        以 bot 评论过的**单条说说**为粒度，而非以 QQ 聚合：每条
        ``(target_qq, feed_id)`` 用其 ``last_followup_check`` 作为优先级，
        最久未查者优先。每轮返回 ``limit`` 条 feed，调用方逐条
        ``fetch_feed_detail`` 精准回查评论区，无需先拉整个 QQ 时间线，
        也不受「时间线最近 20 条」范围限制。

        Args:
            exclude_target_qq: 排除该 QQ
            limit: 最多返回多少条 feed；<= 0 表示不限制
            max_feed_age_hours: 评论过的说说超过该时长（小时）后不再回查；
                <= 0 表示不限制。基于 ``last_ts``（最近一次互动时间）判定。
            only_target_qq: 仅返回该 QQ 下的 feed（非空时生效）

        Returns:
            ``[(target_qq, feed_id), …]``，按最久未回查优先排序。
        """
        exclude = str(exclude_target_qq).strip()
        only = str(only_target_qq).strip()
        now = time.time()
        max_age_secs = (
            max_feed_age_hours * 3600.0 if max_feed_age_hours and max_feed_age_hours > 0 else 0.0
        )
        entries: list[tuple[float, str, str]] = []
        for key, entry in self._data.items():
            if not entry.get(ACTION_COMMENT):
                continue
            if ":" not in key:
                continue
            target_qq, _, feed_id = key.partition(":")
            if not target_qq or not feed_id:
                continue
            if exclude and target_qq == exclude:
                continue
            if only and target_qq != only:
                continue
            if max_age_secs > 0:
                last_ts_raw = entry.get("last_ts", 0)
                last_ts = (
                    float(last_ts_raw)
                    if isinstance(last_ts_raw, (int, float))
                    else 0.0
                )
                if last_ts > 0 and (now - last_ts) > max_age_secs:
                    continue
            ts_raw = entry.get("last_followup_check", 0)
            ts = float(ts_raw) if isinstance(ts_raw, (int, float)) else 0.0
            entries.append((ts, target_qq, feed_id))

        entries.sort(key=lambda x: x[0])
        if limit and limit > 0:
            entries = entries[:limit]
        return [(qq, fid) for _, qq, fid in entries]

    def mark_followup_checked(self, target_qq: str, feed_id: str) -> None:
        """更新该 (target_qq, feed_id) 的最近回查时间戳。

        无论本次是否检测到新回复都应调用，避免轮转死锁在同一条上。
        """
        key = _make_key(target_qq, feed_id)
        entry = self._data.get(key)
        if entry is None:
            return
        entry["last_followup_check"] = time.time()
        self._dirty = True

    def get_external_reply_count(self, target_qq: str, feed_id: str) -> int:
        """获取 bot 在指定 (target_qq, feed_id) 下累计接力回复次数。

        Args:
            target_qq: 说说主人的 QQ 号
            feed_id: 说说 tid

        Returns:
            累计成功 reply 次数，无记录则返回 0
        """
        key = _make_key(target_qq, feed_id)
        entry = self._data.get(key, {})
        raw = entry.get("external_reply_count", 0)
        return int(raw) if isinstance(raw, (int, float)) else 0

    def increment_external_reply_count(self, target_qq: str, feed_id: str) -> int:
        """递增 (target_qq, feed_id) 接力回复计数。

        应在外部回查路径下成功 reply 后调用。

        Args:
            target_qq: 说说主人的 QQ 号
            feed_id: 说说 tid

        Returns:
            递增后的新计数值
        """
        key = _make_key(target_qq, feed_id)
        entry = self._data.setdefault(key, {})
        current = entry.get("external_reply_count", 0)
        new_count = (int(current) if isinstance(current, (int, float)) else 0) + 1
        entry["external_reply_count"] = new_count
        self._dirty = True
        return new_count

    # ------------------------------------------------------------------
    # 写入接口
    # ------------------------------------------------------------------

    def sync_like_state(self, target_qq: str, feed_id: str, liked: bool) -> None:
        """同步「bot 账号对该说说的真实点赞状态」到本地记录。

        与 ``mark`` 不同，本方法不表示 bot 程序执行了点赞操作，而是记录
        外部观察到的真实状态（如用户手动用 bot 账号点的赞、或说说明面上
        已处于已点赞状态）。写入 ``LIKED_STATE`` 标记而非 ``ACTION_LIKE``，
        因此不影响 ``has_seen`` 的「是否处理过」判定，也不影响外部回查的
        互动聚合；仅使 ``has_liked`` 反映真实状态，避免重复点赞。

        Args:
            target_qq: 说说主人的 QQ 号
            feed_id: 说说 tid
            liked: 观察到的真实点赞状态
        """
        key = _make_key(target_qq, feed_id)
        entry: dict[str, object] = self._data.setdefault(key, {})
        current = bool(entry.get(LIKED_STATE))
        if liked == current:
            return
        if liked:
            entry[LIKED_STATE] = True
        else:
            entry.pop(LIKED_STATE, None)
        entry["last_ts"] = time.time()
        self._dirty = True

    def mark(
        self,
        target_qq: str,
        feed_id: str,
        action: str,
        source: str,
    ) -> None:
        """记录一次互动。

        Args:
            target_qq: 说说主人的 QQ 号
            feed_id: 说说 tid
            action: 互动类型，使用 ``ACTION_LIKE`` / ``ACTION_COMMENT`` 常量
            source: 来源，使用 ``SOURCE_POLL`` / ``SOURCE_AGENT`` 常量
        """
        key = _make_key(target_qq, feed_id)
        entry: dict[str, object] = self._data.setdefault(key, {})

        entry[action] = True
        entry["last_ts"] = time.time()

        # 更新 source 字段：若已有不同来源则标记为 "both"
        existing_source = entry.get("source", "")
        if existing_source and existing_source != source:
            entry["source"] = SOURCE_BOTH
        else:
            entry["source"] = source

        self._dirty = True

    async def save(self) -> None:
        """将有变更的记录写回持久化存储。

        仅在 dirty 标记为 True 时实际写入。
        """
        if not self._dirty:
            return
        await storage_api.save_json(
            _STORE_NAMESPACE, _STORE_KEY, {"log": dict(self._data)}
        )
        self._dirty = False
        logger.debug(f"互动记录已保存：共 {len(self._data)} 条")
