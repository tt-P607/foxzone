"""FoxZone 插件组件包。

包含框架可识别的组件：QZoneService（BaseService）等。
"""

#: 全插件统一的 Service signature，用于 ``get_service()`` 查找 QZoneService 单例。
SERVICE_SIG: str = "foxzone:service:qzone_service"

__all__ = ["SERVICE_SIG"]
