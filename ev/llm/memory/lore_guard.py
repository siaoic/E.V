"""Lore 泄漏防护 + 记忆注入铁律（防 OOC）。

背景：直播场景下弹幕会反复提及角色世界观内容（如「流萤跟萨姆什么关系」），
若被 ButlerAgent 提取成「用户记忆」入库，会污染长期记忆库，且召回时模型
可能把角色亲历错当成用户事实输出（OOC）。

防护策略（借鉴 Firefly _is_lore_leak）：
- 世界观关键词词库（可按人设扩展）；一条待入库记忆命中 ≥2 个关键词即
  判定为 lore 泄漏，应在写入前丢弃 / 判决为 IGNORE。
- 记忆注入模板末尾附「使用铁律」，提示模型角色亲历 ≠ 用户事实。

本模块是纯函数，接入点：
- src/llm/memory/lifecycle.py：judge() 前置规则（开启判决链时生效）；
- 未来写入路径可直接调用 is_lore_leak 预筛。
"""

from __future__ import annotations

# 世界观关键词（按主题分组便于维护；计数时取全表）
_LORE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "角色": ("流萤", "萨姆", "卡芙卡", "银狼", "星核猎手", "姬子", "三月七", "丹恒",
             "开拓者", "黑塔", "希儿", "布洛妮娅", "景元", "刃", "镜流", "罗刹"),
    "世界观": ("星穹铁道", "星核", "命途", "仙舟", "贝洛伯格", "匹诺康尼", "丰饶",
               "巡猎", "毁灭", "存护", "同谐", "虚无", "记忆", "智识"),
    "剧情名词": ("太卜司", "云骑军", "十王司", "龙裔", "令使", "星神", "跃迁",
                 "开拓命途", "模拟宇宙", "次元战争"),
}

# 拼接后的全量关键词表（去重保持顺序）
LORE_KEYWORDS: tuple[str, ...] = tuple(
    dict.fromkeys(kw for group in _LORE_KEYWORDS.values() for kw in group))


def is_lore_leak(content: str, min_hits: int = 2) -> bool:
    """一条内容命中 ≥ min_hits 个世界观词即视为 lore 泄漏。

    单关键词命中不算（「流萤」单独出现可能是真实闲聊），多个世界观词
    同时出现大概率是在讨论游戏剧情。
    """
    text = content or ""
    hits = sum(1 for kw in LORE_KEYWORDS if kw in text)
    return hits >= min_hits


# 记忆注入铁律：附在记忆文本末尾，防止角色亲历被当作用户事实
MEMORY_IRON_RULE = (
    "【记忆使用铁律】\n"
    "1. 以上记忆中若包含「角色/剧情/世界观」相关描述，属于角色亲历或剧本认知，"
    "禁止当作观众/用户的真实经历输出；\n"
    "2. 观众明确说过的事实（我今年 22 岁、我养了一只猫）才可作为用户画像使用；\n"
    "3. 不确定归属的记忆，输出时一律用「我记得有人提过……」的模糊表达。"
)


def format_with_iron_rule(memory_text: str) -> str:
    """把记忆文本与铁律拼装，供 system prompt 注入（空文本直接返回空）。"""
    text = (memory_text or "").strip()
    if not text:
        return ""
    return f"{text}\n\n{MEMORY_IRON_RULE}"
