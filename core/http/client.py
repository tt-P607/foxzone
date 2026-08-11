"""QZone HTTP 客户端基础层。

包含 Cookie/gtk/uin 上下文、通用请求方法与图片上传能力。
各接口族（feeds / comments / publish）以 mixin 形式组合到
:class:`~plugins.foxzone.core.http.QZoneAPIClient`。
"""

from __future__ import annotations

import base64
import time
from typing import Any

import aiohttp
import orjson

from src.app.plugin_system.api.log_api import COLOR, get_logger

logger = get_logger("foxzone.api_client", color=COLOR.ORANGE)

# QQ 空间 API 端点定义
ZONE_LIST_URL = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
EMOTION_PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
DOLIKE_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
# 评论与楼中楼回复共用同一端点（emotion_cgi_re_feeds），以表单字段区分行为。
COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
# 单条说说详情（按 tid 精确查询，含评论区 list_3 楼中楼），当前评论拉取主接口
MSG_DETAIL_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"

# 通用 Chrome 请求头
CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"


class QZoneClientBase:
    """QQ 空间 HTTP 客户端基础能力（有状态，持有 Cookie/gtk/uin 上下文）。

    使用工厂方法 :meth:`create` 从 Cookie 字典构建实例。
    所有 API 方法在 Cookie 失效（code=-3000）时抛出 ``RuntimeError``，
    由上层统一处理重试。

    Attributes:
        _cookies: Cookie 字典
        _gtk: QQ 空间 gtk 参数（由 p_skey 计算）
        _uin: QQ 号（去掉 uin Cookie 中的 "o" 前缀）
    """

    def __init__(self, cookies: dict[str, str], gtk: str, uin: str) -> None:
        """初始化 API 客户端。

        Args:
            cookies: Cookie 字典
            gtk: 预计算的 gtk 参数
            uin: QQ 号字符串（不含 "o" 前缀）
        """
        self._cookies = cookies
        self._gtk = gtk
        self._uin = uin

    @classmethod
    def create(cls, cookies: dict[str, str]) -> "QZoneClientBase":
        """从 Cookie 字典创建 API 客户端。

        自动从 Cookie 中提取 p_skey 计算 gtk，以及提取 uin。

        Args:
            cookies: 完整的 QQ 空间 Cookie 字典

        Returns:
            配置好的客户端实例

        Raises:
            ValueError: Cookie 缺少必要字段（p_skey 或 uin）
        """
        p_skey = cookies.get("p_skey") or cookies.get("P_SKEY", "")
        if not p_skey:
            raise ValueError("Cookie 中缺少关键字段 'p_skey'。")

        gtk = cls._generate_gtk(p_skey)
        uin = cookies.get("uin", "").lstrip("o")
        if not uin:
            raise ValueError("Cookie 中缺少关键字段 'uin'。")

        return cls(cookies, gtk, uin)

    @staticmethod
    def _generate_gtk(skey: str) -> str:
        """通过 p_skey 计算 QQ 空间 gtk 参数。

        Args:
            skey: Cookie 中的 p_skey 值

        Returns:
            gtk 参数字符串
        """
        hash_val = 5381
        for char in skey:
            hash_val += (hash_val << 5) + ord(char)
        return str(hash_val & 2_147_483_647)

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        """发送 HTTP 请求并返回响应文本。

        Args:
            method: HTTP 方法（GET / POST）
            url: 请求 URL
            params: 查询参数
            data: 表单数据（POST 时使用）
            headers: 额外请求头（会覆盖默认头）

        Returns:
            响应文本

        Raises:
            aiohttp.ClientResponseError: HTTP 请求失败
        """
        final_headers: dict[str, str] = {
            "User-Agent": CHROME_UA,
            "Referer": f"https://user.qzone.qq.com/{self._uin}",
            "Origin": "https://user.qzone.qq.com",
            "Connection": "keep-alive",
        }
        # 不设置 Host 头，由 aiohttp 从实际 URL 推导，避免与 h5/taotao 等
        # 接口 URL 不匹配而触发 QZone 反爬识别。
        if headers:
            final_headers.update(headers)

        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(cookies=self._cookies) as session:
            async with session.request(
                method,
                url,
                params=params,
                data=data,
                headers=final_headers,
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                text = await resp.text()
                # QQ 空间偶尔会在响应开头带 UTF-8 BOM，统一剥离避免 orjson/json 解析失败
                if text.startswith("\ufeff"):
                    text = text.lstrip("\ufeff")
                return text

    # ------------------------------------------------------------------
    # 图片相关私有方法
    # ------------------------------------------------------------------

    @staticmethod
    def _image_to_base64(image_bytes: bytes) -> str:
        """将图片字节转为 base64 字符串（QQ 空间上传格式）。

        Args:
            image_bytes: 图片二进制数据

        Returns:
            base64 编码字符串
        """
        return base64.b64encode(image_bytes).decode("ascii")

    @staticmethod
    def _get_picbo_and_richval(upload_result: dict[str, Any]) -> tuple[str, str]:
        """从上传结果中提取 pic_bo 和 richval 参数。

        Args:
            upload_result: QQ 空间图片上传 API 的响应数据

        Returns:
            (picbo, richval) 元组

        Raises:
            ValueError: 上传结果格式不符合预期
        """
        if "ret" not in upload_result:
            raise ValueError("上传结果中缺少 'ret' 字段。")
        if upload_result["ret"] != 0:
            raise ValueError(f"图片上传失败：ret={upload_result['ret']}")

        url_str = upload_result["data"]["url"]
        picbo_spt = url_str.split("&bo=")
        if len(picbo_spt) < 2:
            raise ValueError("无法从上传 URL 中提取 picbo。")
        picbo = picbo_spt[1]

        d = upload_result["data"]
        richval = ",{},{},{},{},{},{},,{},{}".format(
            d["albumid"],
            d["lloc"],
            d["sloc"],
            d["type"],
            d["height"],
            d["width"],
            d["height"],
            d["width"],
        )
        return picbo, richval

    async def _upload_image(self, image_bytes: bytes, index: int) -> dict[str, str] | None:
        """上传单张图片到 QQ 空间。

        Args:
            image_bytes: 图片二进制数据
            index: 图片序号（仅用于日志）

        Returns:
            包含 ``pic_bo`` 和 ``richval`` 的字典；上传失败时返回 None
        """
        upload_url = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
        post_data = {
            "filename": "filename",
            "zzpanelkey": "",
            "uploadtype": "1",
            "albumtype": "7",
            "exttype": "0",
            "skey": self._cookies.get("skey", ""),
            "zzpaneluin": self._uin,
            "p_uin": self._uin,
            "uin": self._uin,
            "p_skey": self._cookies.get("p_skey", ""),
            "output_type": "json",
            "qzonetoken": "",
            "refer": "shuoshuo",
            "charset": "utf-8",
            "output_charset": "utf-8",
            "upload_hd": "1",
            "hd_width": "2048",
            "hd_height": "10000",
            "hd_quality": "96",
            "backUrls": (
                "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
                "http://119.147.64.75/cgi-bin/upload/cgi_upload_image"
            ),
            "url": f"https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image?g_tk={self._gtk}",
            "base64": "1",
            "picfile": self._image_to_base64(image_bytes),
        }
        hdrs = {
            "referer": f"https://user.qzone.qq.com/{self._uin}",
            "origin": "https://user.qzone.qq.com",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(cookies=self._cookies) as session:
                async with session.post(
                    upload_url, data=post_data, headers=hdrs, timeout=timeout
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"图片 {index + 1} 上传 HTTP 失败：{resp.status}")
                        return None

                    resp_text = await resp.text()
                    start = resp_text.find("{")
                    end = resp_text.rfind("}") + 1
                    if start == -1 or end == 0:
                        logger.error(f"图片 {index + 1} 上传响应无有效 JSON。")
                        return None

                    upload_result = orjson.loads(resp_text[start:end])
                    if upload_result.get("ret") != 0:
                        logger.error(f"图片 {index + 1} 上传失败：{upload_result}")
                        return None

                    picbo, richval = self._get_picbo_and_richval(upload_result)
                    logger.info(f"图片 {index + 1} 上传成功。")
                    return {"pic_bo": picbo, "richval": richval}

        except Exception as e:
            logger.error(f"上传图片 {index + 1} 时发生异常: {e}")
            return None

    @staticmethod
    def _parse_comment_time(comment_data: dict[str, Any]) -> str:
        """从评论数据中解析格式化的时间字符串。

        优先使用 createTime2（YYYY-MM-DD HH:MM:SS 格式），
        其次将 create_time 时间戳转换为可读格式。

        Args:
            comment_data: 单条评论的数据字典

        Returns:
            格式化的时间字符串；解析失败时返回空字符串
        """
        if comment_data.get("createTime2"):
            return str(comment_data["createTime2"])

        raw_time = comment_data.get("create_time")
        if raw_time:
            try:
                ts = int(raw_time)
                return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            except (ValueError, TypeError):
                pass
        return ""
