"""记忆命名空间与主题推断（规则驱动，零 LLM 依赖）。

命名空间（namespace）：把不同来源的记忆物理隔离，防止跨上下文串扰。
- shared_profile：主播通用画像（跨模式）
- daily_life：日常生活（今天/昨天/吃了/去了）
- work_tasks：工作项目（代码/会议/项目）
- viewer_profile：观众画像（弹幕来源，与主播记忆分开）

主题（topic）：用于差异化衰减——身份类几乎不衰，事件/情绪类快速遗忘。
本模块是纯函数，可被 LiteMemoryBackend 与判决链复用。
"""

from __future__ import annotations

# ---------- 命名空间 ----------

NS_SHARED_PROFILE = "shared_profile"
NS_DAILY_LIFE = "daily_life"
NS_WORK_TASKS = "work_tasks"
NS_VIEWER_PROFILE = "viewer_profile"

# 合法命名空间集合（写库前校验，防脏数据）
VALID_NAMESPACES = frozenset({
    NS_SHARED_PROFILE, NS_DAILY_LIFE, NS_WORK_TASKS, NS_VIEWER_PROFILE,
})

# 来源 → 默认命名空间（danmaku 来源一律进观众画像）
_SOURCE_NS = {
    "danmaku": NS_VIEWER_PROFILE,
    "user_input": NS_DAILY_LIFE,
    "proactive": NS_SHARED_PROFILE,
    "evolution": NS_SHARED_PROFILE,
}


def infer_namespace(
    *,
    source: str = "",
    content: str = "",
    metadata: dict | None = None,
) -> str:
    """从来源 / 元数据 / 内容关键词推断命名空间（默认 shared_profile）。"""
    if metadata and isinstance(metadata.get("namespace"), str):
        return _sanitize_namespace(metadata["namespace"])
    if source in _SOURCE_NS:
        return _SOURCE_NS[source]
    lowered = (content or "").lower()
    if any(kw in lowered for kw in ("工作", "项目", "代码", "会议", "需求")):
        return NS_WORK_TASKS
    if any(kw in content or "" for kw in ("今天", "昨天", "刚才", "吃了", "去了", "周末")):
        return NS_DAILY_LIFE
    return NS_SHARED_PROFILE


def sanitize_namespace(name: str) -> str:
    """把任意字符串规整到合法命名空间；非法回退 shared_profile。"""
    return name if name in VALID_NAMESPACES else NS_SHARED_PROFILE


# 兼容旧命名（避免与新写法混淆，仅导出正式名）
_sanitize_namespace = sanitize_namespace


# ---------- 主题（topic） ----------

# 关键词表：命中即归类；优先级按列表顺序（identity 最优先）
TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "identity": ("我是", "我叫", "我是一名", "我的名字", "我是做"),
    "preference": ("喜欢", "讨厌", "最爱", "偏爱", "不喜欢", "很想"),
    "habit": ("习惯", "经常", "每天", "总是", "从来不"),
    "relationship": ("朋友", "家人", "同事", "对象", "老婆", "老公", "室友"),
    "experience": ("去过", "试过", "经历过", "以前", "曾经"),
    "emotion": ("开心", "难过", "生气", "焦虑", "失落", "害怕", "兴奋"),
    "schedule": ("明天", "下周", "待办", "要开会", "要交", "要去做"),
    "work_project": ("项目", "任务", "上线", "提交", "合并", "bug", "需求"),
    "event_social": ("见面", "聚会", "一起", "约了", "去了"),
    "event_travel": ("旅游", "去了哪", "出发", "回家", "出差"),
    "health_condition": ("生病", "感冒", "发烧", "头疼", "不舒服", "失眠"),
    "learning_skill": ("在学", "学习", "学会", "教程", "练习"),
}

# 未命中任何关键词时的话题兜底
TOPIC_GENERAL = "general"


def infer_topic(content: str) -> str:
    """从内容关键词推断主题（规则优先，按声明顺序首个命中）。"""
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(kw in content for kw in keywords):
            return topic
    return TOPIC_GENERAL
