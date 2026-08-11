"""自己说说评论回复流程。

轮询自己的最近说说，收集未回复的新评论，交给
:func:`~plugins.foxzone.autopilot.external.process_reply_batch`
批量决策并回复（两条路径数据结构一致，共用同一处理器）。
"""

from __future__ import annotations

import typing
from datetime import datetime
from typing import Any

from src.app.plugin_system.api.log_api import COLOR, get_logger

from .external import process_reply_batch

if typing.TYPE_CHECKING:
    from ..runtime import QZoneRuntime

logger = get_logger("foxzone.autopilot.self_comments", color=COLOR.CYAN)


async def poll_self_comments_once(
    runtime: "QZoneRuntime",
    bot_qq: str,
    num_feeds: int,
    max_age_hours: float = 0.0,
) -> None:
    """执行一次自己说说评论轮询并批量处理新评论。

    Args:
        runtime: 插件运行时
        bot_qq: Bot 的 QQ 号
        num_feeds: 检查的说说数量
        max_age_hours: 忽略超过此时间（小时）的评论，0 表示不限制
    """
    try:
        feeds = await runtime.with_client(
            lambda client: client.list_feeds(bot_qq, num_feeds, skip_commented=False)
        )
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

            if runtime.reply_tracker.has_replied(feed_id, comment_tid):
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

    logger.info(
        f"[#F38BA8]本次轮询发现 [#CBA6F7]{len(new_items)}[/#CBA6F7]"
        f" 条新评论，开始批量处理。[/#F38BA8]"
    )
    await process_reply_batch(runtime, new_items)
