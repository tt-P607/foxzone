"""好友说说监控流程。

获取好友动态流，对未互动过的说说先批量点赞，
仅点赞成功的说说进入 LLM 评论决策，再经 BatchSendEngine 批量发送评论。
"""

from __future__ import annotations

import typing
from typing import Any

from src.app.plugin_system.api.log_api import COLOR, get_logger

from ..core.interaction_log import ACTION_COMMENT, ACTION_LIKE, SOURCE_POLL
from ..core.llm import log_llm_prompt
from ..core.llm.vision import fill_image_text
from .engine import BatchPolicy, BatchSendEngine

if typing.TYPE_CHECKING:
    from ..runtime import QZoneRuntime

logger = get_logger("foxzone.autopilot.friend_feeds", color=COLOR.CYAN)


def _preview(text: str, limit: int = 40) -> str:
    """截断正文用于日志预览，避免日志被超长正文刷屏。

    Args:
        text: 原始正文
        limit: 最大字符数

    Returns:
        截断后的预览文本（超出部分以 … 结尾）
    """
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


async def friend_monitor_once(runtime: "QZoneRuntime", num_feeds: int) -> None:
    """执行一次好友说说监控。

    流程：获取动态流 → 过滤已互动 → 批量点赞（成功者标记 LIKE）
    → 识图 → LLM 评论决策 → BatchSendEngine 批量发送评论。

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
        f"[bold #F38BA8]好友说说监控：获取到 [bold #CBA6F7]{len(feeds)}[/bold #CBA6F7]"
        f" 条候选动态，开始逐条过滤…[/bold #F38BA8]"
    )
    candidate_feeds: list[dict[str, Any]] = []
    for feed in feeds:
        target_qq: str = str(feed.get("target_qq", "")).strip()
        tid: str = str(feed.get("tid", "")).strip()

        if not target_qq or not tid:
            continue

        # 已互动（点赞或评论）则跳过
        if runtime.interaction_log.has_interacted(target_qq, tid):
            logger.info(
                f"[bold #F5A97F]跳过已互动说说 (qq=[bold #CBA6F7]{target_qq}"
                f"[/bold #CBA6F7], tid={tid})[/bold #F5A97F]"
            )
            continue

        candidate_feeds.append(feed)

    if not candidate_feeds:
        logger.debug("本次好友说说监控无未处理说说。")
        return

    # ── 先批量点赞 + 标记 LIKE，仅成功者再进入 LLM 评论决策 ──
    feed_items: list[dict[str, Any]] = []
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
            f"[bold #F38BA8]自动点赞成功 (qq=[bold #CBA6F7]{target_qq}"
            f"[/bold #CBA6F7], tid={tid})[/bold #F38BA8]"
        )

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
        f"[bold #F38BA8]好友说说监控：候选 [bold #CBA6F7]{len(candidate_feeds)}"
        f"[/bold #CBA6F7] 条，已点赞 [bold #CBA6F7]{len(feed_items)}"
        f"[/bold #CBA6F7] 条。[/bold #F38BA8]"
    )
    if not feed_items:
        return

    await process_feed_monitor_batch(runtime, feed_items)


async def process_feed_monitor_batch(
    runtime: "QZoneRuntime",
    feed_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """处理「好友说说监控」批次：图片识别 + 决策 + 发评论 + 标记。

    Args:
        runtime: 插件运行时
        feed_items: 已点赞但尚未评论的说说项列表

    Returns:
        ``{"commented": int, "decisions": dict[str, str | None]}``
    """
    if not feed_items:
        return {"commented": 0, "decisions": {}}

    cfg = runtime.config

    # 非多模态模式：批量识别图片，回填 image_text。
    # 多模态模式下图片由 generate_feed_decisions 直接下载传给模型，无需此处识图。
    if not cfg.llm.multimodal_mode:
        await fill_image_text(
            feed_items, cfg.llm.vision_model_task, runtime.vision_cache
        )

    logger.info(
        f"[bold #F38BA8]批量决策 [bold #CBA6F7]{len(feed_items)}[/bold #CBA6F7]"
        f" 条已点赞好友说说是否需要评论…[/bold #F38BA8]"
    )
    try:
        feed_decisions = await runtime.content.generate_feed_decisions(feed_items)
    except Exception as exc:
        logger.error(f"批量生成好友说说评论决策时发生异常: {exc}")
        return {"commented": 0, "decisions": {}}

    decision_map: dict[str, str | None] = {
        d["tid"]: d.get("comment")
        for d in feed_decisions
        if d.get("tid")
    }

    engine = BatchSendEngine(
        runtime.reply_send_lock,
        # 好友监控场景：限流即终止整批，未标记项下一轮重试
        BatchPolicy(stop_batch_on_rate_limit=True),
    )

    def _should_send(item: dict[str, Any]) -> bool:
        tid = str(item.get("tid", ""))
        target_qq = str(item.get("target_qq", ""))
        return bool(tid and target_qq and decision_map.get(tid))

    async def _sender(item: dict[str, Any]) -> bool:
        tid = str(item.get("tid", ""))
        target_qq = str(item.get("target_qq", ""))
        comment_text = decision_map.get(tid) or ""
        return await runtime.with_client(
            lambda client: client.comment(target_qq, tid, comment_text.strip())
        )

    async def _on_success(item: dict[str, Any]) -> None:
        tid = str(item.get("tid", ""))
        target_qq = str(item.get("target_qq", ""))
        comment_text = decision_map.get(tid)
        runtime.interaction_log.mark(target_qq, tid, ACTION_COMMENT, SOURCE_POLL)
        await runtime.interaction_log.save()
        content_preview = _preview(str(item.get("content", "") or "（无正文）"))
        logger.info(
            f"[bold #F38BA8]评论成功：QQ [bold #CBA6F7]{target_qq}[/bold #CBA6F7]"
            f" 的说说「[bold #CBA6F7]{content_preview}[/bold #CBA6F7]」"
            f" → 「{comment_text}」[/bold #F38BA8]"
        )

    def _label(item: dict[str, Any]) -> str:
        return f"[qq={item.get('target_qq', '')} tid={item.get('tid', '')}]"

    result = await engine.run(
        feed_items,
        sender=_sender,
        should_send=_should_send,
        label=_label,
        on_success=_on_success,
    )

    log_llm_prompt(
        "好友说说评论决策",
        决策结果="\n".join(result.lines) if result.lines else "（无）",
    )
    logger.info(
        f"[bold #F38BA8]好友说说监控批量处理完成：共 [bold #CBA6F7]{len(feed_items)}"
        f"[/bold #CBA6F7] 条，评论 [bold #CBA6F7]{result.succeeded}"
        f"[/bold #CBA6F7] 条。[/bold #F38BA8]"
    )
    return {"commented": result.succeeded, "decisions": decision_map}
