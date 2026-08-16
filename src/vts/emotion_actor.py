"""VTubeStudio 表情/动作演员：Embedding 情绪分类 → 按映射播放表情/动作。

仅 vtuber 模式（RUN_MODE=vtuber）生效：
- 启动时扫描当前 VTS 模型的表情（ExpressionStateRequest）与动画热键
  （HotkeysInCurrentModelRequest，.motion3.json）；
  另补**模型文件夹扫描**：VTS 热键需手动注册，模型文件夹里的动作/表情
  无需注册也能显示并播放（动作走 FaceDriver 注入路径，循环点平滑）
- 用户消息 → SiliconFlow Embedding 语义分类情绪 → 查映射表
  （data/emotion_map_vts.json，控制中心「表情与动作」页可配置）→ 播放
- 表情播放：ExpressionActivationRequest（先停用上一个表情再淡入目标，替换语义）
- 动作播放：热键触发（HotkeyTriggerRequest）；未注册热键的文件夹动作
  由 FaceDriver 注入参数路径播放（P2 覆盖 VTS 待机，循环点 smoothstep 混合）
- 命令行指令（控制台/控制中心试播）与桌宠一致：
    /face list           列出模型的表情与动作
    /expr <表情名>        播放指定表情
    /motion <组> [序号]   播放指定动作（默认序号 0）
"""

import json
import os
from typing import Dict

from src.utils import console
from src.emotion.actor import BaseEmotionActor


class VtsEmotionActor(BaseEmotionActor):
    """VTubeStudio 表情/动作演员（embedding 情绪自动控制 + 手动命令）。"""

    def __init__(self, vts, cfg, face=None) -> None:
        super().__init__(cfg)
        self._vts = vts
        self._face = face  # FaceDriver：文件夹动作未注册热键时走注入路径播放
        self._expr_files: Dict[str, str] = {}      # 表情名 → 表情文件（激活按 file）
        self._motion_hotkeys: Dict[str, Dict] = {}  # 动作名 → 热键 {id, file}
        self._folder_motions: Dict[str, str] = {}   # 动作名 → 模型文件夹内 .motion3.json 绝对路径
        self._stop_hotkeys: Dict[str, str] = {}     # 停止动画类热键名 → 热键 id（注入播放前停旧动画）
        self._active_expr_file: str = ""            # 当前激活的表情文件（替换语义）

    # ---------- 启动扫描 ----------

    async def scan(self) -> None:
        """扫描当前 VTS 模型的表情与动画热键（模型加载/切换后调用），
        并把结果写入绑定库缓存供控制中心使用。"""
        vts = self._vts
        self._expressions = []
        self._motions = {}
        self._expr_files = {}
        self._motion_hotkeys = {}
        self._folder_motions = {}
        self._stop_hotkeys = {}
        self._active_expr_file = ""
        self._model_name = ""
        hotkeys: list = []
        scan_ok = False
        # 表情：ExpressionState 优先
        try:
            exprs = await vts.get_expressions()
            self._expr_files = {
                e.get("name", ""): e.get("file", "")
                for e in exprs if e.get("name") and e.get("file")
            }
            scan_ok = True
        except Exception as e:
            console.dim(f"VTS 表情列表获取失败：{e}")
        # 热键列表（动画 / 表情文件兜底共用一次请求）
        try:
            hotkeys = await vts.get_hotkeys()
            scan_ok = True
        except Exception as e:
            console.dim(f"VTS 热键列表获取失败：{e}")
        # 表情兜底：部分模型表情未注册到 ExpressionState，回退热键里的 exp3 文件
        if not self._expr_files:
            self._expr_files = {
                h.get("name", ""): h.get("file", "")
                for h in hotkeys
                if (h.get("file") or "").lower().endswith(".exp3.json")
            }
        self._expressions = list(self._expr_files)
        # 动作：动画类热键（.motion3.json）
        for h in hotkeys:
            f = h.get("file", "") or ""
            if not f.lower().endswith(".motion3.json"):
                continue
            name = h.get("name", "") or f
            self._motion_hotkeys[name] = {"id": h.get("id", ""), "file": f}
        # 停止动画类热键（AnimationStop）：注入路径播放文件夹动作前触发，
        # 停掉正在播放的 VTS 原生动画（见 _trigger_motion）
        for h in hotkeys:
            if (h.get("type", "") or "").lower() in (
                    "animationstop", "stopanimation", "stop animation"):
                name = h.get("name", "") or h.get("id", "")
                if name:
                    self._stop_hotkeys[name] = h.get("id", "")
        self._motions = {name: 1 for name in self._motion_hotkeys}
        # 模型文件夹扫描兜底：VTS 热键需手动注册，未注册的动作/表情
        # 直接从模型文件夹枚举（动作走注入路径播放，表情可直接激活）
        folder_exprs, folder_motions = await self._scan_model_folder()
        for fname in folder_motions:
            # 去 .motion3.json 后缀做绑定名（splitext 只去 .json，会留 .motion3）
            name = os.path.basename(fname)[:-len(".motion3.json")]
            if name in self._motion_hotkeys:
                continue  # 已注册热键的动作以热键为准（可热键触发）
            self._folder_motions[name] = fname
            self._motion_hotkeys[name] = {"id": "", "file": fname}
        for fname in folder_exprs:
            name = os.path.basename(fname)[:-len(".exp3.json")]
            if name not in self._expr_files:
                self._expr_files[name] = fname
        self._expressions = list(self._expr_files)
        self._motions = {name: 1 for name in self._motion_hotkeys}
        # 当前模型名（写入缓存，控制中心展示用）
        try:
            d = await vts.get_current_model()
            self._model_name = d.get("modelName", "")
        except Exception:
            pass
        console.ok(
            f"VTS 模型扫描：表情 {len(self._expressions)}"
            f"（{'、'.join(self._expressions[:3])}{'…' if len(self._expressions) > 3 else ''}）"
            f" | 动画热键 {len(self._motions)}")
        if scan_ok:
            self._save_face_lib()

    # ---------- 控制中心绑定库缓存 ----------

    async def _scan_model_folder(self) -> tuple:
        """枚举当前模型文件夹内的动作/表情文件。

        复用 model_scanner.locate_model_folder 定位模型目录，递归收集
        .motion3.json / .exp3.json。VTS 热键需手动注册，文件夹扫描作为
        兜底：未注册热键的动作也能显示（走注入路径播放）、表情可直接激活。
        定位失败或没有 VTS_ROOT 时返回空（不影响原有流程）。

        Returns:
            (expressions: list[str], motions: list[str]) —— 均为文件绝对路径。
        """
        try:
            from src.vts.model_scanner import locate_model_folder
            folder = await locate_model_folder(self._vts, self._cfg)
        except Exception as e:
            console.dim(f"模型文件夹扫描失败：{e}")
            return [], []
        if not folder or not os.path.isdir(folder):
            return [], []
        exprs, motions = [], []
        for base, _dirs, files in os.walk(folder):
            for name in sorted(files):
                low = name.lower()
                if low.endswith(".exp3.json"):
                    exprs.append(os.path.normpath(os.path.join(base, name)))
                elif low.endswith(".motion3.json"):
                    motions.append(os.path.normpath(os.path.join(base, name)))
        return exprs, motions

    def _save_face_lib(self) -> None:
        """写表情/动作库缓存（data/vts_face_lib.json）：控制中心 vtuber 模式
        读此文件构建「表情与动作」绑定库。绑定名与运行时播放（ExpressionState
        表情 / 动画热键）完全一致；模型切换自动重写。
        """
        try:
            path = os.path.join(
                self._cfg.DATA_ROOT, "vts_face_lib.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "model_name": self._model_name,
                    "expressions": self._expressions,
                    "motions": list(self._motions),
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            console.dim(f"表情/动作库缓存写入失败：{e}")

    # ---------- 播放（VTS 官方 API） ----------

    async def play_expression(self, name: str) -> bool:
        """激活指定表情：先停用上一个激活的表情（替换语义，避免表情叠加）。"""
        if not name or self._vts is None:
            return False
        expr_file = self._expr_files.get(name)
        if not expr_file:
            return False
        try:
            if self._active_expr_file and self._active_expr_file != expr_file:
                await self._vts.activate_expression(self._active_expr_file, active=False)
            await self._vts.activate_expression(expr_file, active=True)
            self._active_expr_file = expr_file
            return True
        except Exception as e:
            console.dim(f"VTS 表情播放失败：{e}")
            return False

    async def _trigger_motion(self, name: str) -> bool:
        """播放动作：文件夹动作优先注入播放，否则热键触发。

        绑定名可能是热键名或文件名（去扩展名），依次尝试：
        1. 文件夹动作精确命中（绑定名=文件名去 .motion3.json）→
           FaceDriver 注入参数播放（P2 覆盖 VTS 待机，循环点 smoothstep 平滑）
        2. 按文件名匹配热键（VTSController.trigger_motion 精确/部分匹配）
        3. 按热键名精确匹配
        """
        if not name or self._vts is None:
            return False
        try:
            # 文件夹动作优先：绑定名（去后缀）精确命中 → 注入路径播放。
            # 不进 trigger_motion 的部分匹配，避免「1」误触发 file 含 1 的其它热键；
            # 该名字若已注册热键，scan 时不会进 _folder_motions（以热键为准）。
            path = self._folder_motions.get(name)
            if path and self._face is not None and os.path.isfile(path):
                # 先停掉正在播放的 VTS 原生动画：注入参数属 P2（面捕通道），
                # 低于 P3（一次性动画）——当前有动画在播时注入被压制，
                # 要等动画播完才生效（「点击预览不立即生效」）。停动画后注入立即接管。
                await self._stop_playing_animation()
                self._face.set_motion(path)
                return True
            # 按文件名匹配热键（VTSController.trigger_motion 支持精确/部分匹配）
            if await self._vts.trigger_motion(name):
                return True
            # 再按热键名精确匹配（映射表可能绑的是 VTS 里显示的热键名）
            hotkey = self._motion_hotkeys.get(name)
            if hotkey and hotkey.get("id"):
                await self._vts.trigger_hotkey(hotkey["id"])
                return True
        except Exception as e:
            console.dim(f"VTS 动作播放失败：{e}")
        return False

    async def _stop_playing_animation(self) -> None:
        """停止当前 VTS 原生动画（尽力而为）。

        VTS API 无「停止动画」消息，只能触发类型为 AnimationStop 的
        热键；模型未配置此类热键时跳过（注入参数在动画播完后自动接管）。
        """
        for stop_id in self._stop_hotkeys.values():
            if stop_id:
                await self._vts.trigger_hotkey(stop_id)

    async def play_motion(self, group: str, no: int) -> bool:
        # VTS 无「组名 序号」概念，序号忽略（兼容旧映射数据「组名 序号」格式）
        return await self._trigger_motion(group)

    async def play_motion_by_name(self, name: str) -> bool:
        return await self._trigger_motion(name)

    async def restore(self) -> None:
        """说话结束复原：停用当前表情 + 停止动作，回到模型默认姿态。"""
        try:
            if self._active_expr_file:
                await self._vts.activate_expression(
                    self._active_expr_file, active=False)
                self._active_expr_file = ""
        except Exception as e:
            console.dim(f"VTS 表情复原失败：{e}")
        try:
            await self._stop_playing_animation()
            if self._face is not None:
                self._face.stop_motion()
        except Exception as e:
            console.dim(f"VTS 动作复原失败：{e}")
