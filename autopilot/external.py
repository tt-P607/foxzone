"""外部空间接力回查流程。

检查 bot 在他人空间评论过的说说下是否有人回复 bot 的评论，
命中则由 LLM 决策并接力回复。含防双 bot 死循环的同 feed 接力上限。
"""

from __future__ import annotations

import asyncio
import random
import typing
from datetime import datetime
from typing import Any

from src.app.plugin_system.api.log_api import COLOR, get_logger

from ..core.interaction_log import ACTION_COMMENT, SOURCE_POLL
from ..core.llm import log_llm_prompt
from ..core.text_utils import truncate_preview
from .engine import BatchPolicy, BatchSendEngine

if typing.TYPE_CHECKING:
    from ..runtime import QZoneRuntime

logger = get_logger("foxzone.autopilot.external", color=COLOR.CYAN)

async def external_followup_once(
    runtime: "QZoneRuntime",
    bot_qq: str,
    max_age_hours: float,
    batch_size: int,
    max_feed_age_hours: float = 0,
) -> None:
    """执行一次外部空间评论回查（按 feed 精准查）。

    对 interaction_log 中 bot 评论过的每条 ``(target_qq, feed_id)`` 直接调
    ``msgdetail_v6`` 拿完整评论区，检查是否存在「别人回复了 bot 评论」的
    接力回复，命中则交给 :func:`process_reply_batch` 决策并回复。
    不再按 QQ 扫时间线，也不受「时间线最近 20 条」范围限制。

    Args:
        runtime: 插件运行时
        bot_qq: Bot 自己的 QQ
        max_age_hours: 评论过期阈值（小时），0 表示不限制
        batch_size: 本轮最多检查多少条 feed；0 表示不限
        max_feed_age_hours: 评论过的说说超过此时长（小时）后不再回查；0 表示不限
    """
    interaction_log = runtime.interaction_log
    feed_targets: list[tuple[str, str]] = interaction_log.iter_followup_feeds(
        exclude_target_qq=bot_qq,
        limit=batch_size,
        max_feed_age_hours=max_feed_age_hours,
    )
    if not feed_targets:
        logger.debug("外部回查：本轮没有待检查的 feed。")
        return

    logger.info(
        f"外部回查：本轮检查 {len(feed_targets)} 条 bot 评论过的说说"
        f"（最久未回查优先）。"
    )
    for qq, fid in feed_targets:
        logger.debug(f"外部回查目标: qq={qq} feed_id={fid}")

    new_items: list[dict[str, Any]] = []
    success_count = 0
    fail_count = 0

    async def _mark_checked(target_qq: str, fid: str) -> None:
        interaction_log.mark_followup_checked(target_qq, fid)
        await interaction_log.save()

    for idx, (target_qq, feed_id) in enumerate(feed_targets):
        # feed 之间加 3-8s 随机抖动，避免短时间集中调用触发风控
        if idx > 0:
            jitter = random.uniform(3.0, 8.0)
            logger.debug(f"外部回查：feed 间隔抖动 {jitter:.1f}s")
            await asyncio.sleep(jitter)

        # 无论是否命中，本轮已检查过该 feed → 更新时间戳
        await _mark_checked(target_qq, feed_id)

        try:
            detail = await runtime.with_client(
                lambda client, qq=target_qq, fid=feed_id: client.fetch_feed_detail(
                    host_qq=str(qq), tid=str(fid)
                )
            )
        except Exception as exc:
            logger.warning(f"外部回查：拉取 feed {feed_id} 详情失败: {exc}")
            fail_count += 1
            continue

        success_count += 1
        comments: list[dict[str, Any]] = list((detail or {}).get("comments", []) or [])
        if not comments:
            logger.debug(f"外部回查：feed {feed_id} 无评论，跳过。")
            continue

        feed_content = str((detail or {}).get("content", "") or "")
        feed_images = list((detail or {}).get("images", []) or [])
        story_time = str((detail or {}).get("created_time", "") or "")

        bot_comment_tids: set[str] = {
            str(c.get("comment_tid", ""))
            for c in comments
            if str(c.get("qq_account", "")) == bot_qq and c.get("comment_tid")
        }
        logger.debug(
            f"外部回查：feed {feed_id} 评论总数={len(comments)}，"
            f"bot 自己的评论 tid={sorted(bot_comment_tids)}"
        )
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
            parent_tid: str = str(parent_tid_raw).strip() if parent_tid_raw else ""
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

            if runtime.reply_tracker.has_replied(feed_id, comment_tid):
                logger.debug(
                    f"外部回查：comment {comment_tid} 已被回复过（reply_tracker 命中），跳过"
                )
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
                "feed_content": feed_content,
                "feed_images": feed_images,
                "story_time": story_time,
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
            logger.debug(
                f"外部回查：发现接力回复 feed={feed_id} "
                f"commenter={commenter_qq} 内容={str(comment.get('content', ''))[:60]!r} "
                f"parent_tid={parent_tid} comment_tid={comment_tid}"
            )

    summary = f"成功 {success_count}/失败 {fail_count} feed"
    if not new_items:
        logger.info(f"外部回查：{summary}，本轮没有新发现的接力回复。")
        return

    logger.info(
        f"外部回查：{summary}，发现 {len(new_items)} 条"
        f"「别人回复 bot 评论」的接力回复，开始批量处理。"
    )
    await process_reply_batch(runtime, new_items)


async def process_reply_batch(
    runtime: "QZoneRuntime",
    comment_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """处理「评论回复」批次：决策 + 发送 + 标记。

    同时服务两条路径（数据结构一致，仅 host_qq 不同）：
    - 自己说说下的新评论（host_qq = bot_qq）
    - 外部空间接力回复（host_qq = 好友 QQ）

    Args:
        runtime: 插件运行时
        comment_items: 评论项列表，每项必含
            ``feed_id, comment_tid, host_qq, commenter_qq, commenter_name`` 等字段

    Returns:
        ``{"replied": int, "skipped": int, "decisions": dict[str, str | None]}``
    """
    if not comment_items:
        return {"replied": 0, "skipped": 0, "decisions": {}}

    cfg = runtime.config

    # 防双 bot 死循环：在 LLM 决策前过滤掉同 feed 已达接力上限的 comment
    max_replies = int(cfg.monitor.external_followup_max_replies_per_feed)
    blocked_count = 0
    if max_replies > 0:
        filtered: list[dict[str, Any]] = []
        blocked: list[tuple[str, str, int]] = []
        for item in comment_items:
            host_qq = str(item.get("host_qq", ""))
            feed_id = str(item.get("feed_id", ""))
            if not (host_qq and feed_id):
                filtered.append(item)
                continue
            count = runtime.interaction_log.get_external_reply_count(host_qq, feed_id)
            if count >= max_replies:
                blocked.append((host_qq, feed_id, count))
                continue
            filtered.append(item)
        if blocked:
            logger.warning(
                f"外部接力触发同 feed 上限保护：{len(blocked)} 条 comment 已跳过 "
                f"(上限={max_replies}，示例 host={blocked[0][0]} feed={blocked[0][1]} count={blocked[0][2]})"
            )
        blocked_count = len(blocked)
        comment_items = filtered
        if not comment_items:
            return {"replied": 0, "skipped": blocked_count, "decisions": {}}

    try:
        decisions = await runtime.content.generate_batch_replies(comment_items)
    except Exception as exc:
        logger.error(f"评论回复批量决策失败: {exc}")
        return {"replied": 0, "skipped": blocked_count, "decisions": {}}

    decision_map: dict[str, str | None] = {
        d["comment_tid"]: d.get("reply")
        for d in decisions
        if d.get("comment_tid")
    }
    for d in decisions:
        logger.debug(
            f"评论回复决策明细: comment_tid={d.get('comment_tid')} "
            f"feed_id={d.get('feed_id')} reply={str(d.get('reply'))[:60]!r}"
        )

    decision_lines: list[str] = []
    for item in comment_items:
        ctid = item.get("comment_tid", "")
        who = item.get("commenter_name", "?")
        reply = decision_map.get(ctid)
        decision_lines.append(f"{'✓' if reply else '✗'} [{who}] → {reply or '跳过'}")
    log_llm_prompt(
        "评论回复决策",
        决策结果="\n".join(decision_lines) if decision_lines else "（无）",
    )

    engine = BatchSendEngine(
        runtime.reply_send_lock,
        BatchPolicy(stop_batch_on_rate_limit=False),
    )

    def _should_send(item: dict[str, Any]) -> bool:
        comment_tid = str(item.get("comment_tid", ""))
        feed_id = str(item.get("feed_id", ""))
        host_qq = str(item.get("host_qq", ""))
        if not (comment_tid and feed_id and host_qq):
            return False
        if not decision_map.get(comment_tid):
            return False
        # commentId 直接取被回复子评论的 parent_tid（即顶层一级评论 tid）。
        # msgdetail_v6 返回的一级/二级评论 tid 均为该说说内的数字局部序号
        # （如 "1"），数字 tid 作为 commentId 提交 reply 可正常成功
        # （服务端按 topicId+commentId 定位）。
        return True

    async def _sender(item: dict[str, Any]) -> bool:
        comment_tid = str(item.get("comment_tid", ""))
        feed_id = str(item.get("feed_id", ""))
        reply_text = decision_map.get(comment_tid) or ""
        # commentId = 被回复子评论所属的顶层一级评论 tid。
        # msgdetail_v6 中子回复的 parent_tid 字段即顶层一级评论 tid
        # （顶层 tid 在顶层内唯一，子回复一律挂在某条顶层下），直接使用
        # 该字段即可，无需按 tid 溯源——全量溯源会因「子回复 tid 与一级
        # tid 从 1 各自编号、跨层级冲突」而解析到错误的评论。
        root_comment_tid = str(item.get("parent_tid", "") or "")
        logger.debug(
            f"reply 前: target={comment_tid!r} parent_tid={root_comment_tid!r} "
            f"(commentId 直接用父级 tid)"
        )
        return await runtime.with_client(
            lambda client: client.reply(
                feed_id,
                str(item.get("host_qq", "")),
                str(item.get("commenter_name", "未知用户")),
                reply_text.strip(),
                root_comment_tid,
                str(item.get("commenter_qq", "")),
            )
        )

    async def _on_success(item: dict[str, Any]) -> None:
        host_qq = str(item.get("host_qq", ""))
        feed_id = str(item.get("feed_id", ""))
        reply_text = decision_map.get(str(item.get("comment_tid", "")))
        feed_preview = truncate_preview(str(item.get("feed_content", "") or "（无正文）"))
        commenter = item.get("commenter_name", "未知用户")
        logger.info(
            f"[#F38BA8]接力回复成功：QQ [#CBA6F7]{host_qq}"
            f"[/#CBA6F7] 的说说「[#CBA6F7]{feed_preview}[/#CBA6F7]」"
            f" 下回复 [#CBA6F7]{commenter}[/#CBA6F7]"
            f" → 「{reply_text}」[/#F38BA8]"
        )
        # 续期 last_ts，避免持续对话中的 feed 被 max_feed_age_hours 过滤掉
        runtime.interaction_log.mark(host_qq, feed_id, ACTION_COMMENT, SOURCE_POLL)
        # 递增同 feed 接力计数（防双 bot 死循环）
        new_count = runtime.interaction_log.increment_external_reply_count(
            host_qq, feed_id
        )
        await runtime.interaction_log.save()
        if max_replies > 0 and new_count >= max_replies:
            logger.warning(
                f"外部接力同 feed 计数已达上限 {new_count}/{max_replies}，"
                f"该 feed 后续将停止接力 (host={host_qq}, feed={feed_id})"
            )

    async def _on_finally(item: dict[str, Any]) -> None:
        feed_id = str(item.get("feed_id", ""))
        comment_tid = str(item.get("comment_tid", ""))
        if feed_id and comment_tid:
            await runtime.reply_tracker.mark_as_replied(feed_id, comment_tid)

    def _label(item: dict[str, Any]) -> str:
        return (
            f"[{item.get('commenter_name', '?')}] "
            f"(feed={item.get('feed_id', '')}, tid={item.get('comment_tid', '')})"
        )

    result = await engine.run(
        comment_items,
        sender=_sender,
        should_send=_should_send,
        label=_label,
        on_success=_on_success,
        on_rate_limited=_on_finally,  # 限流也标记已处理，避免下轮重复触发
        on_finally=_on_finally,
    )

    logger.info(
        f"评论回复批量处理完成：共 {len(comment_items)} 条，"
        f"回复 {result.succeeded} 条，跳过 {result.skipped} 条。"
    )
    return {
        "replied": result.succeeded,
        "skipped": result.skipped + blocked_count,
        "decisions": decision_map,
    }
