"""墨狐空间插件配置定义。

使用 @config_section 划分为语义清晰的配置节，基于 Pydantic + TOML 热重载。
所有 LLM 相关的模型任务名必须与 config/model.toml 中注册的任务名一致。
"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class FoxZoneConfig(BaseConfig):
    """墨狐空间插件配置。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "墨狐空间（QQ 空间说说自动化）插件配置"

    @config_section("general")
    class GeneralSection(SectionBase):
        """基础配置。"""

        enabled: bool = Field(default=True, description="是否启用插件")

    @config_section("llm")
    class LLMSection(SectionBase):
        """LLM 模型配置。任务名须与 config/model.toml 中 [model_tasks] 下的键名一致。"""

        story_model_task: str = Field(default="actor", description="生成说说正文的模型任务")
        comment_model_task: str = Field(default="actor", description="生成评论/回复的模型任务")
        vision_model_task: str = Field(
            default="vlm",
            description="图片视觉识别的模型任务（为空则跳过识图）",
        )
        multimodal_mode: bool = Field(
            default=False,
            description=(
                "多模态模式。开启后，LLM 决策（评论回复 / 好友互动）时"
                "把说说图片直接作为图像 payload 传给模型，不再先经 VLM 识图生成文本描述。"
                "要求所用模型支持视觉输入。"
            ),
        )

    @config_section("monitor")
    class MonitorSection(SectionBase):
        """好友动态监控配置。"""

        enable_auto_monitor: bool = Field(default=True, description="是否启用自动监控好友动态")
        interval_minutes: int = Field(default=10, description="评论回复轮询间隔（分钟）")
        comment_poll_num_feeds: int = Field(
            default=10,
            description="评论回复轮询每次检查的自己说说数量",
        )
        enable_auto_reply: bool = Field(default=True, description="是否自动回复自己说说下的评论")
        enable_external_followup: bool = Field(
            default=False,
            description=(
                "是否启用「外部空间评论回查」（检查 bot 在他人空间评论过的说说下是否有人回复）。"
                "独立于 enable_auto_reply 控制。默认关闭——该功能会对每个互动过的 QQ 轮询，"
                "互动对象多时请求量大、易触发 QZone 风控。"
            ),
        )
        max_comment_age_hours: float = Field(
            default=72.0,
            description="忽略超过此时间（小时）的旧评论，0 表示不限制",
        )
        enable_friend_monitor: bool = Field(
            default=False,
            description="是否自动监控好友说说并由 LLM 决策互动（点赞/评论）",
        )
        always_like: bool = Field(
            default=False,
            description=(
                "好友说说监控是否始终点赞。为 true 时无条件先点赞再决策评论；"
                "为 false 时不再强制点赞，由 LLM 对每条说说自主决定是否点赞与评论，"
                "未点赞的说说会记录为已读避免重复读取。"
            ),
        )
        friend_monitor_interval_minutes: int = Field(
            default=30,
            description="好友说说监控轮询间隔（分钟）",
        )
        friend_monitor_num_feeds: int = Field(
            default=10,
            description="每次监控最多检查的好友说说数量",
        )
        external_followup_minutes: int = Field(
            default=60,
            description=(
                "「外部空间评论回查」轮询间隔（分钟）。"
                "用于检查 bot 在他人空间里评论过的说说下是否有人回复 bot。"
                "仅 enable_external_followup=true 时生效。默认 60 分钟降低请求频率。"
            ),
        )
        external_followup_batch: int = Field(
            default=3,
            description=(
                "外部空间回查每轮最多检查的说说条数（feed 粒度）。"
                "采用「最久未回查优先」轮转策略，每轮查 3 条、轮流推进，"
                "避免单轮请求过多触发 QZone 限流。"
            ),
        )
        external_followup_max_feed_age_hours: float = Field(
            default=48.0,
            description=(
                "外部回查时，bot 评论过的说说超过此时长（小时）后不再回查。"
                "0 表示不限制。基于评论的最近一次互动时间（last_ts）判定。"
            ),
        )
        external_followup_max_replies_per_feed: int = Field(
            default=5,
            description=(
                "外部回查时，bot 在同一条好友说说下的最大累计接力回复次数。"
                "防止双 bot 互装本插件并互为好友时陷入「左脚踩右脚」无限对话。"
                "达到上限后该 feed 将停止接力（仍会被回查以更新 last_followup_check）。"
                "0 表示不限制。"
            ),
        )
        dnd_enabled: bool = Field(
            default=False,
            description="是否启用勿扰时间段（勿扰期间暂停所有轮询）",
        )
        dnd_start_hour: int = Field(
            default=23,
            description="勿扰开始时间（0-23，例如 23 表示晚上 11 点）",
        )
        dnd_end_hour: int = Field(
            default=7,
            description="勿扰结束时间（0-23，例如 7 表示早上 7 点）",
        )

    @config_section("cookie")
    class CookieSection(SectionBase):
        """Cookie 获取配置（经适配器 API 获取 QQ 空间 Cookie）。

        获取顺序：本地文件缓存 → 已启动适配器的 ``get_cookies`` action。
        适配器统一透传 ``get_cookies``（各 QQ 适配器同命令名），
        自动探测已启动的适配器，无需额外开启 HTTP 服务器。
        """

        #: 适配器组件签名（格式 ``plugin:adapter:name``），留空则自动探测。
        adapter_signature: str = Field(
            default="",
            description=(
                "获取 Cookie 的适配器签名（plugin:adapter:name）。"
                "留空自动探测已启动的 QQ 适配器。"
            ),
        )
        #: 需要 Cookie 的域名，QZone 为固定值。
        domain: str = Field(
            default="user.qzone.qq.com",
            description="需要 Cookie 的域名（QQ 空间固定为 user.qzone.qq.com）",
        )
        #: 请求超时秒数。
        request_timeout: float = Field(default=15.0, description="获取 Cookie 的请求超时（秒）")
        #: 适配器获取失败后的重试次数（不含首次尝试）。
        retry_times: int = Field(default=3, description="获取 Cookie 失败后的重试次数（不含首次尝试）")
        #: 首次重试等待秒数，此后逐次翻倍（如 3/6/9 递增）。
        retry_base_delay: float = Field(
            default=3.0, description="首次重试等待秒数，此后逐次递增（如 3/6/9 秒）"
        )

    # ---------- 字段声明（顺序与 Section 定义一致）----------
    general: GeneralSection = Field(default_factory=GeneralSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    monitor: MonitorSection = Field(default_factory=MonitorSection)
    cookie: CookieSection = Field(default_factory=CookieSection)

