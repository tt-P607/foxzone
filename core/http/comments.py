"""QZone 评论 / 楼中楼回复接口族（emotion_cgi_re_feeds）。

reply 接口的字段语义经过浏览器抓包实证校准（详见 QZONE_API.md §3.5），
**不要凭直觉修改任何字段**：

- ``commentUin`` 是操作者 uin（bot 自己），不是评论作者 QQ
- 被回复者身份通过 ``content`` 内的 ``@{uin:...,nick:...,auto:1}`` 前缀传递
- ``commentId`` 必须是顶层一级评论的 hex tid
- 任何字段偏差都会触发 -10049 反爬伪装错误
"""

from __future__ import annotations

from typing import Any

import orjson

from .client import COMMENT_URL, QZoneClientBase, logger


class QZoneRateLimitError(RuntimeError):
    """QZone 反爬限流错误（code=-10049）。

    该错误重试亦无效，由批量发送引擎识别并终止重试。
    """


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
        try:
            resp_text = await self._request(
                "POST", COMMENT_URL, params={"g_tk": self._gtk}, data=data
            )
            try:
                resp_data = orjson.loads(resp_text)
                code = resp_data.get("code", -1)
                if code == 0:
                    return True
                if code == -3000:
                    raise RuntimeError(
                        f"评论失败: {resp_data.get('message')} (错误码: -3000)"
                    )
                logger.error(f"评论 API 返回失败: code={code}, message={resp_data.get('message')}")
                return False
            except orjson.JSONDecodeError:
                # 响应无法解析为 JSON，假定成功
                return True
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"评论说说异常: {e}")
            return False

    async def reply(
        self,
        feed_id: str,
        host_qq: str,
        target_name: str,
        content: str,
        comment_tid: str,
        commenter_qq: str = "",
    ) -> bool:
        """回复指定评论（二级评论）。

        Args:
            feed_id: 说说 tid
            host_qq: 说说主人 QQ 号
            target_name: 被回复的评论者昵称
            content: 回复内容
            comment_tid: 顶层一级评论的 tid（作为 commentId）
            commenter_qq: 被回复的评论者 QQ 号（用于 @ 提及前缀）

        Returns:
            True 表示回复成功

        Raises:
            RuntimeError: Cookie 失效（code=-3000）或 QZone 限流（code=-10049）
        """
        # content 必须包含 @ 提及格式（浏览器对二级评论强制如此），
        # 否则 QZone 反爬会以 -10049 拒绝。
        mentioned_content = (
            f"@{{uin:{commenter_qq},nick:{target_name},auto:1}} {content}"
            if commenter_qq
            else content
        )
        data: dict[str, Any] = {
            "topicId": f"{host_qq}_{feed_id}__1",
            "feedsType": 100,
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "plat": "qzone",
            "source": "ic",
            "hostUin": host_qq,
            "isSignIn": "",
            "platformid": 52,
            "uin": self._uin,
            "format": "fs",
            "ref": "feeds",
            "content": mentioned_content,
            "commentId": comment_tid,
            # commentUin 是"操作者 uin"（即 bot 自己），不是评论作者 QQ
            "commentUin": self._uin,
            "richval": "",
            "richtype": "",
            "private": "0",
            "paramstr": "1",
            "qzreferrer": f"https://user.qzone.qq.com/{self._uin}",
        }
        reply_headers: dict[str, str] = {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Sec-CH-UA": '"Chromium";v="138", "Not(A:Brand";v="99", "Google Chrome";v="138"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Referer": f"https://user.qzone.qq.com/{self._uin}",
            "Origin": "https://user.qzone.qq.com",
        }
        try:
            resp_text = await self._request(
                "POST", COMMENT_URL, params={"g_tk": self._gtk}, data=data, headers=reply_headers
            )
            # 调试日志：输出请求关键字段和响应原文，便于诊断 reply 是否真发出
            logger.debug(
                f"reply 调用: feed_id={feed_id}, host_qq={host_qq}, "
                f"commentId={comment_tid}, 被回复者={commenter_qq}, "
                f"target_name={target_name!r}, content_len={len(content)}"
            )
            logger.debug(f"reply 响应原文: {resp_text[:500]}")
            try:
                resp_data = orjson.loads(resp_text)
                code = resp_data.get("code", -1)
                if code == 0:
                    logger.debug(
                        f"reply 接口返回 code=0: "
                        f"new_tid={resp_data.get('tid') or resp_data.get('commentid')}, "
                        f"raw_keys={list(resp_data.keys())}"
                    )
                    return True
                if code == -3000:
                    raise RuntimeError(
                        f"回复失败: {resp_data.get('message')} (错误码: -3000)"
                    )
                logger.error(
                    f"回复 API 返回失败: code={code}, "
                    f"message={resp_data.get('message')}, fid={feed_id}, "
                    f"raw={resp_text[:300]}"
                )
                return False
            except orjson.JSONDecodeError:
                # format=fs 模式下 QZone 返回的是 frame 桥接 HTML，里面嵌入
                # frameElement.callback({...}) 调用。需要从中提取 JSON 片段判断真实 code。
                import re as _re

                m = _re.search(
                    r"frameElement\.callback\s*\(\s*(\{[\s\S]*?\})\s*\)",
                    resp_text,
                )
                parsed_code: int | None = None
                parsed_msg: str = ""
                parsed_subcode: int | None = None
                if m:
                    candidate = m.group(1).replace("undefined", "null")
                    try:
                        parsed = orjson.loads(candidate)
                        if isinstance(parsed, dict):
                            parsed_code = parsed.get("code", parsed.get("ret"))
                            parsed_msg = str(
                                parsed.get("message") or parsed.get("msg") or ""
                            )
                            parsed_subcode = parsed.get("subcode")
                    except orjson.JSONDecodeError as je:
                        logger.debug(f"frame callback JSON 解析仍失败: {je}; 片段={candidate[:200]}")

                logger.warning(
                    f"reply 响应为 frame 桥接 HTML，提取结果: "
                    f"code={parsed_code}, subcode={parsed_subcode}, msg={parsed_msg!r}"
                )
                logger.debug(f"reply 完整响应原文: {resp_text}")

                if parsed_code == 0:
                    return True
                if parsed_code == -3000:
                    raise RuntimeError(
                        f"回复失败: {parsed_msg} (错误码: -3000)"
                    )
                if parsed_code == -10049:
                    # QZone 限流：重试亦无效，抛专用异常让批量引擎判定为不可重试错误
                    raise QZoneRateLimitError(
                        f"QZone 限流（code=-10049, subcode={parsed_subcode}）：{parsed_msg}"
                    )
                if parsed_code is not None:
                    logger.error(
                        f"回复接口返回错误: code={parsed_code}, "
                        f"subcode={parsed_subcode}, msg={parsed_msg!r}"
                    )
                    return False
                logger.error(
                    f"reply 响应无法解析任何 code/msg 字段，视为失败。原文片段: {resp_text[:300]}"
                )
                return False
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"回复评论异常: {e}")
            return False
