"""LLM 提示词文本块拼装。

包含批量评论项 / 好友说说项 / 自己说说快照的格式化逻辑。
纯函数实现（依赖 comment_tree 做楼中楼渲染）。
"""

from __future__ import annotations

from typing import Any

from ..comment_tree import comment_identity
from .personality import format_story_time


def _format_comments(
    comments: list[dict[str, Any]],
    bot_qq: str = "",
    highlight_identity: str = "",
) -> str:
    """按可见列表顺序展示评论序号及完整身份字段。"""
    if not comments:
        return "暂无评论"
    lines: list[str] = []
    for index, comment in enumerate(comments, start=1):
        comment_id = comment.get("comment_id", comment.get("comment_tid", comment.get("tid")))
        parent_id = comment.get(
            "parent_comment_id", comment.get("parent_tid", comment.get("parent_id"))
        )
        author = comment.get(
            "qq_account", comment.get("commenter_qq", comment.get("comment_uin"))
        )
        parent_author = comment.get(
            "parent_comment_uin",
            comment.get("parent_commenter_qq", comment.get("parent_uin")),
        )
        nickname = str(comment.get("nickname") or "未知用户")
        if bot_qq and str(author) == str(bot_qq):
            nickname = "你（自己）"
        marker = " >>>" if highlight_identity and comment_identity(comment) == highlight_identity else ""
        content = str(comment.get("content") or "（无内容）")
        lines.append(
            f"  {index}.{marker} id={comment_id if comment_id is not None else '?'} "
            f"parent={parent_id if parent_id is not None else '-'} "
            f"author={author if author is not None else '?'} "
            f"parent_author={parent_author if parent_author is not None else '-'} "
            f"[{nickname}]：{content}"
        )
    return "\n".join(lines)


def _content_completeness(value: Any) -> str:
    """描述正文是否完整，避免把未知或折叠内容误当全文。"""
    if value is True:
        return "完整"
    if value is False:
        return "可能为折叠片段"
    return "未知（保留当前返回正文，不截断）"


def format_batch_comment_items(
    comment_items: list[dict[str, Any]],
    bot_qq: str,
    image_descs: dict[str, str] | None = None,
    multimodal: bool = False,
) -> str:
    """将批量评论项格式化为提示词文本块（楼中楼对话链视图）。

    Args:
        comment_items: 评论项列表
        bot_qq: Bot 的 QQ 号，用于在评论区中高亮 Bot 自己的发言
        image_descs: 说说配图 URL → 视觉描述映射；缺失则按"[图片]"占位
        multimodal: 多模态模式下为 True，此时图片以 ``[[IMG:i:j]]`` 标记占位，
            由调用方内联为 Image 内容

    Returns:
        格式化后的多评论描述文本
    """
    descs = image_descs or {}
    blocks: list[str] = []
    for index, item in enumerate(comment_items, start=1):
        feed_id = item.get("feed_id", "")
        feed_content = str(item.get("feed_content") or "（无说说内容）").strip()
        story_time = format_story_time(item.get("story_time")) or "未知时间"
        comment_content = str(item.get("comment_content") or "（无内容）").strip()
        commenter_name = item.get("commenter_name", "未知用户")
        comment_time = format_story_time(item.get("comment_time")) or "未知时间"
        all_comments: list[dict[str, Any]] = item.get("all_comments") or []
        feed_images: list[str] = [str(u) for u in item.get("feed_images") or [] if u]
        is_reply_to_bot: bool = bool(item.get("is_reply_to_bot"))
        parent_content: str = str(item.get("parent_content") or "").strip()

        highlighted_identity = str(item.get("comment_identity") or comment_identity(item))
        threaded = _format_comments(all_comments, bot_qq, highlighted_identity)

        if multimodal:
            image_lines = [f"[[IMG:{index}:{j}]]" for j in range(1, len(feed_images) + 1)]
        else:
            image_lines = [
                f"图片{j}：{descs.get(url, '[图片]')}"
                for j, url in enumerate(feed_images, 1)
            ]
        image_block_text = ("\n" + "\n".join(image_lines)) if image_lines else ""

        # 当前评论的语境标识
        if is_reply_to_bot:
            context_line = (
                f"⚠ 这条评论是 **{commenter_name}** 在接你的话——"
                f"你之前说过：「{parent_content}」，对方现在回应：「{comment_content}」。\n"
                f"必须承接上下文，回应对方的话题/疑问，禁止重起新话题。"
            )
        else:
            context_line = (
                f"评论者：{commenter_name}  评论时间：{comment_time}\n"
                f"评论内容：「{comment_content}」"
            )

        block = (
            f"=== 评论 {index}/{len(comment_items)} ===\n"
            f"你的说说（{story_time}）：「{feed_content}」"
            f"\n正文完整性：{_content_completeness(item.get('content_complete'))}"
            f"{image_block_text}\n"
            f"{context_line}\n"
            f"评论列表按序号供决策；一级评论可能不完整，内联楼中楼不保证完整。"
            f" >>> 标记的是本次需要决策的评论。\n{threaded}\n"
            f"[meta] comment_index={index}  comment_tid={item.get('comment_tid', '')} "
            f"feed_id={feed_id}"
        )
        blocks.append(block)

    return "\n\n".join(blocks)


def format_feed_items_block(
    feed_items: list[dict[str, Any]],
    multimodal: bool = False,
    bot_qq: str = "",
) -> str:
    """将好友说说列表格式化为提示词文本块。

    Args:
        feed_items: 说说项列表
        multimodal: 多模态模式下为 True，此时图片以 ``[[IMG:i:j]]`` 标记占位，
            由调用方内联为 Image 内容；否则使用 ``image_text``（VLM 识别描述）
        bot_qq: Bot 自己的 QQ，用于在评论区中把 bot 的评论标注为「你（自己）」

    Returns:
        格式化后的多说说描述文本
    """
    blocks: list[str] = []
    total = len(feed_items)
    for i, item in enumerate(feed_items, 1):
        tid = item.get("tid", "")
        target_qq = item.get("target_qq", "")
        content = str(item.get("content") or "（无正文）").strip()
        created_time = format_story_time(item.get("created_time")) or "未知时间"
        image_text = str(item.get("image_text") or "")
        comments = item.get("comments", []) or []

        liked = bool(item.get("liked"))
        like_state = "已点赞" if liked else "尚未点赞"
        block = (
            f"=== 说说 {i}/{total} ===\n"
            f"好友 QQ：{target_qq}  发布时间：{created_time}\n"
            f"状态：{like_state}\n"
            f"正文完整性：{_content_completeness(item.get('content_complete'))}\n"
            f"正文：「{content}」\n"
        )
        if multimodal:
            images = [str(u) for u in item.get("images") or [] if u]
            for j in range(1, len(images) + 1):
                block += f"[[IMG:{i}:{j}]]\n"
        elif image_text:
            block += f"{image_text}\n"
        # 展示评论区（供 LLM 判断是否回复某条评论）
        server_count = item.get("comment_count")
        visible_count = len(comments)
        count_label = "未知" if server_count is None else str(server_count)
        block += (
            f"评论区（服务端评论数={count_label}，当前可见扁平项={visible_count}；"
            "一级评论可能部分可见，内联楼中楼不保证完整）：\n"
        )
        block += _format_comments(comments, bot_qq) + "\n"
        block += f"[meta] tid={tid}  target_qq={target_qq}"
        blocks.append(block)

    return "\n\n".join(blocks)


def format_self_feed(
    idx: int,
    feed: dict[str, Any],
    image_descs: dict[str, str] | None = None,
) -> str:
    """格式化单条「自己的说说」为带评论的完整文本块。

    Args:
        idx: 序号（从 1 开始）
        feed: 说说数据字典
        image_descs: 图片 URL → 描述映射

    Returns:
        多行文本块
    """
    tid = str(feed.get("tid", "")).strip()
    content = str(feed.get("content") or "（无正文）").strip()
    created_time = str(feed.get("created_time", "")).strip()
    images: list[str] = feed.get("images") or []
    comments: list[dict[str, Any]] = feed.get("comments", []) or []

    parts: list[str] = []
    header = f"【我的说说 {idx}】"
    if created_time:
        header += f"  {created_time}"
    if tid:
        header += f"  (tid={tid})"
    parts.append(header)
    parts.append(f"正文完整性：{_content_completeness(feed.get('content_complete'))}")
    parts.append(f"正文：{content}")
    if feed.get("rt_con"):
        parts.append(f"转发引用（非当前正文）：{feed['rt_con']}")

    if images:
        descs = image_descs or {}
        for i, url in enumerate(images, start=1):
            desc = descs.get(str(url), "").strip()
            parts.append(f"图片{i}：{desc}" if desc else f"图片{i}：（内容未识别）")

    server_count = feed.get("comment_count")
    count_label = "未知" if server_count is None else str(server_count)
    parts.append(
        f"评论（服务端评论数={count_label}，当前可见扁平项={len(comments)}；"
        "一级评论可能部分可见，内联楼中楼不保证完整）："
    )
    parts.append(_format_comments(comments))

    parts.append("")  # 段落分隔
    return "\n".join(parts)
