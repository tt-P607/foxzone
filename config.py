"""墨狐空间插件配置定义。

使用 @config_section 划分为语义清晰的配置节，基于 Pydantic + TOML 热重载。
所有 LLM 相关的模型任务名必须与 config/model.toml 中注册的任务名一致。
"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class FoxZoneConfig(BaseConfig):
    """墨狐空间插件配置。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "墨狐空间（QQ 空间说说自动化）插件配置"

    @config_section("general")
    class GeneralSection(SectionBase):
        """基础配置。"""

        enabled: bool = Field(default=True, description="是否启用插件")
        bot_qq: str = Field(default="", description="Bot 的 QQ 号，用于监控和回复自己说说的评论")
        expose_feed_write_tools: bool = Field(
            default=True,
            description=(
                "是否向模型暴露写说说 Tool（qzone_start_compose_feed / qzone_submit_feed）。"
                "关闭后仍保留 /send_feed 命令和底层服务能力。"
            ),
        )

    @config_section("llm")
    class LLMSection(SectionBase):
        """LLM 模型配置。任务名须与 config/model.toml 中 [model_tasks] 下的键名一致。"""

        story_model_task: str = Field(default="actor", description="生成说说正文的模型任务")
        comment_model_task: str = Field(default="actor", description="生成评论/回复的模型任务")
        vision_model_task: str = Field(
            default="vlm",
            description="图片视觉识别的模型任务（为空则跳过识图）",
        )

    @config_section("memory")
    class MemorySection(SectionBase):
        """记忆插件集成配置。

        默认启用。开启后，FoxZone 会把 ``booku_memory`` 插件已暴露的所有
        Tool 与 Agent 组件统一暴露给 LLM，由模型自主决定何时读写记忆。
        ``booku_memory`` 究竟以轻量模式（``memory_read`` / ``memory_write``）
        还是 Agent 模式工作，完全由其自身配置决定，FoxZone 不做约束。

        前置条件不满足时（``booku_memory`` 未安装/未加载、未注册任何 Tool/Agent）
        自动回退到普通纯文本调用，不影响主流程。
        """

        enable_memory_integration: bool = Field(
            default=True,
            description=(
                "是否将 booku_memory 暴露的所有 Tool/Agent 组件提供给 LLM。"
                "关闭后写说说与评论决策不会调用记忆工具。"
            ),
        )

    @config_section("monitor")
    class MonitorSection(SectionBase):
        """好友动态监控配置。"""

        enable_auto_monitor: bool = Field(default=True, description="是否启用自动监控好友动态")
        interval_minutes: int = Field(default=10, description="评论回复轮询间隔（分钟）")
        enable_auto_reply: bool = Field(default=True, description="是否自动回复自己说说下的评论")
        max_comment_age_hours: float = Field(
            default=72.0,
            description="忽略超过此时间（小时）的旧评论，0 表示不限制",
        )
        enable_friend_monitor: bool = Field(
            default=False,
            description="是否自动监控好友说说并由 QZoneChatter 决策互动（点赞/评论）",
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
            default=20,
            description=(
                "「外部空间评论回查」轮询间隔（分钟）。"
                "用于检查 bot 在他人空间里评论过的说说下是否有人回复 bot。"
            ),
        )
        external_followup_batch: int = Field(
            default=2,
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
        """Cookie 获取配置（Napcat 备用 HTTP 接口）。"""

        http_fallback_host: str = Field(default="127.0.0.1", description="Napcat HTTP 服务地址")
        http_fallback_port: int = Field(default=9999, description="Napcat HTTP 服务端口")
        napcat_token: str = Field(default="", description="Napcat 认证 Token（可选）")

    @config_section("ai_image")
    class AiImageSection(SectionBase):
        """AI 配图配置。"""

        enable_ai_image: bool = Field(default=False, description="是否启用 AI 生成配图")
        provider: str = Field(
            default="siliconflow",
            description="生图服务商，可选值：siliconflow / novelai / openai",
        )

    @config_section("siliconflow")
    class SiliconFlowSection(SectionBase):
        """硅基流动配图配置（provider = siliconflow 时生效）。"""

        api_key: str = Field(default="", description="硅基流动 API 密钥")
        model: str = Field(
            default="black-forest-labs/FLUX.1-schnell",
            description="绘图模型标识符",
        )
        image_number: int = Field(default=1, description="每次生成的图片数量（1-4 张）")

    @config_section("novelai")
    class NovelAISection(SectionBase):
        """NovelAI 配图配置（provider = novelai 时生效）。"""

        api_key: str = Field(default="", description="NovelAI 官方 API 密钥")
        model: str = Field(
            default="nai-diffusion-4-5-full",
            description="绘图模型（nai-diffusion-4-5-full / nai-diffusion-4 / nai-diffusion-3）",
        )
        character_prompt: str = Field(
            default="",
            description=(
                "画风锚点提示词（NovelAI tag 序列）。会在生图时自动注入到 prompt 头部以锁定整体画风，"
                "如 'masterpiece, best quality, anime style, soft lighting, slice of life'。"
                "角色外貌请由 LLM 根据人设文档自行推导，不要写在这里。"
            ),
        )
        base_negative_prompt: str = Field(
            default=(
                "nsfw, nude, explicit, sexual content, lowres, bad anatomy, "
                "bad hands, missing fingers, extra digit, fewer digits, cropped, "
                "worst quality, low quality"
            ),
            description="基础负面提示词",
        )
        proxy_host: str = Field(default="", description="代理服务器地址（如 127.0.0.1）")
        proxy_port: int = Field(default=0, description="代理服务器端口（如 7890）")

    @config_section("openai")
    class OpenAISection(SectionBase):
        """OpenAI/兼容 chat-completion 接口生图配置（provider = openai 时生效）。

        本插件复用项目统一的 ``config/model.toml``，不在此处保存 API Key/Base URL。
        启用步骤：

          1. 在 ``config/model.toml`` 中新建任务模型段，例如：

             [model_tasks.foxzone_image]
             model_list = ["gpt-image-2-2K"]
             max_tokens = 1024
             temperature = 0.7

          2. 将下方 ``model_set`` 字段填为该段名（例如 ``"foxzone_image"``）。
          3. 将 ``ai_image.provider`` 设为 ``"openai"``。
        """

        model_set: str = Field(
            default="",
            description=(
                "指向 config/model.toml 中 [model_tasks.<name>] 的任务名。"
                "默认空，需先在 model.toml 创建任务模型段（如 [model_tasks.foxzone_image]），"
                "然后将本字段填为该段名。空值时此 provider 不可用。"
            ),
        )
        reference_images: list[str] = Field(
            default_factory=list,
            description=(
                "OpenAI 生图参考图列表（可选）。每项填本地图片路径，"
                "如 'data/foxzone/reference/character.png'。"
                "运行时会以多模态 image_url 形式附加在 user 消息中，"
                "供模型参考画风/角色外貌。仅支持本地路径，HTTP URL 请先下载到本地。"
            ),
        )
        reference_images_guidance: str = Field(
            default="",
            description=(
                "参考图使用提示词（可选，默认空）。当 reference_images 至少有一张有效参考图时，"
                "本字段的内容会被追加到 OpenAI 图像指引段末尾，告诉 LLM 如何使用这些参考图。"
                "默认空（不附加任何提示）。"
                "推荐填写示例："
                "'附带的参考图用于锁定角色外貌（发色/瞳色/服饰等）与画风风格，"
                "请参考它生成新场景，避免在 prompt 中重复描述参考图已有的特征。'"
            ),
        )

    @config_section("prompts")
    class PromptsSection(SectionBase):
        """提示词模板配置（唯一真源）。

        所有提示词文本均存放于本节。框架在首次加载或字段缺失时会按
        ``Field(default=...)`` 自动写回 ``config.toml``，用户后续修改不会被覆盖。
        修改本节字段后重启插件即生效。

        模板内变量占位符（PromptTemplate.build 阶段由调用方传入）：
          ``{personality_desc}`` / ``{current_time}`` / ``{weekday}`` /
          ``{topic_desc}`` / ``{history}`` / ``{target_name}`` / ``{content}`` /
          ``{rt_con_block}`` / ``{image_block}`` / ``{story_content}`` /
          ``{story_time}`` / ``{comment_content}`` / ``{comment_time}`` /
          ``{commenter_name}`` / ``{comments_block}`` / ``{comment_items_block}`` /
          ``{feed_items_block}`` / ``{image_prompt_guide}`` / ``{output_format}``。

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
                "评论统一规范（被 4 个评论类模板共用）。"
                "在 comment_generate / comment_reply / comment_reply_batch / "
                "friend_feed_interact 模板内通过 __GUIDELINES__ 占位符替换。"
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
                "{image_prompt_guide}"
                "**输出格式（JSON）：**\n"
                "只输出一个合法 JSON，不含任何前缀、后缀或 Markdown 代码块。\n"
                "{output_format}\n\n"
                "---历史说说记录---\n"
                "{history}"
            ),
            description="模板 1：写说说正文（foxzone.story.generate）",
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
                "模板 4：批量决策回复自己说说下的新评论"
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
                "模板 5：好友说说接力评论决策（外部回查路径，"
                "foxzone.friend.feed.interact）"
            ),
        )

    # ---------- 字段声明（顺序与 Section 定义一致）----------
    general: GeneralSection = Field(default_factory=GeneralSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    memory: MemorySection = Field(default_factory=MemorySection)
    monitor: MonitorSection = Field(default_factory=MonitorSection)
    cookie: CookieSection = Field(default_factory=CookieSection)
    ai_image: AiImageSection = Field(default_factory=AiImageSection)
    siliconflow: SiliconFlowSection = Field(default_factory=SiliconFlowSection)
    novelai: NovelAISection = Field(default_factory=NovelAISection)
    openai: OpenAISection = Field(default_factory=OpenAISection)
    prompts: "PromptsSection" = Field(default_factory=lambda: FoxZoneConfig.PromptsSection())

