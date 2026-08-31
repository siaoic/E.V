"""系统提示注入段：生效话术建议 / 观众画像 / 进化策略（30s TTL 缓存读文件）。

以混入类提供，LLMBrain 继承后方法仍用 self 访问缓存状态
（_advice_cache_ts 等由 LLMBrain.__init__ 初始化持有）。
"""

import json
import time

from ev.llm.utils.constants import (
    _ADVICE_ACTIVE_PATH,
    _ADVICE_CACHE_TTL,
    _POLICY_PATH,
    _POLICY_CACHE_TTL,
    _PROFILE_INJECT_MAX,
    _PROFILE_PATH,
    _PROFILE_CACHE_TTL,
)
from ev.llm.utils.bigram import _bigram_set


class _InjectionMixin:
    """话术建议 / 观众画像 / 进化策略 / 知识库段注入。"""

    # ---------- Author's Note 尾部人设锚点（近因效应） ----------

    def _tail_anchor_section(self) -> str:
        """返回 Author's Note 人设锚点文本（追加在 messages 末尾的 system 尾注）。

        对标 Firefly build_authors_note：利用 LLM 近因效应，把角色语气/格式
        铁律放在 user 消息之后，比放在 system prompt 开头更有效。
        默认空（.env 未配置 AUTHOR_NOTE）= 不注入，行为与历史完全一致。
        """
        return (getattr(self.cfg, "AUTHOR_NOTE", "") or "").strip()

    # ---------- 知识库注入（防幻觉，对标 Firefly 知识金字塔） ----------

    def _knowledge_section(self, user_text: str) -> str:
        """按信号闸门返回应注入的知识段（data/knowledge 权威设定）。

        闲聊与无关消息返回空串（不注入，省 Token）；知识数据懒加载，
        进程内缓存一次。KNOWLEDGE_ENABLED 关闭时完全不注入。
        """
        if not getattr(self.cfg, "KNOWLEDGE_ENABLED", True):
            return ""
        from ev.llm.knowledge import get_knowledge_service

        return get_knowledge_service().section(
            user_text,
            max_total_chars=getattr(self.cfg, "KNOWLEDGE_MAX_CHARS", 1200),
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

    # ---------- L2 内建长期记忆（MEMORY.md / USER.md 冻结快照） ----------

    def _curated_memory_section(self) -> str:
        """返回 L2 内建长期记忆（MEMORY.md/USER.md）的冻结快照段。

        快照在会话开始时生成、整个会话保持不变（保 Prefix Cache 命中率），
        会话中的写入即时落盘但不改动快照，下一次会话生效。空快照或
        MEMORY_CURATED_ENABLED 关闭时返回空串（不注入，行为与历史一致）。
        """
        if not getattr(self.cfg, "MEMORY_CURATED_ENABLED", True):
            return ""
        from ev.llm.memory.curated import get_curated_store

        store = get_curated_store()
        blocks = []
        for target in ("memory", "user"):
            block = store.format_for_system_prompt(target)
            if block:
                blocks.append(block)
        if not blocks:
            return ""
        return ("### 长期记忆（跨会话稳定事实，会话内冻结）\n"
                "（自然融入，别复述）\n"
                + "\n\n".join(blocks))

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
        # query 的 2-gram 只算一次，避免每条画像重复计算（最多 30 条）
        query_bigrams = _bigram_set(query)
        # 相关度 = 与当前消息的公共 2-gram 片段数，降序取前 N
        hits = sorted(
            ((len(_bigram_set(it["fact"]) & query_bigrams), it)
             for it in self._profile_cache),
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
        5.16 A/B：EVOLUTION_POLICY_AB=1 时按缓存刷新奇偶各 50% 轮换注入
        上一版策略（previous），盲测线上效果；默认关闭，行为与现状一致。
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
                self._policy_previous = (
                    (data.get("previous") or "").strip()
                    if isinstance(data, dict) else ""
                )
            except (OSError, ValueError):
                self._policy_cache = ""
                self._policy_previous = ""
        if not self._policy_cache:
            return ""
        # A/B 盲测：命中奇数缓存周期注入上一版（50%），偶数或无非对照版走当前
        if (getattr(self.cfg, "EVOLUTION_POLICY_AB", False)
                and self._policy_previous
                and int(self._policy_cache_ts) % 2 == 1):
            block = self._policy_previous
        else:
            block = self._policy_cache
        return ("### 进化行为策略（GEPA 迭代沉淀，直播中相关情境优先遵循，"
                "与生效中的话术建议冲突时以本策略为准）\n" + block)
