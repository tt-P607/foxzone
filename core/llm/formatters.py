"""LLM 提示词文本块拼装。

包含批量评论项 / 好友说说项 / 自己说说快照的格式化逻辑。
纯函数实现（依赖 comment_tree 做楼中楼渲染）。
"""

from __future__ import annotations

from typing import Any

from ..comment_tree import build_threaded_view
from .personality import format_story_time


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
        feed_content = item.get("feed_content", "（无说说内容）").strip()
        story_time = format_story_time(item.get("story_time")) or "未知时间"
        comment_tid = str(item.get("comment_tid", ""))
        comment_content = item.get("comment_content", "").strip()
        commenter_name = item.get("commenter_name", "未知用户")
        comment_time = format_story_time(item.get("comment_time")) or "未知时间"
        all_comments: list[dict[str, Any]] = item.get("all_comments", [])
        feed_images: list[str] = [str(u) for u in item.get("feed_images", []) if u]
        is_reply_to_bot: bool = bool(item.get("is_reply_to_bot"))
        parent_content: str = str(item.get("parent_content", "")).strip()

        threaded = build_threaded_view(
            all_comments, bot_qq=bot_qq, highlight_tid=comment_tid
        )

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
            f"{image_block_text}\n"
            f"{context_line}\n"
            f"该说说完整对话链（>>> 标记的是本次需要决策的那条）：\n{threaded}\n"
            f"[meta] comment_tid={comment_tid}  feed_id={feed_id}"
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
        content = str(item.get("content", "（无正文）")).strip()
        created_time = format_story_time(item.get("created_time")) or "未知时间"
        image_text = item.get("image_text", "")
        comments = item.get("comments", []) or []

        liked = bool(item.get("liked"))
        like_state = "已点赞" if liked else "尚未点赞"
        block = (
            f"=== 说说 {i}/{total} ===\n"
            f"好友 QQ：{target_qq}  发布时间：{created_time}\n"
            f"状态：{like_state}\n"
            f"正文：「{content[:200]}{'…' if len(content) > 200 else ''}」\n"
        )
        if multimodal:
            images = [str(u) for u in item.get("images", []) if u]
            for j in range(1, len(images) + 1):
                block += f"[[IMG:{i}:{j}]]\n"
        elif image_text:
            block += f"{image_text}\n"
        # 展示评论区（供 LLM 判断是否回复某条评论）
        if comments:
            block += f"评论区（{len(comments)} 条）：\n"
            for c in comments:
                nickname = str(c.get("nickname", "") or "").strip() or "匿名"
                qq = str(c.get("qq_account", "") or "").strip()
                ctext = str(c.get("content", "") or "").strip()
                ctid = str(c.get("comment_tid", "") or "").strip()
                # bot 自己的评论标注为「你（自己）」，便于 LLM 识别
                if bot_qq and qq == str(bot_qq):
                    display = "你（自己）"
                else:
                    display = nickname
                block += f"  · {display}"
                if qq:
                    block += f"(qq={qq})"
                if ctid:
                    block += f" [cid={ctid}]"
                block += f"：{ctext[:120]}{'…' if len(ctext) > 120 else ''}\n"
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
    content = str(feed.get("content") or feed.get("rt_con") or "（无正文）").strip()
    created_time = str(feed.get("created_time", "")).strip()
    images: list[str] = feed.get("images", []) or []
    comments: list[dict[str, Any]] = feed.get("comments", []) or []

    parts: list[str] = []
    header = f"【我的说说 {idx}】"
    if created_time:
        header += f"  {created_time}"
    if tid:
        header += f"  (tid={tid})"
    parts.append(header)
    parts.append(f"正文：{content}")

    if images:
        descs = image_descs or {}
        for i, url in enumerate(images, start=1):
            desc = descs.get(str(url), "").strip()
            parts.append(f"图片{i}：{desc}" if desc else f"图片{i}：（内容未识别）")

    if comments:
        parts.append(f"评论（共 {len(comments)} 条）：")
        for c in comments:
            nickname = str(c.get("nickname", "")).strip() or "匿名"
            qq = str(c.get("qq_account", "")).strip()
            ctime = str(c.get("create_time", "")).strip()
            ctext = str(c.get("content", "")).strip()
            line = f"  · [{nickname}"
            if qq:
                line += f"({qq})"
            line += "]"
            if ctime:
                line += f" {ctime}"
            line += f"：{ctext}"
            parts.append(line)
    else:
        parts.append("评论：暂无")

    parts.append("")  # 段落分隔
    return "\n".join(parts)
