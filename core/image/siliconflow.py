"""图片生成服务模块（SiliconFlow 提供商）。

负责通过硅基流动（SiliconFlow）API 生成 AI 配图：
1. 接收来自 ContentService 生成的提示词
2. 调用 SiliconFlow Images API 生成图片
3. 下载并保存到本地图片目录

图片存储目录：`data/foxzone/images/`
"""

from __future__ import annotations

import random
import typing
from io import BytesIO
from pathlib import Path

import aiohttp

from src.app.plugin_system.api.log_api import get_logger, COLOR

if typing.TYPE_CHECKING:
    from ...plugin import FoxZonePlugin

from ...config import FoxZoneConfig

logger = get_logger("foxzone.image_service", color=COLOR.ORANGE)

# 图片本地存储目录
_IMAGE_DIR = Path("data/foxzone/images")

# SiliconFlow 默认模型（可通过配置覆盖）
_DEFAULT_MODEL = "Kwai-Kolors/Kolors"

# SiliconFlow API 端点
_SILICONFLOW_API_URL = "https://api.siliconflow.cn/v1/images/generations"

# AI 生图默认负面提示词（过滤低质量和有害内容）
_DEFAULT_NEGATIVE_PROMPT = (
    "lowres, bad anatomy, bad hands, text, error, cropped, worst quality, low quality, "
    "normal quality, jpeg artifacts, signature, watermark, username, blurry"
)


class ImageService:
    """SiliconFlow AI 图片生成服务。

    封装了通过硅基流动 API 生成、下载和保存图片的全部逻辑。

    Attributes:
        _plugin: 宿主插件实例，通过其读取配置
        _image_dir: 图片本地存储目录
    """

    def __init__(self, plugin: "FoxZonePlugin") -> None:
        """初始化图片服务。

        Args:
            plugin: 宿主插件实例
        """
        self._plugin: "FoxZonePlugin" = plugin
        self._cfg: FoxZoneConfig = plugin.config  # type: ignore[assignment]
        self._image_dir = _IMAGE_DIR
        self._image_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """检查 SiliconFlow 服务是否已配置 API Key。

        Returns:
            True 表示已配置，可以使用
        """
        return bool(self._cfg.siliconflow.api_key)

    async def generate_image_from_prompt(
        self, prompt: str, save_dir: Path | None = None
    ) -> tuple[bool, Path | None]:
        """使用指定提示词直接生成图片（SiliconFlow）。

        Args:
            prompt: 英文图片提示词
            save_dir: 自定义保存目录；None 时使用默认目录

        Returns:
            (是否成功, 第一张图片路径)
        """
        try:
            api_key = self._cfg.siliconflow.api_key
            model = self._cfg.siliconflow.model or _DEFAULT_MODEL
            image_num = max(1, min(self._cfg.siliconflow.image_number, 4))

            if not api_key:
                logger.warning("SiliconFlow API Key 未配置，跳过图片生成。")
                return False, None

            target_dir = save_dir or self._image_dir
            target_dir.mkdir(parents=True, exist_ok=True)

            logger.info(f"正在生成 {image_num} 张 AI 配图（SiliconFlow）…")
            return await self._call_siliconflow_api(
                api_key=api_key,
                model=model,
                prompt=prompt,
                image_dir=target_dir,
                batch_size=image_num,
            )

        except Exception as e:
            logger.error(f"生成 AI 配图时发生异常: {e}")
            return False, None

    def get_saved_images(self) -> list[Path]:
        """获取图片目录中所有已保存的图片文件。

        Returns:
            图片路径列表（按修改时间降序）
        """
        images = list(self._image_dir.glob("*.png")) + list(
            self._image_dir.glob("*.jpg")
        )
        return sorted(images, key=lambda p: p.stat().st_mtime, reverse=True)

    def clear_images(self) -> int:
        """清空图片目录。

        Returns:
            已删除的图片文件数量
        """
        deleted = 0
        for f in self._image_dir.glob("*"):
            if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg"):
                try:
                    f.unlink()
                    deleted += 1
                except OSError as e:
                    logger.warning(f"删除图片失败: {f}: {e}")
        if deleted > 0:
            logger.info(f"已清空图片目录，删除 {deleted} 张图片。")
        return deleted

    # ------------------------------------------------------------------
    # 私有方法
    # ------------------------------------------------------------------

    async def _call_siliconflow_api(
        self,
        api_key: str,
        model: str,
        prompt: str,
        image_dir: Path,
        batch_size: int,
    ) -> tuple[bool, Path | None]:
        """调用 SiliconFlow API 生成图片并保存。

        Args:
            api_key: SiliconFlow API Key
            model: 模型 ID
            prompt: 图片提示词
            image_dir: 图片保存目录
            batch_size: 批量生成数量（1-4）

        Returns:
            (是否至少有一张成功, 第一张图片路径)
        """
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "prompt": prompt,
            "negative_prompt": _DEFAULT_NEGATIVE_PROMPT,
            "seed": random.randint(1, 9_999_999_999),
            "batch_size": batch_size,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=120.0)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _SILICONFLOW_API_URL,
                    json=payload,
                    headers=headers,
                    timeout=timeout,
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(
                            f"SiliconFlow API 返回错误 [{resp.status}]: {error_text[:200]}"
                        )
                        return False, None

                    json_data = await resp.json()
                    image_urls = [img["url"] for img in json_data.get("images", [])]
                    if not image_urls:
                        logger.error("SiliconFlow API 返回空图片列表。")
                        return False, None

                    success_count = 0
                    first_path: Path | None = None

                    for i, img_url in enumerate(image_urls):
                        path = await self._download_and_save(
                            session, img_url, image_dir, f"siliconflow_{i}.png"
                        )
                        if path:
                            success_count += 1
                            if first_path is None:
                                first_path = path

                    return success_count > 0, first_path

        except aiohttp.ClientError as e:
            logger.error(f"调用 SiliconFlow API 时网络错误: {e}")
            return False, None
        except Exception as e:
            logger.error(f"调用 SiliconFlow API 时发生异常: {e}")
            return False, None

    async def _download_and_save(
        self,
        session: "aiohttp.ClientSession",
        url: str,
        save_dir: Path,
        filename: str,
    ) -> Path | None:
        """下载图片并保存为 PNG 文件。

        Args:
            session: 复用的 aiohttp 会话
            url: 图片下载 URL
            save_dir: 保存目录
            filename: 目标文件名

        Returns:
            保存后的路径；失败时返回 None
        """
        try:
            async with session.get(url) as img_resp:
                img_resp.raise_for_status()
                img_data = await img_resp.read()

            # PIL 处理（转 RGB、保存为 PNG）
            from PIL import Image

            image = Image.open(BytesIO(img_data))
            if image.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", image.size, (255, 255, 255))
                mask = image.split()[-1] if image.mode in ("RGBA", "LA") else None
                background.paste(image, mask=mask)
                image = background

            save_path = save_dir / filename
            image.save(save_path, format="PNG")
            logger.info(f"图片已保存：{save_path}")
            return save_path

        except Exception as e:
            logger.error(f"下载/保存图片失败（{url}）: {e}")
            return None
