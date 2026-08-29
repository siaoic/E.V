# -*- coding: utf-8 -*-
"""常驻 OMR 服务（read_sheet_music 插件专用）：模型只加载一次，消灭每页冷启动。
用 homr 工具 venv 的 python 运行（需 import homr 全家桶）：
    C:\\Users\\siao\\AppData\\Roaming\\uv\\tools\\homr\\Scripts\\python.exe omr_daemon.py
协议（stdin 行式，stdout 行式 JSON）：
    <img_path>\t<out_xml_path>   -> {"ok":true,"xml":...,"timing":{...}}
    quit                         -> 退出
优化：模型常驻 / 跳过 title OCR / Segnet 会话复用 / 无 debug 输出。
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

T0 = time.perf_counter()
_last = T0


def mark(name: str, timings: dict | None = None) -> None:
    global _last
    now = time.perf_counter()
    msg = f"[daemon] {name:16s} 本阶段 {now - _last:6.2f}s"
    print(msg, file=sys.stderr, flush=True)
    if timings is not None:
        timings[name] = round(now - _last, 3)
    _last = now


# ---- 一次性导入 ----
import cv2
import numpy as np

from homr import color_adjust
from homr.autocrop import autocrop
from homr.bar_line_detection import detect_bar_lines, prepare_bar_line_image
from homr.bounding_boxes import create_bounding_ellipses, create_rotated_bounding_boxes
from homr.brace_dot_detection import (
    find_braces_brackets_and_grand_staff_lines,
    prepare_brace_dot_image,
)
from homr.debug import Debug
from homr.model import InputPredictions
from homr.music_xml_generator import XmlGeneratorArguments, generate_xml
from homr.noise_filtering import filter_predictions
from homr.note_detection import add_notes_to_staffs, combine_noteheads_with_stems
from homr.resize import resize_image
from homr.segmentation.config import segmentation_version
from homr.segmentation.inference_segnet import (
    ExtractResult,
    Segnet,
    merge_patches,
)
from homr.staff_detection import break_wide_fragments, detect_staff, make_lines_stronger
from homr.staff_parsing import parse_staffs
from homr.transformer.configs import Config

mark("imports")

config = Config()
config.use_gpu_inference = True
config.use_coreml_encoder = False

# Segnet 会话常驻（官方 extract() 每页新建 session）
segnet = Segnet(True)
mark("segnet session")

import hashlib
import lzma
import os
import tempfile
from pathlib import Path


def extract_cached(preprocessed, img_path_str, use_cache=True, batch_size=32):
    """复刻 extract()，但复用常驻 Segnet，batch 加大到 32，支持 .npy 缓存。"""
    img_path = Path(img_path_str)
    f_name = os.path.splitext(img_path.name)[0]
    npy_path = img_path.parent / f"{f_name}.npy"
    loaded = False
    if use_cache and npy_path.exists():
        file_hash = hashlib.sha256(preprocessed.tobytes()).hexdigest()
        with lzma.open(npy_path, "rb") as f:
            staff = np.load(f)
            notehead = np.load(f)
            symbols = np.load(f)
            stems_rests = np.load(f)
            clefs_keys = np.load(f)
            cached_hash = f.readline().decode().strip()
            cached_ver = f.readline().decode().strip()
        if cached_hash == file_hash and cached_ver == segmentation_version:
            loaded = True
    if not loaded:
        image_org = cv2.cvtColor(preprocessed, cv2.COLOR_GRAY2BGR)
        image = np.transpose(image_org, (2, 0, 1)).astype(np.float32)
        c, h, w = image.shape
        win = 320
        step = 320
        data = []
        batch = []

        def flush():
            out = segnet.run(np.stack(batch, axis=0))
            for o in out:
                data.append(np.argmax(o, axis=0))
            batch.clear()

        for y_loop in range(0, max(h, win), step):
            y = min(y_loop, h - win)
            for x_loop in range(0, max(w, win), step):
                x = min(x_loop, w - win)
                patch = np.full((c, win, win), 255, dtype=np.float32)
                y0, x0 = max(y, 0), max(x, 0)
                y1, x1 = min(y + win, h), min(x + win, w)
                patch[:, : y1 - y0, : x1 - x0] = image[:, y0:y1, x0:x1]
                batch.append(patch)
                if len(batch) == batch_size:
                    flush()
        if batch:
            flush()
        merged = merge_patches(data, (int(h), int(w)), win, step)
        staff = (merged == 4).astype(np.uint8)
        symbols = (merged == 5).astype(np.uint8)
        stems_rests = (merged == 1).astype(np.uint8)
        notehead = (merged == 2).astype(np.uint8)
        clefs_keys = (merged == 3).astype(np.uint8)
        if use_cache:
            file_hash = hashlib.sha256(preprocessed.tobytes()).hexdigest()
            with lzma.open(npy_path, "wb") as f:
                np.save(f, staff)
                np.save(f, notehead)
                np.save(f, symbols)
                np.save(f, stems_rests)
                np.save(f, clefs_keys)
                f.write((file_hash + "\n").encode())
                f.write((segmentation_version + "\n").encode())
    return ExtractResult(img_path, None, staff, symbols, stems_rests, notehead, clefs_keys)


def process_image(img: str, out_xml: str, pool: ThreadPoolExecutor, use_cache: bool = True) -> dict:
    timings: dict = {}
    _last_t = time.perf_counter()

    def stamp(name):
        nonlocal _last_t
        now = time.perf_counter()
        timings[name] = round(now - _last_t, 3)
        _last_t = now

    t_all = time.perf_counter()
    # cv2.imread 读不了 Windows 中文路径，用 imdecode 兜底
    image = cv2.imdecode(np.fromfile(img, dtype=np.uint8), cv2.IMREAD_COLOR)
    image = autocrop(image)
    image = resize_image(image)
    preprocessed = color_adjust.apply_clahe(image)
    stamp("preprocess")

    t0 = time.perf_counter()
    result = extract_cached(preprocessed, img, use_cache=use_cache)
    timings["segnet"] = round(time.perf_counter() - t0, 3)
    _last_t = time.perf_counter()  # segnet 用独立 t0 计时，补齐 stamp 基准

    original_image = cv2.resize(image, (result.staff.shape[1], result.staff.shape[0]))
    preprocessed_image = cv2.resize(preprocessed, (result.staff.shape[1], result.staff.shape[0]))
    predictions = InputPredictions(
        original=original_image,
        preprocessed=preprocessed_image,
        notehead=result.notehead.astype(np.uint8),
        symbols=result.symbols.astype(np.uint8),
        staff=result.staff.astype(np.uint8),
        clefs_keys=result.clefs_keys.astype(np.uint8),
        stems_rest=result.stems_rests.astype(np.uint8),
    )
    debug = Debug(predictions.original, img, False)
    predictions = filter_predictions(predictions, debug)
    predictions.staff = make_lines_stronger(predictions.staff, (1, 2))
    stamp("filter")

    # 几何检测：串行（线程池实测有 GIL 争用负优化）
    noteheads = create_bounding_ellipses(predictions.notehead, (4, 4))
    staff_fragments = create_rotated_bounding_boxes(predictions.staff, True, (5, 1), (10000, 100))
    clefs_keys = create_rotated_bounding_boxes(predictions.clefs_keys, False, (20, 40), (1000, 1000))
    stems_rest = create_rotated_bounding_boxes(predictions.stems_rest, False, None, None)
    bar_img = prepare_bar_line_image(predictions.stems_rest)
    bar_lines = create_rotated_bounding_boxes(bar_img, True, (1, 5), None)
    stamp("bboxes")

    staff_fragments = break_wide_fragments(staff_fragments)
    noteheads_with_stems = combine_noteheads_with_stems(noteheads, stems_rest)
    avg_h = float(np.median([n.notehead.size[1] for n in noteheads_with_stems]))
    all_noteheads = [n.notehead for n in noteheads_with_stems]
    all_stems = [n.stem for n in noteheads_with_stems if n.stem is not None]
    bar_lines_or_rests = [
        line
        for line in bar_lines
        if not line.is_overlapping_with_any(all_noteheads)
        and not line.is_overlapping_with_any(all_stems)
    ]
    bar_line_boxes = detect_bar_lines(bar_lines_or_rests, avg_h)
    staffs = detect_staff(debug, predictions.staff, staff_fragments, clefs_keys, bar_line_boxes)
    brace_dot_img = prepare_brace_dot_image(predictions.symbols, predictions.staff)
    brace_dot = create_rotated_bounding_boxes(brace_dot_img, True, None, (100, -1))
    notes = add_notes_to_staffs(staffs, noteheads_with_stems, predictions.symbols, predictions.notehead)
    multi_staffs = find_braces_brackets_and_grand_staff_lines(debug, staffs, brace_dot)
    stamp("geometry")

    result_staffs = parse_staffs(debug, multi_staffs, predictions.preprocessed, selected_staff=-1, config=config)
    stamp("transformer")

    xml = generate_xml(XmlGeneratorArguments(False, None, None), result_staffs, "")
    xml.write(out_xml)
    stamp("xml")

    timings["total"] = round(time.perf_counter() - t_all, 3)
    return timings


def warmup(img: str, pool: ThreadPoolExecutor) -> None:
    """启动预热：跑一遍真实流程（不走缓存），触发 CUDA/cudnn 算法搜索与 session 编译。"""
    out = os.path.join(tempfile.gettempdir(), "_omr_warmup.musicxml")
    process_image(img, out, pool, use_cache=False)
    print("[daemon] warmup done", file=sys.stderr, flush=True)


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    pool = ThreadPoolExecutor(max_workers=4)
    warmup_img = sys.argv[1] if len(sys.argv) > 1 else None
    if warmup_img:
        warmup(warmup_img, pool)
    print("READY", flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "quit":
            break
        parts = line.split("\t")
        img = parts[0]
        out_xml = parts[1] if len(parts) > 1 else img.rsplit(".", 1)[0] + ".musicxml"
        try:
            timings = process_image(img, out_xml, pool)
            print(json.dumps({"ok": True, "xml": out_xml, "timing": timings}), flush=True)
        except Exception as ex:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": repr(ex)}), flush=True)


if __name__ == "__main__":
    main()
