"""桌宠表情/动作演员：Embedding 情绪分类 → 按映射播放表情/动作。

仅桌宠模式（RUN_MODE=pet）生效：
- 启动时扫描模型参数/表情/动作（live2d 运行时优先，model3.json 回退）
- 用户消息 → SiliconFlow Embedding 语义分类情绪 → 查映射表
  （data/emotion_map.json，控制中心「表情与动作」页可配置）→ 播放表情/动作
- 命令行指令（控制台/控制中心试播）：
    /face list           列出模型的表情与动作
    /expr <表情名>        播放指定表情
    /motion <组> [序号]   播放指定动作（默认序号 0）

与 VTS 模式的 VtsEmotionActor 共用 BaseEmotionActor（映射/分类/命令逻辑一致），
本类只负责「模型能力来源」：live2d 运行时（widget）。
"""

import json
import os
from typing import Dict

from src.utils import console
from src.pet.motion_files import _MOTION_FILE_GROUP, _scan_motion_files
from src.emotion.actor import BaseEmotionActor, EMOTIONS

__all__ = ["PetEmotionActor", "scan_model3", "EMOTIONS"]


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


class PetEmotionActor(BaseEmotionActor):
    """桌宠表情/动作演员（embedding 情绪自动控制 + 手动命令）。"""

    def __init__(self, widget, cfg) -> None:
        super().__init__(cfg)
        self._widget = widget
        self._params: list = []

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

    # ---------- 播放（委托给 live2d widget） ----------

    async def play_expression(self, name: str) -> bool:
        return self._widget.play_expression(name)

    async def play_motion(self, group: str, no: int) -> bool:
        return self._widget.play_motion(group, no)

    async def play_motion_by_name(self, name: str) -> bool:
        return self._widget.play_motion_by_name(name)
