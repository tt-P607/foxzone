"""QQ 空间 JSON、JSONP 与 frame 回执解析和结果校验。"""

from __future__ import annotations

import re
from typing import Any

import json5
import orjson


class QZoneRateLimitError(RuntimeError):
    """QZone 风控或限流拒绝。"""


class QZoneInvalidResponseError(RuntimeError):
    """响应载荷存在但结果字段不符合协议。"""


def _normalize_undefined(payload: str) -> str:
    """仅将 JavaScript 载荷中的 undefined token 转成 JSON null。"""
    output: list[str] = []
    index = 0
    quote: str | None = None
    escaped = False
    while index < len(payload):
        char = payload[index]
        if quote:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            output.append(char)
            index += 1
            continue
        if payload.startswith("undefined", index):
            before = payload[index - 1] if index else " "
            after_index = index + len("undefined")
            after = payload[after_index] if after_index < len(payload) else " "
            if not (before.isalnum() or before in "_$") and not (
                after.isalnum() or after in "_$"
            ):
                output.append("null")
                index = after_index
                continue
        output.append(char)
        index += 1
    if quote:
        raise ValueError("unterminated response string")
    return "".join(output)


def _extract_payload(text: str) -> str:
    """提取唯一完整对象载荷，不执行响应中的 JavaScript。"""
    source = text.lstrip("\ufeff \t\r\n")
    try:
        value = orjson.loads(source)
    except orjson.JSONDecodeError:
        value = None
    else:
        if isinstance(value, dict):
            return source
        raise ValueError("response root is not an object")

    callback = re.search(r"(?:[\w$.]+\s*\(|frameElement\.callback\s*\()", source)
    if callback is None:
        raise ValueError("response has no JSON object payload")
    start = source.find("{", callback.end())
    if start < 0:
        raise ValueError("response callback has no object payload")

    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                remainder = source[end:]
                if re.fullmatch(r"\s*\);?\s*(?:</[\w:-]+>\s*)*", remainder):
                    return source[start:end]
                if remainder.strip():
                    raise ValueError("response contains trailing executable payload")
                return source[start:end]
    raise ValueError("response object payload is incomplete")


def parse_response(text: str) -> dict[str, Any]:
    """解析 JSON、JSONP 或 frame callback 中唯一的对象回执。"""
    payload = _extract_payload(text)
    try:
        value = orjson.loads(payload)
    except orjson.JSONDecodeError:
        value = json5.loads(_normalize_undefined(payload))
    if not isinstance(value, dict):
        raise TypeError("response payload is not an object")
    return value


def check_response(data: dict[str, Any], operation: str) -> None:
    """验证 QZone 的 code/subcode，按明确拒绝分类并传播。"""
    code = data.get("code")
    subcode = data.get("subcode", 0)
    if isinstance(code, bool) or not isinstance(code, int):
        raise QZoneInvalidResponseError(f"{operation} response has invalid code")
    if isinstance(subcode, bool) or not isinstance(subcode, int):
        raise QZoneInvalidResponseError(f"{operation} response has invalid subcode")

    if code == -10049 or subcode == -10049:
        raise QZoneRateLimitError(f"{operation} rejected by QZone rate limit (code=-10049)")
    if code == -3000 or subcode == -3000:
        raise RuntimeError(f"{operation} rejected (错误码: -3000)")
    if code != 0 or subcode != 0:
        raise RuntimeError(f"{operation} rejected: code={code} subcode={subcode}")