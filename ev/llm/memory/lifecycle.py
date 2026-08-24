"""Mem0 风格记忆判决链：新事实 → ADD / UPDATE / DELETE / IGNORE。

在现有记忆写入路径（tools.memory.memory.commit_recall_files）之上叠加一层
LLM 判决：把「新提取的事实」与「相似的历史记忆」一起交给小模型判断该新增、
更新、删除还是忽略，避免近似重复入库与事实冲突。

设计要点：
- 默认不启用（MEMORY_LIFECYCLE_ENABLED=false）：关闭时现有语义去重路径
  原样保留，行为 100% 不变；开启后由判决链接管去重（IGNORE 覆盖近似重复）。
- 相似记忆召回通过回调注入（recall_similar），与具体存储后端解耦，
  未来切 LiteMemoryBackend 无需改动判决逻辑。
- 规则预筛先行（不调 LLM，省 token）：完全同文本 → IGNORE；高相似 +
  高 token 重叠 → IGNORE；命中世界观词库（Lore 泄漏）→ IGNORE。
  仅存疑的走 LLM 精细判决。
- 判决失败 / 无 LLM 配置时保守回退 ADD（保留事实，由后续纠错兜底）；
  UPDATE/DELETE 的 target_id 必须是召回列表内的真实 id，防 LLM 幻觉误删。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from ev.llm.client.factory import get_async_openai_client
from ev.llm.utils.jsonutil import parse_json_object
from ev.llm.memory.lore_guard import is_lore_leak
from ev.utils import config

_JUDGE_SYSTEM = """你是记忆管家。判断新事实与现有记忆的关系。

判决标准：
- ADD：新事实是全新主题，现有记忆没有相关条目
- UPDATE：新事实是对现有记忆的修正/补充/细化（此时必须给出 target_id）
- DELETE：新事实表明现有记忆已过时/矛盾（此时必须给出 target_id）
- IGNORE：新事实与现有记忆高度重复，无需存储

只输出 JSON：{"verdict": "ADD|UPDATE|DELETE|IGNORE", "target_id": <id或null>, "reason": "..."}"""

# 相似记忆召回回调：async (content, owner, top_k) -> list[{"id", "content", "similarity"}]
RecallFn = Callable[..., Any]


class LifecycleEngine:
    """记忆生命周期判决引擎（进程内单例使用，LLM 客户端懒创建）。"""

    def __init__(
        self,
        *,
        recall_similar: Optional[RecallFn] = None,
        threshold: float = 0.6,
    ) -> None:
        self._recall_similar = recall_similar
        self._threshold = threshold
        self._client: Any = None
        self._model = ""

    # ---------- 判决 ----------

    async def judge(
        self,
        content: str,
        *,
        owner: str = "chao",
    ) -> tuple[str, Optional[str]]:
        """对新事实做判决。

        Returns: (verdict, target_id)。
        - verdict ∈ ADD / UPDATE / DELETE / IGNORE
        - target_id 为 UPDATE / DELETE 指向的现有记忆 id（无则 None）
        """
        content = (content or "").strip()
        if not content:
            return ("IGNORE", None)
        # 1. 召回相似历史记忆（低于阈值的不算相关，直接新增）
        similar: list[dict] = []
        if self._recall_similar is not None:
            try:
                similar = await self._recall_similar(content, owner, 3)
            except Exception:
                similar = []
            similar = [m for m in similar if m.get("similarity", 0.0) >= self._threshold]
        if not similar:
            return ("ADD", None)

        # 1.5 规则预筛（不调 LLM，省 token）：命中任一即直接判决
        rule_verdict = self._rule_prescreen(content, similar)
        if rule_verdict is not None:
            return (rule_verdict, None)

        # 2. LLM 判决（无可用 LLM 配置时保守新增）
        client = self._ensure_client()
        if client is None:
            return ("ADD", None)
        context = "\n".join(
            f"- id={m['id']}: {m['content']} (相似度 {m['similarity']:.2f})"
            for m in similar
        )
        user_text = (
            f"新事实：{content}\n"
            f"记忆归属者：{owner}\n"
            f"现有相关记忆：\n{context}"
        )
        try:
            resp = await client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.2,
                max_tokens=200,
            )
            data = parse_json_object(resp.choices[0].message.content or "")
            verdict = str(data.get("verdict", "ADD")).upper()
            if verdict not in ("ADD", "UPDATE", "DELETE", "IGNORE"):
                verdict = "ADD"
            target_id = data.get("target_id")
            # UPDATE/DELETE 必须命中召回列表中的真实 id，否则视为 IGNORE，
            # 防止模型幻觉出乱序 id 导致误删/误改他人记忆
            if verdict in ("UPDATE", "DELETE"):
                valid_ids = {str(m["id"]) for m in similar}
                if target_id is None or str(target_id) not in valid_ids:
                    return ("IGNORE", None)
            return (verdict, str(target_id) if target_id is not None else None)
        except Exception:
            # 判决失败保守回退：ADD（保留事实，由用户/后续纠错兜底）
            return ("ADD", None)

    # ---------- 内部工具 ----------

    @staticmethod
    def _rule_prescreen(content: str, similar: list[dict]) -> Optional[str]:
        """规则预筛：返回 IGNORE 或 None（None = 需 LLM 精细判决）。

        1. 与任一召回记忆完全同文本（去空白）→ IGNORE；
        2. 最相似记忆 ≥ 0.88 且 token 重叠率（Jaccard）≥ 0.25 → IGNORE
           （近似重复，Firefly 经验值）；
        3. 命中世界观词库（Lore 泄漏）→ IGNORE。
        """
        norm_content = re.sub(r"\s+", "", content)
        best = max(similar, key=lambda m: float(m.get("similarity", 0.0)))
        best_content = str(best.get("content") or "")
        if re.sub(r"\s+", "", best_content) == norm_content:
            return "IGNORE"
        if float(best.get("similarity", 0.0)) >= 0.88:
            overlap = _token_overlap_ratio(content, best_content)
            if overlap >= 0.25:
                return "IGNORE"
        if is_lore_leak(content):
            return "IGNORE"
        return None

    def _ensure_client(self) -> Any:
        """懒创建 LLM 客户端：MEMORY_LIFECYCLE_MODEL 优先，否则用 BUTLER 模型。"""
        if self._client is not None:
            return self._client
        base_url = (config.cfg.LLM_BASE_URL or "").strip()
        api_key = (config.cfg.LLM_API_KEY or "").strip()
        model = (
            config.cfg.MEMORY_LIFECYCLE_MODEL
            or config.cfg.BUTLER_MODEL
            or config.cfg.LLM_MODEL
            or ""
        ).strip()
        if not (base_url and api_key and model):
            return None
        self._client = get_async_openai_client(
            api_key=api_key, base_url=base_url, timeout=30.0)
        self._model = model
        return self._client

    @staticmethod
    def _extract_json(text: str) -> str:
        """从 LLM 输出中提取 JSON（容错 markdown 围栏与前后杂音）。"""
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        # 兜底：直接抠出首个 {...} 块
        if not text.startswith("{"):
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                text = m.group(0)
        return text


# ---------- 模块级工具 ----------


def _token_overlap_ratio(a: str, b: str) -> float:
    """两段文本的 Jaccard 重叠率（中文字符 + 英文词构成的 token 集合）。

    停用词不在本项目语境下显著干扰，直接按字符/词集合计算，
    足够支撑「近似重复」判定（Firefly 同思路）。
    """
    def tokens(text: str) -> set[str]:
        result = set()
        for m in re.finditer(r"[A-Za-z0-9]+", text):
            result.add(m.group(0))
        for ch in text:
            if "\u4e00" <= ch <= "\u9fff":
                result.add(ch)
        return result

    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
