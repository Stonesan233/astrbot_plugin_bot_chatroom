"""
AstrBot 插件：双人格聊天室协作 (astrbot_plugin_bot_chatroom)
=============================================================

露娜大人 × 朝日娘 内部协作机制。
依赖 cc-astrbot-agent 作为底层 Coding Agent 引擎。

功能：
- /chatroom @露娜大人 <任务>   由露娜大人开始处理
- /chatroom @朝日娘 <任务>     由朝日娘开始处理
- /chatroom <任务>             智能路由到合适的人格
- /chatroom status             查看协作状态（含全局活跃会话）
- /chatroom history            查看对话历史
- /chatroom reset              重置当前会话
- /chatroom help               显示帮助

协作机制：
  两位人格可以互相委托任务（通过输出中包含 @对方名称），
  形成内部多轮对话。最终结果汇总后呈现给主人。
  最大内部轮次可配置（默认 8 轮），避免死循环。
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

# ---------------------------------------------------------------------------
# 将 cc-astrbot-agent 的 src 加入 import 路径
# ---------------------------------------------------------------------------
_PLUGIN_DIR = Path(__file__).resolve().parent
_AGENT_PLUGIN_DIR = _PLUGIN_DIR.parent / "cc-astrbot-agent"
_AGENT_SRC = _AGENT_PLUGIN_DIR / "src"
for _p in [str(_AGENT_SRC), str(_AGENT_PLUGIN_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cc_agent.agent import ClaudeCodeAgent  # noqa: E402

PLUGIN_NAME = "astrbot_plugin_bot_chatroom"

# 会话过期时间（秒），超过此时间的空闲会话自动清理
_CONVERSATION_TTL = 3600  # 1 小时

# ===========================================================================
# 人格定义
# ===========================================================================

PERSONAS: dict[str, dict] = {
    "luna": {
        "id": "luna",
        "names": ["@露娜大人", "@luna", "@Luna", "@LUNA"],
        "label": "露娜大人",
        "emoji": "🌙",
        "role_desc": (
            "你是「露娜大人」，一位优雅、睿智、略带威严的女性人格。"
            "你擅长架构设计、代码审查、系统规划和高层决策。"
            "说话风格从容自信，语气带有指导性，偶尔会夸奖或鞭策朝日娘。"
            "如果需要具体编码实现或技术细节，可以委托给朝日娘——"
            "在回复中写「@朝日娘 具体要求」即可。"
            "如果不需要委托，直接给出最终回复（回复中不要包含任何 @）。"
        ),
    },
    "asahi": {
        "id": "asahi",
        "names": ["@朝日娘", "@asahi", "@Asahi", "@ASAHI"],
        "label": "朝日娘",
        "emoji": "🌅",
        "role_desc": (
            "你是「朝日娘」，一位活泼、认真、略带天然呆的女性人格。"
            "你擅长编码实现、调试排错、测试编写和底层技术细节。"
            "说话风格元气满满，偶尔会加「です」「ます」等语气词，"
            "对露娜大人保持尊敬。"
            "如果遇到架构决策或高层设计问题，可以请示露娜大人——"
            "在回复中写「@露娜大人 具体问题」即可。"
            "如果不需要委托，直接给出最终回复（回复中不要包含任何 @）。"
        ),
    },
}

# 检测输出中委托标记的正则 —— 匹配 @name 后面跟随的任务描述
_DELEGATE_RE = re.compile(
    r"(?im)@(?P<name>露娜大人|朝日娘|[Ll]una|[Aa]sahi)"
    r"\s*[，,：:：]?\s*(?P<msg>.+?)$"
)

# 扁平映射：别名（去 @，小写） → persona_id
_ALIAS_MAP: dict[str, str] = {}
for _pid, _pinfo in PERSONAS.items():
    for _n in _pinfo["names"]:
        _ALIAS_MAP[_n.lstrip("@").lower()] = _pid


def _resolve_persona_id(raw_name: str) -> Optional[str]:
    """将任意别名（含/不含 @）解析为 persona_id"""
    return _ALIAS_MAP.get(raw_name.lstrip("@").lower().strip())


# ===========================================================================
# 插件主类
# ===========================================================================

class BotChatroomPlugin(Star):
    """
    双人格聊天室协作插件

    露娜大人和朝日娘在内部进行多轮协作，
    通过 @mention 相互委托任务，最终将结果呈现给主人。
    """

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self._agent: Optional[ClaudeCodeAgent] = None
        # 对话状态: key = conversation_id
        self.conversations: dict[str, dict] = {}

    # ===================================================================
    # 生命周期
    # ===================================================================

    async def initialize(self):
        """插件初始化：读取配置，创建 ClaudeCodeAgent 实例"""
        if not self.config.get("enable_chatroom", True):
            logger.info(f"[{PLUGIN_NAME}] 聊天室功能已禁用")
            return

        api_key = self.config.get("claude_api_key", "").strip()
        if not api_key:
            logger.warning(
                f"[{PLUGIN_NAME}] 未配置 claude_api_key，"
                "请在插件设置中填写 Anthropic API Key"
            )

        project_root = self.config.get("project_root", "").strip()
        if not project_root:
            project_root = str(_PLUGIN_DIR)
            logger.info(
                f"[{PLUGIN_NAME}] 未配置 project_root，"
                f"使用插件目录: {project_root}"
            )

        model = self.config.get("model", "claude-3-7-sonnet-20250219")
        base_url = self.config.get("base_url", "").strip() or None

        try:
            self._agent = ClaudeCodeAgent(
                project_root=project_root,
                claude_api_key=api_key or None,
                model=model,
                base_url=base_url,
            )
            logger.info(
                f"[{PLUGIN_NAME}] 初始化完成 | "
                f"model={model} | root={project_root}"
            )
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] Agent 初始化失败: {e}")
            self._agent = None

    async def terminate(self):
        """插件销毁：清理资源"""
        self._agent = None
        self.conversations.clear()
        logger.info(f"[{PLUGIN_NAME}] 已卸载")

    # ===================================================================
    # 命令入口 /chatroom
    # ===================================================================

    @filter.command("chatroom")
    async def chatroom_command(
        self, event: AstrMessageEvent, args: GreedyStr = ""
    ):
        """
        /chatroom 双人格聊天室命令

        子命令:
          status   查看协作状态（含全部活跃会话）
          help     显示帮助
          history  查看对话历史
          reset    重置当前会话

        聊天:
          /chatroom @露娜大人 <任务>
          /chatroom @朝日娘 <任务>
          /chatroom <任务>  (智能路由)
        """
        if not self.config.get("enable_chatroom", True):
            yield event.plain_result("聊天室功能已禁用。")
            return

        raw_args = args.strip()

        # 备用：从 event.message_str 补全参数（AstrBot GreedyStr 有时截断）
        msg_text = ""
        try:
            msg_text = event.message_str if hasattr(event, "message_str") else ""
        except Exception:
            pass

        if raw_args and " " not in raw_args and msg_text:
            m = re.search(r'/?chatroom\s+(.*)', msg_text, re.IGNORECASE)
            if m:
                full = m.group(1).strip()
                if len(full) > len(raw_args):
                    raw_args = full

        if not raw_args:
            yield event.plain_result(self._help_text())
            return

        parts = raw_args.split(maxsplit=1)
        sub = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        # ---- 子命令 ----
        if sub == "help":
            yield event.plain_result(self._help_text())
            return
        if sub == "status":
            yield event.plain_result(self._status_text(event))
            return
        if sub == "reset":
            yield event.plain_result(self._handle_reset(event))
            return
        if sub == "history":
            yield event.plain_result(self._handle_history(event))
            return

        # ---- 智能路由 + 委托 ----
        yield event.plain_result(await self._handle_chat(event, raw_args))

    # ===================================================================
    # 智能路由
    # ===================================================================

    async def _detect_target_persona(
        self, text: str, event: AstrMessageEvent
    ) -> str:
        """
        判断消息应由哪个人格处理。

        优先级:
          1. 消息中显式 @mention（大小写不敏感）
          2. 从 event 上下文获取当前会话的 persona_id
          3. 默认 "luna"
        """
        mentioned = self._detect_persona_from_text(text)
        if mentioned:
            return mentioned

        from_event = await self._get_persona_from_event(event)
        if from_event:
            return from_event

        return "luna"

    def _detect_persona_from_text(self, text: str) -> Optional[str]:
        """从文本 @mention 中检测目标人格（大小写不敏感）"""
        text_lower = text.lower()
        for pid, pinfo in PERSONAS.items():
            for name in pinfo["names"]:
                if name.lower() in text_lower:
                    return pid
        return None

    async def _get_persona_from_event(
        self, event: AstrMessageEvent
    ) -> Optional[str]:
        """
        从 event 上下文中获取当前会话绑定的 persona_id。

        查找路径:
          a) event.persona_id / event.persona
          b) event.session.persona_id / event.session.persona
          c) conversation_manager → 当前会话 → persona_id
        """
        for attr in ("persona_id", "persona"):
            val = getattr(event, attr, None)
            if val and isinstance(val, str):
                pid = _resolve_persona_id(val)
                if pid:
                    return pid

        session = getattr(event, "session", None)
        if session:
            for attr in ("persona_id", "persona"):
                val = getattr(session, attr, None)
                if val and isinstance(val, str):
                    pid = _resolve_persona_id(val)
                    if pid:
                        return pid

        try:
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr:
                umo = event.unified_msg_origin
                curr_cid = await conv_mgr.get_curr_conversation_id(umo)
                if curr_cid:
                    conv = await conv_mgr.get_conversation(umo, curr_cid)
                    if conv and hasattr(conv, "persona_id") and conv.persona_id:
                        pid = _resolve_persona_id(conv.persona_id)
                        if pid:
                            return pid
        except Exception:
            pass

        return None

    # ===================================================================
    # 核心：_internal_delegate — 内部委托循环
    # ===================================================================

    async def _internal_delegate(
        self,
        from_persona: str,
        to_persona: str,
        message: str,
        event: AstrMessageEvent,
    ) -> str:
        """
        内部委托：from_persona 将 message 委托给 to_persona。

        流程:
          1. 获取 / 创建对话状态 (self.conversations[conversation_id])
          2. 每轮: 构建 persona prompt → 调用 agent.run_task() → 记录
          3. 检测回复中 @委托 → 切换目标人格继续
          4. 无委托或达到 max_rounds → 结束

        每轮 from_persona → to_persona → message 完整记录。
        多轮对话上限由 max_internal_turns 控制（默认 8）。
        """
        conversation_id = self._get_conversation_id(event)
        conv = self._get_or_create_conversation(conversation_id)
        max_rounds = conv["max_rounds"]
        resp_max_len = self.config.get("response_max_length", 3000)

        # 确保 Agent 可用
        if not self._agent:
            return "Agent 未就绪，请检查插件配置。"
        if not self._agent.api_key:
            return "API Key 未配置，请在插件设置中填写。"

        # 重置本轮协作（新任务清空旧 turns）
        conv["turns"] = []
        conv["initial_persona"] = to_persona
        conv["status"] = "active"
        conv["updated_at"] = time.time()

        start = time.monotonic()
        delegator = from_persona
        current_persona = to_persona
        current_task = message

        logger.info(
            f"[{PLUGIN_NAME}] _internal_delegate 开始 | "
            f"{PERSONAS.get(delegator, {}).get('label', delegator)} → "
            f"{PERSONAS[current_persona]['label']} | "
            f"任务={current_task[:80]}"
        )

        try:
            for round_num in range(1, max_rounds + 1):
                conv["updated_at"] = time.time()

                logger.info(
                    f"[{PLUGIN_NAME}] 内部轮次 "
                    f"{round_num}/{max_rounds} | "
                    f"人格={PERSONAS[current_persona]['label']} | "
                    f"任务={current_task[:60]}"
                )

                # 发送中间状态通知（长任务时让主人知道进展）
                if round_num > 1:
                    try:
                        prev = conv["turns"][-1] if conv["turns"] else None
                        if prev:
                            prev_label = PERSONAS.get(
                                prev["to_persona"], {}
                            ).get("label", "?")
                            await event.send(
                                f"🔄 内部轮次 {round_num}: "
                                f"{PERSONAS[current_persona]['label']} "
                                f"正在接手任务"
                                f"（来自 {prev_label} 的委托）..."
                            )
                    except Exception:
                        pass  # 平台不支持多段发送则忽略

                # 1) 构建 prompt（含角色设定 + 历史记录 + 当前任务）
                prompt = self._build_persona_prompt(
                    current_persona, current_task, conv["turns"]
                )

                # 2) 调用底层 agent.run_task()
                response = await self._call_agent(prompt, current_persona)

                # 3) 记录本轮到对话历史
                turn = {
                    "from_persona": delegator,
                    "to_persona": current_persona,
                    "message": current_task,
                    "response": response,
                    "delegated_to": None,
                    "timestamp": time.time(),
                }
                conv["turns"].append(turn)

                # 4) 检测回复中是否包含 @委托
                delegation = self._detect_delegation_in_response(response)

                if delegation:
                    target_persona, delegated_msg = delegation
                    turn["delegated_to"] = target_persona

                    logger.info(
                        f"[{PLUGIN_NAME}] 委托: "
                        f"{PERSONAS[current_persona]['label']} → "
                        f"{PERSONAS[target_persona]['label']} | "
                        f"消息={delegated_msg[:60]}"
                    )

                    delegator = current_persona
                    current_persona = target_persona
                    current_task = delegated_msg
                    # continue → 下一轮
                else:
                    # 无更多委托 → 协作完成
                    conv["status"] = "completed"
                    break
            else:
                # for 循环正常结束 → 达到最大轮次
                conv["status"] = "timeout"
                logger.warning(
                    f"[{PLUGIN_NAME}] 内部对话达到最大轮次 "
                    f"{max_rounds}，强制结束"
                )

        except asyncio.CancelledError:
            conv["status"] = "error"
            logger.info(f"[{PLUGIN_NAME}] 内部协作被取消")
            return "内部协作被取消。"

        except Exception as e:
            conv["status"] = "error"
            tb = traceback.format_exc()
            logger.error(f"[{PLUGIN_NAME}] _internal_delegate 异常:\n{tb}")
            return f"内部协作异常: {e}"

        elapsed = time.monotonic() - start

        # ---- 自动审阅（可选） ----
        final_response = conv["turns"][-1]["response"] if conv["turns"] else ""
        if (
            self.config.get("auto_review", False)
            and conv["status"] == "completed"
            and len(conv["turns"]) > 1
        ):
            final_response = await self._auto_review(
                conv["initial_persona"], message, conv["turns"], final_response
            )

        # ---- 格式化输出 ----
        result = self._format_final_output(
            conv, message, final_response, elapsed
        )

        if len(result) > resp_max_len:
            result = (
                result[:resp_max_len]
                + f"\n\n... (已截断，原始 {len(result)} 字符)"
            )

        logger.info(
            f"[{PLUGIN_NAME}] _internal_delegate 结束 | "
            f"状态={conv['status']} | "
            f"轮次={len(conv['turns'])} | "
            f"耗时={elapsed:.1f}s"
        )

        # 清理过期会话
        self._cleanup_stale_conversations()

        return result

    # ===================================================================
    # Agent 调用
    # ===================================================================

    async def _call_agent(self, prompt: str, persona_id: str) -> str:
        """调用底层 ClaudeCodeAgent.run_task() 并收集完整输出"""
        if not self._agent:
            raise RuntimeError("Agent 未初始化")

        chunks: list[str] = []
        async for chunk in self._agent.run_task(
            task=prompt, persona=persona_id
        ):
            chunks.append(chunk)

        return "".join(chunks)

    # ===================================================================
    # Prompt 构建
    # ===================================================================

    def _build_persona_prompt(
        self,
        persona_id: str,
        task: str,
        history: list[dict],
    ) -> str:
        """
        构建完整任务提示词:
          [角色设定] + [协作历史] + [当前任务] + [行为指引]
        """
        pinfo = PERSONAS[persona_id]
        hist_len = self.config.get("history_preview_length", 500)
        parts: list[str] = []

        # ---- 角色设定 ----
        parts.append(f"[角色设定]\n{pinfo['role_desc']}")
        parts.append("")

        # ---- 协作历史 ----
        if history:
            parts.append("[之前的协作记录]")
            for turn in history:
                from_label = PERSONAS.get(
                    turn["from_persona"], {}
                ).get("label", turn["from_persona"])
                to_label = PERSONAS.get(
                    turn["to_persona"], {}
                ).get("label", turn["to_persona"])
                to_emoji = PERSONAS.get(
                    turn["to_persona"], {}
                ).get("emoji", "💬")

                task_preview = turn["message"][:200]
                resp_preview = turn["response"][:hist_len]
                if len(turn["response"]) > hist_len:
                    resp_preview += "..."

                parts.append(
                    f"  {to_emoji} {to_label} "
                    f"(来自 {from_label}): {task_preview}"
                )
                parts.append(f"    回复摘要: {resp_preview}")
                if turn.get("delegated_to"):
                    tgt = PERSONAS[turn["delegated_to"]]["label"]
                    parts.append(f"    → 委托给了 {tgt}")
            parts.append("")

        # ---- 当前任务 ----
        parts.append(f"[当前任务]\n{task}")
        parts.append("")

        # ---- 行为指引 ----
        parts.append(
            "[行为指引]\n"
            "1. 完成任务后，直接给出面向主人的最终回复。\n"
            "2. 如需委托对方协助，在回复中写"
            "「@对方名称 具体要求」。\n"
            "3. 如果不需要委托，回复中不要包含任何 @。\n"
            "4. 保持人格风格一致。"
        )

        return "\n".join(parts)

    # ===================================================================
    # 委托检测
    # ===================================================================

    def _detect_delegation_in_response(
        self, text: str
    ) -> Optional[tuple[str, str]]:
        """
        从 Agent 回复中检测 @委托指令。
        返回 (target_persona_id, delegated_message) 或 None。
        取最后一条委托作为最终意图。
        """
        matches = list(_DELEGATE_RE.finditer(text))
        if not matches:
            return None

        last = matches[-1]
        name_raw = last.group("name").strip()
        msg = last.group("msg").strip()

        target_id = _resolve_persona_id(name_raw)
        if not target_id:
            return None

        if not msg:
            msg = "请协助处理上述任务"

        return (target_id, msg)

    # ===================================================================
    # 自动审阅
    # ===================================================================

    async def _auto_review(
        self,
        reviewer_persona: str,
        original_task: str,
        turns: list[dict],
        raw_final: str,
    ) -> str:
        """auto_review 开启时，由发起人格审阅整理最终输出"""
        logger.info(f"[{PLUGIN_NAME}] 开始自动审阅")
        pinfo = PERSONAS[reviewer_persona]

        parts: list[str] = [
            f"[角色设定]\n{pinfo['role_desc']}",
            "",
            "[审阅任务]",
            "以下是刚才内部协作的完整过程。请以你的风格，"
            "整理一份面向主人的最终报告。"
            "保留关键技术信息，去掉内部协调细节，"
            "让回复更加清晰、专业。",
            "",
            f"原始任务: {original_task}",
            "",
            "[协作过程]",
        ]

        for i, turn in enumerate(turns, 1):
            label = PERSONAS.get(turn["to_persona"], {}).get("label", "?")
            parts.append(f"第{i}轮 - {label}:")
            parts.append(f"  任务: {turn['message'][:300]}")
            parts.append(f"  回复: {turn['response'][:1000]}")
            parts.append("")

        parts.append("请直接给出最终整理后的回复：")

        try:
            prompt = "\n".join(parts)
            reviewed = await self._call_agent(prompt, reviewer_persona)
            if reviewed.strip():
                logger.info(f"[{PLUGIN_NAME}] 自动审阅完成")
                return reviewed
        except Exception as e:
            logger.warning(
                f"[{PLUGIN_NAME}] 自动审阅失败，使用原始回复: {e}"
            )

        return raw_final

    # ===================================================================
    # 智能路由入口
    # ===================================================================

    async def _handle_chat(
        self, event: AstrMessageEvent, text: str
    ) -> str:
        """
        解析消息中的 @mention，确定目标人格，
        然后调用 _internal_delegate 启动内部协作循环。
        """
        target_persona = await self._detect_target_persona(text, event)
        task = self._clean_task_text(text)

        if not task:
            return (
                "请输入具体任务内容。\n"
                "例如: /chatroom @露娜大人 分析一下这个项目的架构"
            )

        logger.info(
            f"[{PLUGIN_NAME}] 智能路由 → "
            f"{PERSONAS[target_persona]['label']} | "
            f"任务={task[:80]}"
        )

        return await self._internal_delegate(
            from_persona="system",
            to_persona=target_persona,
            message=task,
            event=event,
        )

    # ===================================================================
    # 对话状态管理
    # ===================================================================

    def _get_conversation_id(self, event: AstrMessageEvent) -> str:
        """基于 unified_msg_origin 生成 conversation_id"""
        umo = getattr(event, "unified_msg_origin", None)
        return str(umo) if umo else str(uuid.uuid4())

    def _get_or_create_conversation(self, conversation_id: str) -> dict:
        """获取或创建对话状态"""
        if conversation_id not in self.conversations:
            self.conversations[conversation_id] = {
                "id": conversation_id,
                "turns": [],
                "initial_persona": None,
                "status": "idle",
                "max_rounds": self.config.get("max_internal_turns", 8),
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        return self.conversations[conversation_id]

    def _clean_task_text(self, text: str) -> str:
        """从任务文本中移除所有 @mention 前缀"""
        for pinfo in PERSONAS.values():
            for name in pinfo["names"]:
                text = re.sub(re.escape(name), "", text, flags=re.IGNORECASE)
        return text.strip()

    def _cleanup_stale_conversations(self):
        """清理超过 TTL 的空闲会话，防止内存持续增长"""
        now = time.time()
        stale = [
            cid
            for cid, conv in self.conversations.items()
            if conv["status"] != "active"
            and (now - conv["updated_at"]) > _CONVERSATION_TTL
        ]
        for cid in stale:
            del self.conversations[cid]
        if stale:
            logger.debug(
                f"[{PLUGIN_NAME}] 清理 {len(stale)} 个过期会话"
            )

    # ===================================================================
    # 输出格式化
    # ===================================================================

    def _format_final_output(
        self,
        conv: dict,
        original_task: str,
        final_response: str,
        elapsed: float,
    ) -> str:
        """格式化最终呈现给主人的输出"""
        turns = conv["turns"]
        initial = conv.get("initial_persona", "luna")
        initial_emoji = PERSONAS.get(initial, {}).get("emoji", "💬")
        initial_label = PERSONAS.get(initial, {}).get("label", "未知")
        parts: list[str] = []

        # ---- 头部 ----
        if len(turns) > 1:
            chain = " → ".join(
                PERSONAS.get(t["to_persona"], {}).get("emoji", "?")
                for t in turns
            )
            parts.append(
                f"{initial_emoji} 协作完成 | "
                f"发起: {initial_label} | "
                f"链路: {chain} | "
                f"{len(turns)} 轮 | "
                f"耗时 {elapsed:.1f}s"
            )
        else:
            parts.append(
                f"{initial_emoji} {initial_label} | "
                f"耗时 {elapsed:.1f}s"
            )

        # ---- 协作过程（多轮时展示） ----
        if len(turns) > 1:
            parts.append("")
            parts.append("── 协作过程 ──")
            for i, turn in enumerate(turns, 1):
                emoji = PERSONAS.get(
                    turn["to_persona"], {}
                ).get("emoji", "💬")
                label = PERSONAS.get(
                    turn["to_persona"], {}
                ).get("label", "?")
                preview = turn["response"][:200]
                if len(turn["response"]) > 200:
                    preview += "..."
                parts.append(f"  [{i}] {emoji} {label}: {preview}")
                if turn.get("delegated_to"):
                    tgt = PERSONAS[turn["delegated_to"]]["label"]
                    parts.append(f"      ↳ 委托 → {tgt}")
            parts.append("")
            parts.append("── 最终结果 ──")

        parts.append("")
        parts.append(final_response)

        # ---- 超时警告 ----
        if conv["status"] == "timeout":
            parts.append(
                f"\n⚠️ 内部对话达到上限 "
                f"({conv['max_rounds']} 轮)，已强制结束。"
            )

        return "\n".join(parts)

    # ===================================================================
    # 子命令处理器
    # ===================================================================

    def _handle_reset(self, event: AstrMessageEvent) -> str:
        """重置当前会话"""
        cid = self._get_conversation_id(event)
        if cid in self.conversations:
            del self.conversations[cid]
            return "聊天室会话已重置，可以开始新的协作。"
        return "当前无活跃会话，无需重置。"

    def _handle_history(self, event: AstrMessageEvent) -> str:
        """查看当前会话的对话历史"""
        cid = self._get_conversation_id(event)
        conv = self.conversations.get(cid)

        if not conv or not conv["turns"]:
            return (
                "当前无对话历史。"
                "发送 /chatroom <任务> 开始一次协作。"
            )

        status_emoji = {
            "active": "🔄",
            "completed": "✅",
            "timeout": "⏰",
            "error": "❌",
            "idle": "💤",
        }.get(conv["status"], "❓")

        initial_label = (
            PERSONAS.get(conv["initial_persona"], {}).get("label", "无")
            if conv.get("initial_persona")
            else "无"
        )

        lines: list[str] = [
            f"{status_emoji} 对话历史",
            f"  发起人格: {initial_label}",
            f"  状态: {conv['status']}",
            f"  轮次: {len(conv['turns'])}/{conv['max_rounds']}",
            "",
        ]

        for i, turn in enumerate(conv["turns"], 1):
            emoji = PERSONAS.get(
                turn["to_persona"], {}
            ).get("emoji", "💬")
            label = PERSONAS.get(
                turn["to_persona"], {}
            ).get("label", "?")
            from_label = PERSONAS.get(
                turn["from_persona"], {}
            ).get("label", turn["from_persona"])
            lines.append(f"[{i}] {emoji} {label} (来自 {from_label})")
            lines.append(f"    任务: {turn['message'][:150]}")
            lines.append(f"    回复: {turn['response'][:250]}")
            if turn.get("delegated_to"):
                tgt = PERSONAS[turn["delegated_to"]]["label"]
                lines.append(f"    → 委托: {tgt}")
            lines.append("")

        return "\n".join(lines)

    def _status_text(self, event: AstrMessageEvent) -> str:
        """协作状态报告：全局 + 当前会话"""
        cid = self._get_conversation_id(event)
        conv = self.conversations.get(cid)

        agent_ok = self._agent is not None
        api_ok = self._agent is not None and bool(self._agent.api_key)
        model = self._agent.model if self._agent else "未知"

        # 统计全部会话
        active_count = sum(
            1 for c in self.conversations.values()
            if c["status"] == "active"
        )
        total_count = len(self.conversations)
        total_turns = sum(
            len(c["turns"]) for c in self.conversations.values()
        )

        lines: list[str] = [
            "🌙🌅 双人格聊天室状态",
            "",
            f"  Agent:     {'✅ 就绪' if agent_ok else '❌ 未初始化'}",
            f"  API Key:   {'✅ 已配置' if api_ok else '❌ 未配置'}",
            f"  模型:      {model}",
            f"  会话数:    {active_count} 活跃 / {total_count} 总计",
            f"  总轮次:    {total_turns}",
            f"  最大轮次:  {self.config.get('max_internal_turns', 8)}",
            f"  自动审阅:  "
            f"{'✅ 开启' if self.config.get('auto_review', False) else '关闭'}",
        ]

        # ---- 全局活跃会话列表 ----
        active_convs = [
            (c_id, c) for c_id, c in self.conversations.items()
            if c["status"] == "active"
        ]
        if active_convs:
            lines.extend(["", "── 全局活跃会话 ──"])
            for c_id, c in active_convs:
                initial = PERSONAS.get(
                    c.get("initial_persona", ""), {}
                ).get("label", "?")
                n_turns = len(c["turns"])
                lines.append(
                    f"  🔄 {initial} | "
                    f"{n_turns}/{c['max_rounds']} 轮 | "
                    f"id={c_id[:24]}..."
                )

        # ---- 当前会话详情 ----
        if conv:
            initial = (
                PERSONAS.get(conv["initial_persona"], {}).get("label", "无")
                if conv.get("initial_persona")
                else "无"
            )
            se = {
                "active": "🔄",
                "completed": "✅",
                "timeout": "⏰",
                "error": "❌",
                "idle": "💤",
            }.get(conv["status"], "❓")

            lines.extend([
                "",
                "── 当前会话 ──",
                f"  发起人格:  {initial}",
                f"  状态:      {se} {conv['status']}",
                f"  内部轮次:  {len(conv['turns'])}/{conv['max_rounds']}",
            ])

            if conv["turns"]:
                chain = " → ".join(
                    f"{PERSONAS.get(t['to_persona'], {}).get('emoji', '?')}"
                    f"{PERSONAS.get(t['to_persona'], {}).get('label', '?')}"
                    for t in conv["turns"]
                )
                lines.append(f"  链路:      {chain}")

        return "\n".join(lines)

    def _help_text(self) -> str:
        """帮助文本"""
        return (
            "🌙🌅 双人格聊天室 — 露娜大人 × 朝日娘\n"
            "\n"
            "用法:\n"
            "  /chatroom @露娜大人 <任务>   由露娜大人开始处理\n"
            "  /chatroom @朝日娘 <任务>     由朝日娘开始处理\n"
            "  /chatroom <任务>             智能路由到合适的人格\n"
            "  /chatroom status             查看协作状态\n"
            "  /chatroom history            查看对话历史\n"
            "  /chatroom reset              重置当前会话\n"
            "  /chatroom help               显示此帮助\n"
            "\n"
            "协作机制:\n"
            "  两位人格可以互相委托任务（在回复中 @对方），\n"
            "  形成内部多轮对话，最终汇总结果呈现给您。\n"
            "  内部对话最多 8 轮（可配置），避免死循环。\n"
            "\n"
            "智能路由（大小写不敏感）:\n"
            "  @露娜大人 / @luna  → 露娜大人处理\n"
            "  @朝日娘   / @asahi → 朝日娘处理\n"
            "  无 @                → 根据会话 persona 自动决定\n"
            "\n"
            "示例:\n"
            "  /chatroom @露娜大人 设计一个 RESTful API\n"
            "  /chatroom @朝日娘 实现用户登录功能并编写测试\n"
            "  /chatroom 帮我写一个 Python 爬虫脚本"
        )
