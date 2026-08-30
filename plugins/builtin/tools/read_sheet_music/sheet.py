"""识谱工具实现：乐谱图片 → homr OMR → play_score JSON（read_sheet_music）。

管线（复刻《简单爱》实测链路，识别结果与当时逐字节一致）：
1. 取图：path 支持 * 通配符（多页乐谱，按文件名排序）；不传 path 则截取当前屏幕
2. 每张图识别一次：常驻 OMR daemon（GPU，模型只加载一次，~10s/页，同 daemon
   协议见 omr_daemon.py）→ 同目录 <stem>.musicxml；daemon 不可用/单页失败时
   回退官方 homr 子进程（CPU 40-60s/页）。
   （已存在且比图新的 musicxml 直接复用：识别结果确定，同图同结果）
3. 按序解析全部 MusicXML → score JSON（treble/bass 双 track，跨页 beat 累计，
   处理 divisions 变化 / backup 声部回退 / chord 和弦对齐 / grace 装饰音跳过）
4. 速度：homr 会丢弃图上的速度文字（MusicXML 无 tempo）。两级恢复：
   ① 结构检测（♩=N 节拍器标记，移植自 audiveris_py，纯 cv2，~0.1s，见
   tempo_detect.py）；② 未命中再与 OMR 并行启动视觉模型识别谱图顶部补上
   （优先级：tempo 参数 > MusicXML > 结构检测 > 视觉识别）
5. score 写成 <stem>.score.json 文件，返回路径与摘要给主 LLM → 调 play_score(path=...)

设计分工：本工具只负责「看谱转谱面文件」；「弹」交给 MCP piano 的 play_score
（已支持 path 参数直接读谱面文件，大谱面不必在上下文里传 JSON）。
"""

import asyncio
import atexit
import glob as _glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading

from ev.utils import console

from plugins.builtin.tools.read_sheet_music.tempo_detect import detect_tempo_mark

# homr 可执行：uv tool install homr 后 bin 在 PATH；兜底 uv 默认 bin 目录
_HOMR_FALLBACK = os.path.join(os.path.expanduser("~"), ".local", "bin", "homr.exe")
_HOMR_PAGE_TIMEOUT = 300.0  # 单页 CPU 推理上限（实测约 40-60s/页）


def _find_homr() -> str | None:
    exe = shutil.which("homr")
    if exe:
        return exe
    return _HOMR_FALLBACK if os.path.isfile(_HOMR_FALLBACK) else None


_OMR_DAEMON_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "omr_daemon.py")
_OMR_DAEMON_STARTUP_TIMEOUT = 120.0  # daemon 冷启动（imports + Segnet session）上限
_OMR_DAEMON_PAGE_TIMEOUT = 120.0     # 单页 GPU 识别上限（实测 ~10s/页）


def _find_homr_python() -> str | None:
    """homr 工具 venv 的 python：daemon 要 import homr 全家桶，须用该解释器。"""
    appdata = os.environ.get("APPDATA", "")
    for cand in (
        os.path.join(appdata, "uv", "tools", "homr", "Scripts", "python.exe"),
        os.path.join(appdata, "uv", "tools", "homr", "bin", "python"),
    ):
        if os.path.isfile(cand):
            return cand
    return None


class _OmrDaemon:
    """常驻 OMR daemon 懒启动单例：模型只加载一次，多页/多次识谱复用同一进程。

    生命周期：首次识谱才拉起（启动开销与 tempo VLM 识别并行）；启动失败
    （缺 python/脚本、READY 超时）记 broken，本次进程内不再重试，直接走
    官方 homr 回退；单页请求失败（超时/崩溃）只杀进程，下页重建再试。
    进程退出时 atexit 发 quit，避免孤儿 daemon 占着 GPU 显存。
    """

    _lock = threading.Lock()
    _proc: subprocess.Popen | None = None
    _broken = False
    _warmup_started = False

    @classmethod
    def warmup(cls) -> None:
        """启动即后台预热 daemon（模型加载与第一次识谱解耦）。

        daemon 懒启动要现场加载 Segnet 模型（可达 1-2 分钟），恰逢调用方
        的单步超时窗口就会「首次识谱必超时、重试才成」。启动时在后台线程
        拉起，首次识谱只剩 ~10s/页的推理。失败静默（识别时再拉起兜底）。
        """
        if cls._broken or cls._warmup_started or cls._proc is not None:
            return
        cls._warmup_started = True

        def _go():
            try:
                with cls._lock:  # 与 recognize 互斥：预热中到达的识谱排队等它完成
                    if cls._proc is None or cls._proc.poll() is not None:
                        cls._proc = cls._start()
                console.dim("[OMR] daemon 预热完成（模型已加载，识谱即用）")
            except Exception as e:
                console.dim(f"[OMR] daemon 预热失败（识谱时再拉起）：{e}")

        threading.Thread(target=_go, name="omr-warmup", daemon=True).start()

    @classmethod
    def _start(cls) -> subprocess.Popen:
        py = _find_homr_python()
        if not py:
            raise RuntimeError("找不到 homr 工具的 venv python（uv tools 目录）")
        if not os.path.isfile(_OMR_DAEMON_SCRIPT):
            raise RuntimeError(f"找不到 daemon 脚本：{_OMR_DAEMON_SCRIPT}")
        log_path = os.path.join(tempfile.gettempdir(), "vtuber_omr_daemon.log")
        log = open(log_path, "ab")  # daemon stderr（计时打点）落盘，防管道堵塞
        try:
            proc = subprocess.Popen(
                [py, "-X", "utf8", _OMR_DAEMON_SCRIPT],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                text=True, encoding="utf-8", errors="replace",
            )
        finally:
            log.close()
        box: dict = {}

        def _wait_ready():
            box["line"] = proc.stdout.readline()

        t = threading.Thread(target=_wait_ready, daemon=True)
        t.start()
        t.join(_OMR_DAEMON_STARTUP_TIMEOUT)
        if t.is_alive() or box.get("line", "").strip() != "READY":
            proc.kill()
            raise RuntimeError(f"OMR daemon 启动超时或异常退出（日志：{log_path}）")
        return proc

    @classmethod
    def recognize(cls, img: str, xml: str) -> str:
        """发一页图给 daemon，返回 musicxml 路径；失败抛异常由调用方回退。"""
        with cls._lock:
            if cls._broken:
                raise RuntimeError("OMR daemon 此前启动失败，本次进程内不再重试")
            try:
                proc = cls._proc
                if proc is None or proc.poll() is not None:
                    try:
                        proc = cls._proc = cls._start()
                    except Exception:
                        cls._broken = True
                        cls._proc = None
                        raise
                box: dict = {}

                def _roundtrip():
                    try:
                        proc.stdin.write(f"{img}\t{xml}\n")
                        proc.stdin.flush()
                        box["resp"] = proc.stdout.readline()
                    except Exception as ex:  # noqa: BLE001
                        box["err"] = ex

                t = threading.Thread(target=_roundtrip, daemon=True)
                t.start()
                t.join(_OMR_DAEMON_PAGE_TIMEOUT)
                if t.is_alive() or "resp" not in box:
                    raise TimeoutError(f"OMR daemon 单页响应超时（{_OMR_DAEMON_PAGE_TIMEOUT}s）")
                resp = json.loads(box["resp"]) if box.get("resp") else {}
                if not resp.get("ok"):
                    raise RuntimeError(f"OMR daemon 识别失败：{resp.get('error')}")
                return resp.get("xml") or xml
            except Exception:
                if cls._proc is not None:  # 请求级失败：杀进程，下页重建再试
                    cls._proc.kill()
                    cls._proc = None
                raise

    @classmethod
    def shutdown(cls) -> None:
        with cls._lock:
            cls._broken = True
            proc, cls._proc = cls._proc, None
            if proc is None or proc.poll() is not None:
                return
            try:
                proc.stdin.write("quit\n")
                proc.stdin.flush()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


atexit.register(_OmrDaemon.shutdown)


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


def _run_omr(img: str) -> str:
    """单页 OMR 调度：已有更新 musicxml 直接复用（0s）→ 常驻 daemon（GPU
    ~10s/页）→ daemon 不可用/单页失败时回退官方 homr 子进程（CPU 40-60s/页）。"""
    xml = os.path.splitext(img)[0] + ".musicxml"
    if os.path.isfile(xml) and os.path.getmtime(xml) > os.path.getmtime(img):
        return xml
    try:
        return _OmrDaemon.recognize(img, xml)
    except Exception as daemon_ex:
        homr_exe = _find_homr()
        if not homr_exe:
            raise RuntimeError(
                f"OMR daemon 不可用（{daemon_ex}），且找不到 homr 可执行文件"
                "（需先 uv tool install homr）"
            ) from daemon_ex
        return _run_homr_on_image(homr_exe, img)


def _grab_screen_png() -> str:
    """截取当前屏幕存为本目录临时图片（乐谱显示在屏幕上时用）。"""
    import PIL.Image
    import PIL.ImageGrab

    img = PIL.ImageGrab.grab().convert("RGB")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_screen_sheet.png")
    img.save(out)
    return out


_TEMPO_PROMPT = """这是乐谱图片的顶部区域。找出图中的速度标记（形如 ♩=100、J=100、\
q=100、♪=100、100 BPM、"速度 100" 等，通常在标题附近或谱表上方）。
- 找到：只输出那个数字（如 100），不要输出任何其他内容
- 没找到：只输出 NONE
注意：不要把标题里的数字、页码、小节号当成速度。"""


async def _detect_tempo_by_vlm(image_path: str) -> int | None:
    """用视觉模型从谱图顶部识别速度标记（♩=N / BPM 数字）。

    homr 已知会识别速度文字但故意丢弃（title_detection.is_tempo_marking
    只用于区分标题，不写进 MusicXML）——谱面 <sound tempo> 缺失时这是唯一
    能从图上恢复 BPM 的通道。任何失败（无视觉模型/超时/解析不出）都返回
    None，绝不影响主流程。
    """
    try:
        from ev.llm.butler_agent import ButlerAgent

        import PIL.Image
        img = PIL.Image.open(image_path)
        # 速度标记几乎总在页面上部（标题与第一行谱表之间）：只裁顶部 40%
        crop = img.convert("RGB").crop((0, 0, img.size[0], max(1, int(img.size[1] * 0.4))))
        import io as _io
        buf = _io.BytesIO()
        crop.save(buf, "JPEG", quality=92)
        import base64 as _b64
        b64 = _b64.b64encode(buf.getvalue()).decode("ascii")
        text = await asyncio.wait_for(
            ButlerAgent().describe_image(b64, prompt=_TEMPO_PROMPT, max_tokens=256),
            timeout=150.0,  # 思考型主模型（深度思考开启时）单次响应可能超 60s
        )
        if not text:
            return None
        text = text.strip()
        # 剥离思考模型的 <think>…</think> 段：思考文既可能复述提示词里的
        # "NONE"（误判无速度），也可能含数字（干扰解析）——先剥离再判断
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        if "none" in text.lower():
            return None
        m = re.search(r"(\d{2,3})", text)
        if not m:
            return None
        bpm = int(m.group(1))
        return bpm if 30 <= bpm <= 260 else None
    except Exception:
        return None


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
        # 谱面若标注速度（<sound tempo="...">）则记录（首个为准）
        if tempo_state.get("tempo") is None:
            for s in meas.findall("sound"):
                st = s.get("tempo")
                if st:
                    try:
                        tempo_state["tempo"] = float(st)
                    except ValueError:
                        pass
                    break
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


def _build_score(xml_paths: list[str], tempo: float | None) -> tuple[dict, int, float, str, float | None]:
    """按序解析多页 musicxml → (score, 音数, 总拍数, 声部说明, 实际采用的速度)。

    速度优先级：显式 tempo 参数 > 谱面 <sound tempo> 标注；两者皆无则谱面不带 tempo 字段
    （由 play_score 端自行决定速度，不做无依据的猜测）。
    """
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
    used_tempo = tempo if tempo is not None else tempo_state.get("tempo")
    score = {"tracks": tracks}
    if used_tempo is not None:
        score["tempo"] = used_tempo
    hands = "、".join(f"{t['name']} {len(t['notes'])} 音" for t in tracks)
    return score, len(treble) + len(bass), total_beat, hands, used_tempo


async def _read_sheet_music(path: str = "", tempo: float | None = None) -> dict:
    """OMR 主流程：取图 → homr → musicxml → score 文件 → 指示主 LLM 弹奏。

    返回 dict（tool_registry 自动 json.dumps，避免非 JSON 字符串告警）：
    status/message/score_path 等结构化字段；message 内含给主 LLM 的完整
    指引文案（含"无速度标注必须 bing_search"的引导）。
    """
    if path and path.strip():
        images = _resolve_images(path)
        if not images:
            return {"status": "error", "message": f"错误：通配符没匹配到乐谱图片：{path}"}
        missing = [p for p in images if not os.path.isfile(p)]
        if missing:
            return {"status": "error",
                    "message": f"错误：找不到乐谱图片文件：{'、'.join(missing)}"}
        # 扩展名预检（2026-08-29 实测：.txt 等非图片会一路跑进 OMR 静默数分钟）：
        # 非图片扩展名直接快速失败，并给主 LLM 明确的拒答指引
        bad_ext = [p for p in images
                   if not p.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))]
        if bad_ext:
            return {
                "status": "error",
                "message": (f"错误：{'、'.join(bad_ext)} 不是乐谱图片"
                            "（仅支持 png/jpg/jpeg/webp/bmp）。"
                            "不要再调用本工具或 play_score 处理它：直接告诉用户该文件"
                            "无法识别乐谱；.mid 文件请用 play_midi_file。"),
            }
    else:
        try:
            images = [await asyncio.to_thread(_grab_screen_png)]
        except Exception as e:
            return {"status": "error", "message": f"错误：截取屏幕失败：{e}"}

    # 谱面速度恢复两级通道：homr 已知会 OCR 出速度文字但故意丢弃
    #（title_detection.is_tempo_marking 只用于区分标题），MusicXML 里没有
    # tempo 信息——谱面无 <sound tempo> 时从原图补认。
    # ① 结构检测（♩=N 节拍器标记，移植自 audiveris_py，纯 cv2 ~0.1s、零模型
    #    开销）：先跑它，命中则完全跳过 VLM；
    # ② VLM 视觉识别兜底：结构检测未命中才与 OMR 并行启动（daemon 懒启动
    #    发生在首个 _run_omr 里，启动开销与 VLM 识别天然并行）。
    tempo_struct = await asyncio.to_thread(detect_tempo_mark, images[0])
    tempo_task = None
    if tempo_struct is None:
        tempo_task = asyncio.create_task(_detect_tempo_by_vlm(images[0]))
    try:
        xmls = []
        for img in images:
            xmls.append(await asyncio.to_thread(_run_omr, img))
    except Exception as e:
        if tempo_task is not None:
            tempo_task.cancel()
        return {"status": "error", "message": f"错误：OMR 识别失败：{e}"}
    vlm_tempo = await tempo_task if tempo_task is not None else None

    score, note_count, total_beat, hands, used_tempo = _build_score(xmls, tempo)
    if used_tempo is not None:
        tempo_src = "调用参数指定" if tempo is not None else "MusicXML 标注"
    if used_tempo is None and tempo_struct is not None:
        # 结构检测优先于 VLM：确定性结果，且本来就不启动 VLM
        used_tempo, tempo_src = tempo_struct, "谱面速度标记（结构检测）"
        score["tempo"] = used_tempo
    if used_tempo is None and vlm_tempo is not None:
        # 视觉识别兜底：仅当参数、MusicXML、结构检测都没有速度时采用
        used_tempo, tempo_src = vlm_tempo, "谱面速度标记（视觉识别）"
        score["tempo"] = used_tempo
    if note_count == 0:
        return {"status": "error",
                "message": "错误：识别完成但谱面里没有可演奏的音符（可能不是乐谱图片）。"}

    out_path = os.path.splitext(images[0])[0] + ".score.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(score, f, ensure_ascii=False)

    # 琴谱库同步（跨插件软依赖）：epiano 下载过的谱识别完成后，把该曲标记为
    # 「已识别可直接弹」（library.json + 长期记忆同步刷新）；非 epiano 来源静默跳过
    try:
        from plugins.builtin.tools.everyonepiano_crawler.epiano import mark_omr_ready
        await asyncio.to_thread(mark_omr_ready, os.path.dirname(out_path))
    except Exception:                                  # noqa: BLE001 未装 epiano/失败不阻断
        pass

    if used_tempo is not None:
        tempo_line = f"谱面速度 {used_tempo} BPM（{tempo_src}）"
    else:
        tempo_line = "谱面未标注速度，谱面文件不带 tempo 字段（不做无依据猜测）"

    message = (
        f"识谱完成：{len(images)} 页 → 共 {note_count} 个音符（{hands}），"
        f"总长 {total_beat:.0f} 拍 ≈ {total_beat / 4:.0f} 小节（4/4），{tempo_line}。\n"
        f"谱面文件已保存：{out_path}\n"
        f"请立即调用 play_score 弹奏：path=\"{out_path}\""
        + ("" if used_tempo is not None else
           "。⚠️ 谱面无速度标注：直接调用 play_score(path=\"" + out_path + "\") 即可"
           "（禁止自行构造 score 参数，tempo 可省略用默认速度）；若想按原曲速度弹，"
           "先 bing_search 搜〈歌曲名〉的真实 BPM，把结果作为 tempo 参数传入，"
           "禁止凭记忆猜测 BPM")
        + "。"
    )
    return {
        "status": "ok",
        "message": message,
        "score_path": out_path,
        "note_count": note_count,
        "pages": len(images),
        "hands": hands,
        "total_beat": round(total_beat, 2),
        "tempo": used_tempo,
    }
