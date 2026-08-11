"""Cookie 服务模块。

负责 QQ 空间 Cookie 的获取、本地文件缓存与失效清理。
获取顺序：本地文件缓存 → 已启动适配器的 ``get_cookies`` action。

适配器统一透传 ``get_cookies``（各 QQ 适配器同命令名），
通过 ``adapter_api`` 自动探测已启动的适配器，无需再开 HTTP 服务器。
"""

from __future__ import annotations

import asyncio
import typing
from pathlib import Path

import orjson

from src.app.plugin_system.api.adapter_api import get_all_adapters
from src.app.plugin_system.api.log_api import get_logger, COLOR

if typing.TYPE_CHECKING:
    from ..config import FoxZoneConfig

logger = get_logger("foxzone.cookie_service", color=COLOR.ORANGE)

# Cookie 本地缓存目录（相对于项目根目录）
_COOKIE_DIR = Path("data/foxzone/cookies")

#: 用于获取 Cookie 的适配器 action 名（各 QQ 适配器同命令名）。
_GET_COOKIES_ACTION = "get_cookies"
#: 自动探测时按此顺序尝试已启动的适配器签名。
_DEFAULT_ADAPTER_SIGNATURES = (
    "snowluma_adapter:adapter:snowluma_adapter",
    "onebot_adapter:adapter:onebot_adapter",
)


def parse_cookie_string(cookie_str: str) -> dict[str, str]:
    """解析适配器返回的 Cookie 字符串为字典。

    Args:
        cookie_str: 形如 ``"uin=o123; skey=@abc; p_skey=xyz"`` 的 Cookie 字符串

    Returns:
        解析后的 ``{键: 值}`` 字典
    """
    result: dict[str, str] = {}
    if not cookie_str or not isinstance(cookie_str, str):
        return result
    for part in cookie_str.split("; "):
        if "=" in part:
            key, value = part.split("=", 1)
            result[key.strip()] = value.strip()
    return result


class CookieService:
    """Cookie 获取与缓存服务。

    负责以下职责：
    1. 优先读取本地文件缓存（最快，避免不必要的网络请求）。
    2. 缓存不存在时，通过已启动适配器的 ``get_cookies`` action 获取并写入缓存。
    3. 提供失效清理接口（Cookie 被 QQ 空间拒绝时调用）。

    Attributes:
        _config: 插件配置实例（FoxZoneConfig）
        _cookie_dir: Cookie 本地缓存目录
        _fetch_lock: 串行化适配器取 Cookie 的锁，避免多路任务并发重复拉取
    """

    def __init__(self, config: "FoxZoneConfig") -> None:  # type: ignore[name-defined]
        """初始化 Cookie 服务。

        Args:
            config: 插件配置实例
        """
        self._config = config
        self._cookie_dir = _COOKIE_DIR
        self._cookie_dir.mkdir(parents=True, exist_ok=True)
        # 用来串行化适配器取 Cookie，避免多个轮询任务同时发起 N 路重复请求。
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
        2. 已启动适配器的 ``get_cookies`` action（串行化，锁内二次检查缓存）

        Args:
            qq_account: QQ 账号字符串

        Returns:
            Cookie 字典；全部方法失败时返回 None
        """
        # 空 QQ 号时不读写本地文件缓存（避免生成 cookies-.json），仅从适配器获取
        if qq_account:
            # 1. 本地缓存
            cookies = self._load_from_file(qq_account)
            if cookies:
                logger.debug("从本地缓存加载 Cookie 成功。")
                return cookies

        # 2. 适配器获取（串行化：锁内二次检查本地缓存，避免多路任务重复拉取）
        async with self._fetch_lock:
            if qq_account:
                cookies = self._load_from_file(qq_account)
                if cookies:
                    logger.debug("从本地缓存加载 Cookie 成功（锁内）。")
                    return cookies
            logger.info("本地缓存不存在，尝试从适配器获取 Cookie...")
            cookies = await self._get_from_adapter()
            if cookies:
                if qq_account:
                    logger.info(
                        f"[bold #F38BA8]从适配器获取 QQ [bold #CBA6F7]{qq_account}"
                        f"[/bold #CBA6F7] 的 Cookie 成功[/bold #F38BA8]"
                    )
                else:
                    logger.info("[bold #F38BA8]从适配器获取 Cookie 成功[/bold #F38BA8]")
                if qq_account:
                    self._save_to_file(qq_account, cookies)
                return cookies

            account_desc = f"QQ {qq_account}" if qq_account else "QQ 空间"
            logger.error(
                f"为 {account_desc} 获取 Cookie 失败：本地缓存不可用，"
                "通过适配器获取亦未成功。请确认已启动 QQ 适配器，"
                "或存在有效的本地 Cookie 缓存。"
            )
            return None

    def has_adapter(self) -> bool:
        """判断当前是否存在可用的 Cookie 适配器。

        候选适配器已启动即视为可用（实际能否取回 Cookie 由 ``get_cookies`` 判定）。

        Returns:
            True 表示至少有一个候选适配器已启动
        """
        for signature in self._resolve_adapter_signatures():
            if get_all_adapters().get(signature) is not None:
                return True
        return False

    def clear_cache(self, qq_account: str) -> None:
        """删除指定账号的本地 Cookie 缓存文件。

        当 QQ 空间 API 返回 -3000（Cookie 失效）时调用此方法，
        清除缓存以便下次重新获取。

        Args:
            qq_account: QQ 账号字符串
        """
        if not qq_account:
            return
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

    async def _get_from_adapter(self) -> dict[str, str] | None:
        """通过已启动适配器的 ``get_cookies`` action 获取 Cookie。

        按配置的 ``adapter_signature`` 精确指定，或自动探测已启动的 QQ 适配器。
        所有适配器统一透传 ``get_cookies``。适配器虽已启动但 WebSocket 长连接
        可能尚未建立，故失败后按退避策略重试（默认 3/6/9 秒递增）。

        Returns:
            Cookie 字典；全部重试均失败时返回 None
        """
        cfg = self._config.cookie
        retry_times = max(0, int(cfg.retry_times))
        base_delay = max(0.0, float(cfg.retry_base_delay))

        last_error: Exception | None = None
        for attempt in range(retry_times + 1):
            signatures = self._resolve_adapter_signatures()
            if not signatures:
                logger.warning("未找到可用的适配器来获取 Cookie（无已启动的 QQ 适配器）。")
                return None

            cookies, last_error = await self._try_adapters(signatures, cfg)
            if cookies is not None:
                return cookies
            if attempt >= retry_times:
                break

            delay = base_delay * (attempt + 1)
            logger.info(
                f"[bold #F5A97F]从适配器获取 Cookie 失败，[/bold #F5A97F]"
                f"[bold #CBA6F7]{delay:.0f} 秒后重试"
                f"（第 {attempt + 1}/{retry_times} 次）...[/bold #CBA6F7]"
            )
            await asyncio.sleep(delay)

        if last_error is not None:
            logger.error(f"从适配器获取 Cookie 失败（最后错误: {last_error}）")
        return None

    async def _try_adapters(
        self, signatures: list[str], cfg: typing.Any
    ) -> tuple[dict[str, str] | None, Exception | None]:
        """尝试一轮所有候选适配器，返回首个成功的 Cookie 字典。

        Args:
            signatures: 本次尝试的适配器签名列表
            cfg: Cookie 配置节

        Returns:
            (Cookie 字典, 本轮最后一次异常)；本轮全部失败时 Cookie 为 None
        """
        last_error: Exception | None = None
        for signature in signatures:
            adapter = get_all_adapters().get(signature)
            if adapter is None:
                continue

            # 各 QQ 适配器统一透传 get_cookies，仅保留支持 API 透传的适配器
            if not hasattr(adapter, "send_snowluma_api") and not hasattr(adapter, "send_onebot_api"):
                continue

            try:
                resp = await self._call_adapter(adapter, cfg)
            except Exception as exc:  # noqa: BLE001 - 逐适配器尝试，需兜底
                last_error = exc
                logger.warning(f"从适配器获取 Cookie 失败: {exc}")
                continue

            if resp is None:
                continue
            cookies = parse_cookie_string(str(resp.get("data", {}).get("cookies", "")))
            if cookies:
                return cookies, None
            logger.warning("从适配器返回的 Cookie 为空或格式不正确。")

        return None, last_error

    def _resolve_adapter_signatures(self) -> list[str]:
        """解析本次尝试的适配器签名列表。

        配置显式指定 ``adapter_signature`` 时仅尝试该签名；
        留空时按默认顺序探测已启动的适配器。

        Returns:
            适配器签名列表
        """
        configured = self._config.cookie.adapter_signature.strip()
        if configured:
            return [configured]
        active = set(get_all_adapters().keys())
        return [sig for sig in _DEFAULT_ADAPTER_SIGNATURES if sig in active]

    async def _call_adapter(self, adapter: typing.Any, cfg: typing.Any) -> dict | None:
        """调用单个适配器的 ``get_cookies`` action。

        Args:
            adapter: 适配器实例
            cfg: Cookie 配置节

        Returns:
            原始 API 响应字典；失败返回 None
        """
        params = {"domain": cfg.domain}
        timeout = float(cfg.request_timeout)

        if hasattr(adapter, "send_snowluma_api"):
            return await adapter.send_snowluma_api(_GET_COOKIES_ACTION, params, timeout=timeout)  # type: ignore[attr-defined]
        if hasattr(adapter, "send_onebot_api"):
            return await adapter.send_onebot_api(_GET_COOKIES_ACTION, params, timeout=timeout)  # type: ignore[attr-defined]
        return None
