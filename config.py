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
            default=1,
            description=(
                "外部空间回查每轮最多检查的 (qq, feed) 数量。"
                "采用「最久未检测优先」轮转策略，避免单轮请求过多触发 QZone 限流。"
            ),
        )
        external_followup_max_feed_age_hours: float = Field(
            default=72.0,
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

    @config_section("prompts")
    class PromptsSection(SectionBase):
        """提示词模板配置（唯一真源，共 4 个字段）。

        所有提示词文本均存放于本节。框架在首次加载或字段缺失时会按
        ``Field(default=...)`` 自动写回 ``config.toml``，用户后续修改不会被覆盖。
        修改本节字段后重启插件即生效。

        模板内变量占位符（PromptTemplate.build 阶段由调用方传入）：
          ``{personality_desc}`` / ``{current_time}`` / ``{weekday}`` /
          ``{topic_desc}`` / ``{history}`` / ``{comment_items_block}`` /
          ``{feed_items_block}`` / ``{output_format}``。

        共用占位符：
          ``__GUIDELINES__`` 在注册到 PromptManager 前会被
          ``comment_guidelines`` 字段实际文本一次性替换。
        """

        comment_guidelines: str = Field(
            default=(
                "【QZone 评论统一规范（必须严格遵守）】\n"
                "1. 字数严格控制在 30 字以内。\n"
                "2. 自然口语化，符合人格特征，禁止任何 Emoji。\n"
                "3. 禁止在开头添加 @某人，系统会自动处理。\n"
                "4. 不要写「期待你下次分享」「等你更新」之类诱导对方回复的话。\n"
                "5. 多条评论之间避免重复的句式 / 开场词 / 句尾点缀。\n"
                "6. 人设里反复出现的标签词是底色，不要让它们在评论里几乎每条都跳出来。"
            ),
            description=(
                "评论统一规范（被评论类模板共用）。"
                "在 comment_reply_batch / friend_feed_interact 模板内"
                "通过 __GUIDELINES__ 占位符替换。"
            ),
        )
        story_generate: str = Field(
            default=(
                "{personality_desc}\n\n"
                "现在是 {current_time}（{weekday}），"
                "你想写一条{topic_desc}的说说发表在 QQ 空间上。\n\n"
                "**说说文本规则：**\n"
                "1. **绝对禁止**在说说中直接、完整地提及当前的年月日或几点几分。\n"
                "2. 将当前时间作为创作背景，用它判断现在是「清晨」「傍晚」还是「深夜」。\n"
                "3. 使用自然、模糊的词语暗示时间，例如「刚刚」「今天下午」「夜深啦」。\n"
                "4. **内容简短**：总长度严格控制在 100 字以内。\n"
                "5. **禁止表情**：严禁使用任何 Emoji 表情符号。\n"
                "6. **严禁重复**：下方提供最近发过的说说历史，必须创作全新的、"
                "与历史记录内容和主题都不同的说说。\n"
                "7. 不要刻意突出自身学科背景，不要浮夸，不要夸张修辞。\n\n"
                "**输出格式（JSON）：**\n"
                "只输出一个合法 JSON，不含任何前缀、后缀或 Markdown 代码块。\n"
                "{output_format}\n\n"
                "---历史说说记录---\n"
                "{history}"
            ),
            description="模板 1/3：写说说正文（foxzone.story.generate）",
        )
        comment_reply_batch: str = Field(
            default=(
                "{personality_desc}\n\n"
                "当前时间：{current_time}\n\n"
                "以下是你的 QQ 空间最近收到的新评论，请逐条判断是否需要回复。\n\n"
                "{comment_items_block}\n\n"
                "**关于场景：**\n"
                "QQ 空间评论区不是即时聊天，是说说作者与互动者之间留言式的互动。\n"
                "你可以选择回复，也可以选择不回复——两者都是常见、合理的处置方式。\n\n"
                "**决策时可以参考：**\n"
                "1. 评论的内容性质（提问 / 关心 / 共鸣 / 表情 / 客套 / 一句感慨）；\n"
                "2. 是否真的有想说的话，还是只是“为了回而回”；\n"
                "3. 时效性：每条评论均已标注发布时间，结合与当前时间的差距综合判断——若过去很久才收到提醒，可酌情考虑是否还有回复价值；若决定回复，自然带出「刚看到」的语感即可，无需假装即时；\n"
                "4. **接力对话识别**：若某条评论被标注「在接你的话」（⚠ 标记），表示对方在回复你之前的发言——必须承接上下文、回应对方的话题或疑问，禁止重起新话题或答非所问；\n"
                "5. 同一条说说下若已有你的回复（评论区中显示为「你」），可酌情决定是否继续互动。\n\n"
                "__GUIDELINES__\n\n"
                "**输出格式（JSON 数组）：**\n"
                "只输出合法 JSON 数组，不含任何前缀、后缀或 Markdown 代码块。\n"
                "reply=null 表示不回复该评论；非 null 则填写回复正文。\n"
                '[{{"comment_tid": "评论ID", "feed_id": "说说ID", "reply": "回复内容或 null"}}]'
            ),
            description=(
                "模板 2/3：批量决策回复自己说说下的新评论"
                "（foxzone.comment.reply.batch）"
            ),
        )
        friend_feed_interact: str = Field(
            default=(
                "{personality_desc}\n\n"
                "当前时间：{current_time}\n\n"
                "<task>\n"
                "以下是好友们最近发布的说说，**所有这些说说均已自动点赞**。\n"
                "你只需要逐条判断是否额外写一条评论。\n"
                "</task>\n\n"
                "{feed_items_block}\n\n"
                "<context>\n"
                "QQ 空间不是聊天框，是好友间留言式的轻互动场景。\n"
                "点赞已经表态；评论是另一个独立动作，写或不写都属于正常选择。\n"
                "</context>\n\n"
                "<decision_principles>\n"
                "# 评论不是聊天\n"
                "- 评论是你顺手留下的一句感想，不是对话开头；\n"
                "- 不要 @ 说说作者，不要以“你”开头问候；\n"
                "- 不必把每条都接住，也没有“必须保持互动”的义务。\n"
                "\n"
                "# 内容判断\n"
                "- 评论的取舍可以参考：是否有共鸣、是否有想说的话、是否适合此情景；\n"
                "- 看不懂、纯转发、公式化营销、明显不需要外人插话的场合，可以选择不评论；\n"
                "- 决定写时，按下方 GUIDELINES 控制字数与措辞。\n"
                "\n"
                "# 情绪匹配\n"
                "- 说说是负面/严肃话题→ 收起玩笑，语气克制；\n"
                "- 说说是日常吐槽/晒图→ 自然延续氛围，不要强行升华。\n"
                "\n"
                "# 时效性\n"
                "- 结合发布时间与当前时间的差距调整语气。\n"
                "</decision_principles>\n\n"
                "__GUIDELINES__\n\n"
                "<output_format>\n"
                "只输出合法 JSON 数组，不含任何前缀、后缀或 Markdown 代码块。\n"
                "comment=null 表示仅点赞、不评论；非 null 则填写评论正文。\n"
                '[{{"tid": "说说ID", "target_qq": "QQ号", "comment": "评论内容或 null"}}]\n'
                "</output_format>"
            ),
            description=(
                "模板 3/3：好友说说接力评论决策（外部回查路径，"
                "foxzone.friend.feed.interact）"
            ),
        )

    # ---------- 字段声明（顺序与 Section 定义一致）----------
    general: GeneralSection = Field(default_factory=GeneralSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    monitor: MonitorSection = Field(default_factory=MonitorSection)
    cookie: CookieSection = Field(default_factory=CookieSection)
    prompts: "PromptsSection" = Field(default_factory=lambda: FoxZoneConfig.PromptsSection())

