"""好友说说监控流程。

获取好友动态流，对未处理过的说说：始终点赞模式下先批量点赞，
再进入 LLM 决策是否评论；自主模式下由 LLM 对每条说说独立决策
是否点赞与是否评论，未点赞的说说记录为已读避免重复读取。
"""

from __future__ import annotations

import typing
from typing import Any

from src.app.plugin_system.api.log_api import COLOR, get_logger

from ..core.interaction_log import ACTION_COMMENT, ACTION_LIKE, ACTION_READ, SOURCE_POLL
from ..core.llm import log_llm_prompt
from ..core.llm.vision import fill_image_text
from ..core.text_utils import truncate_preview
from .engine import BatchPolicy, BatchSendEngine

if typing.TYPE_CHECKING:
    from ..runtime import QZoneRuntime

logger = get_logger("foxzone.autopilot.friend_feeds", color=COLOR.CYAN)


async def friend_monitor_once(runtime: "QZoneRuntime", num_feeds: int) -> None:
    """执行一次好友说说监控。

    流程：获取动态流 → 过滤已处理 → 按 ``always_like`` 分流：
    - 始终点赞模式：先批量点赞（成功者标记 LIKE），仅点赞成功的说说
      进入 LLM 评论决策；
    - 自主模式：候选说说全部进入 LLM 决策（点赞 + 评论），
      未点赞但已读取的标记 READ 避免下轮重复。

    Args:
        runtime: 插件运行时
        num_feeds: 最多检查的好友说说数量
    """
    try:
        feeds = await runtime.with_client(
            lambda client: client.monitor_list_feeds(max(1, num_feeds))
        )
    except Exception as e:
        logger.error(f"获取好友动态失败: {e}")
        return

    if not feeds:
        logger.debug("本次好友说说监控未获取到动态。")
        return

    logger.info(
        f"[#F38BA8]好友说说监控：获取到 [#CBA6F7]{len(feeds)}[/#CBA6F7]"
        f" 条候选动态，开始逐条过滤…[/#F38BA8]"
    )
    candidate_feeds: list[dict[str, Any]] = []
    for feed in feeds:
        target_qq: str = str(feed.get("target_qq", "")).strip()
        tid: str = str(feed.get("tid", "")).strip()

        if not target_qq or not tid:
            logger.debug(f"好友说说监控：跳过无 target_qq/tid 的 feed: {feed}")
            continue

        # 已处理（点赞/评论/仅读）则跳过
        if runtime.interaction_log.has_seen(target_qq, tid):
            logger.info(
                f"[#F5A97F]跳过已处理说说 (qq=[#CBA6F7]{target_qq}"
                f"[/#CBA6F7], tid={tid})[/#F5A97F]"
            )
            continue

        logger.debug(
            f"好友说说监控：候选说说 qq={target_qq} tid={tid} "
            f"正文={str(feed.get('content', '') or feed.get('rt_con', ''))[:80]!r} "
            f"图片数={len(feed.get('images', []) or [])} "
            f"评论数={len(feed.get('comments', []) or [])}"
        )
        candidate_feeds.append(feed)

    if not candidate_feeds:
        logger.debug("本次好友说说监控无未处理说说。")
        return

    cfg = runtime.config
    feed_items: list[dict[str, Any]] = []

    if cfg.monitor.always_like:
        # ── 始终点赞模式：先批量点赞 + 标记 LIKE，仅成功者进入 LLM 决策 ──
        for feed in candidate_feeds:
            target_qq = str(feed.get("target_qq", "")).strip()
            tid = str(feed.get("tid", "")).strip()
            try:
                liked = await runtime.with_client(
                    lambda client, qq=target_qq, t=tid: client.like(qq, t)
                )
            except Exception as exc:
                logger.error(f"自动点赞异常 (qq={target_qq}, tid={tid}): {exc}")
                continue

            if not liked:
                # 点赞失败：留待下一轮重试，不进入 LLM 评论决策
                logger.warning(f"自动点赞失败，本轮不交给 LLM 决策评论 (qq={target_qq}, tid={tid})")
                continue

            runtime.interaction_log.mark(target_qq, tid, ACTION_LIKE, SOURCE_POLL)
            await runtime.interaction_log.save()
            logger.info(
                f"[#F38BA8]自动点赞成功 (qq=[#CBA6F7]{target_qq}"
                f"[/#CBA6F7], tid={tid})[/#F38BA8]"
            )
            feed_items.append(_to_feed_item(feed))
    else:
        # ── 自主模式：候选全部进入 LLM 决策（点赞 + 评论）──
        feed_items = [_to_feed_item(feed) for feed in candidate_feeds]

    logger.info(
        f"[#F38BA8]好友说说监控：候选 [#CBA6F7]{len(candidate_feeds)}"
        f"[/#CBA6F7] 条，待决策 [#CBA6F7]{len(feed_items)}"
        f"[/#CBA6F7] 条。[/#F38BA8]"
    )
    if not feed_items:
        return

    await process_feed_monitor_batch(runtime, feed_items)


def _to_feed_item(feed: dict[str, Any]) -> dict[str, Any]:
    """将动态流中的单条说说转换为 LLM 决策输入项。"""
    target_qq = str(feed.get("target_qq", "")).strip()
    tid = str(feed.get("tid", "")).strip()
    content_text = str(feed.get("content") or feed.get("rt_con") or "（无正文）").strip()
    created_time = str(feed.get("created_time", "")).strip()
    images: list[str] = [str(u) for u in feed.get("images", []) if u]
    comments: list[dict[str, Any]] = feed.get("comments", [])

    image_lines: list[str] = [f"图片{j}：[待识别]" for j in range(1, len(images) + 1)]

    return {
        "tid": tid,
        "target_qq": target_qq,
        "content": content_text,
        "created_time": created_time,
        "image_text": "\n".join(image_lines),
        "images": images,
        "comment_count": len(comments),
    }


async def process_feed_monitor_batch(
    runtime: "QZoneRuntime",
    feed_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """处理「好友说说监控」批次：图片识别 + 决策 + 点赞/评论 + 标记。

    按 LLM 决策逐条执行点赞与评论，并记录互动状态：
    - 点赞（like=True）→ 标记 ACTION_LIKE
    - 评论（comment 非空）→ 标记 ACTION_COMMENT
    - 两者都不做 → 标记 ACTION_READ（已读，避免下轮重复读取）

    Args:
        runtime: 插件运行时
        feed_items: 待决策的说说项列表（始终点赞模式下为已点赞项，
            自主模式下为全部候选）

    Returns:
        ``{"liked": int, "commented": int, "decisions": dict[str, dict]}``
    """
    if not feed_items:
        return {"liked": 0, "commented": 0, "decisions": {}}

    cfg = runtime.config

    # 非多模态模式：批量识别图片，回填 image_text。
    # 多模态模式下图片由 generate_feed_decisions 直接下载传给模型，无需此处识图。
    if not cfg.llm.multimodal_mode:
        await fill_image_text(
            feed_items, cfg.llm.vision_model_task, runtime.vision_cache
        )

    logger.info(
        f"[#F38BA8]批量决策 [#CBA6F7]{len(feed_items)}[/#CBA6F7]"
        f" 条好友说说是否需要点赞/评论…[/#F38BA8]"
    )
    try:
        feed_decisions = await runtime.content.generate_feed_decisions(feed_items)
    except Exception as exc:
        logger.error(f"批量生成好友说说互动决策时发生异常: {exc}")
        return {"liked": 0, "commented": 0, "decisions": {}}

    decision_map: dict[str, dict[str, Any]] = {
        d["tid"]: d for d in feed_decisions if d.get("tid")
    }
    for tid, d in decision_map.items():
        logger.debug(
            f"好友说说决策明细: tid={tid} target_qq={d.get('target_qq')} "
            f"like={d.get('like')} comment={str(d.get('comment'))[:50]!r}"
        )

    # ── 自主模式下先按决策执行点赞（like=True）并标记 LIKE ──
    liked_count = 0
    if not cfg.monitor.always_like:
        for item in feed_items:
            tid = str(item.get("tid", ""))
            target_qq = str(item.get("target_qq", ""))
            decision = decision_map.get(tid) or {}
            if not (tid and target_qq and decision.get("like")):
                logger.debug(
                    f"好友说说：决策不点赞，跳过 (qq={target_qq} tid={tid} "
                    f"decision_like={decision.get('like')})"
                )
                continue
            logger.debug(
                f"好友说说：按决策执行点赞 (qq={target_qq} tid={tid})"
            )
            try:
                liked = await runtime.with_client(
                    lambda client, qq=target_qq, t=tid: client.like(qq, t)
                )
            except Exception as exc:
                logger.error(f"点赞异常 (qq={target_qq}, tid={tid}): {exc}")
                continue
            if not liked:
                logger.warning(f"点赞失败 (qq={target_qq}, tid={tid})")
                continue
            runtime.interaction_log.mark(target_qq, tid, ACTION_LIKE, SOURCE_POLL)
            liked_count += 1
        if liked_count:
            await runtime.interaction_log.save()
            logger.info(
                f"[#F38BA8]按决策点赞成功 [#CBA6F7]{liked_count}[/#CBA6F7] 条。[/#F38BA8]"
            )

    # 待评论项（comment 非空）进入批量发送
    comment_items: list[dict[str, Any]] = []
    for item in feed_items:
        tid = str(item.get("tid", ""))
        decision = decision_map.get(tid) or {}
        if decision.get("comment"):
            logger.debug(
                f"好友说说：决策评论 (qq={item.get('target_qq')} tid={tid} "
                f"内容={str(decision.get('comment'))[:50]!r})"
            )
            comment_items.append(item)
        else:
            logger.debug(
                f"好友说说：决策不评论 (qq={item.get('target_qq')} tid={tid})"
            )

    engine = BatchSendEngine(
        runtime.reply_send_lock,
        # 好友监控场景：限流即终止整批，未标记项下一轮重试
        BatchPolicy(stop_batch_on_rate_limit=True),
    )

    def _should_send(item: dict[str, Any]) -> bool:
        tid = str(item.get("tid", ""))
        target_qq = str(item.get("target_qq", ""))
        return bool(tid and target_qq and decision_map.get(tid, {}).get("comment"))

    async def _sender(item: dict[str, Any]) -> bool:
        tid = str(item.get("tid", ""))
        target_qq = str(item.get("target_qq", ""))
        comment_text = decision_map.get(tid, {}).get("comment") or ""
        return await runtime.with_client(
            lambda client: client.comment(target_qq, tid, comment_text.strip())
        )

    async def _on_success(item: dict[str, Any]) -> None:
        tid = str(item.get("tid", ""))
        target_qq = str(item.get("target_qq", ""))
        comment_text = decision_map.get(tid, {}).get("comment")
        runtime.interaction_log.mark(target_qq, tid, ACTION_COMMENT, SOURCE_POLL)
        await runtime.interaction_log.save()
        content_preview = truncate_preview(str(item.get("content", "") or "（无正文）"))
        logger.info(
            f"[#F38BA8]评论成功：QQ [#CBA6F7]{target_qq}[/#CBA6F7]"
            f" 的说说「[#CBA6F7]{content_preview}[/#CBA6F7]」"
            f" → 「{comment_text}」[/#F38BA8]"
        )

    def _label(item: dict[str, Any]) -> str:
        return f"[qq={item.get('target_qq', '')} tid={item.get('tid', '')}]"

    result = await engine.run(
        comment_items,
        sender=_sender,
        should_send=_should_send,
        label=_label,
        on_success=_on_success,
    )

    # 剩余既未点赞也未评论的说说标记为已读（read）
    read_marked = 0
    for item in feed_items:
        tid = str(item.get("tid", ""))
        target_qq = str(item.get("target_qq", ""))
        if not (tid and target_qq):
            continue
        if runtime.interaction_log.has_interacted(target_qq, tid):
            continue
        runtime.interaction_log.mark(target_qq, tid, ACTION_READ, SOURCE_POLL)
        read_marked += 1
        logger.debug(
            f"好友说说：标记仅读 (qq={target_qq} tid={tid})"
        )
    if read_marked:
        await runtime.interaction_log.save()

    log_llm_prompt(
        "好友说说互动决策",
        决策结果="\n".join(result.lines) if result.lines else "（无）",
    )
    logger.info(
        f"[#F38BA8]好友说说监控批量处理完成：共 [#CBA6F7]{len(feed_items)}"
        f"[/#CBA6F7] 条，点赞 [#CBA6F7]{liked_count}[/#CBA6F7] 条，"
        f"评论 [#CBA6F7]{result.succeeded}[/#CBA6F7] 条，"
        f"仅读 [#CBA6F7]{read_marked}[/#CBA6F7] 条。[/#F38BA8]"
    )
    return {
        "liked": liked_count,
        "commented": result.succeeded,
        "decisions": decision_map,
    }
