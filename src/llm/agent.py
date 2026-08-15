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

from tools.memory import memory
from src.utils import config, console
from src.utils.constants import ROLE_ASSISTANT, ROLE_AI_ALIAS

# 提取的系统提示（要求纯 JSON 输出 + 五元组结构化，便于图谱按实体着色）。
# 同时提取双方事实：对方（用户）的信息 + AI 自己表达的观点/承诺/自我认知，
# 保证「AI 的回复」也能进长期记忆（否则只记得用户说了什么）。
# 去重要求收敛同主体碎片：同一主体/话题的多条说法合并为一条完整记忆，
# 避免把一句话的不同转述拆成多条近似重复条目（用户反馈的历史问题）。
_EXTRACT_SYSTEM = """你是长期记忆管家。从给定的对话轮次中提取值得长期记住的实体事实。

【提取范围】
- 用户侧：人物、喜好、约定、情绪状态、关系细节
- AI侧：自我认知、承诺、偏好、个人经历讲述

【角色区分】（说话人是谁，决定 subject 与 owner）
- 「观众: [弹幕@xxx] ...」是观众发言：subject 填具体观众名（从 [弹幕@ 中提取，如「蓝奶」），不要笼统写「观众」
- 「主播: ...」是主播（用户）本人发言：subject 填「主播」，归属 user，不要归为「观众」
- 「AI: ...」是 AI 自己发言：subject 填「AI」，owner 填 self

【过滤原则】
- 保留：稳定偏好、重要关系、约定、身份、经历、持久状态
- 过滤：纯情感宣泄、临时修辞、假设性内容、隐喻性表达

【合并规则】
- 同一主体或同一话题的多条信息合并为一条完整记忆
- 同一事实的不同表述只保留一条底层事实
- 输出前自检，剔除与其它条目高度重复者

【输出格式】
JSON数组，每项包含：
| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 简短主题，≤20字 |
| content | string | 完整记忆，一句话 |
| subject | string | 主体名称，无则空 |
| subject_type | enum | person/location/organization/item/concept/time/event/activity，无则空 |
| predicate | string | 关系或行为描述，无则空 |
| object | string | 客体名称，无则空 |
| object_type | enum | 同 subject_type，无则空 |
| owner | enum | self=AI自身事实，user=用户侧事实，由事实归属判断 |

无值得记录信息时输出 []。
只输出 JSON，不要任何额外文字。"""
_DISTILL_SYSTEM = """你是长期记忆管家。从会话中蒸馏出值得永久保留的条目。

【保留标准】
- 长期身份信息
- 稳定偏好
- 重要关系
- 约定与承诺

【角色区分】（说话人是谁，决定 subject 与 owner）
- 观众发言（「观众: [弹幕@xxx] ...」）：subject 填具体观众名（从 [弹幕@ 中提取，如「蓝奶」），不要笼统写「观众」
- 主播发言（「主播: ...」）：subject 填「主播」，owner 填 user
- AI 发言（「AI: ...」）：subject 填「AI」，owner 填 self

【合并规则】
- 同一主体（同一观众/主播/AI）的多条信息合并为一条完整记忆
- 不同主体的条目分开输出，不要合并，各自保留自己的 subject
- 剔除近似重复条目

【输出格式】
JSON数组，每项包含：
- name：简短主题，≤20字
- content：完整记忆，一句话
- subject：具体主体名（具体观众名/主播/AI），无则空
- owner：self 或 user（事实归属方，由说话人判断）

无内容时输出 []。
只输出 JSON，不要任何额外文字。"""
# 整库整合蒸馏的系统提示（AI 自动蒸馏记忆库碎片用）：一次性处理多批旧碎片，
# 合并同主题、剔除近似重复。整合质量要求高（成功后会删除原碎片），因此走
# 主模型（LLM_* 配置）而非 BUTLER 管家模型——Qwen2.5-7B 曾出现 JSON 缺逗号/
# 幻觉导致整合失败的历史问题。要求单行紧凑 JSON（防 token 截断）。
_INTEGRATE_SYSTEM = """你是长期记忆整理管家。以下是记忆库中的部分碎片条目（按行编号），从历史对话中提取，存在重复、近似、主题分散问题。

每条以「序号. [归属者] 内容」给出，[] 内是该条目的归属者（具体观众名/主播/AI/self）。

【整合任务】
1. 同一主体/同一话题的多条合并为一条完整记忆，保留全部关键细节
2. 剔除与其他条目高度重复的条目
3. 保留有长期价值的事实：身份、偏好、关系、约定、经历、稳定状态
4. 过滤：纯情绪、临时修辞

【归属规则】
- 不同归属者（不同观众/主播/AI）的条目不要合并，分别输出，各自保留 subject
- subject 填具体归属者：观众填具体观众名（如「蓝奶」），主播填「主播」，AI 填「AI」

【输出要求】
JSON数组，元素间用逗号分隔。每项含：
- name：简短主题，≤20字，不加句号
- content：完整记忆，一句话，保留关键细节
- subject：具体主体名（具体观众名/主播/AI），无则空
- owner：self（AI自身）或 user（用户侧）

【格式约束】
- 单行紧凑JSON数组，无换行、无缩进
- 元素间用逗号分隔

示例：
输入：
1. [蓝奶] 观众蓝奶喜欢喝牛奶
2. [chao] 主播喜欢收集蓝箱子

输出：
[{"name":"蓝奶喜好","content":"观众蓝奶喜欢喝牛奶。","subject":"蓝奶","owner":"user"},{"name":"主播喜好","content":"主播喜欢收集蓝箱子。","subject":"主播","owner":"user"}]

只输出 JSON 数组，不要任何额外文字。"""
_SUMMARIZE_SYSTEM = (
"""你是会话记录员。将对话压缩为结构化中文摘要。

【输出结构】
【关键事实】对话中确认的事实性信息
【用户偏好】对方喜好与厌恶
【重要决定】双方约定或决定
【待办事项】未完成或计划之事
【背景信息】话题背景与上下文
【最近状态】双方当前情绪与状态

【规则】
- 无内容的区直接写「无」
- 纯文本输出，无任何格式修饰（无 Markdown、无加粗）"""
)

# 记忆提取后台 worker 数量（密集对话时并行消费，避免提取堆积）
_EXTRACT_WORKERS = 3

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

# 全角字符 → 半角（模型输出 JSON 时常见全角冒号/引号/括号导致解析失败）
_FULLWIDTH_MAP = str.maketrans(
    {
        "，": ",",
        "：": ":",
        "；": ";",
        "？": "?",
        "！": "!",
        "。": ".",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "　": " ",
    }
    | {chr(code): chr(code - 0xFEE0) for code in range(0xFF10, 0xFF1A)}
)  # 全角数字 ０-９ → 半角 0-9


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
        data = json.loads(content[start : end + 1])
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
            # 明确超时：记忆提取/摘要等后台任务不设限会无限等待（agent 后台
            # 静默卡死），超时后放弃本次调用由调用方静默跳过
            self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=45.0)
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
                return await asyncio.wait_for(
                    client.chat.completions.create(
                        model=self._model,
                        messages=messages,
                        temperature=temperature,
                        extra_body={"thinking": {"type": "disabled"}},
                    ),
                    timeout=45.0,
                )
            except Exception:
                console.dim("[ButlerAgent] 服务不支持 thinking 参数，降级为普通模式")
                return await asyncio.wait_for(
                    client.chat.completions.create(
                        model=self._model, messages=messages, temperature=temperature
                    ),
                    timeout=45.0,
                )
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
        """把对话轮次列表格式化为文本（区分弹幕/主播/AI 三方角色）。"""
        return memory.format_turns_text(turns)

    # ---------- 记忆提取（memU agentic 写入链路） ----------

    async def submit_extract_and_store(
        self, new_turns: list[dict], recent_turns: list[dict] | None
    ) -> None:
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
                asyncio.create_task(self._run_worker()) for _ in range(_EXTRACT_WORKERS)
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

    async def extract_and_store(
        self, new_turns: list[dict], recent_turns: list[dict] | None
    ) -> None:
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
            triple = " ".join(
                x
                for x in (
                    item.get("subject"),
                    item.get("predicate"),
                    item.get("object"),
                )
                if x
            )
            # 有五元组 → description 存结构化三元组（图谱按 core/ 层着色）；
            # 无则回落 name。归属：AI 自己的事实记入 self，其余归对话推断的归属者
            description = (
                f"core/实体记忆：{triple}" if triple else item["name"] or "记忆"
            )
            files.append(
                {
                    "name": item["name"] or "记忆",
                    "description": description,
                    "content": item["content"],
                    "user": _entry_owner(item, owner),
                }
            )
        await memory.get_manager().commit_recall_files(files)

    # ---------- 会话摘要与蒸馏 ----------

    async def summarize_session(self, turns: list[dict]) -> str:
        """对会话生成一段中文摘要。"""
        text = self._turns_text(turns)
        if not text:
            return ""
        return await self._chat_text(_SUMMARIZE_SYSTEM, text)

    async def distill_session(self, turns: list[dict]) -> list[dict]:
        """把会话蒸馏为持久记忆条目列表（不自动写入）。

        每条按模型标注的 subject 归属（具体观众名/主播/AI），不再整批
        归为同一个用户——会话里主播、多个观众、AI 的事实各归各的角色。
        """
        text = self._turns_text(turns)
        if not text:
            return []
        entries = await self._chat_json(_DISTILL_SYSTEM, text)
        fallback = _pick_owner(turns, None)
        return [
            {
                "name": item["name"],
                "description": item["name"],
                "content": item["content"],
                "user": _entry_user(item, fallback),
            }
            for item in entries
        ]

    async def integrate_memories(
        self, files: list[dict], batch: int = 15
    ) -> list[dict] | None:
        """整合蒸馏整库记忆碎片（AI 自己蒸馏 + 删除旧条目的前置步骤）。

        与 distill_session（单次会话）不同：本方法一次性处理存量记忆库，
        合并同主题碎片、剔除近似重复，输出可直接 commit_recall_files 的新条目。
        任一批连续解析失败则返回 None（调用方据此保留原记忆、不删除）。

        用主模型（LLM_* 配置）单独建客户端：整库整合质量要求高（成功后会
        删除原碎片），BUTLER 管家模型（Qwen2.5-7B）曾出现 JSON 缺逗号/幻觉
        导致蒸馏失败的历史问题。要求单行紧凑 JSON（防 max_tokens 截断）。
        """
        if not files:
            return []
        base_url = (config.cfg.LLM_BASE_URL or "").strip()
        api_key = (config.cfg.LLM_API_KEY or "").strip()
        model = (config.cfg.LLM_MODEL or "").strip()
        if not (base_url and api_key and model):
            console.warn("[ButlerAgent] 记忆整合：主模型未配置，跳过")
            return None
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=60.0)
        recs: list[dict] = []
        batches = [files[i : i + batch] for i in range(0, len(files), batch)]
        for part in batches:
            # 每条带 [归属者] 前缀，模型据此保留具体观众名/主播/AI 的独立归属
            user_text = "\n".join(
                f"{i + 1}. [{f.get('user') or '?'}] {f.get('content')}"
                for i, f in enumerate(part)
            )
            # 解析失败时带提示重试一次（常见于输出被截断 / JSON 语法错误）
            data: list | None = None
            for attempt in (1, 2):
                retry_hint = (
                    "注意：上次输出无法解析为合法 JSON 数组，请只输出一个合法的 "
                    "JSON 数组，元素间用逗号分隔，不要截断。"
                    if attempt == 2
                    else ""
                )
                try:
                    resp = await asyncio.wait_for(
                        client.chat.completions.create(
                            model=model,
                            messages=[
                                {"role": "system", "content": _INTEGRATE_SYSTEM},
                                {
                                    "role": "user",
                                    "content": user_text + (retry_hint or ""),
                                },
                            ],
                            temperature=0.2,
                            max_tokens=4096,
                        ),
                        timeout=90.0,
                    )
                except Exception as e:
                    console.warn(f"[ButlerAgent] 记忆整合调用失败：{e}")
                    break
                content = _JSON_FENCE_RE.sub("", resp.choices[0].message.content or "")
                content = content.translate(_FULLWIDTH_MAP)
                data = _extract_json_array(content)
                if data:
                    break
            clean = [
                e
                for e in (data or [])
                if isinstance(e, dict) and e.get("name") and e.get("content")
            ]
            if not clean:
                console.warn("[ButlerAgent] 记忆整合某批失败，保留原记忆（不删除）")
                return None
            for e in clean:
                recs.append(
                    {
                        "name": str(e["name"]).strip()[:64],
                        "description": str(e["name"]).strip()[:64],
                        "content": str(e["content"]).strip(),
                        "user": _entry_user(e, memory._USER_DEFAULT),
                    }
                )
        return recs

    # ---------- 视觉描述（图片直接交给 agent 处理） ----------

    async def describe_image(self, image_b64: str, prompt: str = "") -> str:
        """把图片（base64）交给视觉模型描述，返回描述文本。

        优先用主模型（LLM_* 配置）描述画面；主模型不支持图片输入时依次
        回退 BUTLER_* 等其他视觉服务（默认 glm-4v-flash，免费支持图片输入）。
        统一在 agent 侧构建客户端与超时兜底，视觉场景无需主对话介入；
        全部失败返回空串由调用方兜底。
        """
        prompt = prompt or "用简洁自然的中文描述这张图片的内容。"
        candidates = self._vision_candidates()
        if not candidates:
            console.warn("[ButlerAgent] 视觉模型未配置，无法描述图片")
            return ""
        # 依次尝试：主模型优先，失败（不支持图片输入等）时回退下一候选
        for base_url, api_key, model in candidates:
            client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=60.0)
            try:
                resp = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model,
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:image/jpeg;base64,{image_b64}"
                                        },
                                    },
                                ],
                            }
                        ],
                        max_tokens=1024,
                        temperature=0.4,
                    ),
                    timeout=60.0,
                )
            except Exception as e:
                console.warn(
                    f"[ButlerAgent] 视觉模型 {model} 调用失败，尝试下一候选：{e}")
                continue
            return (resp.choices[0].message.content or "").strip()
        console.warn("[ButlerAgent] 视觉模型全部调用失败，无法描述图片")
        return ""

    @staticmethod
    def _vision_candidates() -> list[tuple[str, str, str]]:
        """视觉候选服务列表：主模型（LLM_*）优先，主模型不支持图片时
        依次回退 BUTLER_*（默认 glm-4v-flash），去重相同服务。"""
        candidates: list[tuple[str, str, str]] = []
        main = (
            (config.cfg.LLM_BASE_URL or "").strip(),
            (config.cfg.LLM_API_KEY or "").strip(),
            (config.cfg.LLM_MODEL or "").strip(),
        )
        if all(main):
            candidates.append(main)
        fallback = (
            (config.cfg.BUTLER_BASE_URL or config.cfg.LLM_BASE_URL or "").strip(),
            (config.cfg.BUTLER_API_KEY or config.cfg.LLM_API_KEY or "").strip(),
            (config.cfg.BUTLER_MODEL or "glm-4v-flash").strip(),
        )
        if all(fallback) and fallback not in candidates:
            candidates.append(fallback)
        return candidates

    # ---------- 主动发言构造 ----------

    def build_proactive_prompt(
        self, topic: str = "", memory_context: str = "", hour: int | None = None
    ) -> str:
        """构造主动发言请求（LLM 自主决定：想说就说，不想说保持沉默）。

        topic：可选灵感话题（concept 文本）；主模型可顺着聊，也可自由发挥。
        模型输出 <SILENT> 表示此刻不想说，否则输出的内容即主动发言。
        """
        hour = hour if hour is not None else datetime.now().hour
        period = _period_phrase(hour)
        memory_hint = f"\n相关记忆线索：\n{memory_context}" if memory_context else ""
        topic_hint = (f"可以顺着这个灵感话题聊：{topic}"
                      if topic else "也可以自己决定想聊什么")
        return (
            f"【安静时刻 · 自主开口机会 · {period}】直播间暂时没有人说话，"
            f"现在是你的自由时间。由你决定此刻想不想开口：\n"
            f"1. 保持沉默（只输出 <SILENT>，不带任何其他文字）\n"
            f"2. 说点此刻心里想说的话（感受、感慨、观察、吐槽，"
            f"像自言自语一样简短真诚，不必等待回应）\n"
            f"3. 主动开启一个话题（{topic_hint}）\n\n"
            f"要求：想说就只输出最终要说的话（1~3 句，符合人设，简短自然），"
            f"不要输出编号、标记或解释；不想说就只输出 <SILENT>。"
            f"{memory_hint}"
        )


def _pick_owner(recent_turns: list[dict] | None, new_turns: list[dict] | None) -> str:
    """从轮次中推断记忆归属者：优先最近一条非 AI 发言的归属者。

    弹幕轮次按 [弹幕@用户名] 前缀归属其观众（区分角色，避免弹幕事实
    落到主播名下）；全部都是 AI 发言（如主动孤独倾诉）归属 AI 自己（self）。
    """
    source = new_turns or recent_turns or []
    for turn in reversed(source):
        role = str(turn.get("role") or "").lower()
        if role not in (ROLE_ASSISTANT, ROLE_AI_ALIAS):
            content = str(turn.get("content") or "")
            m = re.match(r"^\[弹幕@([^\]]+)\]", content)
            if m:
                return m.group(1).strip()[:32] or memory._USER_DEFAULT
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


# 蒸馏/整合条目的归属者判定：owner=self → AI 自己；否则优先模型标注的
# subject（蒸馏 prompt 要求按「观众: [弹幕@名]」给出具体观众名/主播/AI），
# 排除笼统词（观众/主播/用户/AI 等）避免角色塌缩，缺失才回退推断归属者。
_ABSTRACT_SUBJECTS = {"观众", "主播", "用户", "AI", "self", "我", "他", "她"}
def _entry_user(item: dict, fallback: str) -> str:
    """蒸馏/整合条目归属者：AI 自己的事实归 self，其余优先具体主体名。"""
    owner = (item.get("owner") or "").strip().lower()
    if owner == "self":
        return memory._USER_SELF
    subject = str(item.get("subject") or "").strip()[:32]
    if subject and subject not in _ABSTRACT_SUBJECTS:
        return subject
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
