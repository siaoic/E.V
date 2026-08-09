"""桌宠 FaceDriver：复用 FaceDriver 的口型/眨眼/节拍计算，驱动本地 Live2D 模型。

继承 vts/face_driver.FaceDriver，只重写注入目标相关方法：
- _ensure_connected：本地模型是否已加载（替代 VTS 连接检查）
- _inject：把参数帧设置到本地模型（替代 VTS InjectParameterDataRequest）
- _resolve_mouth_params / 眨眼参数：直接用 Cubism 原生标准参数名
- _motion_frame：基线动作用原生参数帧（保留 ParamBreath 等呼吸参数）

这样 stream.py / proactive.py / main.py 无需感知渲染目标是 VTS 还是本地桌宠。
"""

from typing import Dict, Optional, Tuple

from src.utils import config
from src.vts.face_driver import FaceDriver
from src.vts.model_scanner import ModelProfile

# 本地 Cubism 标准参数（feiniu.model3.json 的 LipSync / EyeBlink 组）
_MOUTH_PARAM = "ParamMouthOpenY"
_EYE_PARAMS: Tuple[str, str] = ("ParamEyeLOpen", "ParamEyeROpen")


class PetFaceDriver(FaceDriver):
    """桌宠注入目标。"""

    def __init__(self, widget, cfg: Optional["config.Config"] = None) -> None:
        # 不连接 VTS；profile 为空（参数名在下方 _resolve_mouth_params 指定）
        super().__init__(vts=None, profile=ModelProfile())
        self.widget = widget
        self.cfg = cfg or config.cfg
        self._mouth_gain = self.cfg.MOUTH_GAIN
        self._eye_param_ids: Tuple[str, str] = _EYE_PARAMS

    # ---------- 注入目标抽象 ----------

    async def _ensure_connected(self) -> bool:
        return self.widget is not None and self.widget.model is not None

    async def _inject(self, params: Dict[str, float]) -> None:
        self.widget.set_parameters(params)

    def _motion_frame(self) -> Dict[str, float]:
        """基线动作：直接用 Cubism 原生参数帧（保留 ParamBreath 等）。

        剔除嘴部参数（口型由口型同步全权控制）与眨眼参数（眨眼由
        FaceDriver 注入控制）——动作文件里若带 ParamEyeLOpen/ROpen 曲线，
        与注入的眨眼帧每帧互踩会造成眼皮抽搐。
        """
        if not (self._motion and self._motion.active):
            return {}
        frame = self._motion.get_raw_frame()
        frame.pop("ParamMouthForm", None)
        frame.pop("ParamMouthOpenY", None)
        if self._eye_param_ids:
            for p in self._eye_param_ids:
                frame.pop(p, None)
        return frame

    # ---------- 参数名（本地模型标准名） ----------

    def _resolve_mouth_params(self) -> Tuple[str, ...]:
        return (_MOUTH_PARAM,)
