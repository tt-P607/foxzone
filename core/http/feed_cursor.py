"""好友动态服务端分页状态和账号隔离游标。"""

from __future__ import annotations

from typing import Any

_NUMERIC_FIELDS = (
    "begintime",
    "dayspac",
    "pagenum",
    "aisortEndTime",
    "aisortOffset",
    "aisortBeginTime",
    "aisortNextTime",
    "icServerTime",
)
_STRING_FIELDS = ("externparam", "daylist", "uinlist")
_FIELDS = _NUMERIC_FIELDS + _STRING_FIELDS
_INITIAL_STATE = {
    "begintime": "0",
    "dayspac": "0",
    "pagenum": "1",
    "aisortEndTime": "0",
    "aisortOffset": "0",
    "aisortBeginTime": "0",
    "aisortNextTime": "0",
    "icServerTime": "0",
    "externparam": "",
    "daylist": "",
    "uinlist": "",
}


def initial_feed_state() -> dict[str, str]:
    """返回第一页使用的服务端状态字段。"""
    return dict(_INITIAL_STATE)


def next_feed_state(main: dict[str, Any], previous: dict[str, str]) -> dict[str, str]:
    """按服务端提供的字段推进游标，并拒绝类型异常或越界状态。"""
    next_state = dict(previous)
    for field in _NUMERIC_FIELDS:
        value = main.get(field)
        if value is None or value == "":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError(f"好友动态分页字段异常: {field}")
        raw = str(value)
        if not raw.isdecimal() or len(raw) > 16:
            raise ValueError(f"好友动态分页字段异常: {field}")
        number = int(raw)
        if number > 9_007_199_254_740_991 or (field == "pagenum" and number < 1):
            raise ValueError(f"好友动态分页字段异常: {field}")
        next_state[field] = str(number)
    for field in _STRING_FIELDS:
        value = main.get(field)
        if value is None or (value == "" and field != "uinlist"):
            continue
        if not isinstance(value, str) or len(value) > 32_768:
            raise ValueError(f"好友动态分页字段异常: {field}")
        next_state[field] = value
    return next_state


def feed_position(state: dict[str, str]) -> tuple[str, ...]:
    """产生游标位置；单独变化的服务端时钟不算翻页推进。"""
    return tuple(state[field] for field in _FIELDS if field != "icServerTime")
