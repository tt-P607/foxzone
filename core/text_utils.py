"""文本处理工具函数。"""

from __future__ import annotations


def truncate_preview(text: str, limit: int = 40) -> str:
    """截断文本用于日志预览，避免日志被超长正文刷屏。

    Args:
        text: 原始正文
        limit: 最大字符数

    Returns:
        截断后的预览文本（超出部分以 … 结尾）
    """
    text = str(text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"
