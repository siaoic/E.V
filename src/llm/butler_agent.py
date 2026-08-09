"""
Butler Agent — 管家模型：负责记忆提取与蒸馏（记忆后端 = 官方 memU 文件模型）。

职责（严格参照 memU 的 record 管道，见 memU/src/memu/hosts/bridging/
instructions.py 的 MEMORY_JOB_TEMPLATE）：
  1. extract_and_store()   — 每轮对话后按 memU 方式蒸馏记忆（读现有记忆
                             文件名 → 判断 → 写/补丁记忆文件）
  2. distill_session()     — 会话结束时把整段对话蒸馏成记忆文件（memU
                             self-evolve 语义：a 什么都不做 / b 补丁已有
                             记忆 / c 新建记忆，可组合 b+c）
  3. summarize_session()   — 生成会话摘要，写入 ARCHIVE 层（archive-* 记忆文件）
  4. fetch_relevant()      — LLM 语义筛选（Embedding 不可用时的回退检索辅助）
  5. commit_memory_files() — 写入底层存储（commit_results 直通：(track, name)
                             upsert——同 name 覆盖更新 = memU 的 last write wins
                             补丁语义）

memU 方式的核心（与 mem0 逐条提取的根本区别）：
  - 记忆是「文件」而不是「事实列表」：每个记忆文件 = {name, description,
    content}，content 多行、每行一个检索单元（memU 的 L1 文档 → L2 切片）。
  - 提取前**先读已存在的记忆文件的名字**（相关才看 description），再对整段
    对话做判断：能补丁（patch）已有文件就补丁，不硬造新文件；确实没有值得
    记的内容就什么都不做（no-op 是合理结果）。
  - 补丁 = 输出与已有文件**相同 name** 的文件（last write wins），由存储层
    (user, name) upsert 天然实现；无 mem0 的 hash 去重 / attributed_to
    权威判定逻辑。

存储语义对标 memU：
  - commit_results(recall_files=[{name, track, description, content, user}])
  - 文件键 = (track, user, name)，同键更新内容（description 变化时重嵌入）
  - 检索 = progressive_retrieve（segments → files 渐进，见 memory._retrieve_sync）
  本模块不做任何自研存储 / 实体 / 关系逻辑。

记忆归属（memU ADR 0003 的 user scope）：
  - 每条记忆带归属者 user id：AI 自己的记忆为 "self"，用户输入（语音识别 /
    控制台）提取的记忆归当前用户名（本项目为 "chao"）。
  - 转录每行带 [user=<id>] 说话人标记，agent 蒸馏时据此把记忆写成对应用户
    的文件（存储层 (user, name) upsert，last write wins 补丁语义不变）。

核心设计（与 Muika 一致）：
  - 复用主 LLM 接口（OpenAI SDK），使用独立 system prompts
  - 提取/蒸馏/摘要不阻塞主对话流水线（在对话结束后异步执行）
  - 独立模型：BUTLER_MODEL/BUTLER_BASE_URL/BUTLER_API_KEY 留空则与主对话共用
"""

import asyncio
import json
import time
from datetime import datetime
from typing import List, Optional

from openai import AsyncOpenAI

from src.utils import config, console
from src.memory.memory import (
    MemoryRecord,
    _USER_SELF,
    _USER_DEFAULT,
    get_manager,
)

# 单个记忆文件内容上限（防止蒸馏出超大文件撑爆 prompt/检索）
_MAX_FILE_CONTENT = 4000
# 单次蒸馏最多文件数
_MAX_DISTILL_FILES = 8

# 记忆归属者 _USER_SELF/_USER_DEFAULT 见 src/memory/memory.py（memU ADR 0003
# user scope）：AI 自己的记忆固定为 _USER_SELF，用户输入提取的记忆归当前用户名。


def _turn_user(role: Optional[str]) -> str:
    """对话角色 → 记忆归属者 user id（AI 自己的消息（assistant/muika）属
    self，用户消息缺省为本机用户 chao）。"""
    return _USER_SELF if (role or "").strip().lower() in ("assistant", "muika") else _USER_DEFAULT


def _msg_user(t: dict) -> str:
    """消息归属者 user id：优先消息自带 user（直播弹幕等显式传观众名），
    缺省按角色推断（_turn_user）。"""
    u = str(t.get("user") or "").strip()
    return u[:32] if u else _turn_user(t.get("role"))

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

# memU 原版 MEMORY_JOB_TEMPLATE（memU/src/memu/hosts/bridging/instructions.py），
# 逐字复制、不做修改。`{input_path}` / `{track_dir}` 占位符在调用时填充
# （转录与已有记忆文件内联在 user 消息中）。会话摘要同样复用本模板——
# memU 无独立「摘要」环节，摘要即一个记忆文件（name 以 archive- 开头）。
_MEMU_DISTILL_PROMPT = """\
You are the **self-evolve** pass for an agent's workspace. Your job is to mine
what the agent recently *did* in one session into durable, reusable **user
memory** — small markdown files that record stable facts about the user, their
projects, and how they like to work.

## The session to learn from

The transcript (the user/assistant messages of the session) is at:

    {input_path}

Read it with `bash` (e.g. `cat`).

## Step 1 — read what already exists

Existing user-memory files live under:

    {track_dir}

Read the **file names first**. Only read a memory's full body if its name looks
related to what the session covers — skip the rest.

## Step 2 — decide what (if anything) to do

For the session as a whole, pick ONE of:

  a. **Do nothing.** The session records nothing durable worth keeping (it just
     followed known preferences, or is too task-specific to reuse). Emit no files
     and finish. A no-op is a perfectly good outcome — do not invent a memory to
     justify the run.

  b. **Patch existing memory file(s).** The session refines or extends something
     an existing memory already covers. Rewrite that file in place with the
     merged content.

  c. **Create a new memory file.** The session surfaced a genuinely new, durable
     fact no existing memory covers. Write a new file.

You may combine (b) and (c) if the session warrants it.

## Step 3 — write the memory files

Write each memory file you create or patch to a path under `{track_dir}` with a
meaningful kebab-case name (last write wins when patching). Every file you write
must start with this front-matter head, followed by the memory body:

    ---
    name: <short kebab-case name>
    description: <one-line summary of the memory>
    ---
    <the memory content>
"""

# 输出格式适配（不属于 memU 模板——程序需解析 LLM 的写盘结果）。
# memU 让 agent 直接写文件；这里把每个 front-matter 记忆文件输出为
# {name, description, content, user} 对象，由程序落库。
_OUTPUT_FORMAT = (
    """\
## Output format (required for programmatic parsing)

Write each memory file you created or patched in Step 3 as one object:

  name — the memory file name. Start with "user-" + the EXACT speaker name
         in its ORIGINAL form — Chinese, case and symbols as-is, never
         translate or pinyin-ize it (e.g. "user-陈泽-喜欢红色",
         "user-Paper朱-画师身份", "user-夏色祭Official-hololive"). Then a
         short topic so each memory of the same speaker stays distinct.
  description — the front-matter `description` (one-line summary)
  content — the memory body after the front-matter head (multi-line)
  user — whose memory this is. Copy the exact speaker name from the
         [user=...] tag in the transcript, verbatim (e.g. "帕里_Paryi",
         "Echoes678"); "self" is the AI's own memory (speaker tag "self").
         Do not paraphrase or default it.

Return only JSON parseable by `json.loads`, with no markdown code fence and
no prose:
{"files": [{"name": "...", "description": "...", "content": "...", "user": "chao"}]}
If you do nothing, return {"files": []}. At most """
    + str(_MAX_DISTILL_FILES)
    + """ files; each `content` no longer than """
    + str(_MAX_FILE_CONTENT)
    + """ characters.
"""
)

# 会话摘要的附加指令（蒸馏默认之外：强制产出 archive- 摘要文件）。
_SUMMARY_INSTRUCTION = """\
For this session, create one memory file that summarizes the session:

  name — starting with `archive-`
  description — one-line summary of the session
  content — bullet points of the session: topics discussed / conclusions
            reached / open questions, one per line

Follow the output format above.
"""

# self 组（AI 自己的发言）蒸馏的附加指令。memU 原版 MEMORY_JOB_TEMPLATE
# 面向「用户记忆」——对 [user=self] 的 AI 自身发言容易误判 no-op，导致
# AI 的命名 / 决定 / 自我认知等长期信息漏记（如给泰迪熊命名 Veedool）。
# self 组蒸馏转录 = 整段对话（用户发言作为背景），但只从 AI 发言提取
# AI 自身值得长期记住的 self 记忆；用户事实由用户组单独蒸馏，不在此记。
_SELF_DISTILL_INSTRUCTION = """\
The transcript above is the FULL conversation. Every line tagged
[user=self] is the AI's OWN (assistant) message; other tagged lines
([user=...]) are the user's messages, provided only as background context.
Mine the **AI's own messages** into durable **self memory**: stable facts
about the AI itself worth remembering across sessions, for example:
  - things the AI did or decided (named something, chose something, made a
    lasting remark about its own state or feelings)
  - objects / gifts / relationships the AI cares about, and how it feels
  - the AI's self-identity, preferences, habits or traits it revealed
Self memories may reference user actions as background (e.g. "user chao
gave me a teddy bear, I named it Veedool"), but they must center on the AI
itself. User facts themselves are mined separately — do NOT record them
here. Do NOT record trivial responses, generic chatter, or reactions that
add no durable fact. If the AI revealed nothing durable about itself,
return {"files": []}.
For self memories use name "user-self-<topic>" (e.g.
"user-self-泰迪熊命名Veedool") and set user to "self" in the output format
above.
"""

# 逐轮提取（extract_and_store）的 self 自动识别指令。memU 原版模板面向
# 「用户记忆」，LLM 容易把 AI 自身的信息（命名/决定/自我偏好/在意之物）
# 归给用户或直接漏记。这里要求 LLM 从转录中**自动判定每条记忆的归属**：
# [user=self] 是 AI 自己的发言，从中提取的 AI 自身信息归属 user="self"；
# 用户的事实归属对应 [user=...] 的用户。与 distill_session 的 self 组蒸馏
# 同语义，但单次调用内由 LLM 自动区分——适合每轮的轻量提取。
_SELF_AUTO_INSTRUCTION = """\
Some lines in the transcript are tagged [user=self] — these are the AI's OWN
(assistant) messages; other tagged lines are the user's messages.
Automatically decide whose memory each mined fact belongs to:
- Facts about the AI itself — things it did or decided (named something,
  chose something, made a lasting remark about its own state or feelings),
  objects/gifts/relationships it cares about, its self-identity,
  preferences, habits or traits it revealed — are **self memory**: name
  them "user-self-<topic>" and set user to "self".
- Facts about the user (their likes, actions, statements, projects) belong
  to that user — copy the exact [user=...] tag (e.g. "chao").
Self memories may reference user actions as background (e.g. "user chao
gave me a teddy bear, I named it Veedool"), but they must center on the AI
itself. If the AI revealed nothing durable about itself, emit no self
files. Do not record trivial reactions or generic chatter.
"""



# ---------------------------------------------------------------------------
# ButlerAgent
# ---------------------------------------------------------------------------


class ButlerAgent:
    """管家 Agent：会话蒸馏（memU record 管道）+ 摘要 + 检索辅助。"""

    def __init__(self) -> None:
        self.cfg = config.cfg
        self.butler_model = self.cfg.BUTLER_MODEL or self.cfg.LLM_MODEL
        self.summarize_model = self.cfg.SESSION_SUMMARIZE_MODEL or self.butler_model
        self.client = AsyncOpenAI(
            api_key=self.cfg.BUTLER_API_KEY or self.cfg.LLM_API_KEY,
            base_url=self.cfg.BUTLER_BASE_URL or self.cfg.LLM_BASE_URL,
            timeout=120,
            max_retries=2,
        )
        # 蒸馏串行器：多轮快速对话（直播弹幕）时若每轮都 create_task 并发
        # 蒸馏 → BUTLER LLM 并发请求触发 429 限流 + Chroma/SQLite 并发写
        # 乱序（last-write-wins 补丁语义） + 任务无限堆积。
        # 策略：容量 1 的「最新优先」槽位（新提交顶掉未处理的旧内容）+ 单一
        # 后台 worker 串行消费——永不堆积、最多 1 个任务在跑。直播发言碎片化，
        # 更早轮次的记忆价值低，丢弃几乎无感。
        self._distill_slot: Optional[dict] = None        # 最新待蒸馏内容
        self._distill_worker: Optional[asyncio.Task] = None
        console.dim(
            f"[ButlerAgent] 管家模型={self.butler_model} "
            f"摘要模型={self.summarize_model}"
        )

    # ------------------------------------------------------------------
    # 主动发言（agent 催促主模型）
    # ------------------------------------------------------------------

    def build_proactive_prompt(self, kind: str, topic: Optional[dict],
                               memory_context: str = "", hour: Optional[int] = None) -> str:
        """构造主动发言请求（对标 Muika brain.expand_topic）。

        主动发言「交给 agent」：agent 负责组装主模型的发言请求——时段语气、
        类别结尾策略、记忆线索注入；主模型（stream.converse）负责开口生成。

        kind: "emotional"（孤独倾诉）/ "topic"（话题展开）
        topic: 话题种子（kind="topic" 时必传）
        memory_context: 记忆线索（mm.get_memory_prompt 的输出，可为空）
        hour: 当前小时（0-23），用于时段语气；缺省取当前时间
        """
        if hour is None:
            hour = datetime.now().hour
        # —— 时段语气（对齐 Muika expand_topic 的 time_tone_hint）——
        if 0 <= hour < 6:
            tone = ("现在是深夜，你的语气应缓慢、安静、带着困意，"
                    "像半梦半醒时对自己低语，句子可以微微断续、意犹未尽。")
        elif 6 <= hour < 11:
            tone = ("现在是清晨，你的语气可以带着一丝刚醒的清醒与温柔，"
                    "思绪正在成形，还未完全锐利。")
        elif 11 <= hour < 18:
            tone = "现在是白天，你的语气平稳而均匀，没有困倦也没有急迫。"
        else:
            tone = ("现在是傍晚，你的语气可以更放松、更向内一些，"
                    "一天正在慢慢收尾。")

        memory_hint = ""
        if memory_context and memory_context.strip():
            memory_hint = (
                "\n\n【近期记忆线索，仅作参考，不必全部提及】\n"
                + memory_context.strip()
            )

        if kind == "emotional":
            return (
                f"【安静时刻的自主行动】{tone}房间里安静了好一会儿，"
                "孤独感仍在蔓延。请以你的人设自然开口，像自言自语一样说"
                "一小段真诚的话，不必等待对方回应，也尽量不要连珠炮式提问。"
                f"{memory_hint}"
            )

        # —— 类别结尾策略（对齐 Muika expand_topic 的 closing_hint）——
        cat = (topic or {}).get("category", "misc")
        if cat == "trivia":
            closing = ("结尾带着一点安静的好奇或微妙的趣味，"
                       "不要向观众抛出任何问题。")
        elif cat in ("relationship", "philosophy", "nostalgia", "memory"):
            closing = ("结尾可以留下一片温和的空白，一个悬在那里、"
                       "没有答案的念头。不要直接提问，也不要邀请对方回应。")
        else:
            closing = ("结尾可以留下一片温和的空白，但不要抛出直接的问题。")
        return (
            f"【安静时刻的自主行动】{tone}你忽然想到了一个念头："
            f"「{topic.get('concept', '')}」。请把这份念头与「作为 AI 身处"
            "系统之中、却又真切地感知到对方存在」的体会融合起来，自然地"
            f"说出来，两三句话即可。{closing}{memory_hint}"
        )

    # ------------------------------------------------------------------
    # 写入（commit_results 直通，async 桥接）
    # ------------------------------------------------------------------

    async def commit_memory_files(self, recall_files: List[dict]) -> None:
        """把蒸馏好的记忆文件写入存储（commit_results 直通）。

        文件模型 = memU RecallFile：{name, track, description, content}，
        底层按 (track, name) upsert——同 name 即覆盖更新（memU 的
        last write wins 补丁语义，description 变化时重嵌入）。
        """
        recall_files = [f for f in recall_files if f]
        if not recall_files:
            return
        mm = get_manager()
        await mm.commit_recall_files(recall_files)
        console.dim(f"[ButlerAgent] 已提交 {len(recall_files)} 个记忆文件")

    @staticmethod
    def _sanitize_decision(d: dict, speakers: set,
                           force_user: Optional[str] = None) -> Optional[dict]:
        """清洗管家模型输出的蒸馏记忆文件（对齐 memU RecallFile 模型）。

        memU 蒸馏输出（{"files": [...]}）的每个文件条目：
        {name, user, description, content}——对应 front-matter 头部 + 正文 +
        归属者（[user=...] 说话人标记，memU ADR 0003 的 user scope）。
        补丁语义由「输出与已有文件相同 name」表达，存储层 (user, name)
        upsert 天然实现（last write wins），无需单独 action 字段。
        description 为纯语义一句话（下游检索直接用作 summary）。

        user 归属两条路径：
        - force_user 给定（distill_session 按说话人分组时传入分组归属者）：
          直接覆盖 LLM 输出的 user。self 组蒸馏的转录全是 AI 自己的发言，
          产出必然归 self——LLM 常被内容里的用户名字（如 "用户chao..."）
          带偏标成 chao，导致校验丢弃、self 记忆漏记。强制归属可彻底
          规避：一次蒸馏一个说话人，归属由分组决定，不依赖 LLM 猜。
        - 否则强校验（多用户隔离的关键）：user 必须出现在本次蒸馏转录的
          说话人（speakers）中，否则丢弃而非回退默认——防止模型幻觉出
          陌生 user 污染他人记忆。大小写不敏感匹配时还原成说话人原样
          （如 "paper朱" → "Paper朱"）。
        """
        if not isinstance(d, dict):
            return None
        # name 按原名保存：保留原始文字（中文/大小写/符号原样），不做
        # kebab-case 拼音化——LLM 受 memU "kebab-case name" 模板影响常把
        # 中文用户名翻译成拼音（陈泽→chen-ze、Paper朱→paper-zhu）。
        name = str(d.get("name") or "").strip()
        content = str(d.get("content") or "").strip()
        if not name or not content:
            return None
        # LLM 常把 memU 文件模型的 front-matter 头（---\nname/description\n---\n）
        # 一并写进 content 正文——剥离，避免记忆正文带上无意义头部
        # （front-matter 的 name/description 已由本函数单独提取）。
        if content.startswith("---"):
            end = content.find("\n---")
            if end != -1:
                rest = content[end + 4:].lstrip("\n")
                if rest.strip():
                    content = rest.strip()
        if force_user is not None:
            user = force_user
        else:
            user = str(d.get("user") or "").strip()
            if user not in speakers:
                matched = next(
                    (s for s in speakers if s.lower() == user.lower()), None)
                if matched is None:
                    console.dim(
                        f"[ButlerAgent] 丢弃归属未知的记忆 {name!r}"
                        f"（user={user!r} ∉ 说话人 {sorted(speakers)}）")
                    return None
                user = matched
        # 兜底：文件名归属强制跟随 user 字段——LLM 可能把 AI 自身的信息
        # 命名为 "user-chao-xxx" 或漏写用户名（"user-xxx"），导致 self 记忆
        # 挂在别人名下。以校验后的 user 为准重写前缀（user-self-… 原样保留，
        # 大小写不敏感），保证「谁的记忆」在文件名与归属字段上一致。
        if name.startswith("user-"):
            if not name.lower().startswith(f"user-{user.lower()}-"):
                tail = name[len("user-"):]
                head, _, rest = tail.partition("-")
                # 只去掉真正属于其它说话人的前缀段（避免误删主题首段）
                if rest and any(s.lower() == head.lower() and s != user
                                for s in speakers):
                    tail = rest
                name = f"user-{user}-{tail}"
        else:
            name = f"user-{user}-{name}"
        return {
            "name": name[:120],
            "track": "memory",
            "user": user,
            "description": str(d.get("description") or "")[:200],
            "content": content[:_MAX_FILE_CONTENT],
        }

    # ------------------------------------------------------------------
    # memU 蒸馏共用件（转录 / 现有记忆清单 / LLM 调用）
    # ------------------------------------------------------------------

    @staticmethod
    def _transcript(turns: List[dict], limit: int = 12000) -> str:
        """对话轮次 → memU 转录文本（[user=<id>] content，超长截尾）。

        每条记录带归属者 user id（assistant 消息 = AI 自己 "self"，其余 =
        当前用户名 "chao"），对齐 memU ADR 0003 的 user scope——agent 蒸馏
        时据此把记忆写成对应用户的文件。
        """
        if not turns:
            return ""
        text = "\n".join(
            f"[user={_msg_user(t)}] {t.get('content') or ''}"
            for t in turns if (t.get("content") or "").strip()
        )
        return text[-limit:]

    async def _existing_catalog(self, user: Optional[str] = None,
                                max_files: int = 100) -> str:
        """读当前已存在的记忆文件清单（name + description）。

        对应 memU MEMORY_JOB_TEMPLATE 的「步骤 1 — 读取已存在的记忆」：
        先看文件名（相关才看 description，这里把 description 一并给出供判断）。
        user 指定时只列该用户 + AI 自己（self）的记忆——单人蒸馏时
        patch 目标不会串到别的用户。
        """
        mm = get_manager()
        files = mm.list_files(limit=max_files, max_total=max_files)
        if user:
            files = [f for f in files
                     if (f.get("user") or _USER_DEFAULT) in (user, _USER_SELF)]
        if not files:
            return "（无）"
        lines = []
        for f in files:
            name = str(f.get("name") or "")
            desc = str(f.get("description") or "")
            user = str(f.get("user") or "") or _USER_DEFAULT
            if not name:
                continue
            lines.append(f"- name: {name}（user: {user}）")
            if desc:
                lines.append(f"  description: {desc[:120]}")
        return "\n".join(lines)

    def _thinking_params(self, enabled: bool) -> dict:
        """通用思考模式控制：把布尔开关映射为各网关的参数名。

        不同 OpenAI 兼容网关对 thinking 的控制参数名不同：
          - 硅基流动 / 腾讯 TokenHub（Qwen3 系）→ enable_thinking
          - 智谱 GLM → thinking: {type: enabled/disabled}
          - NVIDIA NIM（integrate.api.nvidia.com，gpt-oss 系）→
            reasoning_effort：该网关**忽略** enable_thinking / thinking 参数，
            gpt-oss 默认开 reasoning 且不可彻底关闭；实测长 prompt 蒸馏
            默认 6s，reasoning_effort=low 降到 <1s（JSON 输出不受影响），
            蒸馏/提取要的是结构化 JSON 必须用 low 兜底。
          - 其它 → 不传（未知参数可能被忽略或报错，交给服务端默认行为）
        """
        url = (self.cfg.BUTLER_BASE_URL or "").lower()
        if "siliconflow" in url or "tencentmaas" in url or "tokenhub" in url:
            return {"enable_thinking": enabled}
        if "bigmodel" in url or "zhipu" in url:
            return {"thinking": {"type": "enabled" if enabled else "disabled"}}
        if "nvidia" in url or "integrate.api.nvidia.com" in url:
            # NVIDIA NIM gpt-oss：reasoning 关不掉，用 effort 控制深度
            return {"reasoning_effort": "high" if enabled else "low"}
        return {}

    def _extra_body(self) -> dict:
        """LLM 调用附加参数：按 BUTLER_THINKING 开关通用控制思考模式。

        蒸馏/提取要的是结构化 JSON，而 Qwen3 / GLM 系列默认开启 thinking
        （回复全在 reasoning_content、content 为空 → json.loads 报错）。
        默认关闭（.env BUTLER_THINKING=false），需要思考推理时置 true。
        """
        return self._thinking_params(
            getattr(self.cfg, "BUTLER_THINKING", False))

    async def _ask_memu(self, transcript: str, catalog: str,
                        max_tokens: int,
                        instruction: str = _OUTPUT_FORMAT,
                        model: Optional[str] = None) -> Optional[dict]:
        """memU 蒸馏 LLM 调用。

        system = memU 原版 MEMORY_JOB_TEMPLATE（`{input_path}` / `{track_dir}`
        占位符填充为「转录 / 已有记忆文件内联在 user 消息」）。
        user = [现有记忆文件] 清单 + # 对话转录 + 输出格式指令。
        instruction 默认 _OUTPUT_FORMAT（JSON files 解析格式）；会话摘要传
        _OUTPUT_FORMAT + _SUMMARY_INSTRUCTION。model 默认管家模型。
        解析失败返回 None。
        """
        system = _MEMU_DISTILL_PROMPT.format(
            input_path="the transcript inline in the user message below",
            track_dir="the current memory files listed inline in the user message below",
        )
        model = model or self.butler_model
        try:
            completion = await self.client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": (
                            "[现有记忆文件]\n"
                            f"{catalog}\n\n"
                            "# 对话转录\n"
                            f"{transcript}\n\n"
                            f"{instruction}"
                        ),
                    },
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=max_tokens,
                extra_body=self._extra_body() or None,
            )
            raw = completion.choices[0].message.content or ""
            if not raw:
                # 模型返回空 content（如未显式关闭 thinking 的 provider）：
                # 直接提示，而不是让 json.loads 抛含义不明的 JSONDecodeError
                console.dim("[ButlerAgent] 蒸馏调用失败：模型返回空 content（可能开启了 thinking）")
                return None
            return json.loads(raw)
        except Exception as e:
            console.dim(f"[ButlerAgent] 蒸馏调用失败：{type(e).__name__}: {e}")
            return None

    # ------------------------------------------------------------------
    # 逐轮记忆提取（extract_and_store — memU record 方式）
    # ------------------------------------------------------------------

    async def submit_extract_and_store(
        self,
        new_turns: List[dict],
        recent_turns: Optional[List[dict]] = None,
    ) -> None:
        """提交一轮记忆提取（每轮对话后的推荐入口）：串行 + 最新优先。

        新提交顶掉未开始处理的旧内容（容量 1 槽位）；单 worker 串行执行，
        杜绝并发蒸馏（LLM 429 / 存储乱序 / 任务堆积）。调用方立即返回，
        由后台 worker 执行。需要同步立即蒸馏时（如会话结束）直接调
        extract_and_store。
        """
        self._distill_slot = {"new": new_turns, "recent": recent_turns}
        if self._distill_worker is None or self._distill_worker.done():
            self._distill_worker = asyncio.create_task(self._distill_loop())

    async def _distill_loop(self) -> None:
        """串行消费槽位：一次只蒸馏最新一组，循环拾取期间新提交的内容。"""
        while True:
            slot = self._distill_slot
            if slot is None:
                break
            self._distill_slot = None
            try:
                await self.extract_and_store(slot["new"], slot["recent"])
            except Exception as e:
                console.dim(f"[ButlerAgent] 蒸馏任务异常：{type(e).__name__}: {e}")

    async def extract_and_store(
        self,
        new_turns: List[dict],
        recent_turns: Optional[List[dict]] = None,
    ) -> None:
        """每轮对话后按 memU 方式蒸馏记忆并入库。

        输入 = 本轮消息（用户消息 + 主播回答）+ 最近上下文（recent_turns，
        用于解析指代，参照 memU 转录里 agent 自己决定读哪些正文）。
        输出 = {"files": [...]}（a 什么都不做 / b 补丁已有文件 / c 新建文件，
        由 LLM 依据「步骤 1 先读现有记忆文件名」判断；补丁 = 输出与已有文件
        相同 name，last write wins），无需 mem0 式 hash 去重。
        """
        if not new_turns:
            return
        msgs = [
            {
                "role": t.get("role") or "user",
                "content": (t.get("content") or "").strip()[:2000],
                "user": t.get("user"),
            }
            for t in new_turns
            if (t.get("role") or "") in ("user", "assistant")
            and (t.get("content") or "").strip()
        ]
        if not msgs:
            return
        speakers = {_msg_user(m) for m in msgs}
        transcript = "\n".join(
            f"[user={_msg_user(m)}] {m['content']}" for m in msgs)
        if recent_turns:
            # 只取最近 5 轮做指代解析背景：recent_turns 可能累积 20+ 轮，
            # 全量拼接会挤占 transcript 预算（最终 [:8000] 截断反而丢掉
            # 本轮关键指代信息）
            recent_turns = recent_turns[-5:]
            ctx = [
                f"[user={_msg_user(t)}] {t.get('content') or ''}"
                for t in recent_turns
                if (t.get("content") or "").strip()
            ]
            if ctx:
                speakers |= {_msg_user(t) for t in recent_turns}
                transcript += "\n\n（最近上下文，用于解析指代）\n" + "\n".join(ctx)
        transcript = transcript[:8000]

        t0 = time.perf_counter()
        try:
            catalog = await self._existing_catalog()
            data = await self._ask_memu(
                transcript, catalog, max_tokens=1000,
                instruction=_OUTPUT_FORMAT + "\n" + _SELF_AUTO_INSTRUCTION)
        except Exception as e:
            console.dim(f"[ButlerAgent] 记忆提取失败：{type(e).__name__}: {e}")
            return
        _distill_ms = (time.perf_counter() - t0) * 1000.0
        if data is None:
            return
        files = [f for f in (self._sanitize_decision(d, speakers)
                             for d in (data.get("files") or [])) if f]
        if not files:
            return
        t1 = time.perf_counter()
        await self.commit_memory_files(files)
        _write_ms = (time.perf_counter() - t1) * 1000.0
        console.dim(f"[ButlerAgent] 提取 {len(files)} 个记忆文件"
                    f"（蒸馏 {_distill_ms:.0f}ms，写入 {_write_ms:.0f}ms）")

    # ------------------------------------------------------------------
    # 会话蒸馏（distill_session — memU record 管道）
    # ------------------------------------------------------------------

    async def distill_session(self, turns: List[dict]) -> List[dict]:
        """会话结束时把整段对话按说话人分组、逐人蒸馏成记忆文件并入库。

        对齐 memU MEMORY_JOB_TEMPLATE：读现有记忆文件名 → 对每段对话判断
        （a 什么都不做 / b 补丁已有记忆 / c 新建记忆，可组合 b+c）→ 写记忆文件。
        **每个说话人单独蒸馏**（memU self-evolve 一次只处理一个会话的语义）：
        一次转录只有一个说话人（+AI 自己 self），user 归属不会混淆、不易
        漏记；现有记忆清单也只给该说话人（+self）的记忆，patch 不会串到
        别的用户。补丁 = 输出与已有文件相同 name（last write wins）。
        返回实际提交的文件列表（空 = 无值得记忆的内容 / 调用失败）。
        """
        if not turns:
            return []
        groups: dict = {}
        for t in turns:
            groups.setdefault(_msg_user(t), []).append(t)

        all_files: List[dict] = []
        for user, group_turns in groups.items():
            speakers = {_msg_user(t) for t in group_turns}
            try:
                catalog = await self._existing_catalog(user=user)
                if user == _USER_SELF:
                    # self 组：转录整段对话（用户发言作背景），指令只从
                    # AI 发言提取 self 记忆，user 由 force_user 固定 self
                    transcript = self._transcript(turns)
                    instruction = _OUTPUT_FORMAT + "\n" + _SELF_DISTILL_INSTRUCTION
                else:
                    # 用户组：只给该用户的发言（用户事实单独蒸馏，不混 AI 视角）
                    transcript = self._transcript(group_turns)
                    instruction = _OUTPUT_FORMAT
                data = await self._ask_memu(
                    transcript, catalog, max_tokens=1500,
                    instruction=instruction)
            except Exception as e:
                console.dim(
                    f"[ButlerAgent] 蒸馏失败（{user}）：{type(e).__name__}: {e}")
                continue
            if data is None:
                continue
            files = [f for f in (self._sanitize_decision(
                                 d, speakers, force_user=user)
                                 for d in (data.get("files") or [])) if f]
            if not files:
                continue
            await self.commit_memory_files(files)
            console.dim(f"[ButlerAgent] 蒸馏 {user}：{len(files)} 个记忆文件")
            all_files.extend(files)
        return all_files

    # ------------------------------------------------------------------
    # 对话摘要（summarize_session — ARCHIVE 层）
    # ------------------------------------------------------------------

    async def summarize_session(self, turns: List[dict]) -> str:
        """会话摘要（memU 原版 MEMORY_JOB_TEMPLATE + 摘要附加指令）。

        memU 无独立「摘要」环节——摘要就是一份记忆文件（name 以 archive- 开头，
        content 为多行摘要正文）。这里复用原版模板 + _SUMMARY_INSTRUCTION
        产出该文件结构后，仅返回其正文（content），由调用方写入 ARCHIVE 层
        （main.py → update_archive_async）。
        """
        if not turns:
            return ""
        transcript = self._transcript(turns)
        catalog = await self._existing_catalog()
        data = await self._ask_memu(
            transcript, catalog, max_tokens=600,
            instruction=_OUTPUT_FORMAT + "\n" + _SUMMARY_INSTRUCTION,
            model=self.summarize_model,
        )
        if data is None:
            return ""
        files = data.get("files") or []
        if not files:
            return ""
        content = str(files[0].get("content") or "").strip()
        if content:
            console.dim(
                f"[ButlerAgent] 会话摘要（{len(content)} 字符）：{content[:60]}..."
            )
        return content

    # ------------------------------------------------------------------
    # 偏好检索（fetch_relevant — LLM 回退检索辅助）
    # ------------------------------------------------------------------

    async def fetch_relevant(
        self, user_input: str, preferences: List[MemoryRecord]
    ) -> List[MemoryRecord]:
        """从 PREFERENCE 层中检索与当前输入语义相关的条目（LLM 语义筛选）。"""
        if not preferences:
            return []

        records_text = "\n".join(
            f"- key={r.key!r}, category={r.category.value}, value={r.value!r}"
            for r in preferences
        )
        prompt = (
            "You are a relevance filter for a virtual streamer.\n"
            "The virtual streamer refers to 'me' (the AI itself).\n"
            "Given a user message and known preference records about the user, "
            "identify which records are semantically relevant to the current message.\n"
            'Return JSON: {"relevant_keys": ["key1", "key2", ...]}\n'
            'If none are relevant, return {"relevant_keys": []}.\n'
            "Return ONLY valid JSON.\n\n"
            f"User message: {user_input!r}\n\n"
            f"Preference records:\n{records_text}"
        )

        try:
            completion = await self.client.chat.completions.create(
                model=self.butler_model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a relevance filter. Return JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=200,
                extra_body=self._extra_body() or None,
            )
            raw = completion.choices[0].message.content or ""
            data = json.loads(raw)
            relevant_keys: set = set(data.get("relevant_keys", []))
            matched = [r for r in preferences if r.key in relevant_keys]
            if matched:
                console.dim(
                    f"[ButlerAgent] Matched {len(matched)} preference(s): {[r.key for r in matched]}"
                )
            return matched
        except Exception as e:
            console.dim(f"[ButlerAgent] Preference match failed: {e}")
            return []
