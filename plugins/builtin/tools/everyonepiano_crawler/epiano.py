"""everyonepiano_crawler 工具核心：everyonepiano.cn 钢琴谱搜索/榜单 + 五线谱下载。

数据来源：everyonepiano.cn 列表页 canshu=clicks（浏览次数）排序接口；
五线谱高清图规律：详情页预览 {谱号}-w-s-{页}.jpg ↔ 大图 {谱号}-w-b-{页}.png，
路径 /pianomusic/{三位目录}/{谱号}/{谱号}-w-b-{页}.png（谱号在路径中出现两次）。

输出：<项目根>/data/epiano/sheets/{歌名}/page-NN.png 与 data/epiano/scores/*.eop。
外部内容（标题/作者等站点文本）一律先过 sanitize_external 再回灌，防 prompt 注入。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import glob
import json
import os
import re
from urllib.parse import quote, urlencode

import httpx

from ev.utils import config
from ev.utils.safe_text import sanitize_external

BASE = "https://everyonepiano.cn"
LIST_URL = BASE + "/Music.html"
EOP_URL_TPL = BASE + "/Music/down/{mid}/{no}/{title}"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 项目根 = plugins/builtin/tools/<name>/ 上溯 4 级
_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
DATA_ROOT = os.path.join(_PROJECT_ROOT, "data", "epiano")
SHEETS_ROOT = os.path.join(DATA_ROOT, "sheets")
SCORES_ROOT = os.path.join(DATA_ROOT, "scores")
FOLDER_MAP_PATH = os.path.join(DATA_ROOT, ".folders.json")
LIBRARY_PATH = os.path.join(DATA_ROOT, "library.json")   # 琴谱库索引（记住琴谱）

# 列表条目块（到下一个条目或分页行为止）
RE_ITEM_BLOCK = re.compile(
    r'<div class="MusicIndexBox".*?(?=<div class="MusicIndexBox"|<div class="row EOPMusicIndexPage)',
    re.S)
RE_TOTAL = re.compile(
    r'共(?:搜到)?\s*<span class="EOPRed">(\d+)</span>\s*首钢琴谱'
    r'(?:.*?每页\s*<span class="EOPRed">(\d+)</span>\s*首)?', re.S)
RE_TITLE = re.compile(r'<a\s+href=["\']([^"\']+)["\'][^>]*class=["\']Title["\'][^>]*>([^<]+)</a>')
RE_NO = re.compile(r'MIMusicNO[^>]*>\s*(\d{5,8})')
RE_AUTHOR = re.compile(r'Music\.html\?author=[^"\']*["\'][^>]*>([^<]+)</a>')
RE_VIEWS = re.compile(r'MIMusicInfo2Num">\s*([\d,]+)')
RE_MID = re.compile(r'/Music-(\d+)\.html')

# 五线谱：详情页预览图与 Stave 大图页内的高清 PNG（谱号在路径中出现两次！）
RE_STAVE_PREV = re.compile(
    r'src=["\'](?:https?://[^"\']*)?/pianomusic/([^"\'/]+)/(\d+)/(\d+)-w-s-(\d+)\.(?:jpg|png)["\']',
    re.I)
RE_STAVE_BIG = re.compile(
    r'src=["\']((?:https?://[^"\']*)?/pianomusic/[^"\']+/(\d+)-w-b-(\d+)\.png)["\']', re.I)
RE_STAVE_H2 = re.compile(r'<h2>([^<]+)五线谱预览')
RE_H2_DOWNLOAD = re.compile(r'曲谱下载</font>-(.*?)</h2>', re.S)

_PAGE_DELAY = 1.0        # 列表页/详情页间隔（秒）
_IMG_DELAY = 0.8         # 图片下载间隔（秒）


# ---------------------------------------------------------------------------
# HTTP 基础（带重试 + TLS 容错）
# ---------------------------------------------------------------------------

def _make_client(verify: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=_HEADERS, timeout=httpx.Timeout(30.0),
                             verify=verify, follow_redirects=True)


async def _get_text(client: httpx.AsyncClient, url: str, retries: int = 3) -> str:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = await client.get(url)
            if resp.status_code == 403:
                raise RuntimeError("403 被站点 360 防火墙拦截，请稍后再试或加大请求间隔")
            resp.raise_for_status()
            return resp.content.decode("utf-8", "replace")
        except RuntimeError:
            raise
        except Exception as e:                     # noqa: BLE001 网络类异常统一重试
            last_err = e
            if attempt < retries:
                await asyncio.sleep(1.5 * attempt)
    raise RuntimeError(f"请求最终失败：{url} ({last_err})")


async def _get_bytes(client: httpx.AsyncClient, url: str, retries: int = 2) -> bytes:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
        except Exception as e:                     # noqa: BLE001
            last_err = e
            if attempt < retries:
                await asyncio.sleep(1.5 * attempt)
    raise RuntimeError(f"下载失败：{url} ({last_err})")


def _is_image(data: bytes) -> bool:
    return data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8\xff")


# ---------------------------------------------------------------------------
# 列表解析
# ---------------------------------------------------------------------------

def _parse_items(html: str) -> tuple[list[dict], int]:
    """解析一页列表 → (真实排名条目, 推广位数量)。推广位（top 徽章、无数字）不计入排名。"""
    items, promoted = [], 0
    for block in RE_ITEM_BLOCK.findall(html):
        if "MIMusicNO" not in block:
            continue
        m_title, m_no = RE_TITLE.search(block), RE_NO.search(block)
        if not (m_title and m_no):
            continue
        m_views = RE_VIEWS.search(block)
        if m_views is None and "top.png" in block:
            promoted += 1                          # 每页顶部的轮换推广位，剔除
            continue
        href, title = m_title.group(1).strip(), m_title.group(2).strip()
        m_mid = RE_MID.search(href)
        m_author = RE_AUTHOR.search(block)
        items.append({
            "music_id": int(m_mid.group(1)) if m_mid else None,
            "title": sanitize_external(title),
            "author": sanitize_external(m_author.group(1).strip()) if m_author else "",
            "views": int(m_views.group(1).replace(",", "")) if m_views else None,
            "detail_url": urllib_join(href),
        })
    return items, promoted


def urllib_join(href: str) -> str:
    from urllib.parse import urljoin
    return urljoin(BASE, href)


# ---------------------------------------------------------------------------
# 对外工具 1：搜索 / 榜单
# ---------------------------------------------------------------------------

async def _search(client: httpx.AsyncClient, keyword: str, pages: int, limit: int) -> dict:
    items: list[dict] = []
    total = None
    for p in range(1, pages + 1):
        qs = urlencode({"come": "web", "canshu": "clicks", "paixu": "desc",
                        "word": keyword, "author": "", "jianpu": "",
                        "username": "", "p": p})
        html = await _get_text(client, f"{LIST_URL}?{qs}")
        m = RE_TOTAL.search(html)
        if m and total is None:
            total = int(m.group(1))
        rows, _promoted = _parse_items(html)
        if not rows:
            break
        items.extend(rows)
        if p < pages:
            await asyncio.sleep(_PAGE_DELAY)
    return {"total": total, "items": items[:limit]}


async def epiano_search(keyword: str = "", limit: int = 10, pages: int = 1) -> dict:
    """搜索/榜单入口。keyword 空 = 全站浏览次数 Top 榜单；非空 = 站内搜索（按浏览次数降序）。"""
    keyword = (keyword or "").strip()
    limit = max(1, min(int(limit or 10), 30))
    pages = max(1, min(int(pages or 1), 3))
    try:
        try:
            async with _make_client(True) as client:
                data = await _search(client, keyword, pages, limit)
        except httpx.ConnectError as e:
            if "ssl" not in str(e).lower() and "certificate" not in str(e).lower():
                raise
            async with _make_client(False) as client:   # 站点证书链偶发不完整 → 降级
                data = await _search(client, keyword, pages, limit)
    except Exception as e:                             # noqa: BLE001 统一转可读错误
        return {"readable": f"错误：EveryonePiano 搜索失败（{e}）。", "error": str(e),
                "items": []}

    items = data["items"]
    total = data.get("total")
    if not items:
        return {"readable": f"EveryonePiano 上没有找到「{keyword}」相关的曲谱。",
                "total": 0, "items": []}

    for i, it in enumerate(items, 1):
        it["rank"] = i
        it["views"] = it["views"] if it["views"] is not None else 0

    # 琴谱库标注：本地已下载过的曲谱打上 local 标记（LLM 可跳过下载直接弹）
    library = _load_library()
    cached_readable = []
    for it in items:
        entry = library.get(str(it.get("music_id")) or "")
        if not entry:
            continue
        omr = _live_omr_ready(entry) or bool(entry.get("omr_ready", False))
        folder = entry.get("folder", "")
        local = {"folder": folder,
                 "pages": entry.get("pages", 0),
                 "omr_ready": omr}
        if omr:
            local["play_hint"] = (
                f"path 传具体文件逐页弹：{folder}/page-01.score.json、"
                "page-02.score.json…（禁止 * 通配符，每页一个文件依次 play_score）")
        it["local"] = local
        cached_readable.append(
            f"《{it['title']}》我已经学过（{entry.get('pages', 0)}页谱在本地"
            + ("，已识别可直接弹）" if omr else "）"))

    top = "、".join(
        f"《{it['title']}》{it['author']}（{it['views']:,}次浏览）" for it in items[:3])
    scope = f"「{keyword}」" if keyword else "全站"
    head = (f"在 EveryonePiano 搜到 {total} 首与{scope}相关的曲谱" if total
            else f"在 EveryonePiano 找到 {len(items)} 首与{scope}相关的曲谱")
    readable = f"{head}，浏览次数最高的是：{top}。"
    if cached_readable:
        readable += "注意：" + "；".join(cached_readable) + "，不用重新下载，直接弹就行。"
    else:
        readable += "要下载哪首的五线谱，用它的 music_id 调 epiano_download。"
    return {"readable": readable, "total": total if total is not None else len(items),
            "returned": len(items), "items": items}


# ---------------------------------------------------------------------------
# 对外工具 2：下载五线谱（可选 EOP）
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("._")[:80]


def _load_folder_map() -> dict:
    try:
        with open(FOLDER_MAP_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                  # noqa: BLE001 缺文件/损坏 → 重建
        return {}


def _save_folder_map(mapping: dict) -> None:
    os.makedirs(DATA_ROOT, exist_ok=True)
    with open(FOLDER_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)


def _folder_for(title: str, music_id: int) -> str:
    """按歌名建文件夹；同名不同曲（历史映射里已是别的 music_id）追加谱号防覆盖。"""
    mapping = _load_folder_map()
    name = _sanitize_filename(title) or f"music_{music_id}"
    if name in mapping and str(mapping[name]) != str(music_id):
        name = f"{name}_{music_id}"
    mapping[name] = music_id
    _save_folder_map(mapping)
    return name


# ---------------------------------------------------------------------------
# 琴谱库（记住琴谱）：library.json 索引 + 长期记忆写入
# ---------------------------------------------------------------------------

def _load_library() -> dict:
    """{music_id: entry} 形式的琴谱库索引；损坏/缺失时重建。"""
    try:
        with open(LIBRARY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:                                  # noqa: BLE001
        return {}


def _save_library(lib: dict) -> None:
    os.makedirs(DATA_ROOT, exist_ok=True)
    tmp = LIBRARY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LIBRARY_PATH)


def _library_put(entry: dict) -> None:
    lib = _load_library()
    lib[str(entry["music_id"])] = entry
    _save_library(lib)


async def _remember_score(title: str, music_id: int, rel_folder: str,
                          pages: int, omr_ready: bool) -> None:
    """把「已学过这首琴谱」写入长期记忆（MEMORY_ENABLED 时；失败静默不阻断）。"""
    try:
        if not getattr(config.cfg, "MEMORY_ENABLED", False):
            return
        from tools.memory import memory                # 与 remember_fact 同一写入通道
        state = "谱面已识别可直接弹" if omr_ready else "五线谱已就绪待识别"
        fact = (f"琴谱库：《{title}》(music_id={music_id}) 的钢琴谱在 {rel_folder}/"
                f"（{pages} 页），{state}。用户点这首歌时不用再搜索下载。")
        await asyncio.to_thread(memory.remember_explicit, fact[:40], fact)
    except Exception:                                  # noqa: BLE001 记忆失败不影响主流程
        pass


def _live_omr_ready(entry: dict) -> bool:
    """实时探测该曲谱文件夹是否已有 OMR 识别产物（*.score.json）。

    以文件系统为准（自愈：识谱工具回写失败/库文件损坏时也不会误报「待识别」）。
    """
    try:
        folder = os.path.join(_PROJECT_ROOT, entry.get("folder", ""))
        return bool(glob.glob(os.path.join(folder, "*.score.json")))
    except Exception:                                  # noqa: BLE001
        return False


def mark_omr_ready(folder_dir: str) -> bool:
    """识谱完成后的状态同步：按目录匹配琴谱库条目，置 omr_ready=True 并刷新记忆。

    供 read_sheet_music 在 OMR 成功后调用（跨插件软依赖，失败静默返回 False）。
    """
    try:
        target = os.path.normpath(os.path.abspath(folder_dir))
        lib = _load_library()
        matched = []
        for entry in lib.values():
            entry_dir = os.path.normpath(
                os.path.join(_PROJECT_ROOT, entry.get("folder", "")))
            if entry_dir == target:
                entry["omr_ready"] = True
                matched.append(entry)
        if not matched:
            return False
        _save_library(lib)
        for entry in matched:                          # 同题键重写 → 冲突自动替换
            fact = (f"琴谱库：《{entry['title']}》(music_id={entry['music_id']}) 的"
                    f"钢琴谱在 {entry['folder']}/（{entry.get('pages', 0)} 页），"
                    "谱面已识别可直接弹。用户点这首歌时直接演奏不用再识别。")
            try:
                if getattr(config.cfg, "MEMORY_ENABLED", False):
                    from tools.memory import memory
                    memory.remember_explicit(fact[:40], fact)
            except Exception:                          # noqa: BLE001
                pass
        return True
    except Exception:                                  # noqa: BLE001
        return False


def _detail_title(html: str, music_id: int) -> str:
    m = RE_STAVE_H2.search(html) or RE_H2_DOWNLOAD.search(html)
    if m:
        return sanitize_external(m.group(1).strip())
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    if m:
        raw = re.sub(r"\s*[-_|].*?(EveryonePiano|everyonepiano).*$", "",
                     m.group(1)).strip()
        if raw:
            return sanitize_external(raw)
    return f"music_{music_id}"


def _stave_urls(html: str, music_id: int) -> list[str]:
    """详情页 → 每页五线谱高清 PNG 直链（谱号在路径中出现两次）。"""
    prevs = RE_STAVE_PREV.findall(html)
    if not prevs:
        return []
    dirpart, no_dir, no_file = prevs[0][0], prevs[0][1], prevs[0][2]
    count = max(int(g[3]) for g in prevs)
    return [f"{BASE}/pianomusic/{dirpart}/{no_dir}/{no_file}-w-b-{n}.png"
            for n in range(1, count + 1)]


async def _download(music_id: int, title: str, with_eop: bool) -> dict:
    detail_url = f"{BASE}/Music-{int(music_id)}.html"

    async def _run(client: httpx.AsyncClient) -> dict:
        detail_html = await _get_text(client, detail_url)
        real_title = (title or "").strip() or _detail_title(detail_html, music_id)
        urls = _stave_urls(detail_html, music_id)
        if not urls:
            return {"readable": f"《{real_title}》在 EveryonePiano 上没有五线谱区块，"
                                "可尝试其他版本（用 epiano_search 再搜）。",
                    "title": real_title, "pages": 0, "files": []}

        folder = _folder_for(real_title, music_id)
        out_dir = os.path.join(SHEETS_ROOT, folder)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"music_id": music_id, "title": real_title,
                       "detail_url": detail_url,
                       "downloaded_at": _dt.datetime.now().isoformat(timespec="seconds")},
                      f, ensure_ascii=False, indent=2)

        # 幂等：本地已有足量分页文件时跳过重复下载（秒回）
        existing = sorted(glob.glob(os.path.join(out_dir, "page-*.png")))
        if len(existing) >= len(urls):
            files = existing[:len(urls)]
            skipped = True
        else:
            files: list[str] = []
            skipped = False
            for n, url in enumerate(urls, 1):
                data = b""
                try:
                    data = await _get_bytes(client, url)
                except Exception:                      # noqa: BLE001 直链失效 → 回退 Stave 大图页
                    try:
                        stave_html = await _get_text(
                            client, f"{BASE}/Stave-{music_id}-{n}.html")
                        m = RE_STAVE_BIG.search(stave_html)
                        if m:
                            data = await _get_bytes(client, m.group(1))
                    except Exception:                  # noqa: BLE001
                        pass
                if not _is_image(data):
                    continue
                path = os.path.join(out_dir, f"page-{n:02d}.png")
                with open(path, "wb") as f:
                    f.write(data)
                files.append(path)
                await asyncio.sleep(_IMG_DELAY)

        if not files:
            return {"readable": f"《{real_title}》的五线谱下载失败（图片直链均不可用）。",
                    "title": real_title, "pages": 0, "files": [],
                    "folder": out_dir}

        eop_path = ""
        if with_eop:
            m_no = re.search(r"/pianomusic/[^\"']+/(\d+)/(\d+)-w-s-", detail_html)
            if m_no:
                eop_url = EOP_URL_TPL.format(mid=music_id, no=m_no.group(2),
                                             title=quote(real_title))
                try:
                    data = await _get_bytes(client, eop_url)
                    head = data[:200].lstrip().lower()
                    if not (head.startswith(b"<!doctype") or head.startswith(b"<html")):
                        os.makedirs(SCORES_ROOT, exist_ok=True)
                        eop_path = os.path.join(
                            SCORES_ROOT, f"{m_no.group(2)}_{_sanitize_filename(real_title)}.eop")
                        with open(eop_path, "wb") as f:
                            f.write(data)
                except Exception:                      # noqa: BLE001 EOP 可选，失败不阻断
                    pass

        # 琴谱库：登记本次下载（供 epiano_search 标注「已学过」+ 记忆系统引用）
        omr_ready = bool(glob.glob(os.path.join(out_dir, "*.score.json")))
        rel_folder = f"data/epiano/sheets/{folder}"
        _library_put({
            "music_id": music_id, "title": real_title, "folder": rel_folder,
            "pages": len(files), "eop_path": eop_path or None, "omr_ready": omr_ready,
            "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        })

        # 长期记忆：把「已学过这首琴谱」写进记忆系统（下次用户点歌秒回忆）
        await _remember_score(real_title, music_id, rel_folder, len(files), omr_ready)

        readable = (f"《{real_title}》的五线谱共 {len(files)} 页，"
                    + ("本地已有，直接复用现成文件" if skipped else
                       f"已保存到 data/epiano/sheets/{folder}/（page-01.png 起）")
                    + (f"，EOP 文件已保存到 {os.path.relpath(eop_path, _PROJECT_ROOT)}。"
                       if eop_path else "。"))
        hint = ""
        if not omr_ready:
            hint = (f"接下来调 read_sheet_music(path='{rel_folder}/page-*.png') "
                    "识别成可演奏谱面，识别完就能弹了。")
        else:
            hint = "这首歌之前已经识别过乐谱（omr_ready），直接 play_score 对应 .score.json 即可。"
        return {"readable": readable + hint, "title": real_title, "music_id": music_id,
                "folder": out_dir, "rel_folder": rel_folder, "pages": len(files),
                "files": files, "eop_path": eop_path, "omr_ready": omr_ready}

    try:
        try:
            async with _make_client(True) as client:
                return await _run(client)
        except httpx.ConnectError as e:
            if "ssl" not in str(e).lower() and "certificate" not in str(e).lower():
                raise
            async with _make_client(False) as client:  # TLS 降级重试
                return await _run(client)
    except Exception as e:                             # noqa: BLE001
        return {"readable": f"错误：五线谱下载失败（{e}）。", "error": str(e),
                "music_id": music_id, "pages": 0, "files": []}


async def epiano_download(music_id, title: str = "", with_eop: bool = False) -> dict:
    """下载指定曲谱的五线谱高清 PNG；with_eop=True 时同时下载 EOP 文件。"""
    try:
        music_id = int(music_id)
    except (TypeError, ValueError):
        return {"readable": "错误：music_id 必须是 epiano_search 返回的数字 ID。",
                "pages": 0, "files": []}
    return await _download(music_id, sanitize_external(title or ""), bool(with_eop))
