"""系统提示注入段：生效话术建议 / 观众画像 / 进化策略（30s TTL 缓存读文件）。

以混入类提供，LLMBrain 继承后方法仍用 self 访问缓存状态
（_advice_cache_ts 等由 LLMBrain.__init__ 初始化持有）。
"""

import json
import time

from src.llm.constants import (
    _ADVICE_ACTIVE_PATH,
    _ADVICE_CACHE_TTL,
    _POLICY_PATH,
    _POLICY_CACHE_TTL,
    _PROFILE_INJECT_MAX,
    _PROFILE_PATH,
    _PROFILE_CACHE_TTL,
)
from src.llm.utils.bigram import _bigram_set


class _InjectionMixin:
    """话术建议 / 观众画像 / 进化策略段注入。"""

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
