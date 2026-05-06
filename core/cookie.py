"""Cookie 服务模块。

负责 QQ 空间 Cookie 的获取、本地文件缓存与失效清理。
获取顺序：本地文件缓存 → HTTP 备用端点（Napcat）。
"""

from __future__ import annotations

import asyncio
import typing
from pathlib import Path

import aiohttp
import orjson

from src.app.plugin_system.api.log_api import get_logger, COLOR

if typing.TYPE_CHECKING:
    from ..config import FoxZoneConfig

logger = get_logger("foxzone.cookie_service", color=COLOR.ORANGE)

# Cookie 本地缓存目录（相对于项目根目录）
_COOKIE_DIR = Path("data/foxzone/cookies")


class CookieService:
    """Cookie 获取与缓存服务。

    负责以下职责：
    1. 优先读取本地文件缓存（最快，避免不必要的网络请求）。
    2. 缓存不存在时，通过 Napcat HTTP 端点获取并写入缓存。
    3. 提供失效清理接口（Cookie 被 QQ 空间拒绝时调用）。

    Attributes:
        _config: 插件配置实例（FoxZoneConfig）
        _cookie_dir: Cookie 本地缓存目录
    """

    def __init__(self, config: "FoxZoneConfig") -> None:  # type: ignore[name-defined]
        """初始化 Cookie 服务。

        Args:
            config: 插件配置实例
        """
        self._config = config
        self._cookie_dir = _COOKIE_DIR
        self._cookie_dir.mkdir(parents=True, exist_ok=True)
        # 用来串行化 HTTP 备用地址取 Cookie，避免多个轮询任务同时发起 N 路重复请求。
        self._fetch_lock: asyncio.Lock = asyncio.Lock()



    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def get_cookies(
        self, qq_account: str
    ) -> dict[str, str] | None:
        """获取指定 QQ 账号的 Cookie。

        按以下顺序尝试：
        1. 本地文件缓存
        2. HTTP 备用端点（Napcat）

        Args:
            qq_account: QQ 账号字符串

        Returns:
            Cookie 字典；全部方法失败时返回 None
        """
        # 1. 本地缓存
        cookies = self._load_from_file(qq_account)
        if cookies:
            logger.debug("从本地缓存加载 Cookie 成功。")
            return cookies

        # 2. HTTP 备用端点（串行化：锁内二次检查本地缓存，避免多路任务重复拉取）
        async with self._fetch_lock:
            cookies = self._load_from_file(qq_account)
            if cookies:
                logger.debug("从本地缓存加载 Cookie 成功（锁内）。")
                return cookies
            logger.info("本地缓存不存在，尝试 HTTP 备用地址...")
            cookies = await self._get_from_http()
            if cookies:
                logger.info("从 HTTP 备用地址获取 Cookie 成功。")
                self._save_to_file(qq_account, cookies)
                return cookies

            logger.error(
                f"为 QQ {qq_account} 获取 Cookie 的所有方法均失败。"
                "请确保 Napcat 连接正常，或存在有效的本地 Cookie 缓存。"
            )
            return None

    def clear_cache(self, qq_account: str) -> None:
        """删除指定账号的本地 Cookie 缓存文件。

        当 QQ 空间 API 返回 -3000（Cookie 失效）时调用此方法，
        清除缓存以便下次重新获取。

        Args:
            qq_account: QQ 账号字符串
        """
        cookie_file = self._get_file_path(qq_account)
        if cookie_file.exists():
            try:
                cookie_file.unlink()
                logger.info(f"已清除过期 Cookie 缓存: {cookie_file}")
            except OSError as e:
                logger.error(f"清除 Cookie 缓存失败: {e}")

    # ------------------------------------------------------------------
    # 私有方法
    # ------------------------------------------------------------------

    def _get_file_path(self, qq_account: str) -> Path:
        """获取指定帐号的 Cookie 缓存文件路径。

        Args:
            qq_account: QQ 账号字符串

        Returns:
            Path 对象
        """
        return self._cookie_dir / f"cookies-{qq_account}.json"

    def _load_from_file(self, qq_account: str) -> dict[str, str] | None:
        """从本地文件加载 Cookie。

        Args:
            qq_account: QQ 账号字符串

        Returns:
            Cookie 字典；文件不存在或解析失败时返回 None
        """
        cookie_file = self._get_file_path(qq_account)
        if not cookie_file.exists():
            return None
        try:
            with open(cookie_file, "rb") as f:
                return orjson.loads(f.read())
        except (OSError, orjson.JSONDecodeError) as e:
            logger.warning(f"读取 Cookie 缓存文件失败: {cookie_file}: {e}")
            return None

    def _save_to_file(self, qq_account: str, cookies: dict[str, str]) -> None:
        """将 Cookie 保存到本地文件。

        Args:
            qq_account: QQ 账号字符串
            cookies: Cookie 字典
        """
        cookie_file = self._get_file_path(qq_account)
        try:
            with open(cookie_file, "wb") as f:
                f.write(orjson.dumps(cookies, option=orjson.OPT_INDENT_2))
            logger.debug(f"Cookie 已缓存至: {cookie_file}")
        except OSError as e:
            logger.error(f"保存 Cookie 缓存失败: {cookie_file}: {e}")

    async def _get_from_http(self) -> dict[str, str] | None:
        """通过 Napcat HTTP 备用端点获取 Cookie。

        Returns:
            Cookie 字典；失败时返回 None
        """
        host = self._config.cookie.http_fallback_host
        port = self._config.cookie.http_fallback_port
        token = self._config.cookie.napcat_token

        if not host or not port:
            logger.debug("Cookie HTTP 备用配置未设置，跳过。")
            return None

        url = f"http://{host}:{port}/get_cookies"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        payload = {"domain": "user.qzone.qq.com"}
        max_retries = 3
        retry_delay = 1.0

        for attempt in range(max_retries):
            try:
                timeout = aiohttp.ClientTimeout(total=15.0)
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, json=payload, headers=headers, timeout=timeout
                    ) as resp:
                        if resp.status == 403:
                            logger.debug(
                                "Napcat HTTP 端点返回 403，可能需要配置 napcat_token。"
                            )
                            return None
                        resp.raise_for_status()
                        data = await resp.json()
                        cookie_str = data.get("data", {}).get("cookies", "")
                        if cookie_str and isinstance(cookie_str, str):
                            return {
                                k.strip(): v.strip()
                                for k, v in (
                                    p.split("=", 1)
                                    for p in cookie_str.split("; ")
                                    if "=" in p
                                )
                            }
                        logger.warning("Napcat HTTP 端点返回 Cookie 为空或格式不正确。")
                        return None
            except aiohttp.ClientError as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"无法连接 Napcat HTTP 端点（第 {attempt + 1}/{max_retries} 次）: {e}"
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                logger.error(f"无法连接 Napcat HTTP 端点（最终尝试）: {e}")
            except Exception as e:
                logger.error(f"通过 HTTP 获取 Cookie 时发生异常: {e}")
                break
        return None

