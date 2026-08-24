"""记忆读写/检索/索引/正则强信号辅助方法。

以「接受 ButlerAgent self 作为首参数」的模块级函数形式存在，
由 core.ButlerAgent 中的同名方法转发调用——保持逻辑逐字一致，
只是把方法体移到独立文件，控制单文件行数。
"""

from __future__ import annotations

import asyncio

from ev.llm.memory.lore_guard import is_lore_leak
from ev.utils import console

from tools.memory import memory

from ._prompts import (
    _EXTRACT_SYSTEM,
    _EXTRACT_WORKERS,
    _AI_ROLES,
    _WEAK_OBJS,
    _INSTANT_PATTERNS,
    _ABSTRACT_SUBJECTS,
    _SUBJECT_PREFIX_RE,
)


# ---------- 记忆提取（memU agentic 写入链路） ----------

async def _submit_extract_and_store(
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


async def _extract_and_store(
    self, new_turns: list[dict], recent_turns: list[dict] | None
) -> None:
    """提取新轮次中的长期信息并写入 memU 存储。"""
    text = self._turns_text(new_turns)
    if not text:
        return
    entries = await self._chat_json(_EXTRACT_SYSTEM, text)
    if not entries:
        return
    # 写共享黑板：让 evolution 复盘时直接读到本次提取的事实，省一次 LLM 提取
    try:
        from ev.agent.blackboard import get_blackboard
        await get_blackboard().put("recent_facts", entries, source="butler")
    except Exception:
        pass  # 黑板写入失败不影响入库主流程
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
    """正则命中稳定事实即后台立即入库（异步调度，不阻塞调用方）。"""
    entries = _instant_memory_entries(new_turns)
    if not entries:
        return
    self._instant_pending.extend(entries)
    if self._instant_task is None or self._instant_task.done():
        self._instant_task = asyncio.create_task(_commit_instant(self))


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


# ---------- 模块级 helper（轮次拆分 / 正则捕获 / 归属判定）----------

import re  # noqa: E402  # 与原始模块一致，在需要时顶层导入


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


def _entry_user(item: dict, fallback: str) -> str:
    """蒸馏/整合条目归属者：AI 自己的事实归 self，其余优先具体主体名。"""
    owner = (item.get("owner") or "").strip().lower()
    if owner == "self":
        return memory._USER_SELF
    subject = _SUBJECT_PREFIX_RE.sub("", str(item.get("subject") or "").strip()[:32])
    if subject and subject not in _ABSTRACT_SUBJECTS:
        return subject
    return fallback
