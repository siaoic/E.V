# -*- coding: utf-8 -*-
"""谱面速度标记（♩=N）结构检测 —— 移植自 audiveris_py/audiveris/symbol/tempo.py。

原理（无 OCR 引擎、纯结构 + 字体模板）：
1. 水平投影自估第 1 个五线谱的几何（顶线 y、线距 inter、x 范围）
2. 在顶线上方 1.2~7 个线距的横带内找「等号」候选：两条短横杠上下叠放
3. 等号左侧须有音符符干（细高竖线）与实心符头（宽实块）→ 排除普通文字
4. 等号右侧取连通域数字框，与 Windows 常见字体渲染的 0-9 模板做 NCC 分类
5. 数值合法性（20~300）+ 孤立性（标记周围近乎无其他墨迹）双重校验

供 read_sheet_music 插件在 OMR 之后恢复 homr 丢弃的速度信息；识别不到
返回 None，由调用方走 VLM 视觉识别兜底。任何异常都吞掉并返回 None，
绝不影响识谱主流程。
"""
import os

import cv2
import numpy as np

_FONT_FILES = (
    r'C:\Windows\Fonts\times.ttf',
    r'C:\Windows\Fonts\arial.ttf',
    r'C:\Windows\Fonts\georgia.ttf',
    r'C:\Windows\Fonts\calibri.ttf',
    r'C:\Windows\Fonts\cambria.ttc',
    r'C:\Windows\Fonts\tahoma.ttf',
)

_DIGITS = '0123456789'
_TEMPLATE_H = 56
_TEMPLATE_W = 36

_templates_cache: list | None = None


def _render_templates() -> list:
    """Render 0-9 glyphs in every available font; returns [(digit, img)].."""
    global _templates_cache
    if _templates_cache is not None:
        return _templates_cache
    out: list = []
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:                      # pragma: no cover
        _templates_cache = out
        return out
    for fp in _FONT_FILES:
        if not os.path.exists(fp):
            continue
        try:
            font = ImageFont.truetype(fp, 64)
        except OSError:                      # pragma: no cover
            continue
        for ch in _DIGITS:
            img = Image.new('L', (96, 96), 0)
            ImageDraw.Draw(img).text((12, 8), ch, fill=255, font=font)
            arr = np.asarray(img) > 128
            ys, xs = np.nonzero(arr)
            if ys.size == 0:
                continue
            glyph = arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
            glyph = cv2.resize(glyph.astype(np.float32),
                               (_TEMPLATE_W, _TEMPLATE_H))
            out.append((ch, glyph))
    _templates_cache = out
    return out


def _first_staff_geometry(gray: np.ndarray):
    """水平投影自估第 1 个五线谱 -> (top_y, inter, x_start, x_stop) | None。

    五线谱行几乎横贯页宽（行墨量远超文字行）。实测首页顶部常有标题分隔线
    等杂散「线状」行（如 0000038-w-b-1 顶部 y=520/529/542 三条），故不用
    「前 5 条分组」而用滑动窗口：找第一组连续 5 条等距线作为第 1 个谱表。
    """
    h, w = gray.shape
    ink = (gray < 128).astype(np.uint8)
    row_ink = ink.sum(axis=1)
    line_rows = np.nonzero(row_ink > 0.35 * w)[0]
    if line_rows.size < 5:
        return None
    groups: list[tuple[int, int]] = []       # 连续行聚成一条线
    start = prev = int(line_rows[0])
    for y in line_rows[1:]:
        y = int(y)
        if y - prev <= 2:
            prev = y
        else:
            groups.append((start, prev))
            start = prev = y
    groups.append((start, prev))
    if len(groups) < 5:
        return None
    centers = [(a + b) / 2.0 for a, b in groups]
    # 滑动窗口：第一组连续 5 条等距线 = 第 1 个谱表
    for i in range(len(centers) - 4):
        gaps = np.diff(centers[i:i + 5])
        inter = float(np.median(gaps))
        if not (3.0 <= inter <= 80.0):
            continue
        if float(np.std(gaps)) > max(1.0, 0.2 * inter):
            continue
        ys: list[int] = []
        for a, b in groups[i:i + 5]:
            ys.extend(range(a, b + 1))
        col_cov = ink[ys, :].sum(axis=0)
        cols = np.nonzero(col_cov >= 3)[0]
        if cols.size == 0:
            continue
        return int(round(centers[i])), inter, int(cols[0]), int(cols[-1])
    return None


def _find_eq_pairs(band: np.ndarray, inter: float) -> list:
    """Locate equals-sign candidates: two short stacked horizontal bars."""
    wmin, wmax = 0.4 * inter, 1.8 * inter
    tmin, tmax = 2, max(3, int(round(0.3 * inter)))
    bars = []                            # [x0, x1, y0, y1]
    for y in range(band.shape[0]):
        row = band[y]
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        starts = np.nonzero(d == 1)[0]
        stops = np.nonzero(d == -1)[0]
        for x0, x1 in zip(starts, stops):
            w = x1 - x0
            if wmin <= w <= wmax:
                bars.append([x0, x1, y, y])
    # merge vertically adjacent runs into bars
    merged: list[list] = []
    for x0, x1, y, _ in bars:
        hit = None
        for m in merged:
            if (abs(m[0] - x0) <= 0.3 * inter
                    and abs(m[1] - x1) <= 0.3 * inter
                    and y - m[3] <= 2):
                hit = m
                break
        if hit is None:
            merged.append([x0, x1, y, y])
        else:
            hit[3] = y
            hit[0] = min(hit[0], x0)
            hit[1] = max(hit[1], x1)
    bars = [m for m in merged if tmin <= m[3] - m[2] + 1 <= tmax]
    pairs = []
    for i, a in enumerate(bars):
        for b in bars[i + 1:]:
            vgap = b[2] - a[3]
            if not (1 <= vgap <= 0.5 * inter):
                continue
            ox = min(a[1], b[1]) - max(a[0], b[0])
            if ox < 0.5 * min(a[1] - a[0], b[1] - b[0]):
                continue
            pairs.append((a, b))
    return pairs


def _digit_boxes(band: np.ndarray, x0: float, cy: float, inter: float) -> list:
    """Connected components right of the equals sign that look like digits."""
    h, w = band.shape
    sx0 = max(0, int(x0 + 0.1 * inter))
    sx1 = min(w, int(x0 + 6.0 * inter))
    sy0 = max(0, int(cy - 1.2 * inter))
    sy1 = min(h, int(cy + 1.2 * inter))
    if sx1 <= sx0 or sy1 <= sy0:
        return []
    strip = band[sy0:sy1, sx0:sx1].astype(np.uint8)
    n, _lab, stats, _cent = cv2.connectedComponentsWithStats(strip, 8)
    boxes = []
    for i in range(1, n):
        bx, by, bw, bh, area = stats[i]
        if area < 0.5 * inter:
            continue
        if not (0.15 * inter <= bw <= 1.4 * inter):
            continue
        if not (0.5 * inter <= bh <= 1.9 * inter):
            continue
        boxes.append((sx0 + bx, sy0 + by, bw, bh))
    boxes.sort(key=lambda b: b[0])
    keep = []
    for b in boxes:
        if keep and b[0] < keep[-1][0] + keep[-1][2] * 0.5:
            continue
        keep.append(b)
    return keep[:4]


def _tight(img: np.ndarray):
    ys, xs = np.nonzero(img)
    if ys.size == 0:
        return None
    return img[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def _classify(box_img: np.ndarray, templates: list):
    """Classify one tight digit crop against rendered font templates."""
    crop = _tight(box_img)
    if crop is None:
        return None, 0.0
    crop = cv2.resize(crop.astype(np.float32),
                      (_TEMPLATE_W, _TEMPLATE_H))
    best_ch, best = None, -1.0
    for ch, tmpl in templates:
        s = float(np.sum(crop * tmpl)) / max(
            1e-6, np.sqrt(np.sum(crop * crop) * np.sum(tmpl * tmpl)))
        if s > best:
            best_ch, best = ch, s
    return best_ch, best


def _stem_present(band: np.ndarray, bx0: float, cy: float, inter: float) -> bool:
    """Require a thin tall vertical stem (the note glyph) left of "="."""
    x0 = max(0, bx0 - int(1.3 * inter))
    x1 = max(0, bx0 - int(0.05 * inter))
    y0 = max(0, int(cy - 2.8 * inter))
    y1 = min(band.shape[0], int(cy + 0.6 * inter))
    if x1 <= x0 or y1 <= y0:
        return False
    win = band[y0:y1, x0:x1]
    best = 0
    for c in range(win.shape[1]):
        col = win[:, c]
        d = np.diff(np.concatenate(([0], col.astype(np.int8), [0])))
        starts = np.nonzero(d == 1)[0]
        stops = np.nonzero(d == -1)[0]
        for s0, s1 in zip(starts, stops):
            if s1 - s0 > best:
                best = s1 - s0
    return best >= 1.2 * inter


def _head_present(gray_band: np.ndarray, bx0: float, cy: float,
                  inter: float) -> bool:
    """Require a solid wide blob (the note head) under the stem.

    A quarter-note metronome glyph carries a filled head about one
    interline wide below its stem; stray text strokes (letters, digits)
    have no such wide solid block.  Measured on the GRAYSCALE image so the
    anti-aliased oval width is stable regardless of binarization.
    """
    x0 = max(0, bx0 - int(1.9 * inter))
    x1 = max(0, bx0 - 1)
    y0 = max(0, int(cy + 0.15 * inter))
    y1 = min(gray_band.shape[0], int(cy + 0.95 * inter))
    if x1 <= x0 or y1 <= y0:
        return False
    strip = gray_band[y0:y1, x0:x1] < 140
    colfill = strip.sum(axis=0) / (y1 - y0)
    run = best = 0
    for v in colfill:
        if v >= 0.5:
            run += 1
            if run > best:
                best = run
        else:
            run = 0
    return best >= 0.55 * inter


def _isolated(band: np.ndarray, bx0: float, bx1: float, cy: float,
              boxes: list, inter: float) -> bool:
    """The mark must stand alone: nearly no ink around it."""
    last_right = max(b[0] + b[2] for b in boxes)
    mx0 = max(0, bx0 - int(4.5 * inter))
    mx1 = min(band.shape[1], last_right + int(2.0 * inter))
    my0 = max(0, int(cy - 2.4 * inter))
    my1 = min(band.shape[0], int(cy + 1.3 * inter))
    if mx1 <= mx0 or my1 <= my0:
        return False
    allowed = np.zeros((my1 - my0, mx1 - mx0), dtype=bool)

    def _mark(x0, x1, y0, y1):
        ax0, ax1 = max(mx0, x0), min(mx1, x1)
        ay0, ay1 = max(my0, y0), min(my1, y1)
        if ax1 > ax0 and ay1 > ay0:
            allowed[ay0 - my0:ay1 - my0, ax0 - mx0:ax1 - mx0] = True

    # the note glyph (head + stem) and the equals bars
    _mark(bx0 - int(1.2 * inter), bx1 + 2,
          int(cy - 2.8 * inter), int(cy + 1.3 * inter))
    # the digit boxes
    for (bx, by, bw, bh) in boxes:
        _mark(bx - 2, bx + bw + 2, by - 2, by + bh + 2)
    outside = band[my0:my1, mx0:mx1] & ~allowed
    return int(outside.sum()) <= 0.5 * inter


def detect_tempo_mark(image_path: str) -> int | None:
    """识别乐谱图片第 1 个五线谱上方的节拍器标记，返回 BPM（int）或 None。"""
    try:
        templates = _render_templates()
        if not templates:
            return None
        # cv2.imread 读不了 Windows 中文路径，用 imdecode 兜底
        img = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8),
                           cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        geo = _first_staff_geometry(img)
        if geo is None:
            return None
        top, inter, x_start, x_stop = geo
        binary = (img < 128).astype(np.uint8)
        y0 = max(0, int(top - 7.0 * inter))
        y1 = max(y0 + 1, int(top - 1.2 * inter))
        x0 = max(0, int(x_start))
        x1 = min(binary.shape[1], int(x_start + 0.5 * (x_stop - x_start)))
        band = binary[y0:y1, x0:x1]
        gray_band = img[y0:y1, x0:x1]
        for (a, b) in _find_eq_pairs(band, inter):
            bx0, bx1 = min(a[0], b[0]), max(a[1], b[1])
            cy = (a[2] + b[3]) / 2.0
            # ink to the left: the note glyph of the metronome mark
            lx0 = max(0, bx0 - int(3.5 * inter))
            lx1 = max(0, bx0 - int(0.3 * inter))
            ly0 = max(0, int(cy - 1.6 * inter))
            ly1 = min(band.shape[0], int(cy + 1.6 * inter))
            if lx1 <= lx0 or ly1 <= ly0:
                continue
            left_ink = int(band[ly0:ly1, lx0:lx1].sum())
            if left_ink < 0.8 * inter:
                continue
            boxes = _digit_boxes(band, bx1, cy, inter)
            if not (2 <= len(boxes) <= 3):
                continue
            if not _stem_present(band, bx0, cy, inter):
                continue
            if not _head_present(gray_band, bx0, cy, inter):
                continue
            if not _isolated(band, bx0, bx1, cy, boxes, inter):
                continue
            digits = []
            ok = True
            for (bx, by, bw, bh) in boxes:
                ch, score = _classify(band[by:by + bh, bx:bx + bw], templates)
                if ch is None or score < 0.5:
                    ok = False
                    break
                digits.append(ch)
            if not ok:
                continue
            value = int(''.join(digits))
            if 20 <= value <= 300:
                return value
        return None
    except Exception:                        # noqa: BLE001 兜底：绝不影响主流程
        return None
