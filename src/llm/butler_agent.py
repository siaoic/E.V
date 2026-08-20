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
import os
import re
from datetime import datetime
from typing import Any

from openai import AsyncOpenAI

from src.llm.client.factory import (
    build_thinking_extra_body,
    get_async_openai_client,
)
from src.llm.utils.jsonutil import parse_json_array
from src.llm.memory.lore_guard import is_lore_leak
from tools.memory import memory
from src.utils import config, console
from src.utils.constants import ROLE_ASSISTANT, ROLE_AI_ALIAS

# AI 名字（.env AI_NAME）：配置后提到 AI 自己一律用名字（如 neuro），
# 不再出现「AI 喜欢…」这类无名字表述（图谱/列表也按此名显示）
_AI_NAME = (os.getenv("AI_NAME") or "").strip()
_AI_REF = f"「{_AI_NAME}」" if _AI_NAME else "「AI」"   # 行内引用写法
_AI_SUBJECT = _AI_NAME if _AI_NAME else "AI"           # 归属者字段写法

# 共享子段：说话人判定 + 归属/合并规则（提取/蒸馏/整合统一语义）。
# AI 归属名动态化：配置 AI_NAME 后 subject/name 用名字（如 neuro），
# owner 保持 self（内部分组标识，图谱按它聚到 AI 名下）
_SUBJECT_OWNER_RULES = f"""【说话人判定】（决定 subject 与 owner）
- 「观众: [弹幕@xxx] ...」→ subject 填具体观众名（从 [弹幕@ 中提取，如「蓝奶」），owner=user
- 「主播: ...」→ subject=「主播」，owner=user
- 「{_AI_REF}: ...」→ subject={_AI_REF}，owner=self

【归属与合并】
- subject = 说话人/归属者，决定记忆记在谁名下
- owner = 事实大分类：self=AI 自身事实，user=用户侧事实（观众或主播）
- 不同归属者（不同观众/主播/{_AI_SUBJECT}）的条目不要合并，各自保留 subject
- 观众发言必须给出具体观众名，禁止用「观众/他/她」等笼统词代替
- 同一归属者的多条信息合并为一条完整记忆；剔除与其它条目高度重复者"""

_AI_NAMING_RULE = (
    f"\n【AI 自称】提到 AI 自己（自我认知/偏好/承诺）时一律用名字「{_AI_NAME}」"
    f"表述（如「{_AI_NAME} 喜欢收集蓝箱子」）。name/content/subject 三处"
    f"都禁止出现「AI」字样，全部用「{_AI_NAME}」"
    if _AI_NAME
    else ""
)

# 共享子段：五元组结构化输出 schema（用于 EXTRACT，8 字段）
_TRIPLE_OUTPUT_SCHEMA = f"""【输出格式】JSON 数组，每项字段：
- name：简短主题，≤20字（提到 AI 自己时含名字{_AI_REF}，禁止用「AI」）
- content：完整记忆，一句话
- subject：说话人/归属者（具体观众名/主播/{_AI_SUBJECT}），观众发言必填观众名，无则空
- subject_type：person / location / organization / item / concept / time / event / activity，无则空
- predicate：关系或行为描述，无则空
- object：客体名称，无则空
- object_type：同 subject_type，无则空
- owner：self=AI 自身事实，user=用户侧事实（由说话人判断）"""

# 共享子段：基础输出 schema（用于 DISTILL/INTEGRATE，4 字段）
_BASIC_OUTPUT_SCHEMA = f"""【输出格式】JSON 数组，每项字段：
- name：简短主题，≤20字（提到 AI 自己时含名字{_AI_REF}，禁止用「AI」）
- content：完整记忆，一句话
- subject：说话人/归属者（具体观众名/主播/{_AI_SUBJECT}），观众发言必填观众名，无则空
- owner：self=AI 自身事实，user=用户侧事实（由说话人判断）"""

# 提取的系统提示（要求纯 JSON 输出 + 五元组结构化，便于图谱按实体着色）。
# 同时提取双方事实：对方（用户）的信息 + AI 自己表达的观点/承诺/自我认知，
# 保证「AI 的回复」也能进长期记忆（否则只记得用户说了什么）。
# 去重要求收敛同主体碎片：同一主体/话题的多条说法合并为一条完整记忆，
# 避免把一句话的不同转述拆成多条近似重复条目（用户反馈的历史问题）。
_EXTRACT_SYSTEM = f"""你是长期记忆管家。从对话轮次中提取值得长期记住的实体事实。

【提取范围】
- 用户侧：人物、喜好、约定、情绪状态、关系细节
- AI 侧：自我认知、承诺、偏好、个人经历

【过滤原则】
- 保留：稳定偏好、重要关系、约定、身份、经历、持久状态
- 过滤：纯情绪宣泄、临时修辞、假设性内容、隐喻性表达

{_SUBJECT_OWNER_RULES}

{_AI_NAMING_RULE}

{_TRIPLE_OUTPUT_SCHEMA}

无值得记录信息时输出 []。只输出 JSON 数组，不要任何额外文字。"""

_DISTILL_SYSTEM = f"""你是长期记忆管家。从会话中蒸馏值得永久保留的条目。

【保留标准】长期身份信息 / 稳定偏好 / 重要关系 / 约定与承诺

{_SUBJECT_OWNER_RULES}

{_AI_NAMING_RULE}

{_BASIC_OUTPUT_SCHEMA}

无内容时输出 []。只输出 JSON 数组，不要任何额外文字。"""

# 整库整合蒸馏的系统提示（AI 自动蒸馏记忆库碎片用）：一次性处理多批旧碎片，
# 合并同主题、剔除近似重复。整合质量要求高（成功后会删除原碎片），因此走
# 主模型（LLM_* 配置）而非 BUTLER 管家模型——Qwen2.5-7B 曾出现 JSON 缺逗号/
# 幻觉导致整合失败的历史问题。要求单行紧凑 JSON（防 token 截断）。
_INTEGRATE_SYSTEM = f"""你是长期记忆整理管家。处理记忆库中的碎片条目（按行编号），存在重复/近似/分散问题。

每条以「序号. [归属者] 内容」给出，[] 内是归属者（具体观众名/主播/{_AI_SUBJECT}/self）。

【整合任务】
1. 同一主体/话题的多条合并为一条完整记忆，保留全部关键细节
2. 剔除与其他条目高度重复的条目
3. 保留长期价值：身份、偏好、关系、约定、经历、稳定状态
4. 过滤：纯情绪、临时修辞

{_SUBJECT_OWNER_RULES}

{_AI_NAMING_RULE}

{_BASIC_OUTPUT_SCHEMA}

【格式约束】单行紧凑 JSON 数组，无换行无缩进，元素间用逗号分隔

【示例】
输入：
1. [蓝奶] 观众蓝奶喜欢喝牛奶
2. [chao] 主播喜欢收集蓝箱子
输出：
[{{"name":"蓝奶喜好","content":"观众蓝奶喜欢喝牛奶。","subject":"蓝奶","owner":"user"}},{{"name":"主播喜好","content":"主播喜欢收集蓝箱子。","subject":"主播","owner":"user"}}]

只输出 JSON 数组，不要任何额外文字。"""

_SUMMARIZE_SYSTEM = """你是会话记录员。把对话压缩为 6 段中文摘要（无内容直接写「无」）。

【关键事实】对话中确认的事实性信息
【用户偏好】对方喜好与厌恶
【重要决定】双方约定或决定
【待办事项】未完成或计划之事
【背景信息】话题背景与上下文
【最近状态】双方当前情绪与状态

纯文本输出，无任何格式修饰（无 Markdown、无加粗）。"""

# 主动发言 prompt 模板（{period} 时段、{topic_hint} 话题、{memory_hint} 记忆线索）
# 模型输出 <SILENT> 表示此刻不想说，否则输出即主动发言
_PROACTIVE_PROMPT_TEMPLATE = (
    "【安静时刻 · 自主开口机会 · {period}】直播间暂时没有人说话，"
    "现在是你的自由时间。由你决定此刻想不想开口：\n"
    "1. 保持沉默（只输出 <SILENT>，不带任何其他文字）\n"
    "2. 说点此刻心里想说的话（感受、感慨、观察、吐槽，"
    "像自言自语一样简短真诚，不必等待回应）\n"
    "3. 主动开启一个话题（{topic_hint}）\n\n"
    "要求：想说就只输出最终要说的话（1~3 句，符合人设，简短自然），"
    "不要输出编号、标记或解释；不想说就只输出 <SILENT>。{memory_hint}"
)

# 记忆提取后台 worker 数量（密集对话时并行消费，避免提取堆积）
_EXTRACT_WORKERS = 3


class ButlerAgent:
    """记忆提取 / 会话摘要 / 蒸馏 / 主动发言构造。"""

    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        self._model = ""
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        self._worker_tasks: list[asyncio.Task] = []
        # 实时强信号捕获：待入库条目 + 后台消费任务（合并小批写入）
        self._instant_pending: list[dict] = []
        self._instant_task: asyncio.Task | None = None

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
            self._client = get_async_openai_client(
                api_key=api_key, base_url=base_url, timeout=45.0)
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
                        extra_body=build_thinking_extra_body(False),
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
        data = parse_json_array(content)
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
        # 实时强信号捕获：正则直接命中稳定事实即后台立即入库，不等 LLM 批量
        # 提取——直播高并发下批量提取可能延迟/丢队，强信号事实不值得等待。
        self._submit_instant_capture(new_turns)
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
                    "user": _entry_user(item, owner),
                }
            )
        await memory.get_manager().commit_recall_files(files)

    # ---------- 实时强信号捕获（正则先行，不等 LLM 批量提取） ----------

    def _submit_instant_capture(self, new_turns: list[dict]) -> None:
        """正则命中稳定事实即后台立即入库（异步调度，不阻塞调用方）。

        批量提取（extract_and_store）依赖 LLM 且走队列，直播高并发时可能
        延迟/丢队；明确喜好/关系/年龄等强信号事实不值得等待，这里用确定性
        规则先行捕获。写入仍走 commit_recall_files（精确/语义去重 + 判决链），
        配合 lore_guard 预筛防误写。
        """
        entries = _instant_memory_entries(new_turns)
        if not entries:
            return
        self._instant_pending.extend(entries)
        if self._instant_task is None or self._instant_task.done():
            self._instant_task = asyncio.create_task(self._commit_instant())

    async def _commit_instant(self) -> None:
        """消费待入库的实时捕获条目（合并小批写入，失败静默跳过）。"""
        while True:
            batch, self._instant_pending = (
                self._instant_pending[:16], self._instant_pending[16:])
            if not batch:
                break
            try:
                await memory.get_manager().commit_recall_files(batch)
            except Exception as e:
                console.warn(f"[ButlerAgent] 实时记忆捕获失败：{e}")

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
        client = get_async_openai_client(api_key=api_key, base_url=base_url, timeout=60.0)
        recs: list[dict] = []
        batches = [files[i : i + batch] for i in range(0, len(files), batch)]
        for part in batches:
            # 该批输入中多数归属者作为回退（避免整合把具体观众名塌缩成默认用户）
            part_owners = [str(f.get("user") or memory._USER_DEFAULT) for f in part]
            fallback = (max(set(part_owners), key=part_owners.count)
                        if part_owners else memory._USER_DEFAULT)
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
                content = parse_json_array(resp.choices[0].message.content or "")
                if content:
                    break
            clean = [
                e
                for e in (content or [])
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
                        "user": _entry_user(e, fallback),
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
            client = get_async_openai_client(
                api_key=api_key, base_url=base_url, timeout=60.0)
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
        return _PROACTIVE_PROMPT_TEMPLATE.format(
            period=period, topic_hint=topic_hint, memory_hint=memory_hint)


# AI 发言的 role 取值：内部轮次用 ROLE_ASSISTANT/ROLE_AI_ALIAS（如 "Neuro"/"Neuro sama"），
# 而提取/蒸馏外部构造的轮次用字面量 "assistant"，统一转小写后对比，避免误把 AI 回复
# 当作观众/主播轮次，导致记忆归属全部塌缩成默认用户。
_AI_ROLES = {ROLE_ASSISTANT.lower(), ROLE_AI_ALIAS.lower(), "assistant", "ai"}


# ---------- 实时强信号捕获（正则先行） ----------
#
# 弱宾语：代词/泛指词（喜欢这个/那个人）不算稳定事实，命中即跳过；
# 这类表达由 LLM 批量提取自行判断是否值得记录。
_WEAK_OBJS = {
    "这个", "那个", "这些", "那些", "这样", "那样", "这里", "那里",
    "这个人", "那个人", "你", "你们", "他", "她", "它", "他们", "她们",
    "大家", "自己", "上", "一下", "人", "事", "东西", "玩",
}

# 稳定事实捕获规则（确定性，不依赖 LLM）。每组：
# (正则, 主题名, content 模板, 三元组模板)；正则 obj 组 = 事实主体，
# rel 组（仅关系规则） = 亲属/称谓。正则以「我/我的」口吻出现在弹幕或
# 主播发言中；「我喜欢」前置 (?<!不) 负向断言防止「我不喜欢」误捕获为喜好。
_INSTANT_PATTERNS: tuple[tuple[re.Pattern, str, str, str], ...] = (
    (
        re.compile(
            r"(?<!不)(?:我最喜欢|我超喜欢|我好喜欢|我很喜欢|我特别喜欢|"
            r"我喜欢|我超爱|我爱吃|我爱喝|最爱|我爱|超爱)"
            r"\s*(?P<obj>[^，。！？!?；;、\s]{1,24})"
        ),
        "喜好", "{subject}喜欢{obj}", "{subject} 喜欢 {obj}",
    ),
    (
        re.compile(
            r"(?:我不太喜欢|我不喜欢|我讨厌|我不爱|我不吃)"
            r"\s*(?P<obj>[^，。！？!?；;、\s]{1,24})"
        ),
        "厌恶", "{subject}不喜欢{obj}", "{subject} 不喜欢 {obj}",
    ),
    (
        re.compile(
            r"(?P<obj>[^，。！？!?；;、\s]{1,16})\s*是我"
            r"(?P<rel>姐姐|妹妹|哥哥|弟弟|老婆|老公|女朋友|男朋友|对象|"
            r"室友|闺蜜|兄弟|师傅|师父|同学|朋友)"
        ),
        "关系", "{subject}的{rel}是{obj}", "{obj} 是 {subject} 的 {rel}",
    ),
    (
        re.compile(r"我今年?\s*(?P<obj>\d{1,3})\s*岁"),
        "年龄", "{subject}今年{obj}岁", "{subject} 年龄 {obj}岁",
    ),
    (
        re.compile(r"我(?:住在|住)\s*(?P<obj>[^，。！？!?；;、\s]{1,12})"),
        "所在地", "{subject}住在{obj}", "{subject} 住在 {obj}",
    ),
    (
        re.compile(
            r"我在(?P<obj>[^，。！？!?；;、\s]{1,12})(?:工作|上班|上学|读书|生活)"
        ),
        "所在地", "{subject}在{obj}工作/上学", "{subject} 在 {obj} 工作/上学",
    ),
    (
        re.compile(r"我养了(?:一只|一条|只|条)?\s*(?P<obj>[^，。！？!?；;、\s]{1,12})"),
        "宠物", "{subject}养了{obj}", "{subject} 养了 {obj}",
    ),
    (
        re.compile(r"我(?:的)?生日(?:是|在)?\s*(?P<obj>[^，。！？!?；;、\s]{1,16})"),
        "生日", "{subject}的生日是{obj}", "{subject} 生日 {obj}",
    ),
    (
        re.compile(r"(?:我叫|我的名字叫|本名|名字叫)\s*(?P<obj>[^，。！？!?；;、\s]{1,12})"),
        "名字", "{subject}的名字是{obj}", "{subject} 名字 {obj}",
    ),
)


def _split_turn(turn: dict) -> tuple[str, str]:
    """从轮次提取（归属者, 正文）：弹幕按 [弹幕@名] 归属其观众，其余非 AI
    轮次归主播；AI 自述轮次返回 (self, "") 由调用方跳过（走 LLM 批量提取）。"""
    role = str(turn.get("role") or "").lower()
    if role in _AI_ROLES:
        return (memory._USER_SELF, "")
    content = str(turn.get("content") or "").strip()
    if not content:
        return (memory._USER_DEFAULT, "")
    m = re.match(r"^\[弹幕@([^\]]+)\]\s*(.*)$", content, re.S)
    if m:
        return (m.group(1).strip()[:32] or memory._USER_DEFAULT, m.group(2).strip())
    return (memory._USER_DEFAULT, content)


def _instant_memory_entries(turns: list[dict]) -> list[dict]:
    """从轮次文本中正则捕获稳定事实（仅用户侧；AI 自述走批量提取）。

    返回可直接 commit_recall_files 的条目；命中 lore 词库（世界观讨论）或
    弱宾语（这个/那个等）跳过；一条轮次只取首个命中模式，避免同句多条碎片。
    """
    entries: list[dict] = []
    for turn in turns:
        subject, body = _split_turn(turn)
        if not body or is_lore_leak(body):
            continue
        for pattern, kind, content_tpl, triple_tpl in _INSTANT_PATTERNS:
            m = pattern.search(body)
            if not m:
                continue
            obj = (m.group("obj") or "").strip()
            obj = re.sub(r"[吗呢啊吧]$", "", obj)  # 剥句末语气词（喜欢猫吗→猫）
            if not obj or obj in _WEAK_OBJS:
                continue
            rel = m.groupdict().get("rel") or ""
            content = content_tpl.format(subject=subject, obj=obj, rel=rel)
            triple = triple_tpl.format(subject=subject, obj=obj, rel=rel)
            entries.append(
                {
                    "name": f"{subject}的{kind}",
                    "description": f"core/实体记忆：{triple}",
                    "content": content,
                    "user": subject,
                }
            )
            break  # 一句只捕获一条最强信号
    return entries


def _pick_owner(recent_turns: list[dict] | None, new_turns: list[dict] | None) -> str:
    """从轮次中推断记忆归属者：优先最近一条非 AI 发言的归属者。

    弹幕轮次按 [弹幕@用户名] 前缀归属其观众（区分角色，避免弹幕事实
    落到主播名下）；全部都是 AI 发言（如主动孤独倾诉）归属 AI 自己（self）。
    """
    source = new_turns or recent_turns or []
    for turn in reversed(source):
        role = str(turn.get("role") or "").lower()
        if role not in _AI_ROLES:
            content = str(turn.get("content") or "")
            m = re.match(r"^\[弹幕@([^\]]+)\]", content)
            if m:
                return m.group(1).strip()[:32] or memory._USER_DEFAULT
            return str(turn.get("user") or memory._USER_DEFAULT)
    return memory._USER_SELF


# 蒸馏/整合条目的归属者判定：owner=self → AI 自己；否则优先模型标注的
# subject（蒸馏 prompt 要求按「观众: [弹幕@名]」给出具体观众名/主播/AI），
# 排除笼统词（观众/主播/用户/AI 等）避免角色塌缩，缺失才回退推断归属者。
_ABSTRACT_SUBJECTS = {"观众", "主播", "用户", "AI", "self", "我", "他", "她"}
# subject 常见前缀清洗：模型有时把「观众@名」「弹幕@名」整段当作 subject，
# 剥掉角色前缀只留纯用户名（如「观众@蓝奶」→「蓝奶」），保证图谱归属者干净。
_SUBJECT_PREFIX_RE = re.compile(r"^(观众|弹幕|主播)@")
def _entry_user(item: dict, fallback: str) -> str:
    """蒸馏/整合条目归属者：AI 自己的事实归 self，其余优先具体主体名。"""
    owner = (item.get("owner") or "").strip().lower()
    if owner == "self":
        return memory._USER_SELF
    subject = _SUBJECT_PREFIX_RE.sub("", str(item.get("subject") or "").strip()[:32])
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
