"""情绪演员基类：Embedding 情绪分类 → 按映射播放表情/动作（桌宠 / VTS 共用）。

PetEmotionActor（Live2D 桌宠）与 VtsEmotionActor（VTubeStudio）复用的公共逻辑：
- 情绪映射表加载（按模式存 data/emotion_map.json / emotion_map_vts.json，
  控制中心「表情与动作」页可配置）
- Embedding 情绪分类器初始化（与记忆检索共用同一嵌入服务）
- 手动命令解析（/face list /expr /motion）
- 按情绪随机播放（表情池优先，动作池兜底）

子类只需实现模型相关的两部分：
- scan()：扫描模型的表情与动作（桌宠为同步，VTS 为异步）
- async play_expression / play_motion / play_motion_by_name：播放接口
"""

import json
import os
import random
import re
from typing import Dict, List

from src.utils import console
from src.emotion.reaction import MessageReaction
from src.llm.utils.embedding import (
    EmbeddingEmotionClassifier,
    SiliconFlowEmbeddingProvider,
)

# 情绪列表（对齐 embedding 分类器语料库的 6 种基础情绪）
EMOTIONS: List[str] = [
    "开心", "生气", "疑惑", "悲伤", "害怕", "厌恶",
]


def _as_list(value) -> List[str]:
    """归一化绑定值：字符串 → 单元素列表（兼容旧单值配置），列表原样。"""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return [str(value)]


class BaseEmotionActor:
    """情绪演员基类（模型无关的公共逻辑）。"""

    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self._expressions: List[str] = []
        self._motions: Dict[str, int] = {}
        self._map: Dict[str, Dict[str, str]] = {}
        self._classifier: "EmbeddingEmotionClassifier | None" = None
        # 规则情绪分类器（纯正则、零开销）：AI 回复逐句情绪判断用
        self._rule_classifier = MessageReaction()

    # ---------- 启动扫描（子类实现） ----------

    def scan(self) -> None:
        """扫描模型表情/动作。子类实现（VTS 为异步 scan()）。"""
        raise NotImplementedError

    # ---------- 播放（子类实现，异步） ----------

    async def play_expression(self, name: str) -> bool:
        """播放指定表情。返回是否播放成功。"""
        raise NotImplementedError

    async def play_motion(self, group: str, no: int) -> bool:
        """按「组名 + 序号」播放动作。返回是否播放成功。"""
        raise NotImplementedError

    async def play_motion_by_name(self, name: str) -> bool:
        """按文件名（去扩展名）播放动作。返回是否播放成功。"""
        raise NotImplementedError

    async def restore(self) -> None:
        """说话结束复原：回到默认表情并停止正在播放的动作。子类实现。"""
        raise NotImplementedError

    # ---------- 映射表 ----------

    def load_map(self) -> None:
        """加载情绪 → 表情/动作映射（按模式存 data/emotion_map.json /
        emotion_map_vts.json，缺文件则空映射）。"""
        path = self._cfg.EMOTION_MAP_FILE
        self._map = {}
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._map = {e: (data.get(e) or {}) for e in EMOTIONS}
        except Exception as e:
            console.warn(f"情绪映射文件读取失败（本次无自动表情/动作）：{e}")

    # ---------- 情绪分类与播放 ----------

    async def initialize(self) -> bool:
        """初始化 embedding 分类器（失败返回 False，本次不自动控制）。"""
        if self._classifier is None:
            # 情绪分类与记忆检索共用同一嵌入服务：EMBEDDING_BASE_URL 指向
            # 本地 llama.cpp（127.0.0.1）时无需 API Key（_is_local_url 豁免），
            # 与 memory._SiliconFlowEmbedder 一致；云端则需 EMBEDDING_API_KEY。
            provider = SiliconFlowEmbeddingProvider(
                self._cfg.EMBEDDING_API_KEY or "",
                self._cfg.EMBEDDING_MODEL,
                self._cfg.EMBEDDING_BASE_URL,
            )
            if not provider.configured:
                console.dim("未配置可用嵌入服务，表情/动作不自动控制"
                            "（仍可用 /expr /motion 手动控制）")
                return False
            self._classifier = EmbeddingEmotionClassifier(provider)
        return await self._classifier.initialize()

    async def handle(self, text: str) -> str:
        """处理一条输入：先匹配手动命令；否则分类情绪并播放映射。

        返回播放描述文本（空串表示未播放），供主循环提示用。
        """
        text = (text or "").strip()
        if not text:
            return ""
        cmd = await self._try_command(text)
        if cmd:
            return cmd
        if not await self.initialize():
            return ""
        try:
            intent = await self._classifier.classify(text)
        except Exception as e:
            console.dim(f"情绪分类失败：{e}")
            return ""
        emotion = getattr(intent, "emotion", "中性") or "中性"
        played = await self._play_map(emotion)
        if played:
            console.dim(f"情绪「{emotion}」→ {played}")
        return played

    async def handle_rule(self, text: str) -> str:
        """按句判断情绪（规则分类，零开销）并播放映射：AI 回复逐句用。

        与 handle() 的区别：只做规则情绪分类 → 播放该情绪绑定的表情/动作，
        不匹配手动命令（回复内容不是用户指令），不需要 Embedding 服务
        （离线可用、无网络/初始化依赖）。
        返回播放描述文本（空串表示未播放）。
        """
        text = (text or "").strip()
        if not text:
            return ""
        emotion = self._rule_classifier.classify(text).emotion
        played = await self._play_map(emotion)
        if played:
            console.dim(f"情绪「{emotion}」→ {played}")
        return played

    async def _play_map(self, emotion: str) -> str:
        """按情绪随机播放：同一情绪可绑定多个表情/动作，随机取一个播放；
        映射项不存在（播放失败）则换下一个，表情池全部失败才落动作池。"""
        entry = self._map.get(emotion) or {}
        expr_pool = _as_list(entry.get("expression"))
        motion_pool = _as_list(entry.get("motion"))
        for name in random.sample(expr_pool, len(expr_pool)):
            if await self.play_expression(name):
                return f"表情 {name}"
        for motion in random.sample(motion_pool, len(motion_pool)):
            # 控制中心「动作绑定区域」存动作文件名（去扩展名）；旧数据兼容「组名 序号」
            if await self.play_motion_by_name(motion):
                return f"动作 {motion}"
            parts = motion.split()
            no = 0
            try:
                no = int(parts[1])
            except (IndexError, ValueError):
                no = 0
            if await self.play_motion(parts[0], no):
                return f"动作 {motion}"
        return ""

    # ---------- 手动命令 ----------

    async def _try_command(self, text: str) -> str:
        m = re.match(r"^/(expr|motion|face)\s*(.*)$", text)
        if not m:
            return ""
        cmd, arg = m.group(1), m.group(2).strip()
        if cmd == "face" and arg == "list":
            expr_txt = "、".join(self._expressions) or "（无）"
            motion_txt = "、".join(
                f"{g}×{n}" for g, n in self._motions.items()) or "（无）"
            return f"表情：{expr_txt}；动作：{motion_txt}"
        if cmd == "expr":
            if not arg:
                return "用法：/expr <表情名>"
            ok = await self.play_expression(arg)
            return f"已播放表情 {arg}" if ok else f"表情 {arg} 不存在（/face list 查看）"
        if cmd == "motion":
            parts = arg.split()
            if not parts:
                return "用法：/motion <动作组> [序号]"
            # 先按文件名匹配（模型自带动作文件，MotionFile 组）：控制中心
            # 动作卡片对自带动作文件发的是文件名（去扩展名，如 `wave`），
            # 直接按组名播放会在 _motion_groups 里查不到（文件组的 key 是
            # MotionFile）→ 试播静默失败。按名匹配失败再回落「组名 序号」。
            if await self.play_motion_by_name(parts[0]):
                return f"已播放动作 {parts[0]}"
            no = 0
            try:
                no = int(parts[1]) if len(parts) > 1 else 0
            except ValueError:
                return f"动作序号无效：{parts[1]}"
            ok = await self.play_motion(parts[0], no)
            return (f"已播放动作 {parts[0]} #{no}" if ok
                    else f"动作 {parts[0]} #{no} 不存在（/face list 查看）")
        return ""
