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
import json
import random
import time
import tomllib
from pathlib import Path
from typing import Any, Optional

from maibot_sdk import HookHandler, MaiBotPlugin
from maibot_sdk.components import Tool, ToolParameterInfo
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParamType

from .config import GroupAwarenessConfig, ProbationConfig

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
        # 群成员变动日志：{group_id: [{type, user_id, nickname, ts}]}，供查询工具读取后清空
        self._pending_changes: dict[str, list[dict[str, Any]]] = {}
        # 考察期：{group_id: {user_id: {join_ts, member_name}}}，持久化到 data 目录
        self._probation: dict[str, dict[str, dict[str, Any]]] = {}
        self._probation_file: Path | None = None
        self._probation_task: asyncio.Task | None = None
        # 外部考察期配置（来自「麦麦喊新人说话！」插件的 config.toml），存在时优先使用
        self._external_probation_cfg: Any = None

    @property
    def _probation_cfg(self) -> Any:
        """考察期配置：外部（麦麦喊新人说话！）优先，否则用自身配置。"""
        return self._external_probation_cfg or self.config.probation

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        self._probation_file = Path(self.ctx.paths.data_dir) / "probation.json"
        self._probation_load()
        # 联动：若「麦麦喊新人说话！」插件存在，考察期配置以它为准（只维护一份配置）
        sibling_cfg = self._read_sibling_probation_config()
        if sibling_cfg is not None:
            self._external_probation_cfg = sibling_cfg
            self.ctx.logger.info("[考察期] 检测到「麦麦喊新人说话！」，使用其考察期配置")
        if self._probation_cfg.enabled:
            self._probation_task = asyncio.create_task(
                self._probation_loop(), name="group-awareness.probation",
            )
        self.ctx.logger.info(
            "群感知插件已加载（考察期=%s）", "开" if self._probation_cfg.enabled else "关",
        )

    async def on_unload(self) -> None:
        if self._probation_task:
            self._probation_task.cancel()
            self._probation_task = None
        self._probation_persist()
        self.ctx.logger.info("群感知插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        # 联动：配置热更新时重新探测外部配置
        sibling_cfg = self._read_sibling_probation_config()
        if sibling_cfg is not None:
            self._external_probation_cfg = sibling_cfg
        # 考察期开关变化时同步启停周期任务
        if self._probation_task:
            self._probation_task.cancel()
            self._probation_task = None
        if self._probation_cfg.enabled:
            self._probation_task = asyncio.create_task(
                self._probation_loop(), name="group-awareness.probation",
            )
        self.ctx.logger.info(
            "群感知插件配置已热更新: scope=%s（考察期=%s）",
            scope, "开" if self._probation_cfg.enabled else "关",
        )

    def _read_sibling_probation_config(self) -> Any:
        """读取「麦麦喊新人说话！」(group-probation-plugin) 的考察期配置。

        存在且解析成功时返回 ProbationConfig 实例（联动时配置只维护一份）；
        不存在或解析失败返回 None（回退用自身配置）。
        """
        try:
            sibling_cfg = (
                Path(__file__).resolve().parents[1]
                / "group-probation-plugin"
                / "config.toml"
            )
            if not sibling_cfg.exists():
                return None
            with open(sibling_cfg, "rb") as f:
                data = tomllib.load(f)
            section = data.get("probation")
            if not isinstance(section, dict):
                return None
            return ProbationConfig(**section)
        except Exception as exc:
            self.ctx.logger.debug("[考察期] 读取兄弟配置失败: %s", exc)
            return None

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
            # 退群事件成员已不在群，get_group_member_info 必然失败（会打错误日志），直接走陌生人接口
            if ctx["event"] == EVT_DECREASE:
                ctx["member_name"] = await self.resolve_member_name_stranger(ctx["user_id"])
            else:
                ctx["member_name"] = await self.resolve_member_name(ctx["group_id"], ctx["user_id"])
        if ctx.get("operator_id") and ctx["operator_id"] != ctx.get("user_id"):
            ctx["operator_name"] = await self.resolve_member_name(ctx["group_id"], ctx["operator_id"])

        # 记录成员变动日志（供 get_group_member_changes 工具查询）
        if self.config.plugin.record_changes and ctx["event"] in (EVT_INCREASE, EVT_DECREASE):
            self._record_change(ctx)

        # 考察期：新人进群 → 加入考察名单 + @ 欢迎（身份感知不影响）
        if self._probation_cfg.enabled and ctx["event"] == EVT_INCREASE:
            await self._handle_probation_join(ctx)

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
        """仅将事件注入聊天上下文（匿名，bot 感知但不接触具体身份）。"""
        event_text = self._anonymous_event_text(ctx)
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

    def _anonymous_event_text(self, ctx: dict[str, Any]) -> str:
        """把事件转成不包含具体身份的轻量文本（供 context 模式注入）。

        身份识别交给 get_group_member_changes 工具，避免 bot 从注入文本里
        读到名字后答错人。
        """
        event = ctx["event"]
        sub = ctx.get("sub_type", "")
        duration = ctx.get("duration")

        if event == EVT_INCREASE:
            return "有人加入了群聊"
        if event == EVT_DECREASE:
            if sub == "kick":
                return "有人被移出了群聊"
            return "有人离开了群聊"
        if event == EVT_BAN:
            if sub == "whole_ban":
                return "群开启了全体禁言"
            if sub == "whole_lift_ban":
                return "群全体禁言已解除"
            if sub == "lift_ban":
                return "有人被解除禁言"
            if duration:
                return f"有人被禁言了 {duration} 秒"
            return "有人被禁言了"
        if event == EVT_GROUP_NAME:
            return "群名称被修改了"
        if event == EVT_ADMIN:
            if sub == "unset":
                return "有人被取消管理员"
            return "有人被设为管理员"
        return f"[群事件] {event}.{sub}"

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

        # 昵称与 QQ 号同时给出，且带明确标记，避免 bot 把奇怪昵称（如日志样式）当噪音忽略
        member_desc = f"昵称「{member}」（QQ {member_qq}）" if member_qq and member != member_qq else member
        operator_desc = f"昵称「{operator}」（QQ {operator_qq}）" if operator_qq and operator != operator_qq else operator

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

    def _record_change(self, ctx: dict[str, Any]) -> None:
        """把一次进群/退群事件记入该群的变动日志。"""
        group_id = ctx["group_id"]
        entry = {
            "type": "join" if ctx["event"] == EVT_INCREASE else "leave",
            "user_id": ctx.get("user_id") or "",
            "nickname": ctx.get("member_name") or "",
            "ts": int(time.time()),
        }
        self._pending_changes.setdefault(group_id, []).append(entry)
        self.ctx.logger.info(
            "[群感知] 变动已记录: %s %s(%s) in %s",
            entry["type"], entry["nickname"] or entry["user_id"], entry["user_id"], group_id,
        )

    def _event_config(self, event: str) -> Any:
        cfg = getattr(self.config.events, event, None)
        return cfg

    # ===== 名称 / stream 解析（带 TTL 缓存）=====

    async def resolve_member_name_stranger(self, user_id: str) -> str:
        """只用陌生人接口解析昵称（退群事件专用：成员已不在群，群成员接口必失败）。"""
        name = ""
        try:
            result = await self.ctx.api.call(
                "adapter.napcat.account.get_stranger_info",
                user_id=int(user_id),
                no_cache=True,
            )
            self.ctx.logger.info(
                "[群感知] 名字解析-陌生人(退群): user=%s 返回=%r", user_id, result,
            )
            if isinstance(result, dict):
                name = str(result.get("nick") or result.get("nickname") or "").strip()
        except Exception as exc:
            self.ctx.logger.info(
                "[群感知] 名字解析-陌生人异常: user=%s err=%s", user_id, exc,
            )
        return name

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

    # ===== 考察期（probation）=====

    async def _handle_probation_join(self, ctx: dict[str, Any]) -> None:
        """新人进群：加入考察名单并发送 @ 欢迎。"""
        user_id = ctx.get("user_id") or ""
        if not user_id:
            return
        cfg = self._probation_cfg
        member_name = ctx.get("member_name") or user_id

        self._probation.setdefault(ctx["group_id"], {})[user_id] = {
            "join_ts": time.time(),
            "member_name": member_name,
        }
        self._probation_persist()

        text = ""
        if cfg.greet.mode == "llm":
            text = await self._generate_text(
                f"QQ 群里刚有新成员加入：{member_name}（QQ {user_id}）。"
                "请以你的性格自然地欢迎 TA，并邀请 TA 出来说句话冒个泡。"
                "简短、口语化，只输出要说的话。"
            )
        if not text and cfg.greet.fallback_to_template:
            text = cfg.greet.template or "欢迎新朋友加入～"
        if text:
            await self._send_at_message(ctx["group_id"], user_id, text)
        self.ctx.logger.info(
            "[考察期] 新人 %s(%s) 加入考察名单，欢迎已发送", member_name, user_id,
        )

    async def _probation_loop(self) -> None:
        """周期检查考察名单（超时未发言者移出）。"""
        interval = max(1, self._probation_cfg.check_interval_minutes) * 60
        while True:
            await asyncio.sleep(interval)
            try:
                await self._check_probation()
            except Exception as exc:
                self.ctx.logger.info("[考察期] 检查异常: %s", exc, exc_info=True)

    async def _check_probation(self) -> None:
        """遍历考察名单：转正（已发言）、排除（管理员/白名单/已退群）、移出（超时未发言）。"""
        cfg = self._probation_cfg
        if not cfg.enabled:
            return
        whitelist = set(cfg.whitelist)
        now = time.time()
        probation_seconds = float(cfg.probation_hours) * 3600

        for group_id, members in list(self._probation.items()):
            for user_id, entry in list(members.items()):
                # 白名单跳过
                if user_id in whitelist:
                    self._probation_remove(group_id, user_id)
                    continue
                # 查询成员状态
                try:
                    result = await self.ctx.api.call(
                        "adapter.napcat.group.get_group_member_info",
                        group_id=int(group_id),
                        user_id=int(user_id),
                    )
                except Exception as exc:
                    # 查询失败（已不在群）→ 移出考察
                    self.ctx.logger.info(
                        "[考察期] 查询成员 %s 失败（可能已退群），移出考察: %s", user_id, exc,
                    )
                    self._probation_remove(group_id, user_id)
                    continue
                data = result.get("data") if isinstance(result, dict) else None
                if not isinstance(data, dict):
                    self._probation_remove(group_id, user_id)
                    continue
                role = str(data.get("role") or "")
                if role in ("owner", "admin"):
                    self._probation_remove(group_id, user_id)
                    continue
                last_sent = data.get("last_sent_time") or 0
                # 发过言（进群后）→ 转正
                if isinstance(last_sent, (int, float)) and last_sent > float(entry.get("join_ts") or 0):
                    self.ctx.logger.info(
                        "[考察期] %s(%s) 已发言，转正", entry.get("member_name"), user_id,
                    )
                    self._probation_remove(group_id, user_id)
                    continue
                # 超时未发言 → 移出
                if now - float(entry.get("join_ts") or 0) >= probation_seconds:
                    await self._kick_member(group_id, user_id, entry)
        self._probation_persist()

    async def _kick_member(self, group_id: str, user_id: str, entry: dict[str, Any]) -> None:
        """移出超时未发言成员，并发送移出说明。"""
        cfg = self._probation_cfg
        try:
            await self.ctx.api.call(
                "adapter.napcat.group.set_group_kick",
                group_id=int(group_id),
                user_id=int(user_id),
                reject_add_request=cfg.reject_add_request,
            )
        except Exception as exc:
            self.ctx.logger.info("[考察期] 移出失败 %s(%s): %s", entry.get("member_name"), user_id, exc)
            return
        self.ctx.logger.info(
            "[考察期] 已移出 %s(%s) from %s（超时未发言）", entry.get("member_name"), user_id, group_id,
        )
        # 移出说明（直接调 API 发纯文本，不依赖 stream 解析）
        text = await self._compose_kick_text(group_id, user_id, entry)
        if text:
            try:
                await self.ctx.api.call(
                    "adapter.napcat.group.send_group_msg",
                    group_id=int(group_id),
                    message=[{"type": "text", "data": {"text": text}}],
                )
            except Exception as exc:
                self.ctx.logger.info("[考察期] 移出说明发送失败: %s", exc)
        self._probation_remove(group_id, user_id)

    async def _compose_kick_text(self, group_id: str, user_id: str, entry: dict[str, Any]) -> str:
        """按配置生成移出说明（llm 优先，失败回退模板）。"""
        cfg = self._probation_cfg
        text = ""
        if cfg.kick_message.mode == "llm":
            text = await self._generate_text(
                f"群成员 {entry.get('member_name') or user_id}（QQ {user_id}）加入超过 "
                f"{cfg.probation_hours} 小时未发言，已被移出群聊。"
                "请以你的性格自然地说明这件事（要包含被移出者的 QQ 号），简短，只输出要说的话。"
            )
        if not text and cfg.kick_message.fallback_to_template:
            text = (cfg.kick_message.template or "").replace(
                "{member_name}", entry.get("member_name") or user_id,
            ).replace("{user_id}", user_id).replace(
                "{probation_hours}", str(cfg.probation_hours),
            )
        return text

    async def _generate_text(self, user_prompt: str) -> str:
        """按考察期 llm 槽位生成一句话（失败返回空串，调用方回退模板）。"""
        cfg = self._probation_cfg.llm
        persona = await self._resolve_persona()
        system = (
            f"{persona}\n\n"
            "你是群里的一员，正在自然地和群友交流。"
            "只输出要说的话本身，不要解释、不要加引号。"
        )
        prompt = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ]
        try:
            kwargs: dict[str, Any] = {"prompt": prompt, "temperature": cfg.temperature}
            model = cfg.model.strip()
            if model:
                kwargs["model"] = model
            if cfg.max_tokens > 0:
                kwargs["max_tokens"] = cfg.max_tokens
            result = await self.ctx.llm.generate(**kwargs)
            return self._extract_llm_text(result)
        except Exception:
            self.ctx.logger.debug("[考察期] LLM 生成失败，回退模板", exc_info=True)
            return ""

    async def _send_at_message(self, group_id: str, user_id: str, text: str) -> bool:
        """向群发送一条 @ 指定成员 + 文本的消息（直接调适配器 API，不经 LLM）。"""
        try:
            await self.ctx.api.call(
                "adapter.napcat.group.send_group_msg",
                group_id=int(group_id),
                message=[
                    {"type": "at", "data": {"qq": user_id}},
                    {"type": "text", "data": {"text": text}},
                ],
            )
            return True
        except Exception as exc:
            self.ctx.logger.info("[考察期] 发送 @ 消息失败: %s", exc)
            return False

    def _probation_remove(self, group_id: str, user_id: str) -> None:
        """从考察名单移除一个成员（群为空时一并清理）。"""
        members = self._probation.get(group_id)
        if members and user_id in members:
            del members[user_id]
            if not members:
                del self._probation[group_id]

    def _probation_persist(self) -> None:
        """持久化考察名单（重启不丢，超时者不会被漏踢）。"""
        try:
            if not self._probation_file:
                return
            self._probation_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._probation_file, "w", encoding="utf-8") as f:
                json.dump(self._probation, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.ctx.logger.info("[考察期] 持久化失败: %s", exc)

    def _probation_load(self) -> None:
        """启动时加载持久化的考察名单。"""
        try:
            if self._probation_file and self._probation_file.exists():
                with open(self._probation_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._probation = data
        except Exception as exc:
            self.ctx.logger.info("[考察期] 加载失败: %s", exc)

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
        "get_group_essence",
        description="获取群精华消息列表",
        brief_description="获取群精华消息",
        detailed_description=(
            "当用户询问群精华/精华消息/置顶内容时调用，返回指定群的精华消息（发送者、内容、时间）。"
        ),
        parameters=[
            ToolParameterInfo(
                name="group_id", param_type=ToolParamType.STRING,
                description="目标群号", required=True,
            ),
        ],
    )
    async def get_group_essence(self, group_id: str, **kwargs) -> dict[str, Any]:
        try:
            result = await self.ctx.api.call(
                "adapter.napcat.group.get_essence_msg_list",
                group_id=int(group_id),
            )
        except Exception as e:
            return {"success": False, "reason": str(e)}
        if not isinstance(result, dict):
            return {"success": False, "reason": "返回结构异常"}
        data = result.get("data") or result.get("result") or []
        if not isinstance(data, list) or not data:
            return {"success": True, "result": f"群 {group_id} 暂无精华消息"}
        lines = [f"群 {group_id} 精华消息（{len(data)} 条）："]
        for item in data[:10]:
            if not isinstance(item, dict):
                continue
            sender = str(item.get("sender_nick") or item.get("nickname") or item.get("user_id") or "")
            content = ""
            for seg in item.get("content") if isinstance(item.get("content"), list) else []:
                if isinstance(seg, dict):
                    if str(seg.get("type")) == "text":
                        content += str(seg.get("data", {}).get("text") or "")
                    elif str(seg.get("type")) == "image":
                        content += "[图片]"
            if content:
                lines.append(f"- {sender}：{content[:80]}")
        if len(lines) == 1:
            return {"success": True, "result": f"群 {group_id} 暂无精华消息"}
        return {"success": True, "result": "\n".join(lines)}

    @Tool(
        "get_group_member_changes",
        description="查询群成员变动（自上次查询以来的新加入/退出成员）",
        brief_description="查询群成员变动",
        detailed_description=(
            "当用户询问群里最近谁加入/退出了、有哪些新成员、谁退群了时调用。"
            "返回该群自上次查询以来的成员变动（新加入和退出，含昵称与 QQ 号），"
            "查询后已返回的变动会被清空，下次只返回新的变动。"
        ),
        parameters=[
            ToolParameterInfo(
                name="group_id", param_type=ToolParamType.STRING,
                description="目标群号", required=True,
            ),
        ],
    )
    async def get_group_member_changes(self, group_id: str, **kwargs) -> dict[str, Any]:
        """返回自上次查询以来的群成员变动，并清空。"""
        changes = self._pending_changes.pop(group_id, [])
        joins = [c for c in changes if c["type"] == "join"]
        leaves = [c for c in changes if c["type"] == "leave"]

        def fmt(items: list[dict]) -> str:
            return "、".join(
                f"昵称「{c['nickname'] or c['user_id']}」（QQ {c['user_id']}）" for c in items
            ) or "无"

        if not changes:
            return {"success": True, "result": f"群 {group_id} 自上次查询以来没有成员变动。"}

        result = (
            f"群 {group_id} 自上次查询以来的成员变动：\n"
            f"- 新加入：{fmt(joins)}\n"
            f"- 退出：{fmt(leaves)}"
        )
        return {"success": True, "result": result}

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
