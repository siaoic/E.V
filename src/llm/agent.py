"""ButlerAgent 记忆管家：基于 memU agentic 三入口的全新实现。

职责：
- submit_extract_and_store：把新对话轮次交给 LLM 提取值得长期记住的
  信息，经 memU commit_results 写入记忆存储（异步队列，不阻塞对话）
- summarize_session：对会话生成一段摘要（供归档）
- distill_session：把会话蒸馏成持久记忆条目列表（不自动写入）
- build_proactive_prompt：构造主动发言请求（时段语气 + 记忆线索注入）

模型配置遵循 .env 的 BUTLER_* 语义：留空则与主对话共用 LLM_* 服务。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any

from openai import AsyncOpenAI

from src.memory import memory
from src.utils import config, console

# 提取的系统提示（要求纯 JSON 输出 + 五元组结构化，便于图谱按实体着色）。
# 同时提取双方事实：对方（用户）的信息 + AI 自己表达的观点/承诺/自我认知，
# 保证「AI 的回复」也能进长期记忆（否则只记得用户说了什么）。
# 去重要求收敛同主体碎片：同一主体/话题的多条说法合并为一条完整记忆，
# 避免把一句话的不同转述拆成多条近似重复条目（用户反馈的历史问题）。
_EXTRACT_SYSTEM = (
    "你是长期记忆管家。从给定的对话轮次中，提取值得长期记住的实体事实：\n"
    "- 对方（用户）提到的人物、喜好、约定、情绪状态、关系细节；\n"
    "- AI 自己表达的观点、自我认知、承诺、偏好，以及讲述的关于自己的事。\n"
    "只保留事实（行为、关系、状态、偏好），过滤隐喻、假设、纯情感。\n"
    "AI 自己的事实 subject 用「助手」，对方的事实 subject 用「用户」。\n"
    "去重要求：\n"
    "  - 同一主体或同一话题的多条相关信息合并为一条完整记忆（保留全部细节），"
    "禁止拆成多条近似重复的条目；\n"
    "  - 同一件事的比喻、调侃、转述等不同说法只保留一条底层事实；\n"
    "  - 只提取有长期价值的事实（稳定偏好、重要关系、约定、身份、经历），"
    "临时情绪与纯修辞表达不提取；\n"
    "  - 输出前自查，剔除与其它条目表达高度重复的条目。\n"
    "输出 JSON 数组，每项包含：\n"
    "  name：简短主题（≤20字）\n"
    "  content：完整记忆内容（一句话）\n"
    "  subject：主体名称（没有则为空字符串）\n"
    "  subject_type：主体类型，取值 person/location/organization/item/concept/time/event/activity（没有则空）\n"
    "  predicate：谓词，即关系或行为描述（没有则空字符串）\n"
    "  object：客体名称（没有则为空字符串）\n"
    "  object_type：客体类型，取值同 subject_type（没有则空）\n"
    "  owner：归属者，只能是 self 或 user——self 表示这是 AI 自己的事"
    "（AI 的身份、偏好、拥有的东西、经历、关于自己的讲述），"
    "user 表示这是对方（用户/观众）或其相关的事物。由你根据事实真正的主人判断。\n"
    "没有值得记住的信息时输出 []。只输出 JSON，不要任何多余文字。"
)
_DISTILL_SYSTEM = (
    "你是长期记忆管家。请从这段会话中蒸馏出值得永久保留的条目，"
    "侧重于长期身份、稳定偏好、重要关系与约定。"
    "同一主体的多条相关信息合并为一条完整记忆，剔除近似重复的条目。"
    "输出 JSON 数组，每项包含 name（简短主题，≤20字）和 content（完整记忆内容）。"
    "没有值得蒸馏的内容时输出 []。只输出 JSON，不要任何多余文字。"
)
_SUMMARIZE_SYSTEM = (
    "你是会话记录员。请把这段对话压缩成结构化中文摘要，包含以下六个分区：\n"
    "1. 关键事实：对话中确认的事实性信息\n"
    "2. 用户偏好：对方的喜好与厌恶\n"
    "3. 重要决定：双方做过的约定或决定\n"
    "4. 待办事项：尚未完成或计划要做的事\n"
    "5. 背景信息：话题背景与上下文\n"
    "6. 最近状态：双方当前的情绪与状态\n"
    "每个分区以「【分区名】」开头，没有内容的区写「无」。"
    "输出纯文本，不要任何格式修饰。"
)

# 记忆提取后台 worker 数量（密集对话时并行消费，避免提取堆积）
_EXTRACT_WORKERS = 3

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

# 全角字符 → 半角（模型输出 JSON 时常见全角冒号/引号/括号导致解析失败）
_FULLWIDTH_MAP = str.maketrans({
    "，": ",", "：": ":", "；": ";", "？": "?", "！": "!", "。": ".",
    "“": "\"", "”": "\"", "‘": "'", "’": "'",
    "（": "(", "）": ")", "【": "[", "】": "]", "　": " ",
} | {chr(code): chr(code - 0xFEE0)
   for code in range(0xFF10, 0xFF1A)})  # 全角数字 ０-９ → 半角 0-9


def _extract_json_array(content: str) -> list | None:
    """容错解析 JSON 数组：直接解析失败后截取首个 [ 到末尾 ] 兜底。

    对标 NagaAgent json5 容错解析：模型输出混入前后缀文字时也能取到数组。
    """
    try:
        data = json.loads(content)
        return data if isinstance(data, list) else None
    except (json.JSONDecodeError, TypeError):
        pass
    start = content.find("[")
    if start < 0:
        return None
    end = content.rfind("]")
    if end <= start:
        return None
    try:
        data = json.loads(content[start:end + 1])
        return data if isinstance(data, list) else None
    except (json.JSONDecodeError, TypeError):
        return None


class ButlerAgent:
    """记忆提取 / 会话摘要 / 蒸馏 / 主动发言构造。"""

    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        self._model = ""
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        self._worker_tasks: list[asyncio.Task] = []

    # ---------- 客户端 ----------

    def _ensure_client(self) -> AsyncOpenAI | None:
        """按 BUTLER_*（留空回退 LLM_*）构建 OpenAI 兼容客户端。"""
        if self._client is None:
            base_url = config.cfg.BUTLER_BASE_URL or config.cfg.LLM_BASE_URL
            api_key = config.cfg.BUTLER_API_KEY or config.cfg.LLM_API_KEY
            self._model = config.cfg.BUTLER_MODEL or config.cfg.LLM_MODEL
            if not (base_url and api_key and self._model):
                return None
            self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        return self._client

    async def _complete(self, messages: list[dict], temperature: float):
        """调用管家模型：默认关闭思考（提取要干净 JSON，不被推理文本污染）；
        服务不支持 thinking 参数时降级重试；失败返回 None 由调用方静默跳过。
        """
        client = self._ensure_client()
        if client is None:
            return None
        try:
            try:
                return await client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    temperature=temperature,
                    extra_body={"thinking": {"type": "disabled"}},
                )
            except Exception:
                console.dim("[ButlerAgent] 服务不支持 thinking 参数，降级为普通模式")
                return await client.chat.completions.create(
                    model=self._model, messages=messages, temperature=temperature)
        except Exception as e:
            console.warn(f"[ButlerAgent] 模型调用失败：{e}")
            return None

    @staticmethod
    def _message_text(message) -> str:
        """取模型回复正文：部分推理模型 content 为空、正文在 reasoning_content，
        两者都读，保证提取/摘要不因空 content 而丢失（对标 llm_brain 的兜底）。"""
        content = (getattr(message, "content", None) or "").strip()
        if not content:
            content = (getattr(message, "reasoning_content", None) or "").strip()
        return content

    async def _chat_json(self, system: str, user_text: str) -> list[dict]:
        """调用管家模型并解析 JSON 数组结果（解析失败返回空列表）。"""
        response = await self._complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            temperature=0.2,
        )
        if response is None:
            return []
        content = self._message_text(response.choices[0].message)
        content = _JSON_FENCE_RE.sub("", content)
        content = content.translate(_FULLWIDTH_MAP)  # 全角字符标准化
        data = _extract_json_array(content)
        if data is None:
            return []
        return [
            {
                "name": str(item.get("name") or "").strip()[:64],
                "content": str(item.get("content") or "").strip(),
                "subject": str(item.get("subject") or "").strip()[:64],
                "subject_type": str(item.get("subject_type") or "").strip()[:24],
                "predicate": str(item.get("predicate") or "").strip()[:64],
                "object": str(item.get("object") or "").strip()[:64],
                "object_type": str(item.get("object_type") or "").strip()[:24],
                "owner": str(item.get("owner") or "").strip().lower()[:8],
            }
            for item in data
            if isinstance(item, dict) and (item.get("name") or item.get("content"))
        ]

    async def _chat_text(self, system: str, user_text: str) -> str:
        """调用管家模型并返回纯文本结果。"""
        response = await self._complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            temperature=0.4,
        )
        if response is None:
            return ""
        return self._message_text(response.choices[0].message).strip()

    @staticmethod
    def _turns_text(turns: list[dict] | None) -> str:
        """把对话轮次列表格式化为文本。"""
        if not turns:
            return ""
        return "\n".join(
            f"{t.get('role')}: {t.get('content')}"
            for t in turns if (t.get("content") or "").strip()
        )

    # ---------- 记忆提取（memU agentic 写入链路） ----------

    async def submit_extract_and_store(self, new_turns: list[dict], recent_turns: list[dict] | None) -> None:
        """把新轮次加入后台提取队列（立即返回；队列满则丢弃）。"""
        if not new_turns:
            return
        try:
            self._queue.put_nowait((new_turns, recent_turns))
        except asyncio.QueueFull:
            return
        # 确保有活着的 worker 在消费队列（全部退出后按 _EXTRACT_WORKERS 重建）
        if not self._worker_tasks or all(t.done() for t in self._worker_tasks):
            self._worker_tasks = [
                asyncio.create_task(self._run_worker())
                for _ in range(_EXTRACT_WORKERS)
            ]

    async def _run_worker(self) -> None:
        """消费队列执行提取与写入（多个 worker 并行，对标 NagaAgent 任务管理器）。"""
        while True:
            new_turns, recent_turns = await self._queue.get()
            try:
                await self.extract_and_store(new_turns, recent_turns)
            except Exception as e:
                console.warn(f"记忆提取失败：{e}")
            finally:
                self._queue.task_done()

    async def extract_and_store(self, new_turns: list[dict], recent_turns: list[dict] | None) -> None:
        """提取新轮次中的长期信息并写入 memU 存储。"""
        text = self._turns_text(new_turns)
        if not text:
            return
        entries = await self._chat_json(_EXTRACT_SYSTEM, text)
        if not entries:
            return
        owner = _pick_owner(recent_turns, new_turns)
        files = []
        for item in entries:
            triple = " ".join(x for x in (
                item.get("subject"), item.get("predicate"), item.get("object")) if x)
            # 有五元组 → description 存结构化三元组（图谱按 core/ 层着色）；
            # 无则回落 name。归属：AI 自己的事实记入 self，其余归对话推断的归属者
            description = (f"core/实体记忆：{triple}"
                           if triple else item["name"] or "记忆")
            files.append({
                "name": item["name"] or "记忆",
                "description": description,
                "content": item["content"],
                "user": _entry_owner(item, owner),
            })
        await memory.get_manager().commit_recall_files(files)

    # ---------- 会话摘要与蒸馏 ----------

    async def summarize_session(self, turns: list[dict]) -> str:
        """对会话生成一段中文摘要。"""
        text = self._turns_text(turns)
        if not text:
            return ""
        return await self._chat_text(_SUMMARIZE_SYSTEM, text)

    async def distill_session(self, turns: list[dict]) -> list[dict]:
        """把会话蒸馏为持久记忆条目列表（不自动写入）。"""
        text = self._turns_text(turns)
        if not text:
            return []
        entries = await self._chat_json(_DISTILL_SYSTEM, text)
        owner = _pick_owner(turns, None)
        return [
            {"name": item["name"], "description": item["name"], "content": item["content"], "user": owner}
            for item in entries
        ]

    # ---------- 主动发言构造 ----------

    def build_proactive_prompt(self, kind: str, topic: str, memory_context: str = "", hour: int | None = None) -> str:
        """构造主动发言请求（时段语气 + 类别结尾策略 + 记忆线索）。"""
        hour = hour if hour is not None else datetime.now().hour
        period = _period_phrase(hour)
        memory_hint = f"\n相关记忆线索：\n{memory_context}" if memory_context else ""
        if kind == "emotional":
            return (
                f"【安静时刻的自主行动 · {period}】房间里安静了好一会儿，孤独感仍在蔓延。"
                f"请以你的人设自然开口，像自言自语一样说一小段真诚的话，"
                f"不必等待对方回应，也尽量不要连珠炮式提问。"
                f"{memory_hint}"
            )
        if not topic:
            return ""
        return (
            f"【主动发起话题 · {period}】请以你的人设自然地开启一个话题：{topic}。"
            f"先说一两句引入，再邀请对方分享看法，语气保持你的人设风格。"
            f"{memory_hint}"
        )


def _pick_owner(recent_turns: list[dict] | None, new_turns: list[dict] | None) -> str:
    """从轮次中推断记忆归属者：优先最近一条非 AI 发言的 user；
    全部都是 AI 发言（如主动孤独倾诉）归属 AI 自己（self）。"""
    source = new_turns or recent_turns or []
    for turn in reversed(source):
        role = str(turn.get("role") or "").lower()
        if role not in ("assistant", "muika"):
            return str(turn.get("user") or memory._USER_DEFAULT)
    return memory._USER_SELF


# 记忆归属判定：优先采用提取模型显式标注的 owner（self/user，由模型判断
# 事实真正的主人），缺失或取值非法时回退对话轮次推断的归属者。
def _entry_owner(item: dict, fallback: str) -> str:
    """条目归属：模型标注 owner=self → AI 自己；owner=user → fallback。"""
    owner = (item.get("owner") or "").strip().lower()
    if owner == "self":
        return memory._USER_SELF
    return fallback


def _period_phrase(hour: int) -> str:
    """把小时映射为时段描述（用于主动发言的语气调节）。"""
    if 5 <= hour < 9:
        return "清晨"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 14:
        return "午后"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "深夜"


__all__ = ["ButlerAgent"]
