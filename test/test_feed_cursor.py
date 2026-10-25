"""好友动态分页状态和账号隔离游标测试。"""

from __future__ import annotations

import pytest

from plugins.foxzone.core.http.feed_cursor import (
    feed_position,
    initial_feed_state,
    next_feed_state,
)


def test_state_carries_server_fields_and_clock_is_not_progress() -> None:
    """可选字段沿用旧状态，服务时钟单独变化不算分页推进。"""
    previous = next_feed_state(
        {"begintime": "1700000000", "externparam": "opaque"}, initial_feed_state()
    )
    next_page = next_feed_state({"icServerTime": "99"}, previous)
    assert next_page["externparam"] == "opaque"
    assert feed_position(next_page) == feed_position(previous)


def test_state_maps_every_data_main_continuation_field() -> None:
    """服务端 main 中的全部分页字段都进入下一页状态。"""
    state = next_feed_state(
        {
            "begintime": "11",
            "dayspac": "12",
            "pagenum": "2",
            "aisortEndTime": "13",
            "aisortOffset": "14",
            "aisortBeginTime": "15",
            "aisortNextTime": "16",
            "icServerTime": "17",
            "externparam": "opaque=a%26b",
            "daylist": "days",
            "uinlist": "10002,10003",
        },
        initial_feed_state(),
    )
    assert state["begintime"] == "11"
    assert state["dayspac"] == "12"
    assert state["pagenum"] == "2"
    assert state["aisortEndTime"] == "13"
    assert state["aisortOffset"] == "14"
    assert state["aisortBeginTime"] == "15"
    assert state["aisortNextTime"] == "16"
    assert state["icServerTime"] == "17"
    assert state["externparam"] == "opaque=a%26b"
    assert state["daylist"] == "days"
    assert state["uinlist"] == "10002,10003"


@pytest.mark.parametrize(
    "main",
    [
        {"pagenum": {}},
        {"pagenum": "0"},
        {"begintime": True},
        {"externparam": []},
        {"daylist": "x" * 32_769},
    ],
)
def test_rejects_malformed_server_state(main: dict[str, object]) -> None:
    """异常类型、非法页码和超长不透明字段不进入游标。"""
    with pytest.raises((TypeError, ValueError), match="分页字段异常"):
        next_feed_state(main, initial_feed_state())
