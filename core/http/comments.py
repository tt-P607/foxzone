"""QZone 普通评论与楼中楼回复接口。"""

from __future__ import annotations

from typing import Any

from .client import COMMENT_URL, QZoneClientBase, logger
from .response import (
    QZoneInvalidResponseError,
    QZoneRateLimitError,
    check_response,
    parse_response,
)

__all__ = ["CommentsMixin", "QZoneRateLimitError", "QZoneUnknownResultError"]


class QZoneUnknownResultError(RuntimeError):
    """写请求结果无法确认，禁止调用方盲目重试。"""


class CommentsMixin(QZoneClientBase):
    """评论与楼中楼回复能力。"""

    async def comment(self, target_qq: str, feed_id: str, text: str) -> bool:
        """对指定说说发表评论。

        Args:
            target_qq: 目标 QQ 号
            feed_id: 说说 tid
            text: 评论内容

        Returns:
            True 表示评论成功

        Raises:
            RuntimeError: Cookie 失效（code=-3000）
        """
        data: dict[str, Any] = {
            "topicId": f"{target_qq}_{feed_id}__1",
            "uin": self._uin,
            "hostUin": target_qq,
            "feedsType": 100,
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "plat": "qzone",
            "source": "ic",
            "platformid": 52,
            "format": "fs",
            "ref": "feeds",
            "content": text,
        }
        logger.debug(
            f"comment 调用: target_qq={target_qq} feed_id={feed_id} "
            f"text_len={len(text)}"
        )
        try:
            resp_text = await self._request(
                "POST", COMMENT_URL, params={"g_tk": self._gtk}, data=data
            )
        except Exception as e:
            raise QZoneUnknownResultError("评论请求结果不确定，需先核实投递状态") from e
        try:
            resp_data = parse_response(resp_text)
        except (ValueError, TypeError) as error:
            raise QZoneUnknownResultError("评论结果无法解析，需先核实投递状态") from error
        logger.debug(
            f"comment response: code={resp_data.get('code')} "
            f"subcode={resp_data.get('subcode', 0)}"
        )
        try:
            check_response(resp_data, "评论")
        except QZoneInvalidResponseError as error:
            raise QZoneUnknownResultError("评论回执字段无效，需先核实投递状态") from error
        return True

    async def reply(
        self,
        feed_id: str,
        host_qq: str,
        target_name: str,
        content: str,
        comment_tid: str | int,
        commenter_qq: str = "",
        *,
        parent_comment_uin: str | int,
        images: list[bytes] | None = None,
    ) -> dict[str, str | int]:
        """回复指定的一级评论或楼中楼评论。

        Args:
            feed_id: 说说 tid
            host_qq: 说说主人 QQ 号
            target_name: 被提及者昵称
            content: 回复内容
            comment_tid: 父级一级评论 ID
            commenter_qq: 实际被提及者 QQ 号
            parent_comment_uin: 父级一级评论作者 QQ 号
            images: 回复图片；不支持非空图片列表

        Returns:
            含新回复 ID 与父评论 ID/作者的回执

        Raises:
            RuntimeError: 明确服务端拒绝或结果无法确认
            ValueError: 图片不受支持
        """
        if images:
            raise ValueError("楼中楼回复不支持图片")
        if isinstance(comment_tid, bool) or not isinstance(comment_tid, (str, int)):
            raise ValueError("父评论 ID 必须为字符串或整数")
        comment_tid = str(comment_tid)
        for field, value in (
            ("机器人 QQ 号", self._uin),
            ("说说主人 QQ 号", host_qq),
            ("父评论作者 QQ 号", str(parent_comment_uin)),
        ):
            if not value.isdecimal() or int(value) <= 0:
                raise ValueError(f"{field}必须为正整数")
        if not feed_id.strip() or not content.strip() or not comment_tid.strip():
            raise ValueError("说说 ID、父评论 ID 和回复内容不能为空")
        if commenter_qq and (not commenter_qq.isdecimal() or int(commenter_qq) <= 0):
            raise ValueError("被提及者 QQ 号必须为正整数")
        if bool(commenter_qq) != bool(target_name.strip()):
            raise ValueError("被提及者 QQ 号和昵称必须成对提供")

        mentioned_content = (
            "@{uin:"
            + commenter_qq
            + ",nick:"
            + target_name.strip().replace("%", "%25").replace(",", "%2C").replace("}", "%7D")
            + f",auto:1}} {content}"
            if commenter_qq
            else content
        )
        endpoint = (
            "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/"
            "cgi-bin/emotion_cgi_addreply_ugc"
        )
        data: dict[str, Any] = {
            "uin": self._uin,
            "hostUin": host_qq,
            "hostuin": self._uin,
            "topicId": f"{host_qq}_{feed_id}",
            "commentId": comment_tid,
            "commentUin": str(parent_comment_uin),
            "content": mentioned_content,
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "ref": "",
            "code_version": "1",
            "format": "fs",
            "qzreferrer": f"https://user.qzone.qq.com/{host_qq}",
        }
        try:
            resp_text = await self._request(
                "POST", endpoint, params={"g_tk": self._gtk}, data=data
            )
        except Exception as e:
            raise QZoneUnknownResultError("回复请求结果不确定，需先核实投递状态") from e
        try:
            resp_data = parse_response(resp_text)
        except (ValueError, TypeError) as error:
            raise QZoneUnknownResultError("回复结果无法解析，需先核实投递状态") from error
        logger.debug(
            f"reply response: code={resp_data.get('code')} "
            f"subcode={resp_data.get('subcode', 0)}"
        )
        try:
            check_response(resp_data, "回复")
        except QZoneInvalidResponseError as error:
            raise QZoneUnknownResultError("回复回执字段无效，需先核实投递状态") from error
        reply_data = resp_data.get("data")
        reply_id = reply_data.get("id") if isinstance(reply_data, dict) else None
        if isinstance(reply_id, bool) or not (
            isinstance(reply_id, int) and reply_id >= 0
            or isinstance(reply_id, str) and bool(reply_id.strip())
        ):
            raise QZoneUnknownResultError(
                "回复已接受但缺少有效新回复 ID，需先核实投递状态"
            )
        result_id = str(reply_id)
        return {
            "comment_id": result_id,
            "parent_comment_id": comment_tid,
            "parent_comment_uin": int(parent_comment_uin),
        }
