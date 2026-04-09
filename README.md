# astrbot_plugin_bot_chatroom

AstrBot 插件：双人格聊天室协作（露娜大人 x 朝日娘），依赖 cc-astrbot-agent 作为底层 Agent 引擎。

## 概述

本插件实现两个人格在内部进行多轮协作的机制：

- **露娜大人** (Luna) — 擅长架构设计、代码审查、系统规划
- **朝日娘** (Asahi) — 擅长编码实现、调试排错、测试编写

两位人格可以通过在回复中 `@对方` 互相委托任务，形成内部多轮对话。最终结果汇总后呈现给主人。

## 功能

| 命令 | 说明 |
|---|---|
| `/chatroom @露娜大人 <任务>` | 由露娜大人开始处理 |
| `/chatroom @朝日娘 <任务>` | 由朝日娘开始处理 |
| `/chatroom <任务>` | 智能路由到合适的人格 |
| `/chatroom status` | 查看协作状态 |
| `/chatroom history` | 查看对话历史 |
| `/chatroom reset` | 重置当前会话 |
| `/chatroom help` | 显示帮助 |

## 协作机制

```
主人: /chatroom @露娜大人 设计一个 RESTful API

内部流程:
  [1] 露娜大人 → 分析需求，设计 API 架构
       "API 分为用户、订单、商品三个模块。具体实现请 @朝日娘 编写用户模块"
  [2] 朝日娘 → 实现用户模块代码
       "用户模块已实现完毕！认证部分 @露娜大人 请确认 JWT 方案"
  [3] 露娜大人 → 审阅并确认
       "方案确认。以下是完整的设计与实现..."

→ 最终结果呈现给主人
```

### 智能路由

消息会按以下优先级确定处理人格：

1. **显式 @mention**: 消息中包含 `@露娜大人`/`@luna` 或 `@朝日娘`/`@asahi`
2. **Event 上下文**: 从 AstrBot 会话中获取当前绑定的 persona_id
3. **默认**: 露娜大人

### 内部委托循环

- 最大内部轮次可配置（默认 8 轮），防止死循环
- 超过上限自动结束并返回当前结果
- 支持自动审阅模式（由发起人格整理最终输出）

## 安装

### 前置依赖

- [cc-astrbot-agent](https://github.com/Stonesan233/cc-astrbot-agent) — 必须与本插件放在同一父目录下

```
astrbot_plugins/
├── cc-astrbot-agent/          ← Agent 引擎
└── astrbot_plugin_bot_chatroom/  ← 本插件
```

### 安装步骤

1. 确保 cc-astrbot-agent 已安装并配置好 API Key
2. 将本插件放置在 cc-astrbot-agent 的同级目录
3. 在 AstrBot 管理面板中填写配置

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `claude_api_key` | string | `""` | Anthropic API Key（与 cc-astrbot-agent 共享） |
| `base_url` | string | `""` | 自定义 API 端点 |
| `model` | string | `claude-3-7-sonnet-20250219` | 内部协作使用的模型 |
| `project_root` | string | `""` | Agent 工作目录 |
| `enable_chatroom` | bool | `true` | 启用聊天室功能 |
| `max_internal_turns` | int | `8` | 最大内部委托轮次（1-20） |
| `auto_review` | bool | `false` | 自动审阅最终结果 |
| `response_max_length` | int | `3000` | 回复最大字符数（500-8000） |
| `history_preview_length` | int | `500` | 历史记录截断长度（100-2000） |

## 架构

```
astrbot_plugin_bot_chatroom/
├── main.py                 # 插件全部逻辑
├── metadata.yaml           # 插件元数据
├── _conf_schema.json       # 配置 Schema
├── README.md               # 本文档
└── CLAUDE.md               # AI 开发上下文
```

### 核心流程

```
/chatroom <任务>
  → chatroom_command()          命令入口
    → _handle_chat()            智能路由
      → _detect_target_persona()  检测目标人格 (@mention > event > 默认)
      → _internal_delegate()     内部委托循环
        → _build_persona_prompt() 构建 prompt (角色设定 + 历史 + 任务)
        → _call_agent()          调用 ClaudeCodeAgent.run_task()
        → _detect_delegation()   检测回复中的 @委托
        → (有委托 → 切换人格继续循环)
        → (无委托 → 协作结束)
      → _format_final_output()  格式化最终输出
```

### 对话状态

使用 `self.conversations` dict 管理对话状态，key 为 `conversation_id`（基于 `event.unified_msg_origin`）。

每个会话包含：
- `turns`: 所有轮次记录（from/to/message/response/delegated_to）
- `initial_persona`: 初始处理人格
- `status`: 会话状态 (idle/active/completed/timeout/error)
- `max_rounds`: 最大轮次上限

状态为内存存储，插件重载后清空。

## 许可

MIT License
