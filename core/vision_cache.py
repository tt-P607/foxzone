"""图片视觉识别结果持久化缓存模块。

使用 storage_api 按图片 URL 缓存 LLM 视觉识别结果，避免对同一张图片重复推理。
"""

from __future__ import annotations

from src.app.plugin_system.api import storage_api
from src.app.plugin_system.api.log_api import COLOR, get_logger

logger = get_logger("foxzone.vision_cache", color=COLOR.CYAN)

_STORE_NAMESPACE = "foxzone"
_STORE_KEY = "vision_cache"


class ImageVisionCache:
    """图片视觉识别结果缓存。

    持久化存储结构：
    ``{"cache": {url: description_str}}``

    Attributes:
        _data: 内存中的缓存字典
        _dirty: 是否有未保存的更改
    """

    def __init__(self) -> None:
        """初始化缓存（未加载）。"""
        self._data: dict[str, str] = {}
        self._dirty: bool = False

    async def initialize(self) -> None:
        """从持久化存储加载缓存。"""
        loaded = await storage_api.load_json(_STORE_NAMESPACE, _STORE_KEY)
        if loaded is not None and isinstance(loaded, dict):
            cache_data = loaded.get("cache", {})
            if isinstance(cache_data, dict):
                self._data = {str(k): str(v) for k, v in cache_data.items() if v}
                logger.debug(f"已加载图片识别缓存：{len(self._data)} 条记录")
            else:
                logger.warning("图片识别缓存格式异常，使用空缓存初始化")
        else:
            logger.debug("图片识别缓存文件不存在，使用空缓存初始化")

    def get(self, url: str) -> str | None:
        """按 URL 获取缓存的识别描述。

        Args:
            url: 图片 URL

        Returns:
            缓存的描述文本，或 None（未命中）
        """
        return self._data.get(url)

    def set(self, url: str, description: str) -> None:
        """写入一条缓存记录。

        Args:
            url: 图片 URL
            description: LLM 识别的图片描述
        """
        if description:
            self._data[url] = description
            self._dirty = True

    async def save(self) -> None:
        """将有变更的缓存写回持久化存储。

        仅在 dirty 标记为 True 时实际写入，避免无意义 IO。
        """
        if not self._dirty:
            return
        await storage_api.save_json(
            _STORE_NAMESPACE, _STORE_KEY, {"cache": dict(self._data)}
        )
        self._dirty = False
        logger.debug(f"图片识别缓存已保存：共 {len(self._data)} 条")
