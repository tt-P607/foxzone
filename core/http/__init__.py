"""QZone HTTP API 客户端包。

按接口族拆分：

- :mod:`client`：基础请求 / gtk / 图片上传
- :mod:`feeds`：说说列表 / 详情 / 好友动态流
- :mod:`comments`：评论 / 楼中楼回复
- :mod:`publish`：发布说说 / 点赞

对外暴露单一 :class:`QZoneAPIClient` 类型。
"""

from __future__ import annotations

from .comments import CommentsMixin
from .feeds import FeedsMixin
from .publish import PublishMixin


class QZoneAPIClient(FeedsMixin, CommentsMixin, PublishMixin):
    """QQ 空间 HTTP API 客户端（有状态，持有 Cookie/gtk/uin 上下文）。

    通过 mixin 组合三个接口族。使用工厂方法
    :meth:`~plugins.foxzone.core.http.client.QZoneClientBase.create`
    从 Cookie 字典构建实例。所有 API 方法在 Cookie 失效（code=-3000）时
    抛出 ``RuntimeError``，由上层统一处理重试。
    """


__all__ = ["QZoneAPIClient"]
