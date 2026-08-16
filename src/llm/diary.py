"""日记写作：随时让 LLM 基于当天对话写一篇日记并落盘。

与 daily-diary skill（src/llm/skills/daily-diary/SKILL.md）打通技能系统：
- 默认走技能机制：system 注入「可用技能」段，模型自己 load_skill 选择
  合适的技能（如 daily-diary）并按其文笔写作（技能使用会被记录，供进化
  引擎复盘）
- TOOL_LOAD_SKILL_ENABLED 关闭时回退：直接注入 daily-diary 的 SKILL.md
  写作规则 + references/blogs 范文，保证不依赖技能开关也能写出同风格

- 素材：当天对话轮次（弹幕/主播/AI 三方，format_turns_text 格式化）；
  素材过少时提示 LLM 自由发挥
- 落盘：data/diary/YYYY-MM-DD.md（DATA_ROOT 派生）；当天已有日记时旧文
  一并给 LLM 参考重写合并，多次触发不丢内容
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from openai import AsyncOpenAI

from plugins.tools.defs import _LOCAL_TOOL_DEFS
from plugins.tools.skill_loader import _load_skill, _read_skill_resource
from plugins.tools.skills import get_skill_manager
from src.llm.client.factory import build_thinking_extra_body, get_async_openai_client
from src.utils import config, console
from tools.memory import memory

# 素材轮次上限（超长会话截断，保证 prompt 可控）
_DIARY_MAX_TURNS = 200
# 写作规则 / 参考范文注入的最大字符数（防长文撑爆上下文，仅回退路径使用）
_REF_MAX_CHARS = 6000
# 生成超时与最大 token（日记较长，给足余量）
_MODEL_TIMEOUT = 120.0
_MAX_TOKENS = 4096
# 技能选择轮次上限：模型通常 1 轮 load_skill（+1 轮读范文）后输出正文
_SKILL_STEPS = 3

# 技能系统工具：模型写日记前可自主 load_skill / read_skill_resource
_SKILL_TOOLS = [
    t for t in _LOCAL_TOOL_DEFS
    if t["function"]["name"] in ("load_skill", "read_skill_resource")
]


def _skills_enabled() -> bool:
    """技能系统是否可用（总开关 + load_skill 常驻开关）。"""
    return bool(config.cfg.TOOLS_ENABLED and config.cfg.TOOL_LOAD_SKILL_ENABLED)


def _diary_skill_dir() -> Path:
    """daily-diary 技能目录（SKILLS_DIR 可能逗号分隔多个路径，取第一个）。"""
    root = (config.cfg.SKILLS_DIR or "src/llm/skills").split(",")[0].strip()
    return Path(config.cfg.PROJECT_ROOT) / root / "daily-diary"


def _load_skill_body() -> str:
    """读取 daily-diary SKILL.md 正文（去 YAML frontmatter，截断）。"""
    try:
        raw = (_diary_skill_dir() / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    if raw.startswith("---"):
        sep = raw.find("\n---", 3)
        if sep > 0:
            raw = raw[sep + 4:].lstrip("\n")
    return raw.strip()[:_REF_MAX_CHARS]


def _load_reference_blog() -> str:
    """读取 references/blogs 下最近修改的一篇范文（截断，供文笔参考）。"""
    blogs = _diary_skill_dir() / "references" / "blogs"
    try:
        files = sorted(blogs.glob("*.md"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return ""
    if not files:
        return ""
    try:
        return files[0].read_text(encoding="utf-8").strip()[:_REF_MAX_CHARS]
    except OSError:
        return ""


def _diary_dir() -> Path:
    """日记落盘目录（可写数据根 DATA_ROOT 派生）。"""
    return Path(config.cfg.DATA_ROOT) / "diary"


class DiaryWriter:
    """日记生成器：收集素材 → 调 LLM → 落盘 data/diary/YYYY-MM-DD.md。"""

    def __init__(self) -> None:
        self._candidates: list[tuple[AsyncOpenAI, str, str]] = []

    def _ensure_candidates(self) -> list[tuple[AsyncOpenAI, str, str]]:
        """候选模型链：主模型（LLM_*，写作质量优先）→ 管家模型（BUTLER_* 兜底）。"""
        if self._candidates:
            return self._candidates
        cfg = config.cfg
        main = ((cfg.LLM_BASE_URL or "").strip(),
                (cfg.LLM_API_KEY or "").strip(),
                (cfg.LLM_MODEL or "").strip())
        if all(main):
            self._candidates.append((
                get_async_openai_client(api_key=main[1], base_url=main[0],
                                        timeout=_MODEL_TIMEOUT),
                main[2], "主模型"))
        butler = ((cfg.BUTLER_BASE_URL or "").strip(),
                  (cfg.BUTLER_API_KEY or "").strip(),
                  (cfg.BUTLER_MODEL or "").strip())
        if all(butler) and butler != main:
            self._candidates.append((
                get_async_openai_client(api_key=butler[1], base_url=butler[0],
                                        timeout=_MODEL_TIMEOUT),
                butler[2], "管家模型"))
        return self._candidates

    async def _complete(self, user: str) -> str:
        """调用 LLM 生成日记正文（任一候选成功即返回；全失败返回空串）。

        技能系统开启时允许模型先 load_skill 自选技能（多轮工具循环），
        否则回退为单轮直接输出（SKILL.md 规则已在 system 注入）。
        """
        candidates = self._ensure_candidates()
        if not candidates:
            console.warn("[日记] 未配置 LLM 服务，无法生成日记")
            return ""
        use_skills = _skills_enabled()
        tools = _SKILL_TOOLS if use_skills else None
        messages = [
            {"role": "system", "content": self._build_system(use_skills)},
            {"role": "user", "content": user},
        ]
        for client, model, label in candidates:
            try:
                content = await self._tool_loop(client, model, messages, tools)
                if content:
                    return content
                console.warn(f"[日记] {label}未输出有效正文")
            except Exception as e:
                console.warn(f"[日记] {label}调用失败：{e}")
        return ""

    async def _tool_loop(self, client: AsyncOpenAI, model: str,
                         messages: list[dict], tools: list[dict] | None) -> str:
        """多轮对话循环：模型可先 load_skill 选技能，最终输出日记正文。"""
        for _ in range(_SKILL_STEPS):
            resp = await self._chat_once(client, model, messages, tools)
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None) or []
            if tool_calls:
                # 标准工具协议：assistant(tool_calls) + tool(结果) 成对回传
                messages.append({
                    "role": "assistant",
                    "content": (msg.content or "").strip() or None,
                    "tool_calls": [{
                        "id": tc.id, "type": "function",
                        "function": {"name": tc.function.name,
                                     "arguments": tc.function.arguments or "{}"},
                    } for tc in tool_calls],
                })
                for tc in tool_calls:
                    result = await self._run_skill_tool(
                        tc.function.name, tc.function.arguments)
                    messages.append({
                        "role": "tool", "tool_call_id": tc.id, "content": result,
                    })
                continue
            content = (msg.content or "").strip()
            if content:
                return content
        return ""

    @staticmethod
    async def _chat_once(client: AsyncOpenAI, model: str,
                         messages: list[dict], tools: list[dict] | None):
        """单次 chat 调用（thinking 参数不支持时自动降级普通模式）。"""
        kwargs: dict = dict(model=model, messages=messages, temperature=0.8,
                            max_tokens=_MAX_TOKENS)
        if tools:
            kwargs["tools"] = tools
        try:
            # 显式关闭思考：日记要的是正文，不被推理文本污染
            return await asyncio.wait_for(
                client.chat.completions.create(
                    **kwargs, extra_body=build_thinking_extra_body(False)),
                timeout=_MODEL_TIMEOUT)
        except Exception:
            console.dim("[日记] 模型不支持 thinking 参数，降级为普通模式")
            return await asyncio.wait_for(
                client.chat.completions.create(**kwargs), timeout=_MODEL_TIMEOUT)

    @staticmethod
    async def _run_skill_tool(name: str, arguments: str) -> str:
        """执行技能系统工具（load_skill / read_skill_resource）。"""
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        if name == "load_skill":
            return await _load_skill(str(args.get("skill_name") or ""))
        if name == "read_skill_resource":
            return await _read_skill_resource(
                str(args.get("skill_name") or ""),
                str(args.get("resource_path") or ""))
        return f"错误：未知技能工具 {name!r}"

    @staticmethod
    def _build_system(use_skills: bool) -> str:
        """组装 system 提示。

        技能系统开启：注入「可用技能」段，让模型自己 load_skill 选技能；
        关闭：回退直接注入 daily-diary 的 SKILL.md 写作规则 + 参考范文。
        """
        base = (
            "现在要写一篇自己的日记，严格遵循下面的要求：\n\n"
            "【输出要求】\n"
            "- Markdown 格式，3-6 个片段，用 --- 分隔，每个片段一个 ### 小标题\n"
            "- 内容来自提供的当天对话素材，可以在此基础上夸张表达，但不能编造没发生的事\n"
            "- 对话素材太少时可以自由发挥：写今天的感受、想法，或围绕自己的人设自由发挥\n"
            "- 文末附一句当天日期\n"
            "- 只输出日记正文，不要任何额外说明"
        )
        if use_skills:
            skills_section = get_skill_manager().render_prompt_section()
            if not skills_section:
                return base
            return (
                "【可用技能】\n"
                f"{skills_section}\n\n"
                "写日记前，若其中有用 load_skill 加载了合适的技能（如 daily-diary），"
                "严格按其文笔要求写作；没有合适的技能就直接按下面的输出要求写。\n\n"
                f"{base}"
            )
        guide = _load_skill_body()
        blog = _load_reference_blog()
        return (
            "现在要写一篇自己的日记。严格遵循下面的文笔规则：\n\n"
            f"{guide or '（无写作规则，请用轻松自然、口语化的中文写日记）'}\n\n"
            "【参考范文】\n"
            f"{blog or '（无参考范文）'}\n\n"
            "【输出要求】\n"
            "- Markdown 格式，3-6 个片段，用 --- 分隔，每个片段一个 ### 小标题\n"
            "- 内容来自提供的当天对话素材，可以在此基础上夸张表达，但不能编造没发生的事\n"
            "- 对话素材太少时可以自由发挥：写今天的感受、想法，或围绕自己的人设自由发挥\n"
            "- 文末附一句当天日期\n"
            "- 只输出日记正文，不要任何额外说明"
        )

    @staticmethod
    def _read_existing(today: str) -> str:
        """读取当天已存在的日记（用于多次触发时合并，不丢内容）。"""
        try:
            return (_diary_dir() / f"{today}.md").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    async def write_diary(self, turns: list[dict]) -> str:
        """基于当天对话生成日记并落盘 data/diary/YYYY-MM-DD.md。

        当天已有日记时旧文一并提供给 LLM 重写合并（多次触发不丢内容）；
        对话素材过少时提示 LLM 自由发挥。返回落盘路径，失败返回空串。
        """
        text = memory.format_turns_text(turns[-_DIARY_MAX_TURNS:])
        if not text:
            text = "（今天还没有对话记录）"
        today = datetime.now().strftime("%Y-%m-%d")
        existing = self._read_existing(today)
        parts = [f"今天是 {today}。", "", "【当天对话素材】", text]
        if existing:
            parts += [
                "",
                "【今天已有一篇旧日记，请结合上面的新对话把它重写合并为"
                "更完整的一篇，不要丢失旧内容】",
                existing,
            ]
        parts += ["", "请写今天的日记。"]
        diary = await self._complete("\n".join(parts))
        if not diary:
            return ""
        path = _diary_dir() / f"{today}.md"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(diary.strip() + "\n", encoding="utf-8")
        except OSError as e:
            console.warn(f"[日记] 写入失败：{e}")
            return ""
        return str(path)


__all__ = ["DiaryWriter"]
