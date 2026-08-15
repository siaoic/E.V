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
import json
import os
import winreg
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from src.utils import console
from src.vts.controller import VTSController

# 眨眼 tracking 参数标准名
EYE_PARAMS = ("EyeOpenLeft", "EyeOpenRight")
# 微表情扰动可用参数
MICRO_PARAMS = ("MouthSmile", "BrowLeftY", "BrowRightY", "FaceAngleZ")
# （身体左右摇摆不再由本模块合成，由动作文件直接驱动，相关校准代码已移除）

# VTube Studio 相对 Steam 库的安装子路径
_VTS_STEAM_REL = os.path.join("steamapps", "common", "VTube Studio")


def _find_vts_root(cfg) -> str:
    """定位 VTube Studio 安装根目录：VTS_ROOT 配置优先，其次 Steam 注册表。

    待机动画接管需要读取模型配置文件（官方 API 不返回模型文件路径），
    这里只做文件定位，不发送任何非官方消息。找不到时返回 ""（跳过接管）。
    """
    if getattr(cfg, "VTS_ROOT", ""):
        return cfg.VTS_ROOT
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Valve\Steam") as key:
            steam_path = winreg.QueryValueEx(key, "SteamPath")[0]
        candidate = os.path.join(steam_path, _VTS_STEAM_REL)
        if os.path.isdir(candidate):
            return candidate
    except OSError:
        pass
    return ""


async def locate_model_folder(vts: VTSController, cfg) -> str:
    """定位当前 VTS 模型文件夹的绝对路径（官方 API 不暴露模型文件路径）。

    定位方式：
    1. 定位 VTube Studio 安装目录（VTS_ROOT 配置或 Steam 注册表）
    2. VTSFolderInfoRequest 取模型目录名（如 Live2DModels）
    3. 按 CurrentModelRequest 的 vtsModelName 定位模型文件夹

    任一步失败返回 ""，调用方跳过（不影响原有流程）。
    供待机动画接管（resolve_idle_motion）与表情/动作文件夹扫描
    （VtsEmotionActor）复用。
    """
    vts_root = _find_vts_root(cfg)
    if not vts_root:
        console.dim("未定位到 VTube Studio 安装目录"
                    "（可配置 VTS_ROOT），跳过模型文件夹扫描")
        return ""
    models_dir = (await vts.get_folder_info()).get("models", "") or ""
    if not models_dir:
        console.dim("VTSFolderInfoRequest 未返回模型目录，跳过模型文件夹扫描")
        return ""
    vts_model_name = (await vts.get_current_model()).get("vtsModelName", "") or ""
    if not vts_model_name:
        console.dim("未取得 vtsModelName，跳过模型文件夹扫描")
        return ""

    root = os.path.join(vts_root, "VTube Studio_Data",
                        "StreamingAssets", models_dir)
    if not os.path.isdir(root):
        console.dim(f"模型目录不存在 {root}，跳过模型文件夹扫描")
        return ""
    for entry in os.listdir(root):
        candidate = os.path.join(root, entry)
        if os.path.isfile(os.path.join(candidate, vts_model_name)):
            return candidate
    console.dim(f"模型目录下未找到 {vts_model_name}，跳过模型文件夹扫描")
    return ""


async def resolve_idle_motion(vts: VTSController, cfg) -> str:
    """解析当前模型在 VTS 中配置的待机动画文件（供循环接管）。

    VTS 内置待机动画（P1）循环到尾帧后直接硬跳回首帧，官方 API 无淡入淡出
    接口可修。本函数返回该待机动画的绝对路径，由 FaceDriver 改走插件注入
    路径播放（P2 优先级高于 P1），循环点由 MotionPlayer 平滑混合。

    仅当文件存在且 Meta.Loop=true（循环播放才有跳变问题）时返回绝对路径；
    任一步失败返回 ""，调用方跳过接管，不影响原有流程。
    """
    model_folder = await locate_model_folder(vts, cfg)
    if not model_folder:
        console.dim("待机动画接管：模型文件夹定位失败，跳过")
        return ""

    vts_model_name = (await vts.get_current_model()).get("vtsModelName", "") or ""
    if not vts_model_name:
        console.dim("待机动画接管：未取得 vtsModelName，跳过")
        return ""
    try:
        with open(os.path.join(model_folder, vts_model_name),
                  "r", encoding="utf-8") as f:
            vtube_cfg = json.load(f)
    except Exception as e:
        console.dim(f"待机动画接管：读取 {vts_model_name} 失败（{e}），跳过")
        return ""
    idle_name = (vtube_cfg.get("FileReferences", {})
                 .get("IdleAnimation") or "").strip()
    if not idle_name:
        console.dim("待机动画接管：模型未配置待机动画，跳过")
        return ""
    # VTS 模型设置里待机动画可直接选模型文件夹任意子目录下的文件，
    # 存的是文件名（如 def.motion3.json），需在模型文件夹内递归同名定位
    idle_path = os.path.join(model_folder, idle_name)
    if not os.path.isfile(idle_path):
        idle_path = ""
        for _base, _dirs, _files in os.walk(model_folder):
            if idle_name in _files:
                idle_path = os.path.join(_base, idle_name)
                break
    if not idle_path:
        console.dim(f"待机动画接管：模型文件夹内未找到 {idle_name}，跳过")
        return ""

    # 仅接管循环待机动画：Loop=false 时 VTS 播放一次即停，无循环跳变问题
    try:
        with open(idle_path, "r", encoding="utf-8") as f:
            motion_meta = json.load(f).get("Meta", {})
    except Exception as e:
        console.dim(f"待机动画接管：读取 {idle_name} 失败（{e}），跳过")
        return ""
    if not motion_meta.get("Loop", False):
        console.dim(f"待机动画接管：{idle_name} 非循环动画，跳过")
        return ""
    idle_path = os.path.normpath(idle_path)
    # 文件级无缝化：在待机动画末尾追加首尾过渡段（改前自动备份 .bak），
    # 让 VTS 原生播放待机动画时循环点自然。插件注入只能覆盖少量标准参数，
    # 而动画主体（Live2D 自定义参数）由 VTS 原生播放——文件无缝化才能消除
    # 循环点跳变。幂等（文件已无缝则跳过）；失败不影响接管，按原文件播放。
    try:
        from src.vts.motion_player import make_seamless
        if make_seamless(idle_path):
            console.dim("待机动画已无缝化：循环点跳变已消除"
                        "（原文件备份为 .bak，需在 VTS 重载模型生效）")
    except Exception as e:
        console.dim(f"待机动画无缝化失败（{e}），按原文件播放")
    console.info(f"待机动画接管：{idle_path}")
    return idle_path


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
    idle_motion: str = ""                            # 待机动画文件绝对路径（循环接管用）


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
    console.dim("正在扫描当前模型，自动适配（约数秒）")

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

    # 7) 待机动画接管解析（消除 VTS 待机动画循环点尾帧→首帧硬跳）
    if getattr(cfg, "VTS_IDLE_TAKEOVER", True):
        profile.idle_motion = await resolve_idle_motion(vts, cfg)
        if profile.idle_motion:
            console.kv("待机动画", os.path.basename(profile.idle_motion))

    # 复位眼部参数，从干净基线开始运行
    await vts.inject_parameters(
        {p: 0.0 for p in EYE_PARAMS if p in profile.input_params})
    await asyncio.sleep(1.5)
    console.ok("模型扫描完成，已自动适配")
    return profile