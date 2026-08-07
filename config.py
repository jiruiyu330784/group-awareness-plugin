"""麦麦看到你了！(群感知) 插件配置模型。

事件处理模式（mode，Literal 枚举）：
- template: 发送固定模板文案（可含占位符）
- llm:      LLM 按人设生成自然回应并发送
- context:  仅将事件注入聊天上下文（bot 感知但不主动发言）

工具化查询（LLM 按需调用）：群荣誉、群员称号、群头像、群公告、禁言列表。
"""

from typing import Any, Literal

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator

_MODE_VALUES = ("template", "llm", "context")


def _normalize_mode(value: Any) -> Literal["template", "llm", "context"]:
    """mode 归一化：小写 + 非法值回落默认 context（避免配置加载崩溃）。"""
    normalized = "" if value is None else str(value).strip().lower()
    if normalized in _MODE_VALUES:
        return normalized  # type: ignore[return-value]
    return "context"


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件"},
    )
    record_changes: bool = Field(
        default=True,
        description="记录群成员变动日志（进群/退群），供 get_group_member_changes 工具查询；关闭后工具返回空",
        json_schema_extra={"label": "记录成员变动", "hint": "关闭后 get_group_member_changes 工具无数据"},
    )
    config_version: str = Field(
        default="1.0.0",
        description="配置版本",
        json_schema_extra={"label": "配置版本", "disabled": True},
    )


class ScopeConfig(PluginConfigBase):
    """群范围黑白名单。"""

    __ui_label__ = "生效范围"
    __ui_icon__ = "filter"
    __ui_order__ = 1

    whitelist: list[str] = Field(
        default_factory=list,
        description="群白名单（群号），留空表示全部群生效",
        json_schema_extra={"label": "群白名单", "hint": "只对这些群生效；留空不限制"},
    )
    blacklist: list[str] = Field(
        default_factory=list,
        description="群黑名单（群号），优先于白名单",
        json_schema_extra={"label": "群黑名单", "hint": "这些群不生效；黑名单优先"},
    )


class EventModeConfig(PluginConfigBase):
    """单类事件的处理方式。"""

    __ui_label__ = "事件处理"
    __ui_icon__ = "event"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否处理此类事件",
        json_schema_extra={"label": "启用"},
    )
    mode: Literal["template", "llm", "context"] = Field(
        default="context",
        description=(
            "处理方式：template=固定模板；llm=人格化回复（按人设生成并发送，有 API 费用）；"
            "context=注入上下文（bot 感知但不主动发言，零费用）"
        ),
        json_schema_extra={"label": "处理方式", "hint": "template / llm / context"},
    )
    template: str = Field(
        default="",
        description="mode=template 时的固定文案。占位符：{group_name} {member_name} {operator_name} {target_name} {duration}",
        json_schema_extra={"label": "固定模板", "placeholder": "欢迎 {member_name} 加入 {group_name}！"},
    )

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: Any) -> Literal["template", "llm", "context"]:
        return _normalize_mode(value)


class EventsConfig(PluginConfigBase):
    """五类事件各自独立配置。"""

    __ui_label__ = "事件分类配置"
    __ui_icon__ = "list"
    __ui_order__ = 2

    group_increase: EventModeConfig = Field(
        default_factory=lambda: EventModeConfig(mode="context"),
        description="新人进群事件",
        json_schema_extra={"label": "进群"},
    )
    group_decrease: EventModeConfig = Field(
        default_factory=lambda: EventModeConfig(mode="context"),
        description="成员退群/被移出事件",
        json_schema_extra={"label": "退群"},
    )
    group_ban: EventModeConfig = Field(
        default_factory=lambda: EventModeConfig(mode="context"),
        description="成员被禁言/解除禁言事件",
        json_schema_extra={"label": "禁言/解禁"},
    )
    group_name: EventModeConfig = Field(
        default_factory=lambda: EventModeConfig(mode="context"),
        description="群名称被修改事件",
        json_schema_extra={"label": "群名变动"},
    )
    group_admin: EventModeConfig = Field(
        default_factory=lambda: EventModeConfig(mode="context"),
        description="群管理员设置/取消事件",
        json_schema_extra={"label": "管理变动"},
    )


class SelfBanConfig(PluginConfigBase):
    """bot 自己被禁言时的行为。"""

    __ui_label__ = "自身禁言"
    __ui_icon__ = "shield"
    __ui_order__ = 3

    mode: Literal["notify_admin", "context", "none"] = Field(
        default="none",
        description=(
            "bot 自己被禁言时的行为：notify_admin=私聊通知管理员（需配置下方管理员 QQ）；"
            "context=注入上下文；none=不处理"
        ),
        json_schema_extra={"label": "行为", "hint": "notify_admin / context / none"},
    )
    admin_qqs: list[str] = Field(
        default_factory=list,
        description="管理员 QQ 列表（notify_admin 模式使用）",
        json_schema_extra={"label": "管理员 QQ"},
    )

    @field_validator("mode")
    @classmethod
    def _validate_self_ban_mode(cls, value: Any) -> Literal["notify_admin", "context", "none"]:
        normalized = "" if value is None else str(value).strip().lower()
        if normalized in ("notify_admin", "context", "none"):
            return normalized  # type: ignore[return-value]
        return "none"


class LLMConfig(PluginConfigBase):
    """人格化回复的 LLM 参数。"""

    __ui_label__ = "LLM"
    __ui_icon__ = "sparkles"
    __ui_order__ = 4

    model: Literal["utils", "replyer", "planner"] = Field(
        default="planner",
        description=(
            "LLM 任务槽位（对应 Host model_task_config 下的任务名）："
            "utils=通用快模型；replyer=主回复模型（最贴人设但可能较慢）；planner=规划快模型"
        ),
        json_schema_extra={"label": "模型槽位", "hint": "utils / replyer / planner"},
    )
    temperature: float = Field(
        default=1.0,
        description="生成温度",
        json_schema_extra={"label": "温度", "min": 0, "max": 2, "step": 0.1},
    )
    max_tokens: int = Field(
        default=0,
        description="最大生成 token，0 表示不覆盖用 Host 配置",
        json_schema_extra={"label": "最大 token", "min": 0},
    )

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: Any) -> Literal["utils", "replyer", "planner"]:
        normalized = "" if value is None else str(value).strip().lower()
        if normalized in ("utils", "replyer", "planner"):
            return normalized  # type: ignore[return-value]
        return "planner"


class GreetConfig(PluginConfigBase):
    """新人入群欢迎消息（考察期的一部分）。"""

    __ui_label__ = "入群欢迎"
    __ui_icon__ = "wave"

    mode: Literal["template", "llm"] = Field(
        default="template",
        description="欢迎方式：template=固定模板；llm=按人设生成（有 API 费用）",
        json_schema_extra={"label": "欢迎方式", "hint": "template / llm"},
    )
    template: str = Field(
        default="欢迎新朋友加入～出来冒个泡认识一下？",
        description="mode=template 时的欢迎文案（自动 @ 新人）",
        json_schema_extra={"label": "固定文案", "placeholder": "欢迎新朋友加入～"},
    )

    @field_validator("mode")
    @classmethod
    def _validate_greet_mode(cls, value: Any) -> Literal["template", "llm"]:
        normalized = "" if value is None else str(value).strip().lower()
        if normalized in ("template", "llm"):
            return normalized  # type: ignore[return-value]
        return "template"


class KickMessageConfig(PluginConfigBase):
    """移出新成员的说明消息。"""

    __ui_label__ = "移出说明"
    __ui_icon__ = "log-out"

    mode: Literal["template", "llm"] = Field(
        default="template",
        description="移出说明方式：template=固定模板；llm=按人设生成（有 API 费用）",
        json_schema_extra={"label": "说明方式", "hint": "template / llm"},
    )
    template: str = Field(
        default="成员 {member_name}（QQ {user_id}）加入超过 {probation_hours} 小时未发言，已移出群聊。",
        description=(
            "mode=template 时的说明文案。占位符：{member_name} 昵称；{user_id} 被移出者 QQ；"
            "{probation_hours} 考察时长（小时）"
        ),
        json_schema_extra={"label": "固定文案"},
    )

    @field_validator("mode")
    @classmethod
    def _validate_kick_mode(cls, value: Any) -> Literal["template", "llm"]:
        normalized = "" if value is None else str(value).strip().lower()
        if normalized in ("template", "llm"):
            return normalized  # type: ignore[return-value]
        return "template"


class ProbationConfig(PluginConfigBase):
    """加群考察期：新成员超时未发言自动移出。

    移出是确定性代码行为（定时检查 + 条件判断），不注册任何 LLM 工具，
    机器人不会自主决定移出谁。需要 bot 拥有群管理员权限才能执行移出。
    """

    __ui_label__ = "考察期"
    __ui_icon__ = "timer"
    __ui_order__ = 5

    enabled: bool = Field(
        default=False,
        description=(
            "启用加群考察期（需要 bot 有群管理员权限，仅用于自动移出超时未发言的新成员，"
            "LLM 不参与决策）"
        ),
        json_schema_extra={"label": "启用考察期", "hint": "需 bot 有群管理员权限"},
    )
    probation_hours: float = Field(
        default=48.0,
        description="考察时长（小时），超时未发言的新成员将被移出",
        json_schema_extra={"label": "考察时长（小时）", "min": 0},
    )
    check_interval_minutes: int = Field(
        default=30,
        description="检查间隔（分钟）",
        json_schema_extra={"label": "检查间隔（分钟）", "min": 1},
    )
    reject_add_request: bool = Field(
        default=False,
        description="移出时拒绝该成员再次申请加群",
        json_schema_extra={"label": "拒绝再次加群"},
    )
    whitelist: list[str] = Field(
        default_factory=list,
        description="白名单 QQ（永不考察、永不移出）",
        json_schema_extra={"label": "白名单 QQ"},
    )
    greet: GreetConfig = Field(
        default_factory=GreetConfig,
        description="新人入群欢迎（自动 @ 新人）",
        json_schema_extra={"label": "入群欢迎"},
    )
    kick_message: KickMessageConfig = Field(
        default_factory=KickMessageConfig,
        description="移出新成员的说明消息",
        json_schema_extra={"label": "移出说明"},
    )
    llm: LLMConfig = Field(
        default_factory=lambda: LLMConfig(max_tokens=128, temperature=0.8),
        description="欢迎/移出说明的 LLM 生成参数（greet/kick_message 的 llm 模式使用）",
        json_schema_extra={"label": "LLM 参数"},
    )


class GroupAwarenessConfig(PluginConfigBase):
    """麦麦看到你了！插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    scope: ScopeConfig = Field(default_factory=ScopeConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    self_ban: SelfBanConfig = Field(default_factory=SelfBanConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    probation: ProbationConfig = Field(default_factory=ProbationConfig)


def create_config():
    """创建默认配置实例。"""
    return GroupAwarenessConfig()
