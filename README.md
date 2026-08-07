# 麦麦看到你了！（群感知插件）

> **⚠️ 免责声明**：本插件由 AI 辅助生成，代码可能存在未知缺陷或不当行为。使用前请自行审阅源码、评估风险，并对其在你的环境中产生的任何后果负责。

感知群人员变动事件，按事件类型可选三种处理方式；并提供群信息查询工具。

## 功能

**事件感知**（实时，NapCat notice 事件）：
- 进群（`group_increase`）→ 默认 LLM 自然欢迎
- 退群（`group_decrease`）→ 默认注入上下文（被踢时区分"被 XX 移出"）
- 禁言/解禁（`group_ban`）→ 默认注入上下文（含全体禁言）
- 群名变动（`notify.group_name`）→ 默认注入上下文
- 管理变动（`group_admin`）→ 默认注入上下文

每类事件**独立配置**处理方式：
- `template`：发送固定模板文案（占位符 `{group_name} {member_name} {operator_name} {target_name} {duration}`）
- `llm`：按全局人设生成自然回应并发送（走 planner 槽位）
- `context`：仅注入聊天上下文，bot 感知但不主动发言（零费用）

**工具化查询**（LLM 按需调用）：
- `get_group_honor`：群荣誉（龙王、群聊之火、传说、炽星、冒尖小萌新）
- `get_member_title`：群员称号（群头衔）
- `get_group_avatar`：群头像（CDN URL 规则，无需 API）
- `get_group_notice`：群公告
- `get_group_shut_list`：群禁言列表

## 安装

目录放入 `plugins/` 重启 MaiBot。依赖：无（仅 MaiBot SDK）。

## 配置

见 `config.toml` 注释。要点：
- `scope.whitelist / blacklist`：群范围黑白名单（黑名单优先）
- `events.<类型>.mode`：template / llm / context
- `self_ban.mode`：bot 自己被禁言时 notify_admin（私聊通知）/ context / none
- `llm`：人格化回复的模型槽位（默认 planner）

## 验证

1. 事件：拉个小号进群 → 观察 bot 是否按配置欢迎；群里禁言某人 → 观察上下文注入
2. 工具：私信 bot"查一下我们群的龙王是谁" → LLM 应调用 `get_group_honor`

## 说明

- 群头像/封面无 NapCat API，头像用 CDN URL 规则直接取；封面无解（仅 `hasGroupCustomPortrait` 标记）
- 事件处理完会 abort，通知事件不会进主链路被当作聊天消息回复
- 成员昵称解析带 TTL 缓存（群名片优先，兜底陌生人信息）
