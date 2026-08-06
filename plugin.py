"""群感知插件 — MaiBot SDK v2

感知群人员变动事件（进群/退群/禁言/群名变动/管理变动），通过
``chat.receive.before_process`` 拦截 napcat 注入的 notice 事件，按事件类型
可选三种处理方式：

- template: 发送固定模板文案
- llm:      LLM 按全局人设生成自然回应并发送
- context:  仅注入聊天上下文（bot 感知但不主动发言）

同时注册五个工具化查询能力（LLM 在聊天中按需调用）：
群荣誉、群员称号、群头像、群公告、禁言列表。
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Optional

from maibot_sdk import HookHandler, MaiBotPlugin
from maibot_sdk.components import Tool, ToolParameterInfo
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParamType

from .config import GroupAwarenessConfig

# 事件类型常量（与 napcat notice_type/sub_type 对应）
EVT_INCREASE = "group_increase"
EVT_DECREASE = "group_decrease"
EVT_BAN = "group_ban"
EVT_ADMIN = "group_admin"
EVT_GROUP_NAME = "group_name"  # notify 子类型

# 成员名缓存 TTL（秒）
_NAME_CACHE_TTL = 600
_NAME_NEG_CACHE_TTL = 120
_STREAM_CACHE_TTL = 600

_DEFAULT_LLM_PERSONA = "你是一个群聊机器人，正和大家在同一个 QQ 群里。"

# 群头像 CDN URL 规则（无需 API）
_GROUP_AVATAR_URL = "https://p.qlogo.cn/gh/{group_id}/{group_id}/0"


class GroupAwarenessPlugin(MaiBotPlugin):
    """群感知插件主类。"""

    config_model = GroupAwarenessConfig

    def __init__(self) -> None:
        super().__init__()
        self._name_cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._stream_cache: dict[str, tuple[str, float]] = {}
        self._self_id = ""

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        self.ctx.logger.info("群感知插件已加载")

    async def on_unload(self) -> None:
        self.ctx.logger.info("群感知插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        self.ctx.logger.info("群感知插件配置已热更新: scope=%s", scope)

    # ===== 事件处理 Hook =====

    @HookHandler(
        "chat.receive.before_process",
        name="group_awareness_listener",
        description="感知群人员变动通知事件（进群/退群/禁言/群名/管理变动）",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_group_notice(self, message: dict | None = None, **kwargs):
        del kwargs
        if not self.config.plugin.enabled:
            return None

        ctx = self._extract_notice_context(message)
        if ctx is None:
            return None

        # 范围黑白名单
        if not self._in_scope(ctx["group_id"]):
            self.ctx.logger.debug(
                "[群感知] 群 %s 不在生效范围，跳过事件 %s", ctx["group_id"], ctx["event"],
            )
            return None

        # 解析成员/操作者名字（填充 member_name / operator_name 供模板与上下文使用）
        if ctx.get("user_id"):
            ctx["member_name"] = await self.resolve_member_name(ctx["group_id"], ctx["user_id"])
        if ctx.get("operator_id") and ctx["operator_id"] != ctx.get("user_id"):
            ctx["operator_name"] = await self.resolve_member_name(ctx["group_id"], ctx["operator_id"])

        # 自身禁言特殊处理
        if ctx["event"] == EVT_BAN and ctx.get("target_is_self"):
            await self._handle_self_ban(ctx)
            return {"action": "abort"}

        event_cfg = self._event_config(ctx["event"])
        if event_cfg is None or not event_cfg.enabled:
            return {"action": "abort"}

        self.ctx.logger.info(
            "[群感知] 事件=%s group=%s mode=%s payload=%s",
            ctx["event"], ctx["group_id"], event_cfg.mode, ctx["summary"],
        )

        if event_cfg.mode == "template":
            await self._handle_template(ctx, event_cfg)
        elif event_cfg.mode == "llm":
            await self._handle_llm(ctx, event_cfg)
        else:  # context
            await self._handle_context(ctx)

        # 通知事件不应进入主链路被当作聊天消息回复
        return {"action": "abort"}

    # ===== 事件解析 =====

    def _extract_notice_context(self, message: Any) -> Optional[dict[str, Any]]:
        """从消息 dict 中提取群成员变动通知；不是目标事件时返回 None。"""
        if not isinstance(message, dict):
            return None
        if not message.get("is_notify"):
            return None

        msg_info = message.get("message_info") or {}
        if not isinstance(msg_info, dict):
            return None
        additional = msg_info.get("additional_config") or {}
        if not isinstance(additional, dict):
            return None

        notice_type = str(additional.get("napcat_notice_type") or "").strip()
        sub_type = str(additional.get("napcat_notice_sub_type") or "").strip()
        payload = additional.get("napcat_notice_payload") or {}
        if not isinstance(payload, dict):
            return None

        self_id = str(payload.get("self_id") or "").strip()
        if self_id:
            self._self_id = self_id

        group_id = str(payload.get("group_id") or "").strip()
        if not group_id:
            return None

        if notice_type == "notify" and sub_type == EVT_GROUP_NAME:
            event = EVT_GROUP_NAME
        elif notice_type in (EVT_INCREASE, EVT_DECREASE, EVT_BAN, EVT_ADMIN):
            event = notice_type
        else:
            return None

        user_id = str(payload.get("user_id") or "").strip()
        operator_id = str(payload.get("operator_id") or "").strip()
        duration = payload.get("duration")
        new_group_name = str(payload.get("group_name") or "").strip()

        return {
            "event": event,
            "sub_type": sub_type,
            "group_id": group_id,
            "user_id": user_id,
            "operator_id": operator_id,
            "duration": duration,
            "new_group_name": new_group_name,
            "target_is_self": bool(user_id and self_id and user_id == self_id),
            "stream_id": str(message.get("session_id") or ""),
            "summary": f"{notice_type}.{sub_type}",
        }

    # ===== 处理方式实现 =====

    async def _handle_template(self, ctx: dict[str, Any], cfg: Any) -> None:
        """固定模板文案发送。"""
        template = str(getattr(cfg, "template", "") or "").strip()
        if not template:
            template = self._default_template_text(ctx)
        text = self._render_template(template, ctx)
        await self._send_to_group(ctx["group_id"], text)

    async def _handle_llm(self, ctx: dict[str, Any], cfg: Any) -> None:
        """LLM 按人设生成自然回应并发送；失败回退模板。"""
        event_text = self._default_template_text(ctx)
        text = await self._generate_llm_text(ctx, event_text)
        if not text:
            text = event_text
        await self._send_to_group(ctx["group_id"], text)

    async def _handle_context(self, ctx: dict[str, Any]) -> None:
        """仅将事件注入聊天上下文（bot 感知但不主动发言）。"""
        event_text = self._default_template_text(ctx)
        stream_id = await self.resolve_stream_id_for_group(ctx["group_id"])
        if not stream_id:
            self.ctx.logger.warning(
                "[群感知] 无法解析 stream_id，上下文注入被跳过 (group=%s, event=%s)",
                ctx["group_id"], ctx["event"],
            )
            return
        try:
            resp = await self.ctx.maisaka.context.append(
                stream_id=stream_id,
                segments=[{"type": "text", "content": event_text}],
                visible_text=event_text,
                source_kind=f"plugin:group_awareness:{ctx['event']}",
            )
            self.ctx.logger.info(
                "[群感知] 上下文注入完成: %r (group=%s)", event_text, ctx["group_id"],
            )
            if isinstance(resp, dict) and resp.get("success") is False:
                self.ctx.logger.warning("[群感知] 上下文注入业务失败: %s", resp.get("error"))
        except Exception:
            self.ctx.logger.warning("[群感知] 上下文注入调用异常", exc_info=True)

    async def _handle_self_ban(self, ctx: dict[str, Any]) -> None:
        """bot 自己被禁言时的行为（按配置）。"""
        mode = self.config.self_ban.mode.strip().lower()
        if mode == "notify_admin":
            text = self._default_template_text(ctx)
            for admin_qq in self.config.self_ban.admin_qqs:
                await self._send_private_text(admin_qq, text)
        elif mode == "context":
            await self._handle_context(ctx)
        else:
            self.ctx.logger.info("[群感知] bot 被禁言，mode=none 不处理")

    # ===== 模板与 LLM =====

    def _default_template_text(self, ctx: dict[str, Any]) -> str:
        """把事件转成自然的一句话（带昵称与 QQ 号，便于 bot 直接回答）。"""
        event = ctx["event"]
        sub = ctx.get("sub_type", "")
        group_name = ctx.get("new_group_name") or "这个群"
        member = ctx.get("member_name") or ctx.get("user_id") or "有人"
        member_qq = ctx.get("user_id") or ""
        operator = ctx.get("operator_name") or ctx.get("operator_id") or "有人"
        operator_qq = ctx.get("operator_id") or ""
        duration = ctx.get("duration")

        # 昵称与 QQ 号同时给出，避免 bot 需要额外查证“是谁”
        member_desc = f"{member}（QQ {member_qq}）" if member_qq and member != member_qq else member
        operator_desc = f"{operator}（QQ {operator_qq}）" if operator_qq and operator != operator_qq else operator

        if event == EVT_INCREASE:
            return f"{member_desc} 加入了群聊"
        if event == EVT_DECREASE:
            if sub == "kick":
                return f"{member_desc} 被 {operator_desc} 移出了群聊"
            return f"{member_desc} 离开了群聊"
        if event == EVT_BAN:
            if sub == "whole_ban":
                return f"{operator_desc} 开启了全体禁言"
            if sub == "whole_lift_ban":
                return "群全体禁言已解除"
            if sub == "lift_ban":
                return f"{member_desc} 被解除禁言"
            if duration:
                return f"{member_desc} 被禁言了 {duration} 秒"
            return f"{member_desc} 被禁言了"
        if event == EVT_GROUP_NAME:
            return f"{operator_desc} 修改了群名称"
        if event == EVT_ADMIN:
            if sub == "unset":
                return f"{member_desc} 被取消管理员"
            return f"{member_desc} 被设为管理员"
        return f"[群事件] {event}.{sub}"

    def _render_template(self, template: str, ctx: dict[str, Any]) -> str:
        result = template
        result = result.replace("{group_name}", ctx.get("new_group_name") or "这个群")
        result = result.replace("{member_name}", ctx.get("member_name") or ctx.get("user_id") or "")
        result = result.replace("{operator_name}", ctx.get("operator_name") or ctx.get("operator_id") or "")
        result = result.replace("{target_name}", ctx.get("member_name") or ctx.get("user_id") or "")
        result = result.replace("{duration}", str(ctx.get("duration") or ""))
        return result

    async def _generate_llm_text(self, ctx: dict[str, Any], event_text: str) -> str:
        """用 LLM 生成对群事件的自然回应。"""
        cfg = self.config.llm
        persona = await self._resolve_persona()
        scene = f"QQ 群里刚发生了一件事件：{event_text}"
        system = (
            f"{persona}\n\n"
            f"{scene}\n"
            "请就这件事自然地回应一句，像平时和群友聊天一样；"
            "简短、口语化，贴合你的性格。只输出要说的话本身，不要解释。"
        )
        prompt = [
            {"role": "system", "content": system},
            {"role": "user", "content": "回应这件事。"},
        ]
        try:
            kwargs: dict[str, Any] = {"prompt": prompt, "temperature": cfg.temperature}
            model = cfg.model.strip()
            if model:
                kwargs["model"] = model
            if cfg.max_tokens > 0:
                kwargs["max_tokens"] = cfg.max_tokens
            result = await self.ctx.llm.generate(**kwargs)
        except Exception:
            self.ctx.logger.debug("[群感知] LLM 生成失败，回退事件文本", exc_info=True)
            return ""
        return self._extract_llm_text(result)

    async def _resolve_persona(self) -> str:
        try:
            persona = await self.ctx.config.get("personality.personality", "")
        except Exception:
            self.ctx.logger.debug("[群感知] 读取全局人设失败", exc_info=True)
            return _DEFAULT_LLM_PERSONA
        if isinstance(persona, str) and persona.strip():
            return persona.strip()
        return _DEFAULT_LLM_PERSONA

    @staticmethod
    def _extract_llm_text(result: Any) -> str:
        if not isinstance(result, dict):
            return ""
        if result.get("success") is False:
            return ""
        text = str(result.get("response") or "").strip()
        if not text:
            return ""
        if len(text) >= 2 and text[0] in "\"'“「『" and text[-1] in "\"'”」』":
            text = text[1:-1].strip()
        return text

    # ===== 范围与事件配置 =====

    def _in_scope(self, group_id: str) -> bool:
        blacklist = {str(x).strip() for x in self.config.scope.blacklist if str(x).strip()}
        if group_id in blacklist:
            return False
        whitelist = {str(x).strip() for x in self.config.scope.whitelist if str(x).strip()}
        if whitelist and group_id not in whitelist:
            return False
        return True

    def _event_config(self, event: str) -> Any:
        cfg = getattr(self.config.events, event, None)
        return cfg

    # ===== 名称 / stream 解析（带 TTL 缓存）=====

    async def resolve_member_name(self, group_id: str, user_id: str) -> str:
        """解析群成员昵称：群名片优先于昵称；带 TTL 缓存与负缓存。"""
        if not user_id:
            return ""
        cached = self._name_cache.get((group_id, user_id))
        if cached and time.monotonic() - cached[1] < _NAME_CACHE_TTL:
            return cached[0]
        if cached and time.monotonic() - cached[1] < _NAME_CACHE_TTL + _NAME_NEG_CACHE_TTL:
            return ""  # 负缓存

        name = ""
        try:
            result = await self.ctx.api.call(
                "adapter.napcat.group.get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
            )
            self.ctx.logger.info(
                "[群感知] 名字解析-群成员: user=%s 返回=%r", user_id, result,
            )
            if isinstance(result, dict):
                name = str(result.get("card") or result.get("nickname") or "").strip()
        except Exception as exc:
            self.ctx.logger.info(
                "[群感知] 名字解析-群成员异常: user=%s err=%s", user_id, exc,
            )

        if not name:
            try:
                result = await self.ctx.api.call(
                    "adapter.napcat.account.get_stranger_info",
                    user_id=int(user_id),
                    no_cache=True,
                )
                self.ctx.logger.info(
                    "[群感知] 名字解析-陌生人: user=%s 返回=%r", user_id, result,
                )
                if isinstance(result, dict):
                    # 注意：get_stranger_info 返回的昵称字段是 nick（非 nickname）
                    name = str(result.get("nick") or result.get("nickname") or "").strip()
            except Exception as exc:
                self.ctx.logger.info(
                    "[群感知] 名字解析-陌生人异常: user=%s err=%s", user_id, exc,
                )

        self.ctx.logger.info(
            "[群感知] 名字解析结果: group=%s user=%s name=%r", group_id, user_id, name,
        )

        self._name_cache[(group_id, user_id)] = (name, time.monotonic())
        return name

    async def resolve_stream_id_for_group(self, group_id: str, *, allow_open: bool = True) -> str:
        """根据群号查 stream_id（带 TTL 缓存）；落空时允许 open_session 创建会话。"""
        cached = self._stream_cache.get(group_id)
        if cached and time.monotonic() - cached[1] < _STREAM_CACHE_TTL:
            return cached[0]

        stream_id = ""
        try:
            stream = await self.ctx.chat.get_stream_by_group_id(group_id, platform="qq")
            if isinstance(stream, dict):
                # 兼容 Host 返回的多种流字段名（session_id / stream_id / id）
                stream_id = str(
                    stream.get("session_id") or stream.get("stream_id") or stream.get("id") or ""
                )
        except Exception:
            self.ctx.logger.debug("[群感知] get_stream_by_group_id 失败 (group=%s)", group_id, exc_info=True)

        if not stream_id and allow_open:
            try:
                result = await self.ctx.chat.open_session(
                    platform="qq", chat_type="group", group_id=group_id,
                )
                if isinstance(result, dict) and result.get("success") is not False:
                    stream_id = str(
                        result.get("session_id") or result.get("stream_id") or result.get("id") or ""
                    )
            except Exception:
                self.ctx.logger.debug("[群感知] open_session 失败 (group=%s)", group_id, exc_info=True)

        if stream_id:
            self._stream_cache[group_id] = (stream_id, time.monotonic())
        return stream_id

    async def _send_to_group(self, group_id: str, text: str) -> bool:
        stream_id = await self.resolve_stream_id_for_group(group_id)
        if not stream_id:
            self.ctx.logger.warning("[群感知] 无法解析 stream_id，发送失败 (group=%s)", group_id)
            return False
        try:
            return bool(await self.ctx.send.text(text, stream_id))
        except Exception:
            self.ctx.logger.exception("[群感知] 发送消息失败 (group=%s)", group_id)
            return False

    async def _send_private_text(self, user_id: str, text: str) -> bool:
        try:
            stream = await self.ctx.chat.get_stream_by_user_id(user_id, platform="qq")
            stream_id = ""
            if isinstance(stream, dict):
                stream_id = str(stream.get("session_id") or stream.get("stream_id") or "")
            if not stream_id:
                result = await self.ctx.chat.open_session(
                    platform="qq", chat_type="private", user_id=user_id,
                )
                if isinstance(result, dict):
                    stream_id = str(result.get("session_id") or result.get("stream_id") or "")
            if not stream_id:
                return False
            return bool(await self.ctx.send.text(text, stream_id))
        except Exception:
            self.ctx.logger.exception("[群感知] 私聊发送失败 (user=%s)", user_id)
            return False

    # ===== 工具化查询 =====

    @Tool(
        "get_group_honor",
        description="获取群荣誉信息（龙王、群聊之火、传说、炽星、冒尖小萌新）",
        brief_description="获取群荣誉",
        detailed_description=(
            "当用户询问群里的荣誉/称号/龙王/群聊之火等时调用，返回指定群的荣誉榜单文本。"
        ),
        parameters=[
            ToolParameterInfo(
                name="group_id", param_type=ToolParamType.STRING,
                description="目标群号", required=True,
            ),
        ],
    )
    async def get_group_honor(self, group_id: str, **kwargs) -> dict[str, Any]:
        try:
            result = await self.ctx.api.call(
                "adapter.napcat.group.get_group_honor_info",
                group_id=int(group_id),
                type="all",
            )
        except Exception as e:
            return {"success": False, "reason": str(e)}
        if not isinstance(result, dict):
            return {"success": False, "reason": "返回结构异常"}
        lines = [f"群 {group_id} 荣誉信息："]
        current = result.get("current_talkative") or {}
        if isinstance(current, dict) and current.get("user_id"):
            lines.append(f"- 龙王：{current.get('nickname') or current.get('user_id')}")
        for key, label in (
            ("talkative_list", "群聊之火"),
            ("legend_list", "传说"),
            ("performer_list", "群聊炽星"),
            ("strong_newbie_list", "冒尖小萌新"),
        ):
            items = result.get(key) or []
            names = []
            for item in items:
                if isinstance(item, dict):
                    names.append(str(item.get("nickname") or item.get("user_id") or ""))
            if names:
                lines.append(f"- {label}：{'、'.join(names)}")
        return {"success": True, "result": "\n".join(lines)}

    @Tool(
        "get_member_title",
        description="获取群成员称号（群头衔）",
        brief_description="获取群员称号",
        detailed_description=(
            "当用户询问某个群成员的称号/头衔时调用，返回该成员的群头衔（title）。"
        ),
        parameters=[
            ToolParameterInfo(
                name="group_id", param_type=ToolParamType.STRING,
                description="目标群号", required=True,
            ),
            ToolParameterInfo(
                name="user_id", param_type=ToolParamType.STRING,
                description="成员 QQ 号", required=True,
            ),
        ],
    )
    async def get_member_title(self, group_id: str, user_id: str, **kwargs) -> dict[str, Any]:
        try:
            result = await self.ctx.api.call(
                "adapter.napcat.group.get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
            )
        except Exception as e:
            return {"success": False, "reason": str(e)}
        if not isinstance(result, dict):
            return {"success": False, "reason": "返回结构异常"}
        title = str(result.get("title") or "").strip()
        nickname = str(result.get("card") or result.get("nickname") or user_id)
        return {
            "success": True,
            "result": f"{nickname}（{user_id}）的群头衔：{title or '无'}",
        }

    @Tool(
        "get_group_avatar",
        description="获取群头像",
        brief_description="获取群头像",
        detailed_description=(
            "当用户询问群头像/群头像图片时调用，返回群头像图片 URL。"
        ),
        parameters=[
            ToolParameterInfo(
                name="group_id", param_type=ToolParamType.STRING,
                description="目标群号", required=True,
            ),
        ],
    )
    async def get_group_avatar(self, group_id: str, **kwargs) -> dict[str, Any]:
        url = _GROUP_AVATAR_URL.format(group_id=group_id)
        return {"success": True, "result": f"群 {group_id} 头像 URL：{url}"}

    @Tool(
        "get_group_notice",
        description="获取群公告",
        brief_description="获取群公告",
        detailed_description=(
            "当用户询问群公告/群通知时调用，返回指定群的最新公告内容。"
        ),
        parameters=[
            ToolParameterInfo(
                name="group_id", param_type=ToolParamType.STRING,
                description="目标群号", required=True,
            ),
        ],
    )
    async def get_group_notice(self, group_id: str, **kwargs) -> dict[str, Any]:
        try:
            result = await self.ctx.api.call(
                "adapter.napcat.group.get_group_notice",
                group_id=int(group_id),
            )
        except Exception as e:
            return {"success": False, "reason": str(e)}
        notices = []
        if isinstance(result, dict):
            data = result.get("data") or result.get("result") or result
            for item in data if isinstance(data, list) else []:
                if not isinstance(item, dict):
                    continue
                content = ""
                for seg in item.get("content") if isinstance(item.get("content"), list) else []:
                    if isinstance(seg, dict) and str(seg.get("type")) == "text":
                        content += str(seg.get("data", {}).get("text") or "")
                if content:
                    notices.append(f"- {content[:100]}")
        if not notices:
            return {"success": True, "result": f"群 {group_id} 暂无公告"}
        return {"success": True, "result": f"群 {group_id} 公告：\n" + "\n".join(notices[:5])}

    @Tool(
        "get_group_shut_list",
        description="获取群禁言列表",
        brief_description="获取群禁言列表",
        detailed_description=(
            "当用户询问群里谁被禁言/禁言状态时调用，返回当前被禁言的成员列表。"
        ),
        parameters=[
            ToolParameterInfo(
                name="group_id", param_type=ToolParamType.STRING,
                description="目标群号", required=True,
            ),
        ],
    )
    async def get_group_shut_list(self, group_id: str, **kwargs) -> dict[str, Any]:
        try:
            result = await self.ctx.api.call(
                "adapter.napcat.group.get_group_shut_list",
                group_id=int(group_id),
            )
        except Exception as e:
            return {"success": False, "reason": str(e)}
        members = []
        if isinstance(result, dict):
            data = result.get("data") or result.get("result") or result
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, list):
                        for item in v:
                            if isinstance(item, dict) and item.get("user_id"):
                                members.append(
                                    f"- {item.get('nickname') or item.get('user_id')}"
                                    f"（至 {item.get('shut_up_timestamp') or '未知时间'}）"
                                )
        if not members:
            return {"success": True, "result": f"群 {group_id} 当前无被禁言成员"}
        return {"success": True, "result": f"群 {group_id} 被禁言成员：\n" + "\n".join(members[:20])}


def create_plugin() -> GroupAwarenessPlugin:
    """MaiBot 插件工厂函数。"""
    return GroupAwarenessPlugin()
