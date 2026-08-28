"""识谱工具实现：乐谱图片 → homr OMR → play_score JSON（read_sheet_music）。

管线（复刻《简单爱》实测链路，识别结果与当时逐字节一致）：
1. 取图：path 支持 * 通配符（多页乐谱，按文件名排序）；不传 path 则截取当前屏幕
2. 每张图跑一次 homr（uv tool 安装的 OMR，权重用本地 release zip）→ 同目录 <stem>.musicxml
   （已存在且比图新的 musicxml 直接复用：homr 输出确定，同图同结果）
3. 按序解析全部 MusicXML → score JSON（treble/bass 双 track，跨页 beat 累计，
   处理 divisions 变化 / backup 声部回退 / chord 和弦对齐 / grace 装饰音跳过）
4. score 写成 <stem>.score.json 文件，返回路径与摘要给主 LLM → 调 play_score(path=...)

设计分工：本工具只负责「看谱转谱面文件」；「弹」交给 MCP piano 的 play_score
（已支持 path 参数直接读谱面文件，大谱面不必在上下文里传 JSON）。
"""

import asyncio
import glob as _glob
import json
import os
import shutil
import subprocess

# homr 可执行：uv tool install homr 后 bin 在 PATH；兜底 uv 默认 bin 目录
_HOMR_FALLBACK = os.path.join(os.path.expanduser("~"), ".local", "bin", "homr.exe")
_HOMR_PAGE_TIMEOUT = 300.0  # 单页 CPU 推理上限（实测约 40-60s/页）
_DEFAULT_TEMPO = 88  # 谱面未标速度时的默认 BPM


def _find_homr() -> str | None:
    exe = shutil.which("homr")
    if exe:
        return exe
    return _HOMR_FALLBACK if os.path.isfile(_HOMR_FALLBACK) else None


def _resolve_images(path: str) -> list[str]:
    """展开通配符/目录为排序后的图片列表；普通文件路径原样返回单元素列表。"""
    target = path.strip().strip('"')
    if os.path.isdir(target):
        imgs = [
            os.path.join(target, p) for p in os.listdir(target)
            if p.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
        ]
        return sorted(imgs)
    if any(ch in target for ch in "*?["):
        imgs = [
            p for p in _glob.glob(target)
            if p.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
        ]
        return sorted(imgs)
    return [target]


def _run_homr_on_image(homr_exe: str, img: str) -> str:
    """对单张图跑 homr（子进程），返回产出的 musicxml 路径。

    homr 内部用 OpenCV imread，Windows 下读不了非 ASCII 路径——
    中文文件名先复制到 ASCII 临时路径识别，再把 musicxml 移回原图旁。
    """
    xml = os.path.splitext(img)[0] + ".musicxml"
    if os.path.isfile(xml) and os.path.getmtime(xml) > os.path.getmtime(img):
        return xml  # 已有更新的识别产物：复用
    work, tmp_dir = img, None
    try:
        if not img.isascii():
            import hashlib
            import tempfile
            tmp_dir = tempfile.mkdtemp(prefix="omr_")
            work = os.path.join(
                tmp_dir,
                f"page_{hashlib.md5(img.encode('utf-8')).hexdigest()[:8]}{os.path.splitext(img)[1]}",
            )
            shutil.copy2(img, work)
        proc = subprocess.run(
            [homr_exe, work],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=_HOMR_PAGE_TIMEOUT,
        )
        src_xml = os.path.splitext(work)[0] + ".musicxml"
        if work != img and os.path.isfile(src_xml):
            shutil.move(src_xml, xml)
        if not os.path.isfile(xml):
            tail = (proc.stderr or proc.stdout or "")[-600:]
            raise RuntimeError(f"homr 未产出 musicxml（退出码 {proc.returncode}）：{tail}")
        return xml
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _grab_screen_png() -> str:
    """截取当前屏幕存为本目录临时图片（乐谱显示在屏幕上时用）。"""
    import PIL.Image
    import PIL.ImageGrab

    img = PIL.ImageGrab.grab().convert("RGB")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_screen_sheet.png")
    img.save(out)
    return out


# ---------- MusicXML → score（移植自 _steinway_tmp/omr_xml2score.py 实测版） ----------

_DIV_FALLBACK = 4


def _note_name(pitch_el) -> str:
    step = pitch_el.findtext("step")
    alter = int(pitch_el.findtext("alter") or 0)
    octv = int(pitch_el.findtext("octave"))
    acc = "#" * alter if alter > 0 else "b" * (-alter)
    return f"{step}{acc}{octv}"


def _parse_page(path: str, treble: list, bass: list, tempo_state: dict,
                start_beat: float = 0.0) -> float:
    """解析单页 MusicXML，音符追加进 treble/bass；返回本页结束的绝对拍。"""
    import xml.etree.ElementTree as ET

    root = ET.parse(path).getroot()
    part = root.find("part")
    # homr 每页 divisions 可能不同，必须先预扫描确定再算偏移
    divisions = _DIV_FALLBACK
    for a in part.iter("attributes"):
        d = a.findtext("divisions")
        if d:
            divisions = int(d)
            break
    offset = int(start_beat * divisions)  # 全局偏移（ticks）
    for meas in part.findall("measure"):
        for a in meas.findall("attributes"):
            d = a.findtext("divisions")
            if d:
                divisions = int(d)
            t = a.find("time")
            if t is not None and tempo_state.get("beats") is None:
                tempo_state["beats"] = t.findtext("beats")
                tempo_state["beat_type"] = t.findtext("beat-type")
        beats = int(tempo_state.get("beats") or 4)
        btype = int(tempo_state.get("beat_type") or 4)
        len_ticks = int(beats * 4 / btype * divisions)  # 4/4 -> 16
        tick = 0  # 小节内游标
        max_intra = 0
        last_onset = 0  # 上一个音的起始 tick（<chord> 与其对齐，游标不动）
        for el in meas:
            if el.tag == "backup":
                # homr 的 backup 是切声部回退，可能超过当前游标，clamp
                tick = max(0, tick - int(el.findtext("duration")))
            elif el.tag == "forward":
                tick += int(el.findtext("duration"))
            elif el.tag == "note":
                dur = int(el.findtext("duration") or 0)
                if el.find("grace") is not None:
                    continue
                is_chord = el.find("chord") is not None
                is_rest = el.find("rest") is not None
                staff = int(el.findtext("staff") or 1)
                track = treble if staff == 1 else bass
                if is_chord:
                    onset = last_onset  # 与前一个音同拍
                else:
                    onset = tick
                    tick += dur
                if not is_rest and el.find("pitch") is not None:
                    track.append({
                        "note": _note_name(el.find("pitch")),
                        "beat": (offset + onset) / divisions,
                        "duration": dur / divisions,
                    })
                last_onset = onset
                max_intra = max(max_intra, tick)
        offset += len_ticks
    return offset / divisions


def _build_score(xml_paths: list[str], tempo: float) -> tuple[dict, int, float, str]:
    """按序解析多页 musicxml → (score, 音数, 总拍数, 声部说明)。"""
    treble: list = []
    bass: list = []
    tempo_state: dict = {}
    page_beat = 0.0
    for p in xml_paths:
        page_beat = _parse_page(p, treble, bass, tempo_state, start_beat=page_beat)
    treble.sort(key=lambda n: n["beat"])
    bass.sort(key=lambda n: n["beat"])
    tracks = [{"name": "treble", "notes": treble}]
    if bass:
        tracks.append({"name": "bass", "notes": bass})
    total_beat = max([n["beat"] + n["duration"] for n in treble + bass] or [0])
    score = {"tempo": tempo, "tracks": tracks}
    hands = "、".join(f"{t['name']} {len(t['notes'])} 音" for t in tracks)
    return score, len(treble) + len(bass), total_beat, hands


async def _read_sheet_music(path: str = "", tempo: float = _DEFAULT_TEMPO) -> str:
    """OMR 主流程：取图 → homr → musicxml → score 文件 → 指示主 LLM 弹奏。"""
    if path and path.strip():
        images = _resolve_images(path)
        if not images:
            return f"错误：通配符没匹配到乐谱图片：{path}"
        missing = [p for p in images if not os.path.isfile(p)]
        if missing:
            return f"错误：找不到乐谱图片文件：{'、'.join(missing)}"
    else:
        try:
            images = [await asyncio.to_thread(_grab_screen_png)]
        except Exception as e:
            return f"错误：截取屏幕失败：{e}"

    homr_exe = _find_homr()
    if not homr_exe:
        return "错误：找不到 homr 可执行文件（需先 uv tool install homr）"

    try:
        xmls = []
        for img in images:
            xmls.append(await asyncio.to_thread(_run_homr_on_image, homr_exe, img))
    except Exception as e:
        return f"错误：OMR 识别失败：{e}"

    score, note_count, total_beat, hands = _build_score(xmls, tempo)
    if note_count == 0:
        return "错误：识别完成但谱面里没有可演奏的音符（可能不是乐谱图片）。"

    out_path = os.path.splitext(images[0])[0] + ".score.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(score, f, ensure_ascii=False)

    return (
        f"识谱完成：{len(images)} 页 → 共 {note_count} 个音符（{hands}），"
        f"总长 {total_beat:.0f} 拍 ≈ {total_beat / 4:.0f} 小节（4/4），"
        f"tempo={tempo} BPM。\n"
        f"谱面文件已保存：{out_path}\n"
        f"请立即调用 play_score 弹奏：path=\"{out_path}\""
        "（速度不合适可在结果里改 tempo 重弹）。"
    )
