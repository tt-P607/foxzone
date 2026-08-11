"""FoxZone 自治层（Autopilot）。

FoxZone 自己的定时闭环：

- :mod:`scheduler`：DND 判定 + 三条定时循环
- :mod:`engine`：BatchSendEngine（抖动/重试/限流/标记，三条流程共用）
- :mod:`self_comments`：自己说说下的新评论
- :mod:`friend_feeds`：好友动态监控
- :mod:`external`：外部空间接力回查
"""

from __future__ import annotations

from .scheduler import Autopilot

__all__ = ["Autopilot"]
