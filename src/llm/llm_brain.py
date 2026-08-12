"""LLM 流式对话大脑（OpenAI 兼容接口，openai SDK）—— 严格参考 live-2d(2) 重构。

对标 live-2d(2)：
  - llm-handler.js      → 多轮工具调用循环（max 30 轮）、空响应「催 1 次 + 放弃」、
                          轮数超限后的非流式兜底（tools=[] 强制最终回复）
  - llm-client.js       → _cleanMessagesForAPI（控制字符 / 8000 截断 / assistant null→''）、
                          流式累积 content + tool_calls、thinking 过滤、
                          Qwen 文本格式工具调用解析 + 从 content 移除工具调用文本、
                          非流式 reasoning_content 兜底
  - tool-message-utils.js → 工具消息序列清洗（sanitize）+ 不切断工具链的裁剪
  - api-utils.js getMergedToolsList → 每轮对话实时合并一次工具列表（不缓存）

兼容任意 OpenAI 协议的服务：OpenAI 官方 / 智谱 GLM / DeepSeek / 本地 vLLM 等。
通过 .env 配置 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 切换服务，无需改代码。

调用范式：
    client.chat.completions.create(
        model=cfg.LLM_MODEL,
        messages=[...],
        tools=[...],        # 可选：function calling 工具（MCP + 本地）
        stream=True,
        max_tokens=2048,
        temperature=0.95,
        extra_body={"thinking": {"type": "enabled"}},  # 仅 LLM_THINKING 启用时
    )
    for chunk in response:
        delta = chunk.choices[0].delta
        # delta.reasoning_content → 思考过程（实时灰字打印，支持的服务才有）
        # delta.content           → 回复内容（按句 yield，交 TTS 播报）
        # delta.tool_calls        → 工具调用增量（流结束累积完整后执行）

OpenAI SDK 的流式迭代是同步的，这里放到子线程跑，通过 asyncio.Queue 把
content 增量传回主事件循环，按句切分后 yield，实现「边思考边产出边播报」。
工具调用则在主协程执行（httpx 异步），不阻塞 TTS 播放。
"""

import asyncio
import json
import os
import re
import time
from typing import Any, AsyncGenerator, List, Optional

from src.utils import config, console
from src.memory import memory
from src.llm.tools.skills import get_skill_manager
from src.llm.tool_message_utils import (
    sanitize_tool_message_sequence,
    trim_messages_preserving_tool_rounds,
)
from src.utils.perf_tracker import PerfTracker

# 句子边界：遇符号立即切分。
# 中文：。！？…换行；英文：句号 '.' 仅在后跟空格/结尾时算边界（避开 '...'、小数）。
_SENTENCE_ENDS = "。！？!?\n…，,；;"

# 多轮工具调用上限（对标 live-2d(2) llm-handler.js 的 maxIterations=30）
_MAX_TOOL_ITERATIONS = 30

# 429 限流自动等待重试：总等待封顶（秒）。免费档服务端 1 并发限流，
# 高峰期常触发 429——按服务端头信息等待限流窗口结束后自动重试，
# 而不是直接中断对话；封顶后仍 429 才放弃。
_MAX_429_WAIT = 60.0

# 长对话摘要压缩：被裁剪的早期对话至少这么多条消息才值得压缩成摘要
_SUMMARIZE_MIN_TURNS = 4

# 工具执行结果日志截断长度（防刷屏；完整结果仍保留在工具上下文供模型使用）
_MAX_TOOL_RESULT_LOG = 300

# 跨轮历史中工具结果保留的最大长度（防 token 污染；本轮内仍用完整结果）
_MAX_TOOL_HISTORY_LOG = 500

# 生效话术建议文件：进化引擎写入（data/evolution_advice_active.json），
# 到期后由进化复盘回评续期/移除，这里只读取未到期条目注入系统提示
_ADVICE_ACTIVE_PATH = os.path.join(
    config.cfg.PROJECT_ROOT, "data", "evolution_advice_active.json")

# 生效话术建议读取缓存时长（秒）：避免每轮对话都读文件
_ADVICE_CACHE_TTL = 30

# 观众画像文件：进化引擎复盘提炼（data/evolution_profile.json），
# 本模块每轮按关键词召回注入系统提示（对标 hermes 的 USER.md/MEMORY.md）
_PROFILE_PATH = os.path.join(
    config.cfg.PROJECT_ROOT, "data", "evolution_profile.json")

# 画像读取缓存时长（秒）：避免每轮对话都读文件
_PROFILE_CACHE_TTL = 30

# 单轮最多注入的画像条数（按与当前消息相关度排序取前 N，控制 token）
_PROFILE_INJECT_MAX = 3

# GEPA 进化策略段文件：prompt_evo.py 择优落盘（data/evolution_policy.json），
# 本模块每轮读取注入系统提示（对标 hermes GEPA 的 prompt 进化层）
_POLICY_PATH = os.path.join(
    config.cfg.PROJECT_ROOT, "data", "evolution_policy.json")

# 策略段读取缓存时长（秒）：避免每轮对话都读文件
_POLICY_CACHE_TTL = 30


def _bigram_hits(text: str, query: str) -> int:
    """公共 2-gram 片段数：轻量关键词召回（无第三方分词依赖）。

    中文按字符 2-gram 取交集，两个文本共享片段越多说明越相关。
    """
    def bigrams(s: str) -> set:
        s = re.sub(r"\s+", "", s)
        return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()

    return len(bigrams(text) & bigrams(query))


def _summarize_tool_content(content) -> str:
    """工具结果历史摘要化：跨轮次历史中的工具消息只保留结果摘要。

    dict/list → JSON 单行；超长截断并加提示。发送给模型时仍走
    _clean_messages_for_api 的既有长度上限，不影响工具链配对。
    """
    if isinstance(content, (dict, list)):
        try:
            text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(content)
    else:
        text = str(content or "")
    if len(text) <= _MAX_TOOL_HISTORY_LOG:
        return text
    return text[:_MAX_TOOL_HISTORY_LOG] + f"…（结果过长，已存 {_MAX_TOOL_HISTORY_LOG} 字摘要）"


def _format_tool_result(name: str, result) -> str:
    """工具执行结果的结构化日志：JSON 压缩为一行 + 超长截断。

    控制中心日志区据此展示「工具名 + 结果摘要」的紧凑块，便于调试；
    截断仅影响日志展示，不裁剪进入工具上下文的结果。
    """
    if result is None:
        return f"  ↳ 「{name}」结果：（无返回）"
    if isinstance(result, (dict, list)):
        try:
            text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(result)
    else:
        text = str(result)
    text = text.strip()
    if len(text) > _MAX_TOOL_RESULT_LOG:
        text = (text[:_MAX_TOOL_RESULT_LOG]
                + f"…（超长已截断至 {_MAX_TOOL_RESULT_LOG} 字符，完整结果保留在工具上下文）")
    return f"  ↳ 「{name}」结果：{text}"


def _parse_retry_after(e) -> float:
    """从 429 异常的响应头解析服务端建议的等待秒数；解析失败返回 0。

    优先 Retry-After（秒数），其次 X-RateLimit-Reset（未来 Unix 时间戳，秒）。
    """
    try:
        headers = e.response.headers
    except Exception:
        return 0.0
    for name in ("Retry-After", "retry-after"):
        v = headers.get(name)
        if v:
            try:
                return max(0.0, float(v))
            except (TypeError, ValueError):
                pass
    for name in ("X-RateLimit-Reset", "x-ratelimit-reset"):
        v = headers.get(name)
        if v:
            try:
                ts = float(v)
                if ts > time.time():
                    return ts - time.time()
            except (TypeError, ValueError):
                pass
    return 0.0

# 工具响应内容长度上限（对标 llm-client.js _cleanMessagesForAPI 的 MAX_CONTENT_LENGTH）
_MAX_TOOL_CONTENT_LENGTH = 8000
# 控制字符（可能导致 JSON 解析失败）：移除不可见字符，保留换行符(\n)和制表符(\t)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")


def _find_sentence_end(text: str) -> int:
    """返回 text 中第一个句末符号的下标；找不到返回 -1。

    主循环（消费端）唯一的切句规则：按符号立即切，
    英文句号仅在后跟空格/结尾时算边界。
    """
    for i, ch in enumerate(text):
        if ch in _SENTENCE_ENDS:
            return i
        if ch == ".":
            # 英文句号：后跟空格或到结尾才算一句（'...'、小数、缩写不切）
            if i == len(text) - 1 or text[i + 1] == " ":
                return i
    return -1


# 角色扮演动作/表情标注（*blushes deeply* 等）：LLM 常用来表达情绪/动作，
# 若不清除 TTS 会直接念出来（"星号 blushes deeply 星号"），既"瞎喊"又拖慢合成。
_ACTION_ANNOT_RE = re.compile(r"\*[^*\n]*\*")

# 模型偶发输出 <time>...</time> 之类 HTML 标签（字段名/伪标签），
# 不清理会被 TTS 直接念成"小于 time 大于"。统一替换为空。
_HTML_TAG_RE = re.compile(r"<[^<>]{1,32}>")

# 思考内容防御过滤（对标 live-2d(2) filterThinkingContent）：
# Gemini/DeepSeek 等模型偶尔把 <think>/<thinking> 块混进 content
_THINK_BLOCK_RE = re.compile(r"<(?:think|thinking)>[\s\S]*?</(?:think|thinking)>", re.IGNORECASE)

# Qwen 文本格式工具调用（<tool_call>{json}</tool_call> 或 <fn_name attr="value"/>）
_QWEN_TOOL_CALL_JSON_RE = re.compile(r"<tool_call>\s*(\{[\s\S]*?\})\s*</tool_call>", re.IGNORECASE)
_QWEN_TOOL_CALL_XML_RE = re.compile(r"<(\w+)\s+([^>]+?)\/>", re.IGNORECASE)
# 常见 HTML 自闭合标签——XML 格式工具解析时跳过，避免误伤
_HTML_SELF_CLOSING_TAGS = {
    "br", "hr", "a", "img", "input", "meta", "link", "source", "area",
    "base", "col", "embed", "param", "track", "wbr",
}
# 清洗时移除工具调用文本，防止 TTS 念出 <tool_call> 等
_TOOL_CALL_TEXT_RE = re.compile(
    r"<tool_call>[\s\S]*?</tool_call>|<\w+\s+[^>]+?\/>", re.IGNORECASE
)

# 颜文字（(´•ω•̥`) / (T_T) 等）：GPT-SoVITS 无法合成（HTTP 500 死音）。
# 只匹配含"颜文字信号字符"的短括号（假名/希腊字母 ω/´/•/组合符/点/下划线等），
# 不会误伤 "(笑)" 这类正常括号。
_KAOMOJI_RE = re.compile(
    r"[（(](?=[^（）()]*[\u3040-\u30ff\u0370-\u03ff\u00b4\u2022\u0300-\u036f"
    r"\u30fb\u00b7\u005e\u005f\u0060])[^（）()]{1,14}[)）]"
)

# Emoji 过滤：匹配主流 Unicode emoji 区域（含 ZWJ 序列、肤色修饰符等）
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"   # 表情符号
    "\U0001F300-\U0001F5FF"   # 符号和象形文字
    "\U0001F680-\U0001F6FF"   # 交通和地图符号
    "\U0001F1E0-\U0001F1FF"   # 国旗
    "\U00002702-\U000027B0"   # 杂项符号
    "\U00002460-\U000024FF"   # 带括号的字母数字（Enclosed Alphanumerics，含 Ⓜ）
    "\U0001F000-\U0001F1FF"   # 麻将/多米诺/扑克牌/封闭字母数字补充
    "\U0001F900-\U0001F9FF"   # 补充符号和象形文字
    "\U0001FA00-\U0001FA6F"   # 国际象棋符号
    "\U0001FA70-\U0001FAFF"   # 符号和象形文字扩展-A
    "\U00002600-\U000026FF"   # 杂项符号
    "\U0000FE00-\U0000FE0F"   # 变体选择器
    "\U0000200D"              # 零宽连接符（ZWJ）
    "\U0000200C"              # 零宽非连接符
    "\U00002B50"              # 白色五角星⭐
    "\U00002728"              # 火花✨
    "\U00002764"              # 红心❤
    "\U0001F48B"              # 吻💋
    "\U0001F4AF"              # 100💯
    "\U0001F4A1"              # 灯泡💡
    "]+"
)

# 实质内容检测：句子清理后若不含任何中英文/数字/假名（纯标点、符号、空白），
# 直接丢弃——GPT-SoVITS 对无意义文本会合成退化，输出拖长音（"啊——"怪叫）。
_HAS_CONTENT_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def _strip_emojis(text: str) -> str:
    """移除文本中的 Emoji 字符。"""
    return _EMOJI_RE.sub("", text) if text else text


def _filter_thinking_content(text: str) -> str:
    """过滤模型混入 content 的思考内容（对标 live-2d(2) filterThinkingContent）。"""
    if not text:
        return text
    filtered = _THINK_BLOCK_RE.sub("", text)
    if re.match(r"^思考\s*\n", filtered):
        return ""
    if re.match(r"^Thinking\s*\n", filtered, re.IGNORECASE):
        return ""
    return filtered


def _remove_tool_calls_from_content(content: str) -> str:
    """从内容中移除工具调用部分（对标 llm-client.js _removeToolCallsFromContent）。"""
    if not content:
        return content
    return _TOOL_CALL_TEXT_RE.sub("", content).strip()


def _clean_sentence(sentence: str) -> str:
    """纯文本清洗（无副作用）：记忆标签 / 动作标注 / HTML / 工具文本 / 颜文字 / emoji。

    关键：绝不 strip 首尾空白！句子按软边界（空格）切分时，
    分界空格在 chunk 尾部；按硬边界（标点）切分时在下一 chunk 头部。
    strip 会吞掉单词分界，导致英文粘连（I mean → Imean），
    送进 TTS 后合成异常慢、甚至念出 *blushes* 之类的"瞎喊"。
    """
    if not sentence:
        return sentence
    sentence = memory.extract_and_strip(sentence)
    sentence = _filter_thinking_content(sentence)
    sentence = _ACTION_ANNOT_RE.sub(" ", sentence)
    sentence = _HTML_TAG_RE.sub("", sentence)
    sentence = _TOOL_CALL_TEXT_RE.sub(" ", sentence)
    sentence = _KAOMOJI_RE.sub(" ", sentence)
    sentence = _strip_emojis(sentence)
    return sentence


def _split_sentences(text: str) -> List[str]:
    """按句末符号把文本切成句子（与主循环同一切句规则，无长度兜底）。"""
    sentences: List[str] = []
    buffer = text
    while True:
        idx = _find_sentence_end(buffer)
        if idx < 0:
            if buffer:
                sentences.append(buffer)
            break
        sentences.append(buffer[: idx + 1])
        buffer = buffer[idx + 1:]
    return sentences


def _parse_qwen_tool_calls(content: str) -> Optional[List[dict]]:
    """解析 Qwen 模型的文本格式工具调用（对标 llm-client.js _parseQwenToolCalls）。

    格式1：<tool_call>{"name": ..., "arguments": {...}}</tool_call>
    格式2：<function_name attr1="value1" attr2="value2"/>
    """
    if not content:
        return None

    tool_calls: List[dict] = []
    # 时间戳前缀保证跨轮次唯一（历史保留完整工具链后，id 不能与其他轮冲突）
    _ts = int(time.time() * 1000)

    # 格式1：JSON
    for m in _QWEN_TOOL_CALL_JSON_RE.finditer(content):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, TypeError):
            continue
        name = data.get("name") or ""
        arguments = data.get("arguments") or {}
        tool_calls.append({
            "id": f"call_qwen_{_ts}_{len(tool_calls)}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        })

    # 格式2：XML 属性（跳过常见 HTML 自闭合标签）
    for m in _QWEN_TOOL_CALL_XML_RE.finditer(content):
        fname = m.group(1)
        if fname.lower() in _HTML_SELF_CLOSING_TAGS:
            continue
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2)))
        tool_calls.append({
            "id": f"call_qwen_{_ts}_{len(tool_calls)}",
            "type": "function",
            "function": {
                "name": fname,
                "arguments": json.dumps(attrs, ensure_ascii=False),
            },
        })

    return tool_calls or None


def _format_tool_calls(tool_calls: list) -> str:
    """格式化工具调用日志（对标 llm-handler.js formatToolCalls）。"""
    lines = []
    for tc in tool_calls:
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"] or "{}")
            arg_str = ", ".join(f"{k}={v}" for k, v in args.items()) or "（无参数）"
        except (json.JSONDecodeError, TypeError):
            arg_str = (tc["function"]["arguments"] or "")[:100] or "（无参数）"
        lines.append(f"AI调用了：{name} 工具 输入参数：{arg_str}")
    return "；".join(lines)


class LLMBrain:
    """LLM 流式大脑：支持多轮工具调用，按句 yield 纯对话文本。"""

    def __init__(self, mcp=None) -> None:
        self.cfg = config.cfg
        # 延迟导入，避免缺包时整个程序无法启动提示
        from openai import OpenAI
        self.client = OpenAI(
            api_key=self.cfg.LLM_API_KEY or "not-needed",
            base_url=self.cfg.LLM_BASE_URL or None,
            timeout=120.0,
            # 重试次数必须小：免费档限流(429)时，SDK 会按 1s/2s/4s…指数退避
            # 悄悄重试，max_retries=5 最多可干等 ~30s 才报错——正是"很慢"的元凶。
            # 只留 1 次重试，真限流立刻走友好提示，而不是把几秒耗在空等上。
            max_retries=2,
        )
        self.history: list = []
        # LLM 并发信号量（.env LLM_MAX_CONCURRENCY，默认 2）：用户对话 + agent
        # 主动 + 弹幕回复共用，同时最多 N 个 LLM 推理，超出排队等待而非无限
        # 并发——本地大模型防显存打满、远程 API 防限流。
        self._llm_semaphore = asyncio.Semaphore(
            max(1, int(config.cfg.LLM_MAX_CONCURRENCY or 2)))
        # 长对话摘要压缩（对标 NagaAgent 上下文压缩）：
        # 历史超长被裁剪时，后台把丢弃的早期轮次压缩成摘要，
        # 下一轮以 system 段注入，避免长聊后早期信息永久丢失；
        # 摘要同时写入记忆实现跨会话继承。
        self._session_summary: Optional[str] = None
        self._summary_task: Optional[asyncio.Task] = None
        # MCP 管理器（外部工具服务器）；None 表示禁用
        self.mcp = mcp
        # 生效话术建议缓存（进化引擎写入 active json，30s TTL 防每轮读文件）
        self._advice_cache_ts = 0.0
        self._advice_cache: list[str] = []
        # 观众画像缓存（进化引擎写入 profile json，30s TTL 防每轮读文件）
        self._profile_cache_ts = 0.0
        self._profile_cache: list[dict] = []
        # GEPA 进化策略段缓存（prompt_evo.py 写入 policy json，30s TTL）
        self._policy_cache_ts = 0.0
        self._policy_cache: str = ""
        # 模型路由进化（多臂老虎机）：配置多 LLM 服务时按历史表现选服务；
        # 未配置/未启用时 router 为 None，完全走原有单一 LLM 服务逻辑
        from src.llm.model_router import get_router
        self.router = get_router()

    def reload_client(self) -> None:
        """控制中心「更新配置」热更新后重建 OpenAI client。

        API Key / Base URL / 模型名变化时 client 需重建；LLM_MODEL 变化
        也会随 cfg 单例在下一轮对话读取时自动生效。
        """
        from openai import OpenAI
        self.cfg = config.cfg  # 指向 reload 后的最新单例
        self.client = OpenAI(
            api_key=self.cfg.LLM_API_KEY or "not-needed",
            base_url=self.cfg.LLM_BASE_URL or None,
            timeout=120.0,
            max_retries=2,
        )

    # ---------- 生效话术建议注入 ----------

    def _active_advice_section(self) -> str:
        """读取未到期的生效话术建议，拼装成注入系统提示的段落。

        30s TTL 缓存避免每轮对话都读文件；无生效建议时返回空字符串。
        """
        now = time.time()
        if now - self._advice_cache_ts >= _ADVICE_CACHE_TTL:
            self._advice_cache_ts = now
            try:
                with open(_ADVICE_ACTIVE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._advice_cache = [
                    (it.get("text") or "").strip()
                    for it in data
                    if isinstance(it, dict) and (it.get("expires") or 0) > now
                ]
            except (OSError, ValueError):
                self._advice_cache = []
        if not self._advice_cache:
            return ""
        return ("### 生效中的话术建议（直播时尽量遵循，若已被验证无效可忽略）\n"
                + "\n".join(f"- {t}" for t in self._advice_cache))

    # ---------- 观众画像注入（进化引擎复盘的长期事实，关键词召回） ----------

    def _profile_section(self, user_text: str) -> str:
        """按关键词召回观众画像，拼装成注入系统提示的段落。

        轻量关键词召回（公共 2-gram 片段，无第三方分词依赖），补充向量
        记忆检索之外的召回；30s TTL 缓存避免每轮对话都读文件；
        无相关条目时返回空字符串。
        """
        now = time.time()
        if now - self._profile_cache_ts >= _PROFILE_CACHE_TTL:
            self._profile_cache_ts = now
            try:
                with open(_PROFILE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._profile_cache = [
                    {"owner": (it.get("owner") or "").strip()[:32],
                     "fact": (it.get("fact") or "").strip()}
                    for it in data
                    if isinstance(it, dict) and (it.get("fact") or "").strip()
                ]
            except (OSError, ValueError):
                self._profile_cache = []
        if not self._profile_cache:
            return ""
        query = (user_text or "").strip()
        if not query:
            return ""
        # 相关度 = 与当前消息的公共 2-gram 片段数，降序取前 N
        hits = sorted(
            ((_bigram_hits(it["fact"], query), it) for it in self._profile_cache),
            key=lambda x: x[0], reverse=True,
        )
        top = [it for score, it in hits if score > 0][:_PROFILE_INJECT_MAX]
        if not top:
            return ""
        return ("### 观众画像（复盘提炼的长期事实，相关时自然融入回答，不要机械复述）\n"
                + "\n".join(f"- {it['owner'] or 'chao'}：{it['fact']}" for it in top))

    # ---------- GEPA 进化策略段注入 ----------

    def _policy_section(self) -> str:
        """读取 GEPA 择优落盘的行为策略段，拼装成注入系统提示的段落。

        30s TTL 缓存避免每轮对话都读文件；无策略或未启用进化时返回空字符串。
        """
        if not getattr(self.cfg, "EVOLUTION_PROMPT_EVO_ENABLED", True):
            return ""
        now = time.time()
        if now - self._policy_cache_ts >= _POLICY_CACHE_TTL:
            self._policy_cache_ts = now
            try:
                with open(_POLICY_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._policy_cache = (
                    (data.get("text") or "").strip() if isinstance(data, dict) else ""
                )
            except (OSError, ValueError):
                self._policy_cache = ""
        if not self._policy_cache:
            return ""
        return ("### 进化行为策略（GEPA 迭代沉淀，直播中相关情境优先遵循，"
                "与生效中的话术建议冲突时以本策略为准）\n" + self._policy_cache)

    # ---------- 工具 ----------

    def _get_tools(self) -> List[dict]:
        """合并 MCP + 本地工具（对标 live-2d(2) getMergedToolsList）。

        每轮对话实时获取一次（不缓存）：MCP 服务器可能在运行中动态增删工具。
        """
        from src.llm.tools import get_merged_tools
        return get_merged_tools(self.mcp)

    def _clean_messages_for_api(self, messages: List[dict]) -> List[dict]:
        """清理消息格式，确保 API 兼容（对标 llm-client.js _cleanMessagesForAPI）。

        - assistant 有 tool_calls 但 content 为 null → 设为 ''（部分 API 要求 content 非 null）
        - tool 消息：content 对象→JSON 字符串、移除控制字符、超过 8000 截断
        - 最后用 sanitize_tool_message_sequence 兜底，保证 tool_calls 与 tool 响应严格配对
        """
        normalized: List[dict] = []
        for msg in messages:
            msg = dict(msg)
            if msg.get("role") == "assistant":
                # 有 tool_calls 但 content 为 null 时，某些 API 要求 content 不能为 null
                if msg.get("content") is None and msg.get("tool_calls"):
                    msg["content"] = ""
            elif msg.get("role") == "tool":
                content = msg.get("content")
                # content 是对象/数组 → 转 JSON 字符串
                if isinstance(content, (dict, list)):
                    try:
                        content = json.dumps(content, ensure_ascii=False)
                    except (TypeError, ValueError):
                        content = str(content)
                # 确保 content 是字符串
                if not isinstance(content, str):
                    content = str(content or "")
                # 移除控制字符（可能导致 JSON 解析失败），保留 \n 和 \t
                content = _CONTROL_CHAR_RE.sub("", content)
                # 超长内容截断，避免超大响应
                if len(content) > _MAX_TOOL_CONTENT_LENGTH:
                    content = content[:_MAX_TOOL_CONTENT_LENGTH] + "...(内容过长已截断)"
                msg = {
                    "role": "tool",
                    "name": msg.get("name") or "unknown_tool",
                    "content": content,
                    "tool_call_id": msg.get("tool_call_id"),
                }
            normalized.append(msg)

        # 最后一道防线：清理 assistant.tool_calls 与 tool 响应不配对的序列
        return sanitize_tool_message_sequence(normalized)

    async def _execute_tool_calls(self, tool_calls: list) -> List[dict]:
        """并行执行工具并返回结果消息（对标 NagaAgent agentic_tool_loop：
        一轮多个工具 asyncio.gather 并行执行，显著缩短多工具轮次延迟）。

        gather 保持返回顺序与 tool_calls 一致，tool_call_id 配对不乱；
        单个工具失败只影响该工具，不拖垮整轮。
        """
        from src.llm.tools import call_tool

        async def _run(tc: dict) -> dict:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            console.dim(f"  ↳ 执行「{name}」...")
            result = await _call_tool_with_retry(name, args, self.mcp)
            console.dim(_format_tool_result(name, result))
            return {
                "role": "tool",
                "name": name,
                "tool_call_id": tc.get("id") or f"call_{name}",
                "content": result,
            }

        async def _call_tool_with_retry(name: str, args: dict, mcp) -> Any:
            """单工具执行失败自动重试 1 次（指数退避 1s），减少偶发失败。"""
            try:
                return await call_tool(name, args, mcp)
            except Exception as e:
                console.warn(f"  ↳ 「{name}」执行失败（{e}），1s 后自动重试...")
                await asyncio.sleep(1.0)
                return await call_tool(name, args, mcp)

        return await asyncio.gather(*(_run(tc) for tc in tool_calls))

    async def _request_final_reply(self, messages: List[dict]) -> str:
        """轮数超限后的非流式兜底（对标 llm-handler.js L684-696）。

        用 tools=[]（等效不传）强制模型基于已有工具结果给出最终文字回复，
        避免"工具链跑满 30 轮却一句人话都没有"。
        """
        kwargs = dict(
            model=self.cfg.LLM_MODEL,
            messages=self._clean_messages_for_api(messages),
            stream=False,
            max_tokens=2048,
            temperature=0.95,
        )
        extra_body = {"thinking": {"type": "enabled"
                                   if self.cfg.LLM_THINKING else "disabled"}}
        try:
            try:
                resp = self.client.chat.completions.create(**kwargs, extra_body=extra_body)
            except Exception:
                # 服务不支持 thinking 字段时降级重试
                console.warn("LLM 服务不支持 thinking 参数，降级为普通模式")
                resp = self.client.chat.completions.create(**kwargs)
            message = resp.choices[0].message
            content = getattr(message, "content", None) or ""
            # reasoning_content 替代空 content（仅非流式且无 tool_calls；对标 llm-client.js L107-109）
            if (not content.strip()
                    and getattr(message, "reasoning_content", None)
                    and not getattr(message, "tool_calls", None)):
                content = getattr(message, "reasoning_content") or ""
            content = _filter_thinking_content(content or "")
            if content.strip():
                return content
        except Exception as e:
            console.error(f"获取最终回复失败：{e}")
        return "抱歉，任务太复杂了，我已经尽力了~"

    # ---------- 流式对话 ----------

    def _consume_summary(self) -> Optional[str]:
        """取后台摘要任务的结果（完成即用，未完成则等下一轮），返回当前生效摘要。

        非阻塞：不等待任务，避免拖慢本轮对话首字延迟。
        """
        if self._summary_task is not None:
            if self._summary_task.done():
                try:
                    result = self._summary_task.result()
                    if isinstance(result, str) and result.strip():
                        self._session_summary = result.strip()
                except Exception as e:
                    console.dim(f"[摘要] 后台压缩失败：{e}")
                self._summary_task = None
        return self._session_summary

    async def _summarize_dropped(self, turns: list[dict]) -> str:
        """后台任务：把被裁剪的早期对话压缩成中文摘要，并写入记忆跨会话继承。"""
        try:
            from src.llm.agent import ButlerAgent, _pick_owner
            butler = ButlerAgent()
            text = await asyncio.wait_for(
                butler.summarize_session(turns), timeout=30.0
            )
            text = (text or "").strip()
            if not text:
                return ""
            # 摘要写入记忆（归属从被裁剪轮次推断），重启后检索可带出
            try:
                owner = _pick_owner(turns, None)
                await butler.commit_recall_files([{
                    "name": "session-summary",
                    "description": "archive/对话摘要：早期对话压缩",
                    "content": text,
                    "user": owner,
                }])
            except Exception:
                pass
            return text
        except Exception as e:
            console.dim(f"[摘要] 后台压缩失败：{e}")
            return ""

    def _max_tool_iterations(self) -> int:
        """工具调用轮数上限（对标 NagaAgent max_loop_stream：.env TOOL_MAX_ITERATIONS 可配，默认 30）。"""
        try:
            return max(1, int(config.cfg.TOOL_MAX_ITERATIONS or _MAX_TOOL_ITERATIONS))
        except (TypeError, ValueError, AttributeError):
            return _MAX_TOOL_ITERATIONS

    async def chat_stream(self, user_text: str, *, proactive: bool = False,
                          history: Optional[list] = None) -> AsyncGenerator[str, None]:
        """流式对话生成器：多轮工具调用，实时打印思考过程，按句 yield 纯对话文本。

        proactive=True 表示这是「内部自主行动指令」（主动发言）而非用户消息：
        请求时照常以 user 角色注入以便模型理解，但保存历史时会剔除该条
        prompt（不冒充用户发言），只保留模型回复保持上下文连贯
        （对标 Muika 的 time_tick prompt 不写入 recent_turns）。

        history：可选历史快照（agent 主动发言只给最近 N 条精简上下文，降
        token）；None 时用完整 self.history（与原有行为一致）。
        """
        # LLM 并发信号量：用户对话 / agent 主动 / 弹幕回复共用，防止同时
        # 发起多个 LLM 请求（本地 GPU 显存打满 / 远程 API 限流）
        async with self._llm_semaphore:
            async for item in self._chat_stream_inner(user_text, proactive=proactive, history=history):
                yield item

    async def _chat_stream_inner(self, user_text: str, *, proactive: bool,
                                 history: Optional[list]) -> AsyncGenerator[str, None]:
        """chat_stream 实际实现体（信号量外），保持原有逻辑不变。"""
        tracker = PerfTracker("LLM")
        tracker.begin("首字延迟")     # 从调用到第一个 content chunk
        tracker.begin("总生成")       # 整个流程（含工具调用）耗时

        # 注入记忆上下文（严格参照 memU hosts/instruction.py 的 standing instruction）：
        # 1) 记忆使用说明常驻系统提示（segments/files 两层渐进 + fail-open）；
        # 2) 检索结果按 memU hosts/retrieval.py _shape_for_agent 的三层形状注入。
        #    Embedding 不可用/失败时自动回退 LLM 检索（仍输出同形状）。
        sys_content = self.cfg.SYSTEM_PROMPT
        if self.cfg.MEMORY_ENABLED:
            sys_content += "\n\n" + memory.STANDING_INSTRUCTION
            mem_ctx = await memory.retrieve(user_text)
            # 记忆写入完全交给管家模型（ButlerAgent 每轮从对话提取，参照
            # <memory> 标签（曾在此注入写标签指令，主模型经常漏写/写错）
            if mem_ctx:
                sys_content += (
                    "\n\n### 检索到的记忆（segments / files，按相关度排序）\n"
                    + mem_ctx
                )
        # 注入观众画像（进化引擎复盘的长期事实，关键词召回补充向量记忆）
        profile_section = self._profile_section(user_text)
        if profile_section:
            sys_content += "\n\n" + profile_section
        # 注入技能段（严格参照 Muika agent.py：系统提示 = 人设 + Available skills 段）。
        # 只列技能名+描述（轻量），完整指令由 load_skill 工具按需加载。
        skills_section = get_skill_manager().render_prompt_section()
        if skills_section:
            sys_content += "\n\n" + skills_section
        # 注入生效中的话术建议（进化引擎沉淀，到期由复盘回评续期/移除）
        advice_section = self._active_advice_section()
        if advice_section:
            sys_content += "\n\n" + advice_section
        # 注入 GEPA 进化策略段（对标 hermes 的 GEPA：变异 → 评审择优落盘，
        # 与话术建议互补——策略是长期行为准则，建议是短期话术优化）
        policy_section = self._policy_section()
        if policy_section:
            sys_content += "\n\n" + policy_section
        messages: List[dict] = [{"role": "system", "content": sys_content}]
        # 注入早期对话摘要（历史裁剪时后台压缩生成，跨会话继承）
        summary = self._consume_summary()
        if summary:
            messages.append({
                "role": "system",
                "content": f"【早期对话摘要（以下为被压缩掉的更早对话内容）】{summary}",
            })
        # agent 主动发言：只带精简历史快照（最近 N 条），降低 token 消耗
        messages.extend(history if history is not None else self.history)
        messages.append({"role": "user", "content": user_text})

        # 每轮对话实时合并一次工具列表（对标 live-2d(2) getMergedToolsList，不缓存）
        tools = self._get_tools()

        # 模型路由进化：本轮按 UCB1 选择要用的 LLM 服务（未启用时均为
        # None → 沿用 self.client / LLM_MODEL，与原有行为完全一致）
        route_name = None
        route_client = None
        route_model = None
        if self.router is not None:
            route_name = self.router.select()
            if route_name is not None:
                route_client = self.router.client_for(route_name)
                service = self.router.service(route_name)
                route_model = (service or {}).get("model") or None

        loop = asyncio.get_running_loop()
        iteration = 0            # 工具调用轮数（含空响应催促轮）
        empty_count = 0          # 连续空响应计数
        tool_call_total = 0      # 工具调用总次数
        sentence_count = 0       # 已产出句数
        final_reply: Optional[str] = None

        # ===== 多轮工具调用循环（对标 llm-handler.js 的 while (iteration < maxIterations)） =====
        max_tool_iterations = self._max_tool_iterations()
        while iteration < max_tool_iterations:
            q: asyncio.Queue = asyncio.Queue()
            tool_calls_acc: List[Optional[dict]] = []
            full_raw: List[str] = []
            round_content: List[str] = []
            _first_content = True

            def _run(messages=messages, tools=tools) -> None:
                """子线程：同步迭代流式响应，推送 content 增量并累积 tool_calls。"""
                nonlocal _first_content

                def _create():
                    """发起一次带 tools 的流式请求。thinking 字段不被支持时
                    自动降级重试；路由服务整体不可用时回退默认 LLM 服务重试一次。"""
                    nonlocal extra_body, route_name, route_client, route_model
                    client = route_client or self.client
                    model = route_model or self.cfg.LLM_MODEL

                    def _send():
                        kwargs = dict(
                            model=model,
                            messages=self._clean_messages_for_api(messages),
                            stream=True,
                            max_tokens=2048,
                            temperature=0.95,
                        )
                        if tools:
                            kwargs["tools"] = tools
                        if extra_body is not None:
                            return client.chat.completions.create(
                                **kwargs, extra_body=extra_body)
                        return client.chat.completions.create(**kwargs)

                    try:
                        return _send()
                    except Exception as e:
                        # 429 是服务端限流不是 thinking 不支持，不能走降级分支
                        # （否则会把 429 误判成「不支持 thinking」打误导警告）
                        from openai import RateLimitError
                        if (extra_body is None
                                or isinstance(e, RateLimitError)
                                or "429" in str(e)):
                            raise
                        console.warn("LLM 服务不支持 thinking 参数，降级为普通模式")
                        extra_body = None
                        try:
                            return _send()
                        except Exception as e2:
                            from openai import RateLimitError
                            if isinstance(e2, RateLimitError) or "429" in str(e2):
                                raise
                            # 路由服务整体不可用 → 记录失败并回退默认服务重试
                            # （route_name 清空后成功路径不再误记到该服务头上）
                            if route_name is not None:
                                self.router.record(route_name, False)
                                console.warn(
                                    f"[模型路由] 服务 {route_name!r} 不可用，"
                                    "回退默认 LLM 服务重试")
                                route_name = None
                                route_client = None
                                route_model = None
                                client = self.client
                                model = self.cfg.LLM_MODEL
                                return _send()
                            raise

                def _push(content: str) -> None:
                    """把 content 增量推回主循环（记录首字延迟）。"""
                    nonlocal _first_content
                    if not content:
                        return
                    if _first_content:               # 首字到达
                        _first_content = False
                        loop.call_soon_threadsafe(tracker.end, "首字延迟")
                    loop.call_soon_threadsafe(q.put_nowait, content)

                try:
                    # —— 组装请求（thinking 显式控制） ——
                    # GLM-4.5/4.7 系列默认「强制思考」：即使不传 thinking 参数，
                    # 模型也会输出思维链（造成"明明关了还在思考"）。
                    # 所以关闭时必须显式传 {"type": "disabled"} 才能真正禁用；
                    # 不支持 thinking 字段的服务（OpenAI 官方等）会 400，自动降级重试。
                    def _drain(response) -> None:
                        """迭代一个流式响应：推送 content 增量 + 累积工具调用。"""
                        for chunk in response:
                            if not getattr(chunk, "choices", None):
                                continue
                            delta = chunk.choices[0].delta
                            reasoning = getattr(delta, "reasoning_content", None) or ""
                            if reasoning:
                                # 思考过程：灰字实时打印
                                print(console.paint(reasoning, console.GRAY), end="", flush=True)
                            content = getattr(delta, "content", None) or ""
                            if content:
                                full_raw.append(content)
                                _push(content)
                            # —— 工具调用增量累积（对标 llm-client.js _handleStreamResponse）——
                            tcs = getattr(delta, "tool_calls", None)
                            if tcs:
                                for tc in tcs:
                                    index = tc.index
                                    while len(tool_calls_acc) <= index:
                                        tool_calls_acc.append(None)
                                    if tool_calls_acc[index] is None:
                                        tool_calls_acc[index] = {
                                            "id": "",
                                            "type": "function",
                                            "function": {"name": "", "arguments": ""},
                                        }
                                    if getattr(tc, "id", None):
                                        tool_calls_acc[index]["id"] = tc.id
                                    fn = getattr(tc, "function", None)
                                    if fn is not None:
                                        if getattr(fn, "name", None):
                                            tool_calls_acc[index]["function"]["name"] = fn.name
                                        if getattr(fn, "arguments", None):
                                            tool_calls_acc[index]["function"]["arguments"] += fn.arguments

                    # 429 自动等待重试：免费档服务端 1 并发限流（高峰期常触发）。
                    # 按服务端 Retry-After / X-RateLimit-Reset 等待限流窗口结束后
                    # 自动重试，而不是直接中断；总等待封顶 _MAX_429_WAIT 秒，
                    # 超时仍 429 才放弃（走 __RATELIMIT__ 友好提示）。
                    waited = 0.0
                    while True:
                        try:
                            extra_body = {"thinking": {"type": "enabled"
                                                       if self.cfg.LLM_THINKING else "disabled"}}
                            _drain_start = time.perf_counter()
                            _drain(_create())
                            # 路由服务调用成功 → 记录奖励与耗时（供 UCB1 择优）；
                            # route_name 已被回退清空时不记录（成功属于默认服务）
                            if route_name is not None:
                                self.router.record(
                                    route_name, True,
                                    time.perf_counter() - _drain_start)
                            break
                        except Exception as e:
                            from openai import RateLimitError
                            if not (isinstance(e, RateLimitError) or "429" in str(e)):
                                raise
                            wait = _parse_retry_after(e) or 15.0   # 无头信息用保守默认
                            wait = min(wait, _MAX_429_WAIT - waited)
                            if wait <= 0:
                                raise
                            waited += wait
                            print(console.paint(
                                f"⏳ LLM 限流(429)，等待 {wait:.0f}s 后自动重试…",
                                console.YELLOW), flush=True)
                            time.sleep(wait)
                except Exception as e:
                    # 429 自动重试耗尽 → 中文友好提示；其他错误原样上报
                    from openai import RateLimitError
                    if isinstance(e, RateLimitError) or "429" in str(e):
                        loop.call_soon_threadsafe(
                            q.put_nowait,
                            "__RATELIMIT__::你的 LLM 账户达到速率限制（429），"
                            "自动等待重试后仍被限流。\n"
                            "  免费模型限 1 并发，高峰期请稍等片刻再提问。",
                        )
                    else:
                        loop.call_soon_threadsafe(q.put_nowait, f"__ERROR__::{e}")
                finally:
                    loop.call_soon_threadsafe(q.put_nowait, None)  # 结束哨兵

            # 子线程跑同步流式迭代
            bg_task = loop.run_in_executor(None, _run)

            # 首轮思考过程提示
            if iteration == 0 and self.cfg.LLM_THINKING:
                print(console.paint("💭 思考过程：", console.GRAY), end="", flush=True)

            # 主协程：消费 content 增量，按句切分 yield
            buffer = ""

            def _emit(sentence: str) -> str:
                """清洗、保存记忆、计数并返回清理后的文本。空文本返回 ''。"""
                nonlocal sentence_count
                cleaned = _clean_sentence(sentence)
                # 纯标点/符号/空白句（无实质文字）直接丢弃：
                # GPT-SoVITS 对这类文本会合成退化，输出拖长音怪叫
                if not cleaned.strip() or not _HAS_CONTENT_RE.search(cleaned):
                    return ""
                round_content.append(cleaned)
                sentence_count += 1
                return cleaned

            while True:
                item = await q.get()
                if item is None:
                    break
                if isinstance(item, str) and item.startswith("__ERROR__::"):
                    console.error(f"LLM 调用失败：{item[len('__ERROR__::'):]}")
                    await bg_task
                    tracker.end("总生成", f"{sentence_count} 句（出错中断）")
                    tracker.print_report()
                    return
                if isinstance(item, str) and item.startswith("__RATELIMIT__::"):
                    console.warn(item[len("__RATELIMIT__::"):])
                    await bg_task
                    tracker.end("总生成", f"{sentence_count} 句（限流）")
                    tracker.print_report()
                    return
                buffer += item

                # 按符号切分：遇句末符号（。！？!?…换行 / 英文 '.'+空格）立即切出。
                # 不做长度兜底——没有符号就继续等，保证语义完整、不拆词。
                while True:
                    idx = _find_sentence_end(buffer)
                    if idx < 0:
                        break
                    sentence = buffer[: idx + 1]
                    buffer = buffer[idx + 1:]
                    cleaned = _emit(sentence)
                    if cleaned:
                        yield cleaned

            # 收尾：剩余内容作为最后一句
            cleaned = _emit(buffer)
            if cleaned:
                yield cleaned

            await bg_task

            # ---- 流结束：判断本轮结果（对标 llm-client.js _handleStreamResponse）----
            raw_content = "".join(full_raw)
            # 先过滤思考内容，再解析工具调用（与 JS 顺序一致）
            clean_content = _filter_thinking_content(raw_content)
            tool_calls = [tc for tc in tool_calls_acc if tc and tc["function"]["name"]]

            # Qwen 文本格式工具调用解析（对标 llm-client.js _parseQwenToolCalls）
            if not tool_calls:
                parsed = _parse_qwen_tool_calls(clean_content)
                if parsed:
                    tool_calls = parsed
                    console.info(f"🔧 解析到 {len(parsed)} 个 Qwen 文本格式工具调用")
                    # 从 content 中移除工具调用文本，只保留文本回复（对标 _removeToolCallsFromContent）
                    clean_content = _remove_tool_calls_from_content(clean_content)

            if tool_calls:
                # ===== 执行工具并进入下一轮（对标 llm-handler.js） =====
                iteration += 1
                tool_call_total += len(tool_calls)
                console.accent(f"===== 🔧 第 {iteration} 轮工具调用 =====")
                console.accent(_format_tool_calls(tool_calls))

                # 1) assistant 消息（含 tool_calls；content 为 null 时
                #    由 _clean_messages_for_api 兜底转为 ''，兼容严格模式 API）
                messages.append({
                    "role": "assistant",
                    "content": clean_content.strip() or None,
                    "tool_calls": tool_calls,
                })

                # 2) 执行工具 → tool 响应消息（工具链完整进入下一轮上下文）
                tool_messages = await self._execute_tool_calls(tool_calls)
                messages.extend(tool_messages)
                continue

            # ===== 无工具调用 =====
            if not clean_content.strip():
                # 空响应处理（对标 llm-handler.js consecutiveEmptyResponses：
                # 第 1 次催模型回复，第 2 次仍空则放弃）
                empty_count += 1
                if empty_count == 1:
                    console.warn("⚠️ 空响应，添加提示消息催促模型回复")
                    messages.append({
                        "role": "user",
                        "content": "请根据工具执行结果，回复用户。",
                    })
                    iteration += 1
                    continue
                console.error(f"❌ 连续 {empty_count} 次空响应，放弃等待")
                final_reply = "抱歉，我好像卡住了，请重新问我吧~"
                yield final_reply
                break

            # 正常产出文本，结束（本轮已流式 yield 的句子即最终回复）
            final_reply = "".join(round_content)
            break

        # ===== 达到最大工具轮数：非流式强制获取最终回复（对标 llm-handler.js L684-696）=====
        if final_reply is None and iteration >= max_tool_iterations:
            console.warn(f"⚠️ 已达到最大工具调用次数限制（{max_tool_iterations} 轮），"
                         "非流式获取最终回复")
            final_reply = await self._request_final_reply(messages)
            for seg in _split_sentences(final_reply):
                cleaned = _clean_sentence(seg)
                if cleaned.strip():
                    yield cleaned

        # 思考过程换行
        if self.cfg.LLM_THINKING:
            print()

        meta = f"{sentence_count} 句"
        if tool_call_total:
            meta += f"，{tool_call_total} 次工具调用"
        tracker.end("总生成", meta)

        # ===== 历史保存完整工具链（对标 live-2d(2)：
        # assistant+tool_calls + tool 响应都进历史，跨轮保留上下文）=====
        if final_reply is not None:
            messages.append({"role": "assistant", "content": final_reply})
        if history is not None:
            # 用快照发起的请求（agent 主动发言）：只把本轮新产出的消息并入
            # 真实历史，不能整体替换——否则被精简掉的早期轮次会丢失
            new_parts = messages[1 + len(history):]
            self.history = self.history + [
                m for m in new_parts
                if not (m.get("role") == "user"
                        and m.get("content") == user_text)
            ]
        else:
            # 丢弃 system（每轮按最新记忆重建），其余完整保留
            self.history = messages[1:]
            if proactive:
                # 主动发言：剔除注入的内部指令 user 消息（不冒充用户发言），
                # 保留 assistant 回复以维持上下文连贯
                self.history = [m for m in self.history
                                if not (m.get("role") == "user"
                                        and m.get("content") == user_text)]
        # 工具结果历史摘要化（对标 NagaAgent「消除历史污染」）：
        # 跨轮次历史中的 tool 消息只保留结果摘要（截断超长 JSON），
        # 防 token 污染；本轮内 messages 保持完整，不影响多轮工具调用链。
        self.history = [
            {**m, "content": _summarize_tool_content(m["content"])}
            if m.get("role") == "tool" else m
            for m in self.history
        ]
        max_messages = self.cfg.HISTORY_ROUNDS * 2
        if len(self.history) > max_messages:
            dropped = self.history[: len(self.history) - max_messages]
            # 以「单元」为粒度从后向前裁剪，不切断工具调用链
            self.history = trim_messages_preserving_tool_rounds(
                self.history, max_messages
            )
            # 被裁剪的早期轮次足够多时，后台压缩成摘要（不阻塞本轮，
            # 下一轮对话开始时若已生成则注入）
            if self._summary_task is None and len(dropped) >= _SUMMARIZE_MIN_TURNS:
                self._summary_task = asyncio.create_task(
                    self._summarize_dropped(dropped)
                )

        # 打印 LLM 性能报告
        tracker.print_report()