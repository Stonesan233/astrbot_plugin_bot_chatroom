# CLAUDE.md — astrbot_plugin_bot_chatroom

## 项目定位

AstrBot 插件，实现露娜大人和朝日娘的双人格内部协作机制。依赖 cc-astrbot-agent（同级目录）作为底层 Agent 引擎。不自带 Agent 实现，所有 LLM 调用通过 `ClaudeCodeAgent.run_task()` 完成。

## 关键路径

- 插件入口: `main.py` → `BotChatroomPlugin(Star)`
- 依赖: `../cc-astrbot-agent/src/cc_agent/agent.py` → `ClaudeCodeAgent`

## 导入约定

`main.py` 将 `../cc-astrbot-agent/src` 加入 `sys.path`，然后 `from cc_agent.agent import ClaudeCodeAgent`。因此本插件必须与 cc-astrbot-agent 放在同一父目录下。

## 人格系统

`PERSONAS` dict 定义了两个人格，每个人格包含：
- `id`: persona_id 字符串 ("luna" / "asahi")
- `names`: 触发别名列表（含 @ 前缀），用于消息路由
- `label`: 显示名称
- `emoji`: 标识 emoji
- `role_desc`: 系统提示词（定义人格性格、能力、委托行为）

`_ALIAS_MAP` 扁平化映射：别名（去掉 @，小写）→ persona_id。

## 核心方法调用链

```
chatroom_command()        /chatroom 命令入口
  ├─ 子命令: status / help / history / reset
  └─ _handle_chat()       智能路由
       ├─ _detect_target_persona()    三级优先路由
       │   ├─ _detect_persona_from_text()    文本 @mention 匹配
       │   ├─ _get_persona_from_event()      event/session/conv_mgr 属性查找
       │   └─ 默认 "luna"
       └─ _internal_delegate()        核心委托循环
            ├─ _build_persona_prompt()       构建 prompt
            ├─ _call_agent()                 agent.run_task()
            ├─ _detect_delegation_in_response()  正则提取 @委托
            └─ (循环直到无委托或达到 max_rounds)
```

## _internal_delegate 详解

参数: `(from_persona, to_persona, message, event)`

1. 获取/创建 `self.conversations[conversation_id]`
2. 进入 while 循环（受 `max_rounds` 限制，默认 8）：
   - 构建 prompt（角色设定 + 协作历史 + 当前任务 + 行为指引）
   - 调用 `agent.run_task(task=prompt, persona=persona_id)`
   - 记录到 `conv["turns"]`
   - 正则检测回复中的 `@<name> <message>` 模式
   - 有委托 → 切换 persona 继续循环
   - 无委托 → 标记 completed，跳出
3. 可选 auto_review：追加一轮由发起人格审阅整理
4. 格式化输出（含协作过程摘要 + 最终回复）

## 对话状态管理

`self.conversations: dict[str, dict]`，key = conversation_id。

conversation_id 来自 `event.unified_msg_origin`（即频道/群组 ID），意味着同一频道所有用户共享一个会话。

会话结构:
```python
{
    "turns": [{"from_persona", "to_persona", "message", "response", "delegated_to", "timestamp"}],
    "initial_persona": str,
    "status": "idle|active|completed|timeout|error",
    "max_rounds": int,
    "created_at": float,
    "updated_at": float,
}
```

纯内存存储，无持久化，无 TTL 清理。

## 委托检测

`_DELEGATE_RE` 正则匹配回复中的 `@name message` 模式。取最后一条匹配作为委托意图。`_resolve_persona_id()` 通过 `_ALIAS_MAP` 将任意别名映射到 persona_id。

## AstrBot 插件模式

- 继承 `Star`，用 `@filter.command("chatroom")` 注册命令
- `GreedyStr` 有时会截断参数，需要从 `event.message_str` 备用提取
- 响应用 `yield event.plain_result(...)`
- 配置通过 `_conf_schema.json` 定义

## 配置项速查

| 键 | 默认 | 说明 |
|---|---|---|
| `enable_chatroom` | `true` | 总开关 |
| `max_internal_turns` | `8` | 最大内部轮次 |
| `auto_review` | `false` | 自动审阅 |
| `response_max_length` | `3000` | 输出截断 |
| `history_preview_length` | `500` | 历史 prompt 截断 |
