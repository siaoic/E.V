"""信号闸门：决定何时注入知识，避免每轮对话都带知识段浪费 Token。

判定规则：
- 纯闲聊（标点 / emoji）→ 零注入
- 过短且无实体 → 零注入
- 剧情意图（"你是谁""她跟谁什么关系"）→ 全层注入
- 实体别名命中（角色名 / 地名 / 专有名词…）→ L0a+L0b
- 其余非闲聊 → L0a（默认安全；无匹配内容时 recall 返回空，不实际注入）
"""

import re


class KnowledgeGate:
    def __init__(self, *, chat_threshold: int = 4):
        self.chat_threshold = chat_threshold
        # 纯闲聊：只有标点/空白/emoji 的短消息
        self._chitchat = re.compile(
            r"^[\s\.\!\?\,\uff0c\u3002\uff01\uff1f\ud83c-\udbff\udc00-\udfff~～…—]*$")
        # 剧情意图模式：问角色身份 / 关系 / 经历 / 世界观
        self._intent = [
            re.compile(r"(你|他|她|它).{0,4}(谁|叫什么|名字)", re.I),
            re.compile(r"(你|他|她).{0,6}(去过|到过|认识|见过|关系|来自|出身)", re.I),
            re.compile(r"(说说|聊聊|介绍|讲讲|关于).{0,6}(你|他|她|世界观|背景|故事)", re.I),
            re.compile(r"(为什么|怎么|如何).{0,6}(你|他|她)", re.I),
            re.compile(r"(?i)(who|what|where|when|why|how).{0,6}(you|he|she|it)"),
        ]
        # 实体别名（facts.yaml / curated_cards 提取注入）
        self._entities: list = []

    def register_entities(self, entities: list) -> None:
        """注入实体别名表。过滤过短词（1 字符）与高频虚词，避免误触发。"""
        stop = {"我", "你", "他", "她", "它", "是", "吗", "呢", "的", "了"}
        self._entities = sorted({
            e.strip().lower() for e in entities
            if e.strip() and len(e.strip()) >= 2 and e.strip() not in stop
        })

    def should_inject(self, message: str) -> bool:
        return self.level(message) > 0

    def level(self, message: str) -> int:
        """0=零注入 / 1=L0a+L0b / 2=全层。"""
        msg = message.strip()
        if not msg:
            return 0
        # 规则 1：纯闲聊（标点 / emoji）→ 零注入
        if self._chitchat.match(msg):
            return 0
        # 规则 2：剧情意图 → 全层（须在短消息规则之前：短身份问题
        # 「你是谁」也是剧情意图，不能被短消息规则误杀）
        if any(p.search(msg) for p in self._intent):
            return 2
        # 规则 3：过短且无实体 → 零注入
        if len(msg) <= self.chat_threshold and not self._has_entity(msg):
            return 0
        # 规则 4：实体别名命中 → L0a+L0b
        if self._has_entity(msg):
            return 1
        # 规则 5：其余 → L0a（默认安全；无匹配内容时实际不注入）
        return 1

    def _has_entity(self, message: str) -> bool:
        lower = message.lower()
        return any(e in lower for e in self._entities)
