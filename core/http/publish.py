"""QZone 发布 / 点赞接口族（emotion_cgi_publish_v6 / internal_dolike_app）。"""

from __future__ import annotations

import time
from typing import Any

import orjson

from .client import DOLIKE_URL, EMOTION_PUBLISH_URL, QZoneClientBase, logger


class PublishMixin(QZoneClientBase):
    """说说发布与点赞能力。"""

    async def publish(self, content: str, images: list[bytes]) -> bool:
        """发布说说（支持带图）。

        Args:
            content: 说说正文
            images: 图片字节列表（可为空）

        Returns:
            True 表示发布成功

        Raises:
            RuntimeError: Cookie 失效（code=-3000）
        """
        post_data: dict[str, Any] = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": content,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": "1",
            "to_sign": "0",
            "hostuin": self._uin,
            "code_version": "1",
            "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{self._uin}",
        }

        if images:
            logger.info(f"开始上传 {len(images)} 张图片…")
            pic_bos: list[str] = []
            richvals: list[str] = []
            for i, img_bytes in enumerate(images):
                upload_result = await self._upload_image(img_bytes, i)
                if upload_result:
                    pic_bos.append(upload_result["pic_bo"])
                    richvals.append(upload_result["richval"])

            if pic_bos:
                post_data["pic_bo"] = ",".join(pic_bos)
                post_data["richtype"] = "1"
                post_data["richval"] = "\t".join(richvals)
                logger.info(f"将附带 {len(pic_bos)} 张图片发布说说。")
            else:
                logger.warning("所有图片上传失败，将改为发布纯文本。")

        try:
            res_text = await self._request(
                "POST", EMOTION_PUBLISH_URL, params={"g_tk": self._gtk}, data=post_data
            )
            result = orjson.loads(res_text)
            if result.get("code") == -3000:
                raise RuntimeError(
                    f"发布说说失败: {result.get('message')} (错误码: -3000)"
                )
            tid = result.get("tid", "")
            if tid:
                logger.info(f"说说发布成功，tid: {tid}")
                return True
            else:
                logger.error(f"发布说说失败，API 返回: {result}")
                return False
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"发布说说异常: {e}")
            return False

    async def like(self, target_qq: str, feed_id: str) -> bool:
        """对指定说说点赞。

        Args:
            target_qq: 目标 QQ 号
            feed_id: 说说 tid

        Returns:
            True 表示点赞成功

        Raises:
            RuntimeError: Cookie 失效（code=-3000）
        """
        data: dict[str, Any] = {
            "opuin": self._uin,
            "unikey": f"http://user.qzone.qq.com/{target_qq}/mood/{feed_id}",
            "curkey": f"http://user.qzone.qq.com/{target_qq}/mood/{feed_id}",
            "from": 1,
            "appid": 311,
            "typeid": 0,
            "abstime": int(time.time()),
            "fid": feed_id,
            "active": 0,
            "format": "json",
            "fupdate": 1,
        }
        logger.debug(
            f"like 调用: target_qq={target_qq} feed_id={feed_id}"
        )
        try:
            resp_text = await self._request(
                "POST", DOLIKE_URL, params={"g_tk": self._gtk}, data=data
            )
            try:
                resp_data = orjson.loads(resp_text)
                code = resp_data.get("code", -1)
                logger.debug(f"like 响应: code={code} message={resp_data.get('message')}")
                if code == 0:
                    return True
                if code == -3000:
                    raise RuntimeError(
                        f"点赞失败: {resp_data.get('message')} (错误码: -3000)"
                    )
                logger.warning(
                    f"点赞 API 返回失败: code={code}, message={resp_data.get('message')}"
                )
                return False
            except orjson.JSONDecodeError:
                return True
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"点赞说说异常: {e}")
            return False
