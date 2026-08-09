"""桌宠表情/动作演员：Embedding 情绪分类 → 按映射播放表情/动作。

仅桌宠模式（RUN_MODE=pet）生效：
- 启动时扫描模型参数/表情/动作（live2d 运行时优先，model3.json 回退）
- 用户消息 → SiliconFlow Embedding 语义分类情绪 → 查映射表
  （data/emotion_map.json，控制中心「表情与动作」页可配置）→ 播放表情/动作
- 命令行指令（控制台/控制中心试播）：
    /face list           列出模型的表情与动作
    /expr <表情名>        播放指定表情
    /motion <组> [序号]   播放指定动作（默认序号 0）
"""

import json
import os
import re
from typing import Dict, List

from src.utils import console
from src.llm.embedding import (
    EmbeddingEmotionClassifier,
    SiliconFlowEmbeddingProvider,
)
from src.pet.motion_files import _MOTION_FILE_GROUP, _scan_motion_files

# 情绪列表（对齐 embedding 分类器语料库的 6 种基础情绪）
EMOTIONS: List[str] = [
    "开心", "生气", "疑惑", "悲伤", "害怕", "厌恶",
]


def scan_model3(model3_path: str) -> Dict:
    """解析 model3.json：参数（Groups）/ 表情 / 动作（不依赖 live2d 运行时）。

    动作 = model3.json 声明的 Motions + 模型目录 motion/motions 子目录的
    自带动作文件（归入 MotionFile 组，「有什么用什么」）。
    """
    out = {"params": [], "expressions": [], "motions": {}}
    try:
        with open(model3_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        refs = data.get("FileReferences", {}) or {}
        for g in data.get("Groups", []) or []:
            if (g or {}).get("Target") == "Parameter":
                out["params"].extend(g.get("Ids", []) or [])
        for e in refs.get("Expressions", []) or []:
            name = (e or {}).get("Name") or os.path.basename((e or {}).get("File", ""))
            if name:
                out["expressions"].append(name)
        for gname, entries in (refs.get("Motions", {}) or {}).items():
            out["motions"][gname] = len(entries or [])
        files = _scan_motion_files(model3_path)
        if files:
            out["motions"][_MOTION_FILE_GROUP] = len(files)
    except Exception as e:
        console.dim(f"model3.json 解析失败：{e}")
    return out


class PetEmotionActor:
    """桌宠表情/动作演员（embedding 情绪自动控制 + 手动命令）。"""

    def __init__(self, widget, cfg) -> None:
        self._widget = widget
        self._cfg = cfg
        self._expressions: List[str] = []
        self._motions: Dict[str, int] = {}
        self._params: List[str] = []
        self._map: Dict[str, Dict[str, str]] = {}
        self._classifier: "EmbeddingEmotionClassifier | None" = None
        self._init_task = None

    # ---------- 启动扫描 ----------

    def scan(self) -> None:
        """扫描模型参数/表情/动作（widget 加载完成后调用；加载失败时回退 model3.json）。"""
        w = self._widget
        # 参数：运行时完整列表优先
        try:
            if w.model is not None:
                ids = w.model.GetParamIds() or []
                self._params = [str(i) for i in ids]
        except Exception:
            self._params = []
        # 表情：运行时表达式 id 优先
        try:
            if w.model is not None:
                self._expressions = [str(i) for i in (w.model.GetExpressionIds() or [])]
        except Exception:
            self._expressions = []
        # 动作：widget 已在构造时解析 model3.json
        self._motions = w.motion_groups()
        # 运行时拿不到（模型未加载）→ 回退解析 model3.json
        if not (self._expressions or self._motions):
            fallback = scan_model3(self._widget._model_path())
            self._params = fallback["params"]
            self._expressions = fallback["expressions"]
            self._motions = fallback["motions"]
        console.ok(
            f"桌宠模型扫描：参数 {len(self._params)} | 表情 {len(self._expressions)}"
            f"（{'、'.join(self._expressions[:3])}{'…' if len(self._expressions) > 3 else ''}）"
            f" | 动作组 {len(self._motions)}")

    def load_map(self) -> None:
        """加载情绪 → 表情/动作映射（data/emotion_map.json，缺文件则空映射）。"""
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
        cmd = self._try_command(text)
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
        played = self._play_map(emotion)
        if played:
            console.dim(f"情绪「{emotion}」→ {played}")
        return played

    def _play_map(self, emotion: str) -> str:
        entry = self._map.get(emotion) or {}
        expr = str(entry.get("expression") or "").strip()
        motion = str(entry.get("motion") or "").strip()
        if expr:
            if self._widget.play_expression(expr):
                return f"表情 {expr}"
            # 映射的表情不存在 → 落到动作
        if motion:
            # 控制中心「动作绑定区域」存动作文件名（去扩展名）；旧数据兼容「组名 序号」
            if self._widget.play_motion_by_name(motion):
                return f"动作 {motion}"
            parts = motion.split()
            no = 0
            try:
                no = int(parts[1])
            except (IndexError, ValueError):
                no = 0
            if self._widget.play_motion(parts[0], no):
                return f"动作 {motion}"
        return ""

    # ---------- 手动命令 ----------

    def _try_command(self, text: str) -> str:
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
            ok = self._widget.play_expression(arg)
            return f"已播放表情 {arg}" if ok else f"表情 {arg} 不存在（/face list 查看）"
        if cmd == "motion":
            parts = arg.split()
            if not parts:
                return "用法：/motion <动作组> [序号]"
            # 先按文件名匹配（模型自带动作文件，MotionFile 组）：控制中心
            # 动作卡片对自带动作文件发的是文件名（去扩展名，如 `wave`），
            # 直接按组名播放会在 _motion_groups 里查不到（文件组的 key 是
            # MotionFile）→ 试播静默失败。按名匹配失败再回落「组名 序号」。
            if self._widget.play_motion_by_name(parts[0]):
                return f"已播放动作 {parts[0]}"
            no = 0
            try:
                no = int(parts[1]) if len(parts) > 1 else 0
            except ValueError:
                return f"动作序号无效：{parts[1]}"
            ok = self._widget.play_motion(parts[0], no)
            return (f"已播放动作 {parts[0]} #{no}" if ok
                    else f"动作 {parts[0]} #{no} 不存在（/face list 查看）")
        return ""
