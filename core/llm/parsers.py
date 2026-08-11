"""LLM 响应解析工具。

包含 markdown fence 剥离、截断 JSON 检测与修复、回复文本清洗，
以及三种业务 JSON（简单 text / 带 image / 批量决策数组）的容错解析。
纯函数实现，无框架依赖。
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.app.plugin_system.api.log_api import COLOR, get_logger

logger = get_logger("foxzone.parsers", color=COLOR.ORANGE)


def strip_markdown_fence(response: str) -> str:
    """移除响应中的 Markdown 代码块包裹。

    Args:
        response: LLM 原始响应文本

    Returns:
        去掉 ```json / ``` 包裹后的文本
    """
    raw = response.strip()
    if raw.startswith("```json"):
        raw = raw[7:]
    elif raw.startswith("```"):
        raw = raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    return raw.strip()


def is_truncated_json(response: str) -> bool:
    """检测 JSON 文本是否明显被截断。

    Args:
        response: 待检测文本

    Returns:
        True 表示疑似被截断
    """
    stripped = response.strip()
    if stripped.startswith("{") and not stripped.endswith("}"):
        return True
    if stripped.startswith("[") and not stripped.endswith("]"):
        return True
    if stripped.startswith("{") and stripped.count('"') % 2 != 0:
        return True
    return False


def extract_text_from_broken_json(response: str) -> str:
    """从损坏 JSON 中尽量提取 text 字段。

    Args:
        response: 疑似截断/损坏的 JSON 文本

    Returns:
        提取出的 text 字段值；无法提取时返回空字符串
    """
    patterns = [
        r'"text"\s*:\s*"([^"\\]*)"',
        r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"',
        r'"text"\s*:\s*"([^\n}]*)',
    ]
    for pattern in patterns:
        match = re.search(pattern, response, re.DOTALL)
        if not match:
            continue

        extracted = match.group(1)
        extracted = (
            extracted.replace(r'\"', '"')
            .replace(r"\n", "\n")
            .replace(r"\t", "\t")
            .strip()
        )
        if len(extracted) >= 3 and not extracted.endswith(("\\", ",")):
            return extracted
    return ""


def clean_reply(text: str) -> str:
    """清理 LLM 返回文本中的格式噪音（引号包裹 / 前缀 @xxx）。

    Args:
        text: LLM 生成的回复文本

    Returns:
        清洗后的纯文本
    """
    cleaned = text.strip()
    if cleaned.startswith('"') and cleaned.endswith('"'):
        cleaned = cleaned[1:-1]
    if cleaned.startswith("'") and cleaned.endswith("'"):
        cleaned = cleaned[1:-1]

    cleaned = re.sub(r"^回复\s*@[^:：]+[:：]?\s*", "", cleaned)
    cleaned = re.sub(r"^@[^:：\s]+[:：]?\s*", "", cleaned)
    return cleaned.strip()


def _loads_tolerant(raw: str) -> Any:
    """先用标准 json 解析，失败再用 json5 容错解析。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        import json5

        return json5.loads(raw)


def parse_simple_json_text(response: str) -> str:
    """解析 ``{"text": "..."}`` 格式响应。

    Args:
        response: LLM 原始响应文本

    Returns:
        text 字段内容；解析失败时尽量从损坏 JSON 中提取。
    """
    raw = strip_markdown_fence(response)
    if is_truncated_json(raw):
        logger.warning("检测到响应 JSON 被截断，尝试提取 text 字段。")
        return extract_text_from_broken_json(raw)

    try:
        data = _loads_tolerant(raw)
        if not isinstance(data, dict):
            return extract_text_from_broken_json(raw)
        return str(data.get("text", "")).strip()
    except Exception as exc:
        logger.warning(f"解析 JSON 失败: {exc}，尝试直接提取 text 字段。")
        return extract_text_from_broken_json(raw)


def parse_batch_reply_decisions(response: str) -> list[dict[str, Any]]:
    """解析批量回复决策 JSON 数组。

    Args:
        response: LLM 原始响应文本

    Returns:
        决策列表，每项保证含 comment_tid、feed_id 字段；
        reply 字段为 None 或非空字符串。
    """
    raw = strip_markdown_fence(response)
    try:
        data = _loads_tolerant(raw)

        if not isinstance(data, list):
            logger.warning(f"批量决策响应不是 JSON 数组: {raw[:200]}")
            return []

        result: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            comment_tid = str(item.get("comment_tid", "")).strip()
            feed_id = str(item.get("feed_id", "")).strip()
            if not (comment_tid and feed_id):
                continue
            reply_raw = item.get("reply")
            if reply_raw is None:
                reply: str | None = None
            else:
                reply = clean_reply(str(reply_raw))
                if not reply:
                    reply = None
            result.append({"comment_tid": comment_tid, "feed_id": feed_id, "reply": reply})

        return result
    except Exception as exc:
        logger.error(f"解析批量决策 JSON 失败: {exc}，响应内容: {raw[:300]}")
        return []


def parse_feed_decisions(response: str) -> list[dict[str, Any]]:
    """解析好友说说互动决策 JSON 数组。

    Args:
        response: LLM 原始响应文本

    Returns:
        决策列表，每项保证含 tid、target_qq、like、comment 字段；
        ``like`` 缺失时视为 False（默认不点赞）。
    """
    raw = strip_markdown_fence(response)
    try:
        data = _loads_tolerant(raw)

        if not isinstance(data, list):
            logger.warning(f"好友说说决策响应不是 JSON 数组: {raw[:200]}")
            return []

        result: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            tid = str(item.get("tid", "")).strip()
            target_qq = str(item.get("target_qq", "")).strip()
            if not (tid and target_qq):
                continue
            like_raw = item.get("like")
            like: bool = bool(like_raw) if like_raw is not None else False
            comment_raw = item.get("comment")
            comment: str | None = None
            if comment_raw is not None:
                comment = clean_reply(str(comment_raw))
                if not comment:
                    comment = None
            result.append(
                {"tid": tid, "target_qq": target_qq, "like": like, "comment": comment}
            )

        return result
    except Exception as exc:
        logger.error(f"解析好友说说决策 JSON 失败: {exc}，响应内容: {raw[:300]}")
        return []
