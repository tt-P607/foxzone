"""ReadFeedTool：读取 QQ 空间说说的 Tool 组件。

供外部 Chatter 通过 LLM tool calling 调用，返回指定 QQ 用户的说说及评论完整性状态。
"""

from __future__ import annotations

from typing import Annotated, Any

from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.base import BaseTool

from .. import SERVICE_SIG


class ReadFeedTool(BaseTool):
    """读取 QQ 空间说说内容（含评论）的 Tool。

    LLM 可通过 Tool Calling 触发此 Tool，以了解指定好友最近发布的说说、
    评论等完整快照，为后续互动或回复提供上下文。
    """

    name = "qzone_read_feed"
    description = "读取指定 QQ 用户最近的 QQ 空间说说，包含正文、图片描述、评论等完整内容"

    async def execute(
        self,
        target_qq: Annotated[str, "要查看其说说的 QQ 号（纯数字字符串）"],
        num: Annotated[int, "读取几条说说，默认 5 条，最多 20 条"] = 5,
    ) -> tuple[bool, str]:
        """读取指定 QQ 用户的说说列表及评论。

        Args:
            target_qq: 目标 QQ 号
            num: 读取条数

        Returns:
            (是否成功, 格式化后的说说内容文本)
        """
        from ..service import QZoneService

        service: QZoneService | None = get_service(SERVICE_SIG)  # type: ignore[assignment]
        if service is None:
            return False, "FoxZone 服务未注册。"

        target_qq = target_qq.strip()
        if not target_qq.isdigit():
            return False, f"'{target_qq}' 不是有效的 QQ 号，请提供纯数字 QQ 号。"

        num = max(1, min(num, 20))
        listed_feeds = await service.list_feeds(
            target_qq=target_qq,
            num=num,
            skip_commented=False,
            paginate_comments=False,
        )
        if not listed_feeds:
            return True, f"QQ {target_qq} 的空间暂无说说或无法访问。"

        feeds: list[dict[str, Any]] = []
        for listed in listed_feeds:
            feed_id = str(listed.get("tid") or "")
            detail = await service.get_feed_detail(target_qq, feed_id) if feed_id else None
            feeds.append({**listed, **(detail or {})})

        # 收集所有图片 URL，批量识别（有缓存则直接复用）
        all_image_urls: list[str] = []
        for feed in feeds:
            images = feed.get("images")
            if isinstance(images, list):
                all_image_urls.extend(str(url) for url in images if url)
        image_descs: dict[str, str] = {}
        if all_image_urls:
            image_descs = await service.describe_images(all_image_urls)

        lines: list[str] = [f"=== QQ {target_qq} 最近 {len(feeds)} 条说说 ===\n"]
        for idx, feed in enumerate(feeds, start=1):
            lines.append(self._format_feed(idx, feed, image_descs))

        return True, "\n".join(lines)

    @staticmethod
    def _format_feed(idx: int, feed: dict[str, Any], image_descs: dict[str, str] | None = None) -> str:
        """将单条说说格式化为包含评论的完整文本块。"""
        tid = str(feed.get("tid", ""))
        content = str(feed.get("content") or "（无正文）").strip()
        created_time = str(feed.get("created_time", "")).strip()
        raw_images = feed.get("images")
        images = raw_images if isinstance(raw_images, list) else []
        raw_comments = feed.get("comments")
        comments = raw_comments if isinstance(raw_comments, list) else []
        comments_complete = feed.get("comments_complete")
        inline_replies_complete = feed.get("inline_replies_complete")
        content_complete = feed.get("content_complete")
        content_status = (
            str(content_complete) if isinstance(content_complete, bool) else "未知"
        )
        comments_status = (
            str(comments_complete) if isinstance(comments_complete, bool) else "未知"
        )
        replies_status = (
            str(inline_replies_complete)
            if isinstance(inline_replies_complete, bool)
            else "未知"
        )

        parts: list[str] = []
        header = f"【说说 {idx}】"
        if created_time:
            header += f"  {created_time}"
        if tid:
            header += f"  (tid={tid})"
        parts.append(header)
        parts.append(f"内容：{content}")
        parts.append(f"正文完整={content_status}")
        if feed.get("rt_con"):
            parts.append(f"转发引用（非当前正文）：{feed['rt_con']}")

        if images:
            descs = image_descs or {}
            for i, url in enumerate(images, 1):
                desc = descs.get(str(url), "")
                if desc:
                    parts.append(f"图片{i}：{desc}")
                else:
                    parts.append(f"图片{i}：（内容未识别）")

        if comments:
            visible_count = len(comments)
            server_count = feed.get("comment_count", feed.get("comment_total"))
            count_label = (
                f"服务端 {server_count} 条，当前可见 {visible_count} 条"
                if server_count is not None
                else f"服务端数量未知，当前可见 {visible_count} 条"
            )
            parts.append(
                f"评论（{count_label}；一级评论完整={comments_status}；"
                f"楼中楼完整={replies_status}）："
            )
            for comment in comments:
                nickname = str(comment.get("nickname", "")).strip() or "匿名"
                qq = str(comment.get("qq_account", "")).strip()
                ctime = str(comment.get("create_time", "")).strip()
                ctext = str(comment.get("content", "")).strip()
                ctid = str(comment.get("comment_tid", "")).strip()

                line = f"  · [{nickname}"
                if qq:
                    line += f"({qq})"
                line += "]"
                if ctime:
                    line += f" {ctime}"
                if ctid:
                    line += f" (comment_id={ctid}, comment_uin={qq or '?'})"
                parent_id = comment.get("parent_comment_id")
                if parent_id is None:
                    parent_id = comment.get("parent_tid")
                if parent_id is not None and str(parent_id) != "":
                    parent_uin = comment.get("parent_comment_uin")
                    line += (
                        f" (parent_comment_id={parent_id}, "
                        f"parent_comment_uin={parent_uin if parent_uin is not None else '?'})"
                    )
                line += f"：{ctext}"
                parts.append(line)
        else:
            server_count = feed.get("comment_count", feed.get("comment_total"))
            server_count_label = "未知" if server_count is None else str(server_count)
            parts.append(
                f"评论：当前未见评论（服务端数量={server_count_label}；"
                f"一级评论完整={comments_status}；"
                f"楼中楼完整={replies_status}）"
            )

        parts.append("")  # 空行分隔
        return "\n".join(parts)
