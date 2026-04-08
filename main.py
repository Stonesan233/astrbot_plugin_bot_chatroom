"""
AstrBot 插件：双人格聊天室协作 (astrbot_plugin_bot_chatroom)
=============================================================

露娜大人 × 朝日娘 内部协作机制。
依赖 cc-astrbot-agent 作为底层 Coding Agent 引擎。

功能：
- /chatroom @露娜大人 <任务>   由露娜大人开始处理
- /chatroom @朝日娘 <任务>     由朝日娘开始处理
- /chatroom <任务>             默认由露娜大人开始
- /chatroom status             查看当前协作状态
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
from dataclasses import dataclass, field
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
            "如果需要具体编码实现或技术细节，可以委托给朝日娘——在回复中写「@朝日娘 具体要求」即可。"
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
            "说话风格元气满满，偶尔会加「です」「ます」等语气词，对露娜大人保持尊敬。"
            "如果遇到架构决策或高层设计问题，可以请示露娜大人——在回复中写「@露娜大人 具体问题」即可。"
            "如果不需要委托，直接给出最终回复（回复中不要包含任何 @）。"
        ),
    },
}

# 用于检测输出中是否包含委托标记的正则
_DELEGATE_PATTERN = re.compile(
    r"@(?P<name>露娜大人|朝日娘|[Ll]una|[Aa]sahi)\s*[，,：:：]?\s*(?P<msg>.*?)(?:\n|$)",
)


# ===========================================================================
# 会话数据结构
# ===========================================================================

@dataclass
class ChatroomTurn:
    """一轮内部对话记录"""
    persona_id: str
    task: str
    response: str
    delegated_to: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class ChatroomSession:
    """一个聊天室会话的完整状态"""
    session_id: str
    turns: list[ChatroomTurn] = field(default_factory=list)
    current_turn: int = 0
    max_turns: int = 8
    status: str = "idle"          # idle / active / completed / timeout / error
    initial_persona: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def last_turn(self) -> Optional[ChatroomTurn]:
        return self.turns[-1] if self.turns else None


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
        self._sessions: dict[str, ChatroomSession] = {}

    # -----------------------------------------------------------------------
    # 生命周期
    # -----------------------------------------------------------------------

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
                f"[{PLUGIN_NAME}] 未配置 project_root，使用插件目录: {project_root}"
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
                f"[{PLUGIN_NAME}] 初始化完成 | model={model} | root={project_root}"
            )
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] Agent 初始化失败: {e}")
            self._agent = None

    async def terminate(self):
        """插件销毁：清理资源"""
        self._agent = None
        self._sessions.clear()
        logger.info(f"[{PLUGIN_NAME}] 已卸载")

    # -----------------------------------------------------------------------
    # 命令入口
    # -----------------------------------------------------------------------

    @filter.command("chatroom")
    async def chatroom_command(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """
        /chatroom 双人格聊天室命令

        用法:
          /chatroom @露娜大人 <任务>   由露娜大人开始处理
          /chatroom @朝日娘 <任务>     由朝日娘开始处理
          /chatroom <任务>             默认由露娜大人开始
          /chatroom status             查看协作状态
          /chatroom history            查看对话历史
          /chatroom reset              重置当前会话
          /chatroom help               显示帮助
        """
        # 检查功能开关
        if not self.config.get("enable_chatroom", True):
            yield event.plain_result("聊天室功能已禁用。")
            return

        raw_args = args.strip()

        # 尝试从 event 获取完整消息作为备用
        msg_text = ""
        try:
            msg_text = event.message_str if hasattr(event, "message_str") else ""
        except Exception:
            pass

        # 如果 GreedyStr 未捕获完整参数，从消息原文中提取
        if raw_args and " " not in raw_args and msg_text:
            cr_match = re.search(r'/?chatroom\s+(.*)', msg_text, re.IGNORECASE)
            if cr_match:
                full_args = cr_match.group(1).strip()
                if len(full_args) > len(raw_args):
                    raw_args = full_args

        # 无参数 → 显示帮助
        if not raw_args:
            yield event.plain_result(self._help_text())
            return

        # 解析子命令
        parts = raw_args.split(maxsplit=1)
        sub = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        # ---- 子命令分发 ----
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

        # ---- 默认：作为聊天消息处理 ----
        yield event.plain_result(await self._handle_chat(event, raw_args))

    # -----------------------------------------------------------------------
    # 核心：内部协作逻辑
    # -----------------------------------------------------------------------

    def _get_session_id(self, event: AstrMessageEvent) -> str:
        """获取会话 ID（基于 unified_msg_origin）"""
        umo = getattr(event, "unified_msg_origin", None)
        return str(umo) if umo else str(uuid.uuid4())

    def _get_or_create_session(self, session_id: str) -> ChatroomSession:
        """获取或创建会话"""
        if session_id not in self._sessions:
            max_turns = self.config.get("max_internal_turns", 8)
            self._sessions[session_id] = ChatroomSession(
                session_id=session_id,
                max_turns=max_turns,
            )
        return self._sessions[session_id]

    def _detect_persona(self, text: str) -> Optional[str]:
        """
        检测消息中指定的人格。
        返回 persona_id 或 None。
        """
        text_lower = text.lower()
        for pid, pinfo in PERSONAS.items():
            for name in pinfo["names"]:
                if name.lower() in text_lower:
                    return pid
        return None

    def _extract_delegation(self, text: str) -> Optional[tuple[str, str]]:
        """
        从 Agent 输出中提取委托信息。

        返回 (target_persona_id, delegated_message) 或 None。
        查找最后一条委托指令（避免中间讨论被误识别）。
        """
        matches = list(_DELEGATE_PATTERN.finditer(text))
        if not matches:
            return None

        # 取最后一条委托（通常是最终的意图）
        last_match = matches[-1]
        name_raw = last_match.group("name").strip()
        msg = last_match.group("msg").strip()

        # 将名称映射到 persona_id
        target_id = None
        for pid, pinfo in PERSONAS.items():
            for name in pinfo["names"]:
                if name.lstrip("@").lower() == name_raw.lower():
                    target_id = pid
                    break
            if target_id:
                break

        if not target_id:
            return None

        if not msg:
            msg = "请协助处理上述任务"

        return (target_id, msg)

    def _clean_task_text(self, text: str) -> str:
        """从任务文本中移除 @mention 前缀"""
        for pid, pinfo in PERSONAS.items():
            for name in pinfo["names"]:
                text = re.sub(re.escape(name), "", text, flags=re.IGNORECASE)
        return text.strip()

    def _build_task_prompt(
        self,
        persona_id: str,
        task: str,
        history: list[ChatroomTurn],
    ) -> str:
        """
        构建完整的任务提示词。
        包含人格设定、协作历史、当前任务和指令。
        """
        pinfo = PERSONAS[persona_id]
        hist_len = self.config.get("history_preview_length", 500)
        parts: list[str] = []

        # 1) 人格设定
        parts.append(f"[角色设定]\n{pinfo['role_desc']}")
        parts.append("")

        # 2) 协作历史
        if history:
            parts.append("[之前的协作记录]")
            for turn in history:
                label = PERSONAS[turn.persona_id]["label"]
                emoji = PERSONAS[turn.persona_id]["emoji"]
                task_preview = turn.task[:200]
                resp_preview = turn.response[:hist_len]
                if len(turn.response) > hist_len:
                    resp_preview += "..."

                parts.append(f"  {emoji} {label} 的任务: {task_preview}")
                parts.append(f"    回复摘要: {resp_preview}")
                if turn.delegated_to:
                    target_label = PERSONAS[turn.delegated_to]["label"]
                    parts.append(f"    → 委托给了 {target_label}")
            parts.append("")

        # 3) 当前任务
        parts.append(f"[当前任务]\n{task}")
        parts.append("")

        # 4) 行为指令
        parts.append(
            "[行为指引]\n"
            "1. 完成任务后，直接给出面向主人的最终回复。\n"
            "2. 如需委托对方协助，在回复中写「@对方名称 具体要求」，"
            "委托内容之后的文字将被视为你的最终回复的一部分。\n"
            "3. 如果不需要委托，回复中不要包含任何 @。\n"
            "4. 保持人格风格一致。"
        )

        return "\n".join(parts)

    async def _run_single_turn(
        self,
        persona_id: str,
        task: str,
        history: list[ChatroomTurn],
    ) -> str:
        """执行单轮 Agent 调用"""
        if not self._agent:
            raise RuntimeError("Agent 未初始化")

        prompt = self._build_task_prompt(persona_id, task, history)
        output_parts: list[str] = []

        async for chunk in self._agent.run_task(
            task=prompt,
            persona=persona_id,
        ):
            output_parts.append(chunk)

        return "".join(output_parts)

    def _build_review_prompt(
        self,
        persona_id: str,
        original_task: str,
        internal_history: list[ChatroomTurn],
    ) -> str:
        """构建自动审阅提示词（auto_review 开启时使用）"""
        pinfo = PERSONAS[persona_id]

        parts: list[str] = []
        parts.append(f"[角色设定]\n{pinfo['role_desc']}")
        parts.append("")
        parts.append("[审阅任务]")
        parts.append(
            "以下是刚才内部协作的完整过程。请以你的风格，"
            "整理一份面向主人的最终报告。"
            "保留关键技术信息，去掉内部协调细节，"
            "让回复更加清晰、专业。"
        )
        parts.append("")
        parts.append(f"原始任务: {original_task}")
        parts.append("")

        parts.append("[协作过程]")
        for i, turn in enumerate(internal_history, 1):
            label = PERSONAS[turn.persona_id]["label"]
            parts.append(f"第{i}轮 - {label}:")
            parts.append(f"  任务: {turn.task[:300]}")
            parts.append(f"  回复: {turn.response[:1000]}")
            parts.append("")

        parts.append("请直接给出最终整理后的回复：")
        return "\n".join(parts)

    async def _handle_chat(self, event: AstrMessageEvent, text: str) -> str:
        """处理聊天室消息：启动内部协作循环"""
        session_id = self._get_session_id(event)
        session = self._get_or_create_session(session_id)
        max_turns = session.max_turns
        resp_max_len = self.config.get("response_max_length", 3000)

        # 确定初始人格
        initial_persona = self._detect_persona(text)
        if not initial_persona:
            initial_persona = "luna"  # 默认由露娜大人开始

        # 清理任务文本
        task = self._clean_task_text(text)
        if not task:
            return (
                "请输入具体任务内容。\n"
                "例如: /chatroom @露娜大人 分析一下这个项目的架构"
            )

        # 确保 Agent 可用
        if not self._agent:
            return "Agent 未就绪，请检查插件配置中的 claude_api_key。"
        if not self._agent.api_key:
            return "API Key 未配置，请在插件设置中填写。"

        # 重置会话状态
        session.turns.clear()
        session.current_turn = 0
        session.initial_persona = initial_persona
        session.status = "active"
        session.updated_at = time.monotonic()

        start = time.monotonic()
        current_persona = initial_persona
        current_task = task

        logger.info(
            f"[{PLUGIN_NAME}] 开始协作 | "
            f"初始人格={PERSONAS[initial_persona]['label']} | "
            f"任务={task[:80]}"
        )

        # ---- 内部协作循环 ----
        try:
            while session.current_turn < max_turns:
                session.current_turn += 1
                session.updated_at = time.monotonic()

                logger.info(
                    f"[{PLUGIN_NAME}] 内部轮次 "
                    f"{session.current_turn}/{max_turns} | "
                    f"人格={PERSONAS[current_persona]['label']} | "
                    f"任务={current_task[:60]}"
                )

                # 执行单轮
                response = await self._run_single_turn(
                    current_persona,
                    current_task,
                    session.turns,
                )

                # 检查是否包含委托
                delegation = self._extract_delegation(response)

                turn = ChatroomTurn(
                    persona_id=current_persona,
                    task=current_task,
                    response=response,
                    delegated_to=delegation[0] if delegation else None,
                )
                session.turns.append(turn)

                if delegation:
                    target_persona, delegated_msg = delegation
                    logger.info(
                        f"[{PLUGIN_NAME}] 委托: "
                        f"{PERSONAS[current_persona]['label']} → "
                        f"{PERSONAS[target_persona]['label']} | "
                        f"消息={delegated_msg[:60]}"
                    )
                    current_persona = target_persona
                    current_task = delegated_msg
                else:
                    # 无更多委托，协作完成
                    session.status = "completed"
                    break

            else:
                # 达到最大轮次
                session.status = "timeout"
                logger.warning(
                    f"[{PLUGIN_NAME}] 内部对话达到最大轮次 {max_turns}，强制结束"
                )

        except asyncio.CancelledError:
            session.status = "error"
            logger.info(f"[{PLUGIN_NAME}] 协作被取消")
            return "内部协作被取消。"

        except Exception as e:
            session.status = "error"
            tb = traceback.format_exc()
            logger.error(f"[{PLUGIN_NAME}] 内部对话异常:\n{tb}")
            return f"内部协作异常: {e}"

        elapsed = time.monotonic() - start

        # ---- 自动审阅（可选） ----
        final_response = session.turns[-1].response if session.turns else ""
        if self.config.get("auto_review", False) and session.status == "completed":
            logger.info(f"[{PLUGIN_NAME}] 开始自动审阅")
            try:
                review_prompt = self._build_review_prompt(
                    initial_persona, task, session.turns
                )
                review_parts: list[str] = []
                async for chunk in self._agent.run_task(
                    task=review_prompt,
                    persona=initial_persona,
                ):
                    review_parts.append(chunk)
                reviewed = "".join(review_parts)
                if reviewed.strip():
                    final_response = reviewed
                logger.info(f"[{PLUGIN_NAME}] 自动审阅完成")
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 自动审阅失败，使用原始回复: {e}")

        # ---- 构建最终输出 ----
        result = self._format_final_output(
            session, task, final_response, elapsed
        )

        # 截断过长输出
        if len(result) > resp_max_len:
            result = result[:resp_max_len] + f"\n\n... (已截断，原始 {len(result)} 字符)"

        logger.info(
            f"[{PLUGIN_NAME}] 协作结束 | "
            f"状态={session.status} | "
            f"轮次={len(session.turns)} | "
            f"耗时={elapsed:.1f}s"
        )

        return result

    def _format_final_output(
        self,
        session: ChatroomSession,
        original_task: str,
        final_response: str,
        elapsed: float,
    ) -> str:
        """格式化最终输出结果"""
        parts: list[str] = []

        # 头部：协作摘要
        initial_emoji = (
            PERSONAS[session.initial_persona]["emoji"]
            if session.initial_persona else "💬"
        )
        initial_label = (
            PERSONAS[session.initial_persona]["label"]
            if session.initial_persona else "未知"
        )

        if len(session.turns) > 1:
            # 多轮协作
            delegate_chain = " → ".join(
                PERSONAS[t.persona_id]["emoji"]
                for t in session.turns
            )
            parts.append(
                f"{initial_emoji} 协作完成 | "
                f"发起: {initial_label} | "
                f"链路: {delegate_chain} | "
                f"{len(session.turns)} 轮 | "
                f"耗时 {elapsed:.1f}s"
            )
        else:
            # 单轮直接回复
            parts.append(
                f"{initial_emoji} {initial_label} | "
                f"耗时 {elapsed:.1f}s"
            )

        # 协作详情（多轮时展示）
        if len(session.turns) > 1:
            parts.append("")
            parts.append("── 协作过程 ──")
            for i, turn in enumerate(session.turns, 1):
                emoji = PERSONAS[turn.persona_id]["emoji"]
                label = PERSONAS[turn.persona_id]["label"]
                resp_preview = turn.response[:200]
                if len(turn.response) > 200:
                    resp_preview += "..."
                parts.append(f"  [{i}] {emoji} {label}: {resp_preview}")
                if turn.delegated_to:
                    target = PERSONAS[turn.delegated_to]["label"]
                    parts.append(f"      ↳ 委托 → {target}")
            parts.append("")
            parts.append("── 最终结果 ──")

        parts.append("")
        parts.append(final_response)

        # 状态提示
        if session.status == "timeout":
            parts.append(
                f"\n⚠️ 内部对话达到上限 ({session.max_turns} 轮)，已强制结束。"
            )

        return "\n".join(parts)

    # -----------------------------------------------------------------------
    # 子命令处理器
    # -----------------------------------------------------------------------

    def _handle_reset(self, event: AstrMessageEvent) -> str:
        """重置当前会话"""
        session_id = self._get_session_id(event)
        if session_id in self._sessions:
            del self._sessions[session_id]
            return "聊天室会话已重置，可以开始新的协作。"
        return "当前无活跃会话，无需重置。"

    def _handle_history(self, event: AstrMessageEvent) -> str:
        """查看当前会话的对话历史"""
        session_id = self._get_session_id(event)
        session = self._sessions.get(session_id)

        if not session or not session.turns:
            return "当前无对话历史。发送 /chatroom <任务> 开始一次协作。"

        status_emoji = {
            "active": "🔄",
            "completed": "✅",
            "timeout": "⏰",
            "error": "❌",
            "idle": "💤",
        }.get(session.status, "❓")

        initial_label = (
            PERSONAS[session.initial_persona]["label"]
            if session.initial_persona else "无"
        )

        lines: list[str] = [
            f"{status_emoji} 对话历史",
            f"  发起人格: {initial_label}",
            f"  状态: {session.status}",
            f"  轮次: {len(session.turns)}/{session.max_turns}",
            "",
        ]

        for i, turn in enumerate(session.turns, 1):
            emoji = PERSONAS[turn.persona_id]["emoji"]
            label = PERSONAS[turn.persona_id]["label"]
            lines.append(f"[{i}] {emoji} {label}")
            lines.append(f"    任务: {turn.task[:150]}")
            lines.append(f"    回复: {turn.response[:250]}")
            if turn.delegated_to:
                target_label = PERSONAS[turn.delegated_to]["label"]
                lines.append(f"    → 委托: {target_label}")
            lines.append("")

        return "\n".join(lines)

    def _status_text(self, event: AstrMessageEvent) -> str:
        """协作状态报告"""
        session_id = self._get_session_id(event)
        session = self._sessions.get(session_id)

        # Agent 状态
        agent_ok = self._agent is not None
        api_ok = self._agent is not None and bool(self._agent.api_key)
        model = self._agent.model if self._agent else "未知"

        # 会话统计
        active_count = sum(
            1 for s in self._sessions.values() if s.status == "active"
        )
        total_count = len(self._sessions)

        lines: list[str] = [
            "🌙🌅 双人格聊天室状态",
            "",
            f"  Agent:     {'✅ 就绪' if agent_ok else '❌ 未初始化'}",
            f"  API Key:   {'✅ 已配置' if api_ok else '❌ 未配置'}",
            f"  模型:      {model}",
            f"  会话数:    {active_count} 活跃 / {total_count} 总计",
            f"  最大轮次:  {self.config.get('max_internal_turns', 8)}",
            f"  自动审阅:  {'✅ 开启' if self.config.get('auto_review', False) else '关闭'}",
        ]

        if session:
            initial = (
                PERSONAS[session.initial_persona]["label"]
                if session.initial_persona
                else "无"
            )
            status_emoji = {
                "active": "🔄",
                "completed": "✅",
                "timeout": "⏰",
                "error": "❌",
                "idle": "💤",
            }.get(session.status, "❓")

            lines.extend([
                "",
                f"── 当前会话 ──",
                f"  发起人格:  {initial}",
                f"  状态:      {status_emoji} {session.status}",
                f"  内部轮次:  {len(session.turns)}/{session.max_turns}",
            ])

            if session.turns:
                delegate_chain = " → ".join(
                    f"{PERSONAS[t.persona_id]['emoji']}{PERSONAS[t.persona_id]['label']}"
                    for t in session.turns
                )
                lines.append(f"  链路:      {delegate_chain}")

        return "\n".join(lines)

    def _help_text(self) -> str:
        """帮助文本"""
        return (
            "🌙🌅 双人格聊天室 — 露娜大人 × 朝日娘\n"
            "\n"
            "用法:\n"
            "  /chatroom @露娜大人 <任务>   由露娜大人开始处理\n"
            "  /chatroom @朝日娘 <任务>     由朝日娘开始处理\n"
            "  /chatroom <任务>             默认由露娜大人开始\n"
            "  /chatroom status             查看协作状态\n"
            "  /chatroom history            查看对话历史\n"
            "  /chatroom reset              重置当前会话\n"
            "  /chatroom help               显示此帮助\n"
            "\n"
            "协作机制:\n"
            "  两位人格可以互相委托任务（在回复中 @对方），\n"
            "  形成内部多轮对话，最终汇总结果呈现给您。\n"
            "  内部对话最多进行 8 轮（可配置），避免死循环。\n"
            "\n"
            "示例:\n"
            "  /chatroom @露娜大人 设计一个 RESTful API\n"
            "  /chatroom @朝日娘 实现用户登录功能并编写测试\n"
            "  /chatroom 帮我写一个 Python 爬虫脚本"
        )
