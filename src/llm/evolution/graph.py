"""学习可视化（journey / Star Map，5.13）。

对标 hermes agent/learning_graph.py + cli/journey.py 精简落地（纯读侧，
不写盘）：技能节点 + 画像节点 + 记忆卡片节点 + 2-gram 技能-记忆边。

- 技能节点：技能注册表（name/description/pinned/location）+ skill_usage.json
  （state/loads/views/patches/last_used/created_by）；
- 画像节点：evolution_profile.json 长期事实（owner/fact/created）；
- 记忆卡片节点：MEMORY.md / USER.md（§ 分隔条目，含 AI 笔记与观众认知）；
- 技能-记忆边：技能名/描述与记忆卡片的 2-gram 重叠计数（无第三方分词），
  每技能取 top 4——观众常提片段与主播沉淀的技能同框，暴露技能与认知脱节；
- !journey 命令输出 ASCII 星图；build_learning_graph() 供控制中心渲染 JSON；
- 全程 fail-open：任何读取失败静默降级，不影响运行。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from src.utils import config

# 技能-记忆边：每技能最多关联的记忆卡片数（对标文档 top 4）
_MAX_EDGES_PER_SKILL = 4

# 星图单列上限（技能/画像/记忆各列条数，防止库过大时输出爆炸）
_MAX_COLUMN = 30


def _is_han(ch: str) -> bool:
    """是否中文字符（CJK 统一表意文字区）。"""
    return "\u4e00" <= ch <= "\u9fff"


def _two_grams(text: str) -> set[str]:
    """取文本的中文 2-gram 集合（去重，只取中文连续字对）。"""
    grams: set[str] = set()
    for i in range(len(text) - 1):
        a, b = text[i], text[i + 1]
        if _is_han(a) and _is_han(b):
            grams.add(a + b)
    return grams


def _overlap_score(a: str, b: str) -> int:
    """技能文本 a 的 2-gram 在记忆卡片 b 中命中的数量（弱主题关联）。"""
    return len(_two_grams(a) & _two_grams(b))


def _read_cards(path: str) -> list[str]:
    """按 § 分隔读取纯文本卡片文件（缺失/损坏返回空列表）。"""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    cards = [c.strip() for c in raw.split("\n§\n")]
    return [c for c in cards if c]


def _load_skills() -> list[dict]:
    """技能节点：注册表（name/description/pinned）与使用统计合并。"""
    try:
        from plugins.tools.skills import get_skill_manager
        manager = get_skill_manager()
        skills = list(manager.skills)
    except Exception:
        return []
    nodes = []
    for skill in skills:
        usage: dict = {}
        try:
            usage = manager.usage_of(skill.name) or {}
        except Exception:
            pass
        nodes.append({
            "id": f"skill:{skill.name}",
            "type": "skill",
            "name": skill.name,
            "description": skill.description,
            "pinned": skill.pinned or bool(usage.get("pinned")),
            "state": usage.get("state") or "active",
            "loads": int(usage.get("loads") or 0),
            "views": int(usage.get("views") or 0),
            "patches": int(usage.get("patches") or 0),
            "last_used": float(usage.get("last_used") or 0),
            "created_by": (usage.get("created_by") or ""),
        })
    return nodes


def _load_profiles() -> list[dict]:
    """画像节点：evolution_profile.json 的长期事实。"""
    try:
        from .profile import _load_profile
        items = _load_profile()
    except Exception:
        return []
    nodes = []
    for item in items:
        fact = (item.get("fact") or "").strip()
        if not fact:
            continue
        nodes.append({
            "id": f"profile:{abs(hash(fact)) & 0xFFFFFF:06X}",
            "type": "profile",
            "fact": fact,
            "owner": (item.get("owner") or "")[:24],
            "created": float(item.get("created") or 0),
        })
    return nodes


def _load_memories() -> list[dict]:
    """记忆卡片节点：MEMORY.md（AI 笔记）与 USER.md（观众认知）。"""
    nodes = []
    base = os.path.join(config.cfg.DATA_ROOT, "memories")
    for filename, owner in (("MEMORY.md", "memory"), ("USER.md", "user")):
        for text in _read_cards(os.path.join(base, filename)):
            nodes.append({
                "id": f"memory:{owner}:{abs(hash(text)) & 0xFFFFFF:06X}",
                "type": "memory",
                "owner": owner,
                "text": text[:120],
            })
    return nodes


def _build_edges(skills: list[dict], memories: list[dict]) -> list[dict]:
    """技能-记忆边：技能名/描述与记忆卡片 2-gram 重叠计数，每技能取 top 4。"""
    edges: list[dict] = []
    for skill in skills:
        query = (skill.get("name") or "") + " " + (skill.get("description") or "")
        scored = []
        for mem in memories:
            weight = _overlap_score(query, mem.get("text") or "")
            if weight > 0:
                scored.append((weight, mem))
        scored.sort(key=lambda t: (t[0], t[1].get("text", "")), reverse=True)
        for weight, mem in scored[:_MAX_EDGES_PER_SKILL]:
            edges.append({
                "source": skill["id"],
                "target": mem["id"],
                "weight": weight,
            })
    return edges


def build_learning_graph() -> dict:
    """构建学习星图 JSON（技能/画像/记忆节点 + 2-gram 边），供控制中心渲染。"""
    skills = _load_skills()
    profiles = _load_profiles()
    memories = _load_memories()
    return {
        "generated": time.time(),
        "nodes": skills + profiles + memories,
        "edges": _build_edges(skills, memories),
    }


def _fmt_ts(ts: float) -> str:
    """时间戳转短格式；0 表示从未使用。"""
    return (time.strftime("%m-%d %H:%M", time.localtime(ts))
            if ts else "从未")


def journey_timeline() -> str:
    """ASCII 学习星图（!journey 输出）：技能/画像/记忆三列 + 技能-记忆关联。"""
    graph = build_learning_graph()
    nodes = graph["nodes"]
    skills = [n for n in nodes if n["type"] == "skill"]
    profiles = [n for n in nodes if n["type"] == "profile"]
    memories = [n for n in nodes if n["type"] == "memory"]
    edges = graph["edges"]

    lines = [
        f"【学习星图】技能 {len(skills)} · 画像 {len(profiles)} · 记忆卡片 {len(memories)}",
    ]
    # 技能列：状态/使用次数/最近使用/来源/关联记忆
    for n in sorted(skills, key=lambda x: x["loads"], reverse=True)[:_MAX_COLUMN]:
        flags = []
        if n.get("state") == "stale":
            flags.append("stale")
        if n.get("pinned"):
            flags.append("pin")
        if n.get("created_by") == "user":
            flags.append("用户创建")
        flag = (f"（{'/'.join(flags)}）" if flags else "")
        rel = [e["target"] for e in edges if e["source"] == n["id"]]
        rel_text = ""
        if rel:
            by_id = {m["id"]: m for m in memories}
            names = []
            for rid in rel:
                m = by_id.get(rid)
                if m:
                    names.append(f"{'AI' if m['owner'] == 'memory' else '观众'}:{m['text'][:20]}")
            rel_text = "  └ 相关记忆 " + " / ".join(names)
        lines.append(
            f"[技能] {n['name']}（用{n['loads']}次，最近 {_fmt_ts(n['last_used'])}）"
            f"{flag}{rel_text}")
    for n in profiles[:_MAX_COLUMN]:
        lines.append(f"[画像] {n['fact']}")
    for n in memories[:_MAX_COLUMN]:
        who = "AI笔记" if n["owner"] == "memory" else "观众认知"
        lines.append(f"[{who}] {n['text']}")
    if not lines[1:]:
        return "暂无学习沉淀（技能/画像/记忆均为空）"
    return "\n".join(lines)
