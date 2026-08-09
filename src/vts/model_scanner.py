"""模型扫描器：启动时扫描当前模型的全部可用跟踪参数，自动适配（切换模型无需改代码）。

扫描内容：
- 当前模型名称 / ID（CurrentModelRequest）
- VTS 输入参数列表（InputParameterListRequest，tracking 参数）
- 模型输出参数列表（Live2DParameterListRequest）
- 嘴部驱动参数：实测注入各候选参数，检测模型嘴部输出参数响应，
  自动选出有效的嘴部参数并定标增益（替代手动配置 MOUTH_PARAMETER）
- 眨眼 / 微表情参数可用性
- 身体左右摇摆：实测发现能驱动身体旋转的输入参数
  （Body*/Mocopi* 等，自动探测）；无身体参数时用「摇头/歪头」联动驱动身体，
  注入系数上限取输入参数自身范围（VTS 按 min/max 钳制，超出只会爆表）

LLM 通过 [参数:名称=数值] 指令直接控制跟踪参数来驱动表情，
不再依赖模型内置表情预设或热键。"""

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from src.utils import console
from src.vts.controller import VTSController

# 眨眼 tracking 参数标准名
EYE_PARAMS = ("EyeOpenLeft", "EyeOpenRight")
# 微表情扰动可用参数
MICRO_PARAMS = ("MouthSmile", "BrowLeftY", "BrowRightY", "FaceAngleZ")
# （身体左右摇摆不再由本模块合成，由动作文件直接驱动，相关校准代码已移除）


@dataclass
class ModelProfile:
    """一次扫描得到的模型信息与自动适配结果。"""
    model_name: str = ""
    model_id: str = ""
    input_params: Set[str] = field(default_factory=set)      # VTS 输入参数名
    output_params: Set[str] = field(default_factory=set)     # 模型输出参数名
    mouth_param: Optional[str] = None                        # 探测出的嘴部参数
    mouth_gain: float = 0.4                                  # 嘴型增益（按响应定标）
    mouth_form_neutral: Optional[float] = None               # MouthSmile「闭合中性点」（映射偏移校正）
    eyes: Optional[Tuple[str, str]] = None                   # 眨眼可用参数
    micro_params: Set[str] = field(default_factory=set)      # 微表情扰动可用参数
    sway_drivers: List[Tuple[str, float]] = field(default_factory=list)  # 摇摆驱动[(参数,系数)]
    face_angle_range: float = 1.0                    # FaceAngleY 输入参数范围（呼吸注入上限）


async def _read_params(vts: VTSController, names: Set[str]) -> Dict[str, float]:
    """读取指定模型输出参数的当前值。"""
    params = await vts.get_output_parameters()
    return {p["name"]: p.get("value") or 0.0
            for p in params if p.get("name") in names}


# 口型语义分组：语音/张嘴类参数优先（随语音开合正确）；
# MouthSmile/MouthX 是表情参数，做口型会让嘴呈微笑/歪嘴状，仅作无语音参数时的兜底。
_MOUTH_VOICE = ["VoiceVolumePlusMouthOpen", "MouthOpen", "VoiceVolume"]
_MOUTH_EXPR = ["MouthSmile", "MouthX"]
MOUTH_CANDIDATES = _MOUTH_VOICE + _MOUTH_EXPR


async def _measure_candidate(vts: VTSController, cand: str,
                             mouth_out: Set[str]) -> float:
    """测量单个候选的嘴部输出响应（0.6s 注入 + 峰值采样，抗 VTS 平滑/瞬时噪声）。"""
    await vts.inject_parameters({c: 0.0 for c in MOUTH_CANDIDATES})
    await asyncio.sleep(1.2)  # 复位，等残留回落
    before = await _read_params(vts, mouth_out)
    for _ in range(15):  # 持续注入 0.6s，让参数充分到达目标
        await vts.inject_parameters({cand: 0.6})
        await asyncio.sleep(0.04)
    amp = 0.0
    for _ in range(3):  # 注入停止后峰值采样，捕捉平滑到达的最大响应
        s = await _read_params(vts, mouth_out)
        amp = max(amp, max((abs(s.get(n, 0.0) - before.get(n, 0.0))
                            for n in mouth_out), default=0.0))
        await asyncio.sleep(0.08)
    await vts.inject_parameters({cand: 0.0})
    await asyncio.sleep(1.2)  # 收尾，残留充分回落再测下一个
    return amp


async def _probe_mouth(vts: VTSController,
                       output_params: Set[str]) -> Tuple[Optional[str], float]:
    """实测探测嘴部驱动参数：语音类优先 + 峰值采样 + 复核防瞬时噪声。

    Returns:
        (嘴部参数名, 增益)。找不到有效参数时返回 (None, 0.4)。
    """
    mouth_out = {n for n in output_params if "Mouth" in n}
    if not mouth_out:
        console.dim("嘴部：模型无 Mouth 输出参数，跳过探测")
        return None, 0.4

    # 1) 语音/张嘴类候选（正确口型来源）
    best, best_amp = None, 0.0
    console.progress("探测语音/张嘴类候选：")
    for cand in _MOUTH_VOICE:
        amp = await _measure_candidate(vts, cand, mouth_out)
        mark = console.paint("  ✓", console.BRIGHT_GREEN) if amp >= 0.3 else ""
        print(f"      {console.paint(cand.ljust(28), console.CYAN)}"
              f"输出响应 {amp:.3f}{mark}")
        if amp > best_amp:
            best, best_amp = cand, amp

    # 2) 语音类无可靠响应时，用表情类兜底（口型近似，打印警告）
    if best_amp < 0.3:
        console.progress("语音类均无可靠响应，尝试表情类候选：")
        for cand in _MOUTH_EXPR:
            amp = await _measure_candidate(vts, cand, mouth_out)
            mark = console.paint("  ⚠", console.BRIGHT_YELLOW) if amp >= 0.5 else ""
            print(f"      {console.paint(cand.ljust(28), console.CYAN)}"
                  f"输出响应 {amp:.3f}{mark}")
            if amp > best_amp:
                best, best_amp = cand, amp
        if best_amp < 0.5:
            console.warn(f"无可靠嘴部参数（最大响应 {best_amp:.3f}），用默认配置")
            return None, 0.4
        console.warn(f"未找到语音参数，用表情参数近似「{best}」")

    # 3) 复核最佳候选，防止瞬时噪声误选
    verify = await _measure_candidate(vts, best, mouth_out)
    if verify < max(best_amp * 0.5, 0.15):
        console.warn(f"候选「{best}」复核不稳定（{best_amp:.3f}→{verify:.3f}），"
                     f"回退默认 VoiceVolumePlusMouthOpen")
        return "VoiceVolumePlusMouthOpen", 0.4
    console.ok(f"嘴部驱动参数：{best}（复核 {verify:.3f}）")
    gain = min(1.0, 0.5 / max(verify, 0.15))
    return best, round(gain, 2)


async def _probe_mouth_form(vts: VTSController,
                            input_params: Set[str],
                            output_params: Set[str]) -> Optional[float]:
    """标定 MouthSmile → 嘴型输出的「闭合中性点」。

    部分模型（如 hiyori）的 MouthSmile 映射是「反向/偏移线性放大」：
        ParamMouthForm = -1.0 + 2×MouthSmile
    注入 0 或极小值时嘴型被拉到 -1（看起来一直张着嘴）。本函数实测拟合
    MouthSmile 输入 vs Form 输出的线性关系，返回「嘴型输出≈0（自然闭合）」
    所需的 MouthSmile 输入值，供 FaceDriver 做基线校正。

    Returns:
        闭合中性点（0~1）；模型无 MouthSmile 输入 / 无 Form 输出 /
        映射不清晰（中性点≈0 或超范围）时返回 None（不做校正）。
    """
    if "MouthSmile" not in input_params:
        return None
    form_out = {n for n in output_params if "Form" in n}
    if not form_out:
        return None
    # 复位后注入两个采样点，拟合线性映射
    await vts.inject_parameters({c: 0.0 for c in MOUTH_CANDIDATES})
    await asyncio.sleep(1.2)
    await vts.inject_parameters({"MouthSmile": 0.0})
    for _ in range(15):
        await asyncio.sleep(0.04)
    s0 = await _read_params(vts, form_out)
    await vts.inject_parameters({"MouthSmile": 0.6})
    for _ in range(15):
        await asyncio.sleep(0.04)
    s1 = await _read_params(vts, form_out)
    await vts.inject_parameters({"MouthSmile": 0.0})
    await asyncio.sleep(1.0)
    # 选响应变化最大的 Form 参数拟合
    key = max(form_out,
              key=lambda n: abs(s1.get(n, 0.0) - s0.get(n, 0.0)))
    f0, f1 = s0.get(key, 0.0), s1.get(key, 0.0)
    slope = (f1 - f0) / 0.6
    if abs(slope) < 0.15:
        console.dim(f"嘴型：{key} 对 MouthSmile 响应过弱（{slope:.2f}），无需校正")
        return None
    neutral = -f0 / slope
    # 中性点≈0：默认即闭合，无需校正；超范围：无法把嘴型拉回闭合
    if not 0.15 <= neutral <= 1.0:
        console.dim(f"嘴型：{key} 中性点 {neutral:.2f} 不在校正范围，跳过")
        return None
    console.info(f"嘴型：{key} 映射 f={f0:.2f}+{slope:.2f}x，"
                 f"闭合中性点 MouthSmile={neutral:.2f}")
    return round(neutral, 2)


async def scan_model(vts: VTSController, cfg) -> ModelProfile:
    """启动时扫描当前模型全部信息，自动适配。

    无论成功与否都会返回 ModelProfile（失败时多为空字段，调用方按默认值兜底）。
    """
    profile = ModelProfile()
    console.header("正在扫描当前模型，自动适配（约数秒）")

    # 1) 当前模型
    d = await vts.get_current_model()
    profile.model_name = d.get("modelName", "")
    profile.model_id = d.get("modelID", "")
    console.kv("模型", profile.model_name or "（未加载）")

    if not d.get("modelLoaded"):
        console.warn("未加载模型，尝试自动加载第一个可用模型...")
        models = await vts.get_available_models()
        if not models:
            console.error("VTS 中没有任何可用模型，请先在 VTubeStudio 中添加模型。")
            return profile
        # 优先选已加载的（极少情况响应延迟导致第一次没读到），否则选第一个
        target = next((m for m in models if m.get("modelLoaded")), models[0])
        mid = target.get("modelID", "")
        mname = target.get("modelName", "")
        if not mid:
            console.error("可用模型缺少 modelID，无法加载。")
            return profile
        console.info(f"正在加载模型「{mname or mid}」...")
        ok = await vts.load_model(mid)
        if not ok:
            console.error(f"加载模型「{mname or mid}」失败，请在 VTS 中手动加载。")
            return profile
        console.ok(f"模型「{mname or mid}」加载成功")
        await asyncio.sleep(1.0)  # 等待 VTS 完成加载与参数初始化
        # 重新获取模型信息
        d = await vts.get_current_model()
        profile.model_name = d.get("modelName", "") or mname
        profile.model_id = d.get("modelID", "") or mid
        console.kv("模型", profile.model_name)

    # 2) VTS 输入参数列表
    default_params, custom_params = await vts.get_input_parameters()
    input_ranges: Dict[str, float] = {}   # 参数名 → 对称范围上限（min/max 较大者）
    for p in default_params + custom_params:
        name = p.get("name", "")
        if not name:
            continue
        profile.input_params.add(name)
        try:
            r = max(abs(float(p.get("min", 0.0))), abs(float(p.get("max", 0.0))))
            input_ranges[name] = r if r > 0 else 1.0
        except (TypeError, ValueError):
            input_ranges[name] = 1.0
    console.kv("输入参数", f"{len(profile.input_params)} 个")

    # 3) VTS 输出参数列表（Live2D 模型输出参数，用于嘴部探测）
    out_params = await vts.get_output_parameters()
    for p in out_params:
        name = p.get("name", "")
        if name:
            profile.output_params.add(name)
    console.kv("输出参数", f"{len(profile.output_params)} 个")

    # 4) 嘴部驱动参数（自动定位，替代手动配置）
    console.progress("探测嘴部驱动参数...")
    profile.mouth_param, profile.mouth_gain = await _probe_mouth(
        vts, profile.output_params)
    console.kv("嘴部参数", f"{profile.mouth_param or '未找到（用默认）'} "
                           f"(gain={profile.mouth_gain})")
    # MouthSmile 闭合中性点标定（映射偏移/反向模型自动校正，防嘴一直张着）
    profile.mouth_form_neutral = await _probe_mouth_form(
        vts, profile.input_params, profile.output_params)

    # 5) 眨眼 / 微表情参数可用性
    if all(e in profile.input_params for e in EYE_PARAMS):
        profile.eyes = EYE_PARAMS
    profile.micro_params = {p for p in MICRO_PARAMS if p in profile.input_params}
    console.kv("面部能力", f"眨眼: {'可用' if profile.eyes else '不可用'} | "
                           f"微表情: {sorted(profile.micro_params) or '无'}")

    # 6) 参数列表输出
    console.kv("可用参数", f"{len(profile.input_params)} 个输入 tracking 参数")

    # 复位眼部参数，从干净基线开始运行
    await vts.inject_parameters(
        {p: 0.0 for p in EYE_PARAMS if p in profile.input_params})
    await asyncio.sleep(1.5)
    console.ok("模型扫描完成，已自动适配")
    return profile