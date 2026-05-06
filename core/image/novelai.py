"""NovelAI 图片生成服务模块（独立实现）。

专为 FoxZone 插件实现，不依赖其他插件。
支持 NovelAI Diffusion 系列模型（二次元风格），
与 ContentService 生成的配图信息字典（prompt / negative_prompt /
include_character / aspect_ratio）直接配合使用。

图片存储目录：`data/foxzone/images/`
"""

from __future__ import annotations

import io
import random
import typing
import uuid
import zipfile
from pathlib import Path

import aiohttp

from src.app.plugin_system.api.log_api import get_logger, COLOR

if typing.TYPE_CHECKING:
    from ...plugin import FoxZonePlugin

logger = get_logger("foxzone.novelai_service", color=COLOR.ORANGE)

# NovelAI API 端点
_NOVELAI_API_URL = "https://image.novelai.net/ai/generate-image"

# 图片本地存储目录
_IMAGE_DIR = Path("data/foxzone/images")

# 画幅到宽高的映射
_ASPECT_RATIO_MAP: dict[str, tuple[int, int]] = {
    "方图": (1024, 1024),
    "竖图": (832, 1216),
    "横图": (1216, 832),
}


class NovelAIService:
    """NovelAI 图片生成服务。

    依赖插件配置中的 ``novelai`` 段（api_key / character_prompt /
    base_negative_prompt / proxy_host / proxy_port）。
    调用 NovelAI Diffusion API 并支持 V4/V4.5 接口格式。

    Attributes:
        _plugin: 宿主插件实例
        _image_dir: 图片本地存储目录
    """

    def __init__(self, plugin: "FoxZonePlugin") -> None:
        """初始化 NovelAI 服务。

        Args:
            plugin: 宿主插件实例
        """
        self._plugin = plugin
        self._image_dir = _IMAGE_DIR
        self._image_dir.mkdir(parents=True, exist_ok=True)

        if self.is_available():
            logger.info(f"NovelAI 服务已配置，模型: {self._plugin.config.novelai.model}")

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """检查 NovelAI API Key 是否已配置。

        Returns:
            True 表示可用
        """
        return bool(self._plugin.config.novelai.api_key)

    async def generate_image_from_prompt_data(
        self,
        prompt: str,
        negative_prompt: str | None = None,
        aspect_ratio: str = "方图",
    ) -> tuple[bool, Path | None, str]:
        """根据提示词数据生成图片。

        Args:
            prompt: NovelAI 格式的英文提示词（角色外貌 tag 由 LLM 自行写入，画风锚点由 cfg.character_prompt 自动注入到头部）
            negative_prompt: LLM 生成的针对性负面提示词（可选，会与 cfg.base_negative_prompt 合并）
            aspect_ratio: 画幅类型，支持 "方图"、"竖图"、"横图"

        Returns:
            (是否成功, 图片路径, 消息)
        """
        if not self.is_available():
            return False, None, "NovelAI API Key 未配置"

        try:
            cfg = self._plugin.config.novelai

            # 注入画风锚点（不再受 include_character 控制，画风总是锚定）
            final_prompt = prompt
            if cfg.character_prompt:
                final_prompt = f"{cfg.character_prompt}, {prompt}"
                logger.debug("已注入画风锚点提示词。")

            # 合并负面提示词
            base_neg = cfg.base_negative_prompt
            if negative_prompt:
                final_negative = (
                    f"{base_neg}, {negative_prompt}" if base_neg else negative_prompt
                )
            else:
                final_negative = base_neg

            # 解析画幅
            width, height = _ASPECT_RATIO_MAP.get(aspect_ratio, (1024, 1024))

            logger.info(
                f"开始 NovelAI 生图… 尺寸: {width}x{height}"
            )
            logger.debug(f"正面提示词: {final_prompt[:120]}…")
            logger.debug(f"负面提示词: {final_negative[:80]}…")

            # 构建请求载荷
            payload = self._build_payload(final_prompt, final_negative, width, height)

            # 配置代理
            proxy: str | None = None
            if cfg.proxy_host and cfg.proxy_port:
                proxy = f"http://{cfg.proxy_host}:{cfg.proxy_port}"

            # 调用 API
            image_data = await self._call_novelai_api(
                payload=payload,
                api_key=cfg.api_key,
                proxy=proxy,
            )
            if not image_data:
                return False, None, "API 请求失败"

            # 保存图片
            image_path = self._save_image(image_data)
            if not image_path:
                return False, None, "图片保存失败"

            logger.info(f"NovelAI 图片生成成功: {image_path}")
            return True, image_path, "生成成功"

        except Exception as e:
            logger.error(f"NovelAI 生图时发生异常: {e}", exc_info=True)
            return False, None, f"生成失败: {e!s}"

    # ------------------------------------------------------------------
    # 私有方法
    # ------------------------------------------------------------------

    def _build_payload(
        self,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
    ) -> dict:
        """构建 NovelAI API 请求载荷。

        自动检测模型版本（V4/V4.5 与 V3 格式不同）。

        Args:
            prompt: 最终正面提示词
            negative_prompt: 最终负面提示词
            width: 图片宽度
            height: 图片高度

        Returns:
            完整的请求载荷字典
        """
        _model = self._plugin.config.novelai.model
        is_v4_model = "diffusion-4" in _model
        is_v3_model = "diffusion-3" in _model

        parameters: dict = {
            "width": width,
            "height": height,
            "scale": 5.0,
            "steps": 28,
            "sampler": "k_euler",
            "seed": random.randint(0, 9_999_999_999),
            "n_samples": 1,
            "ucPreset": 0,
            "qualityToggle": True,
            "sm": False,
            "sm_dyn": False,
            "noise_schedule": "karras" if is_v4_model else "native",
        }

        if is_v4_model:
            parameters.update(
                {
                    "params_version": 3,
                    "cfg_rescale": 0,
                    "autoSmea": False,
                    "legacy": False,
                    "legacy_v3_extend": False,
                    "legacy_uc": False,
                    "add_original_image": True,
                    "controlnet_strength": 1,
                    "dynamic_thresholding": False,
                    "prefer_brownian": True,
                    "normalize_reference_strength_multiple": True,
                    "use_coords": True,
                    "inpaintImg2ImgStrength": 1,
                    "deliberate_euler_ancestral_bug": False,
                    "skip_cfg_above_sigma": None,
                    "characterPrompts": [],
                    "stream": "msgpack",
                    "v4_prompt": {
                        "caption": {
                            "base_caption": prompt,
                            "char_captions": [],
                        },
                        "use_coords": True,
                        "use_order": True,
                    },
                    "v4_negative_prompt": {
                        "caption": {
                            "base_caption": negative_prompt,
                            "char_captions": [],
                        },
                        "legacy_uc": False,
                    },
                    "negative_prompt": negative_prompt,
                    "reference_image_multiple": [],
                    "reference_information_extracted_multiple": [],
                    "reference_strength_multiple": [],
                }
            )
        elif is_v3_model:
            parameters["negative_prompt"] = negative_prompt

        payload: dict = {
            "input": prompt,
            "model": self._plugin.config.novelai.model,
            "action": "generate",
            "parameters": parameters,
        }
        if is_v4_model:
            payload["use_new_shared_trial"] = True

        return payload

    async def _call_novelai_api(
        self,
        payload: dict,
        api_key: str,
        proxy: str | None,
    ) -> bytes | None:
        """向 NovelAI API 发送生图请求。

        Args:
            payload: 请求载荷
            api_key: NovelAI API Key
            proxy: HTTP 代理地址（可选）

        Returns:
            PNG 图片字节数据；失败时返回 None
        """
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        request_kwargs: dict = {
            "json": payload,
            "headers": headers,
            "timeout": aiohttp.ClientTimeout(total=120),
        }
        if proxy:
            request_kwargs["proxy"] = proxy
            logger.debug(f"使用代理: {proxy}")

        try:
            connector = aiohttp.TCPConnector() if proxy else None
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    _NOVELAI_API_URL, **request_kwargs
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(
                            f"NovelAI API 返回错误 ({resp.status}): {error_text[:200]}"
                        )
                        return None

                    raw_data = await resp.read()
                    logger.debug(f"收到响应: {len(raw_data)} bytes")

                    # 检测数据格式
                    if raw_data[:4] == b"PK\x03\x04":
                        # ZIP 压缩包（含 PNG）
                        logger.debug("检测到 ZIP 格式，正在解压…")
                        return self._extract_from_zip(raw_data)
                    elif raw_data[:8] == b"\x89PNG\r\n\x1a\n":
                        return raw_data
                    else:
                        logger.warning(
                            f"未知图片格式，前 4 字节: {raw_data[:4].hex()}"
                        )
                        return raw_data  # 尝试直接使用

        except aiohttp.ClientError as e:
            logger.error(f"调用 NovelAI API 时网络错误: {e}")
            return None
        except Exception as e:
            logger.error(f"调用 NovelAI API 时发生异常: {e}")
            return None

    def _extract_from_zip(self, zip_data: bytes) -> bytes | None:
        """从 ZIP 压缩包中提取第一个 PNG 文件。

        Args:
            zip_data: ZIP 文件的二进制内容

        Returns:
            PNG 图片字节数据；失败时返回 None
        """
        try:
            with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                for filename in zf.namelist():
                    if filename.lower().endswith(".png"):
                        data = zf.read(filename)
                        logger.debug(f"从 ZIP 提取: {filename} ({len(data)} bytes)")
                        return data
            logger.error("ZIP 压缩包中未找到 PNG 文件。")
            return None
        except Exception as e:
            logger.error(f"解压 ZIP 失败: {e}")
            return None

    def _save_image(self, image_data: bytes) -> Path | None:
        """将图片二进制数据保存为本地 PNG 文件。

        Args:
            image_data: PNG 图片字节数据

        Returns:
            保存后的文件路径；失败时返回 None
        """
        try:
            from PIL import Image

            filename = f"novelai_{uuid.uuid4().hex[:12]}.png"
            filepath = self._image_dir / filename

            with open(filepath, "wb") as f:
                f.write(image_data)

            # 验证图片有效性
            try:
                with Image.open(filepath) as img:
                    img.verify()
                # verify() 会关闭文件，需要重新打开
                with Image.open(filepath) as img:
                    logger.debug(f"图片验证成功: {img.format} {img.size}")
            except Exception as e:
                logger.warning(f"图片验证失败（但文件已保存）: {e}")

            return filepath

        except Exception as e:
            logger.error(f"保存 NovelAI 图片失败: {e}")
            return None
