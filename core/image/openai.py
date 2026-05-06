"""OpenAI 兼容 chat-completion 接口的生图 provider。

通过项目统一的 LLM 框架（``llm_api.create_llm_request``）调用 OpenAI / NEWAPI 风格的
chat completion 接口，将自然语言 prompt 作为 user 消息发送，
从返回文本中解析图片 URL 或 base64 并下载到本地。

依赖配置：
  ``cfg.openai.model_set`` —— ``config/model.toml`` 中 ``[model_tasks.<name>]`` 的键名。
  默认空字符串：用户需先在 ``model.toml`` 中创建任务模型段，例如：

      [model_tasks.foxzone_image]
      model_list = ["gpt-image-2"]
      max_tokens = 1024
      temperature = 0.7

  然后将 ``cfg.openai.model_set`` 设置为 ``"foxzone_image"``。
"""

from __future__ import annotations

import base64
import re
import typing
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.app.plugin_system.api.log_api import COLOR, get_logger
from src.app.plugin_system.types import LLMPayload, ROLE, Image, Text

from ...prompts import IMAGE_GUIDANCE_OPENAI

if typing.TYPE_CHECKING:
    from ...plugin import FoxZonePlugin

from ...config import FoxZoneConfig

logger = get_logger("foxzone.openai_image", color=COLOR.ORANGE)

_IMAGE_DIR = Path("data/foxzone/images")

# 图片解析正则：依次尝试 markdown 图片语法、纯 URL、data URI。
_MARKDOWN_IMG_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
_PLAIN_URL_RE = re.compile(
    r"https?://\S+?\.(?:png|jpe?g|webp|gif)(?:\?[^\s)]*)?",
    re.IGNORECASE,
)
_DATA_URI_RE = re.compile(
    r"data:image/(?P<ext>\w+);base64,(?P<b64>[A-Za-z0-9+/=]+)"
)

# 画幅 → 自然语言宽高比提示
_RATIO_HINT: dict[str, str] = {
    "方图": "1:1",
    "竖图": "9:16",
    "横图": "16:9",
}


class OpenAIImageService:
    """OpenAI 兼容 chat-completion 协议的生图服务。

    Attributes:
        provider_id: 固定为 ``"openai"``，与 ``cfg.ai_image.provider`` 对应。
        _plugin: 宿主插件实例
        _image_dir: 图片本地存储目录
    """

    provider_id: str = "openai"

    def __init__(self, plugin: "FoxZonePlugin") -> None:
        """初始化 OpenAI 生图服务。

        Args:
            plugin: 宿主插件实例
        """
        self._plugin = plugin
        self._cfg: FoxZoneConfig = plugin.config  # type: ignore[assignment]
        self._image_dir = _IMAGE_DIR
        self._image_dir.mkdir(parents=True, exist_ok=True)

        if self.is_available():
            logger.info(
                f"OpenAI 生图服务已配置 model_set={self._cfg.openai.model_set!r}"
            )

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """检查 OpenAI provider 是否可用。

        Returns:
            True 表示 ``cfg.openai.model_set`` 非空（用户已在 model.toml
            中配置任务模型段并指向本字段）。
        """
        return bool(self._cfg.openai.model_set.strip())

    def format_guidance(self) -> str:
        """返回已填充完成的 OpenAI 发说说图像指引。

        当存在有效参考图 **且** 用户在 ``cfg.openai.reference_images_guidance``
        中配置了使用提示后，才会在末尾追加 ``<reference_images>`` 段；
        否则不附加（避免无意义的提示噪声）。
        """
        guidance = IMAGE_GUIDANCE_OPENAI
        refs = self._collect_valid_reference_paths()
        ref_guide = self._cfg.openai.reference_images_guidance.strip()
        if refs and ref_guide:
            guidance += (
                f"\n<reference_images count=\"{len(refs)}\">\n{ref_guide}\n</reference_images>"
            )
        return guidance

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _collect_valid_reference_paths(self) -> list[Path]:
        """返回配置中存在的参考图本地路径列表。不存在的项仅警告，不报错。"""
        valid: list[Path] = []
        for raw in self._cfg.openai.reference_images:
            path = Path(str(raw).strip())
            if not str(path):
                continue
            if not path.exists() or not path.is_file():
                logger.warning(f"参考图未找到，已跳过：{path}")
                continue
            valid.append(path)
        return valid

    async def generate(
        self,
        *,
        prompt: str,
        negative_prompt: str | None = None,
        aspect_ratio: str = "方图",
        extras: dict[str, Any] | None = None,
    ) -> tuple[bool, Path | None, str]:
        """通过 chat-completion 接口生成图片。

        Args:
            prompt: 自然语言图片描述（中英文均可）
            negative_prompt: 仅记录为日志，OpenAI 协议不支持此概念
            aspect_ratio: 画幅类型，``"方图" | "竖图" | "横图"``
            extras: 预留扩展参数

        Returns:
            ``(success, image_path, message)``
        """
        del extras  # 暂未使用
        task_name = self._cfg.openai.model_set.strip()
        if not task_name:
            return False, None, "OpenAI provider 未配置 model_set"

        if negative_prompt:
            logger.debug("OpenAI 协议不支持 negative_prompt，已忽略。")

        try:
            model_set = get_model_set_by_task(task_name)
        except Exception as exc:
            logger.error(f"获取 model_set 失败 [{task_name}]: {exc}")
            return False, None, f"获取 model_set 失败: {exc!s}"

        user_text = self._build_user_prompt(prompt, aspect_ratio)
        reference_paths = self._collect_valid_reference_paths()
        logger.info(
            f"开始 OpenAI 生图… 画幅: {aspect_ratio}，参考图: {len(reference_paths)} 张"
        )
        logger.debug(f"用户消息: {user_text[:120]}…")

        try:
            request = create_llm_request(model_set, request_name="foxzone_openai_image")
            content_parts: list = [Text(user_text)]
            for ref_path in reference_paths:
                try:
                    content_parts.append(Image(ref_path))
                except Exception as exc:
                    logger.warning(f"加载参考图失败、已跳过 [{ref_path}]: {exc}")
            request.add_payload(LLMPayload(ROLE.USER, content_parts))
            response = await request.send(stream=False)
            response_text = await response
        except Exception as exc:
            logger.error(f"OpenAI 生图请求失败: {exc}", exc_info=True)
            return False, None, f"请求失败: {exc!s}"

        if not response_text:
            return False, None, "OpenAI 返回空响应"

        url, data_uri = self._parse_response(str(response_text))
        if data_uri:
            path = self._save_base64(data_uri)
            if path:
                logger.info(f"OpenAI 图片生成成功（base64）: {path}")
                return True, path, "生成成功（base64）"
            return False, None, "保存 base64 图片失败"

        if url:
            path = await self._download(url)
            if path:
                logger.info(f"OpenAI 图片生成成功（URL）: {path}")
                return True, path, "生成成功（URL）"
            return False, None, "下载图片失败"

        logger.warning(f"未能从 OpenAI 响应中提取图片，响应预览: {response_text[:200]!r}")
        return False, None, "响应中未包含图片"

    # ------------------------------------------------------------------
    # 私有方法
    # ------------------------------------------------------------------

    @staticmethod
    def _build_user_prompt(prompt: str, aspect_ratio: str) -> str:
        """构造发给 chat 接口的用户消息。"""
        ratio_hint = _RATIO_HINT.get(aspect_ratio, "1:1")
        return (
            f"请根据以下描述生成一张图片（画幅比例 {ratio_hint}）。"
            "只输出图片本身（URL 或 base64 data URI），不要附加任何解释文字。\n\n"
            f"描述：{prompt}"
        )

    @staticmethod
    def _parse_response(text: str) -> tuple[str | None, str | None]:
        """从响应文本提取 ``(url, data_uri)``。

        优先级：markdown 图片语法 → 纯 URL → data URI。
        """
        m = _MARKDOWN_IMG_RE.search(text)
        if m:
            return m.group(1), None
        m = _PLAIN_URL_RE.search(text)
        if m:
            return m.group(0), None
        m = _DATA_URI_RE.search(text)
        if m:
            return None, m.group(0)
        return None, None

    def _save_base64(self, data_uri: str) -> Path | None:
        """将 data URI 解码后落盘。"""
        m = _DATA_URI_RE.match(data_uri)
        if not m:
            return None
        try:
            ext = m.group("ext").lower()
            ext = "jpg" if ext == "jpeg" else ext
            data = base64.b64decode(m.group("b64"))
            path = self._image_dir / f"openai_{uuid.uuid4().hex}.{ext}"
            path.write_bytes(data)
            return path
        except Exception as exc:
            logger.error(f"保存 base64 图片失败: {exc}")
            return None

    async def _download(self, url: str) -> Path | None:
        """下载远程图片到本地。"""
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    data = await resp.read()
            ext = self._guess_ext(url)
            path = self._image_dir / f"openai_{uuid.uuid4().hex}.{ext}"
            path.write_bytes(data)
            return path
        except Exception as exc:
            logger.error(f"下载图片失败 [{url}]: {exc}")
            return None

    @staticmethod
    def _guess_ext(url: str) -> str:
        """根据 URL 后缀推测扩展名。"""
        lower = url.lower()
        for e in ("png", "webp", "gif", "jpg", "jpeg"):
            if f".{e}" in lower:
                return "jpg" if e == "jpeg" else e
        return "png"
