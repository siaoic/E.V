#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EveryonePiano (everyonepiano.cn) 钢琴谱爬虫
============================================
按「浏览次数」降序抓取全站谱子榜单（Top N），可选下载谱面 EOP 文件。

原理:
  列表页 https://everyonepiano.cn/Music.html 支持 canshu=clicks 排序参数:
      /Music.html?come=web&canshu=clicks&paixu=desc&p={page}
  每页 10 首, 全站约 2 万首 / 2000 余页。列表条目中的 MIMusicInfo2Num
  即总浏览次数。
  注意: 每页顶部还有 1 个高亮的「推广位」条目 (top.png 徽章, 无浏览数字,
  轮换展示), 不属于真实排名, 默认剔除, --keep-promoted 可保留并标记。

用法:
  python crawler.py                          # 默认抓 5 页 = 浏览次数 Top 50
  python crawler.py --pages 10               # Top 100
  python crawler.py --pages 10 --download 5  # 同时下载前 5 首的 EOP 谱面文件
  python crawler.py --pages 3 --delay 2      # 加大请求间隔 (默认 1 秒)

输出 (output/ 目录):
  top_views.csv   表格数据
  top_views.json  含抓取元信息的完整数据
  top_views.md    可读榜单
  scores/*.eop    下载的谱面文件 (--download 时)

仅依赖 Python 标准库 (3.8+)。
"""

import argparse
import csv
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError

BASE = "https://everyonepiano.cn"
LIST_URL = BASE + "/Music.html"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# EOP 大谱表文件直链模板 (从详情页解析得到的规律):
#   /Music/down/{id}/{七位谱号}/{标题}
EOP_URL_TPL = BASE + "/Music/down/{mid}/{no}/{title}"

# 列表条目块: 到下一个条目或分页行结束
RE_ITEM_BLOCK = re.compile(
    r'<div class="MusicIndexBox".*?(?=<div class="MusicIndexBox"|<div class="row EOPMusicIndexPage)',
    re.S)
RE_TOTAL = re.compile(
    r'共(?:搜到)?\s*<span class="EOPRed">(\d+)</span>\s*首钢琴谱'
    r'(?:.*?每页\s*<span class="EOPRed">(\d+)</span>\s*首)?',
    re.S)
RE_TITLE = re.compile(r'<a\s+href=["\']([^"\']+)["\'][^>]*class=["\']Title["\'][^>]*>([^<]+)</a>')
RE_NO = re.compile(r'MIMusicNO[^>]*>\s*(\d{5,8})')
RE_AUTHOR = re.compile(r'Music\.html\?author=[^"\']*["\'][^>]*>([^<]+)</a>')
RE_UPDATE = re.compile(r'MIMusicUpdate">\s*([0-9/]+)')
RE_VIEWS = re.compile(r'MIMusicInfo2Num">\s*([\d,]+)')
RE_THUMB = re.compile(r'MIMusicPIC"\s+src="([^"]+)"')
RE_MP3 = re.compile(r'/Mp3-(\d+)\.html')
RE_VIDEO = re.compile(r'/Music/returns/(\d+)')
RE_MID = re.compile(r'/Music-(\d+)\.html')

# 五线谱: 详情页预览图 /pianomusic/{目录}/{谱号}/{谱号}-w-s-{页}.jpg (谱号在路径中出现两次!)
#         Stave 大图页内的高清 PNG: {谱号}-w-b-{页}.png
RE_STAVE_PREV = re.compile(
    r'src=["\'](?:https?://[^"\']*)?/pianomusic/([^"\'/]+)/(\d+)/(\d+)-w-s-(\d+)\.(?:jpg|png)["\']', re.I)
RE_STAVE_BIG = re.compile(
    r'src=["\']((?:https?://[^"\']*)?/pianomusic/[^"\']+/(\d+)-w-b-(\d+)\.png)["\']', re.I)


def http_get(url: str, timeout: int = 30, retries: int = 3, backoff: float = 2.0) -> bytes:
    """带重试与 TLS 容错的 GET。返回原始字节。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            })
            try:
                ctx = ssl.create_default_context()
                resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
            except ssl.SSLError:
                # 证书链校验失败时降级 (站点证书偶有链不完整的情况)
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
            return resp.read()
        except HTTPError as e:
            if e.code == 403:
                raise RuntimeError(
                    f"403 被拒绝 (站点 360 防火墙可能拦截): {url}\n"
                    "  建议: 增大 --delay、更换网络环境, 或改用 web-crawler skill 的代理通道") from e
            if e.code < 500:
                raise
            last_err = e
        except (URLError, TimeoutError, OSError) as e:
            last_err = e
        if attempt < retries:
            wait = backoff ** attempt
            print(f"  [retry {attempt}/{retries}] {type(last_err).__name__}: {last_err} — {wait:.0f}s 后重试")
            time.sleep(wait)
    raise RuntimeError(f"请求最终失败: {url} ({last_err})")


def http_get_text(url: str, **kw) -> str:
    return http_get(url, **kw).decode("utf-8", "replace")


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def parse_list(html: str, keep_promoted: bool = False):
    """解析一页列表, 返回 (条目列表, 本页推广位数)。

    每页顶部的「推广位」条目 (top.png 徽章、无浏览数字) 默认剔除;
    keep_promoted=True 时保留并标记 promoted=True (views 为 None)。
    """
    items, promoted_seen = [], 0
    for block in RE_ITEM_BLOCK.findall(html):
        if "MIMusicNO" not in block:
            continue
        m_title = RE_TITLE.search(block)
        m_no = RE_NO.search(block)
        if not (m_title and m_no):
            continue
        m_views = RE_VIEWS.search(block)
        promoted = m_views is None and "top.png" in block
        if promoted:
            promoted_seen += 1
            if not keep_promoted:
                continue
        href, title = m_title.group(1).strip(), m_title.group(2).strip()
        if href.startswith("/"):
            href = BASE + href
        m_mid = RE_MID.search(href)
        m_mp3 = RE_MP3.search(block)
        m_video = RE_VIDEO.search(block)
        m_author = RE_AUTHOR.search(block)
        m_thumb = RE_THUMB.search(block)
        m_upd = RE_UPDATE.search(block)
        items.append({
            "music_id": int(m_mid.group(1)) if m_mid else None,
            "music_no": m_no.group(1),
            "title": title,
            "author": m_author.group(1).strip() if m_author else "",
            "views": int(m_views.group(1).replace(",", "")) if m_views else None,
            "promoted": promoted,                    # 推广位条目 (不属于真实排名)
            "update_date": m_upd.group(1) if m_upd else "",
            "detail_url": href,
            "thumb_url": urllib.parse.urljoin(BASE, m_thumb.group(1)) if m_thumb else "",
            "has_mp3": bool(m_mp3),
            "mp3_id": int(m_mp3.group(1)) if m_mp3 else None,
            "has_video": bool(m_video),
            "video_id": int(m_video.group(1)) if m_video else None,
        })
    return items, promoted_seen


def page_url(page: int, word: str = "") -> str:
    qs = urllib.parse.urlencode({
        "come": "web", "canshu": "clicks", "paixu": "desc",
        "word": word, "author": "", "jianpu": "", "username": "", "p": page,
    })
    return f"{LIST_URL}?{qs}"


def crawl(pages: int, delay: float, timeout: int, keep_promoted: bool = False,
          word: str = "", page_from: int = 1):
    """抓取按浏览次数降序的前 pages 页; word 非空时为站内搜索模式。"""
    all_items, total, per_page = [], None, 10
    skipped_promoted = 0
    pages_max = page_from + pages - 1
    for p in range(page_from, pages_max + 1):
        url = page_url(p, word=word)
        print(f"[{p}] GET {url}")
        html = http_get_text(url, timeout=timeout)
        m = RE_TOTAL.search(html)
        if m and total is None:
            total = int(m.group(1))
            per_page = int(m.group(2)) if m.group(2) else per_page
            print(f"  匹配 {total} 首谱子, 每页 {per_page} 首")
        rows, n_promoted = parse_list(html, keep_promoted=keep_promoted)
        skipped_promoted += n_promoted
        if not rows:
            print("  本页未解析到条目, 停止。")
            break
        all_items.extend(rows)
        print(f"  解析到 {len(rows)} 条 (累计 {len(all_items)})")
        if p < pages_max:
            time.sleep(delay)
    return all_items, total, per_page, skipped_promoted


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("._")[:80]


def download_eop(item: dict, out_dir: str, timeout: int = 60) -> str:
    """下载某条的 EOP 谱面文件, 返回保存路径; 失败返回空串。"""
    if not (item.get("music_id") and item.get("music_no")):
        return ""
    url = EOP_URL_TPL.format(mid=item["music_id"], no=item["music_no"],
                             title=urllib.parse.quote(item["title"]))
    try:
        data = http_get(url, timeout=timeout, retries=2)
    except Exception as e:
        print(f"    下载失败: {e}")
        return ""
    head = data[:200].lstrip().lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html"):
        print(f"    返回的是 HTML (该谱可能无 EOP 文件): {url}")
        return ""
    fname = f"{item['music_no']}_{sanitize_filename(item['title'])}.eop"
    path = os.path.join(out_dir, fname)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _is_image(data: bytes) -> bool:
    return data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8\xff")


def stave_big_urls(item: dict, timeout: int) -> list:
    """从详情页解析五线谱区块, 构造每页高清大图 (-w-b-*.png) 的 URL 列表。"""
    html = http_get_text(item["detail_url"], timeout=timeout)
    prevs = RE_STAVE_PREV.findall(html)
    if not prevs:
        return []
    dirpart, no_dir, no_file = prevs[0][0], prevs[0][1], prevs[0][2]
    count = max(int(g[3]) for g in prevs)
    return [f"{BASE}/pianomusic/{dirpart}/{no_dir}/{no_file}-w-b-{n}.png"
            for n in range(1, count + 1)]


def download_staves(item: dict, out_dir: str, timeout: int = 60) -> int:
    """下载一首曲子的全部五线谱高清 PNG, 返回成功页数。"""
    try:
        urls = stave_big_urls(item, timeout)
    except Exception as e:
        print(f"    详情页获取失败: {e}")
        return 0
    if not urls:
        print("    该谱没有五线谱区块")
        return 0
    os.makedirs(out_dir, exist_ok=True)
    ok = 0
    for n, u in enumerate(urls, 1):
        data = b""
        try:
            data = http_get(u, timeout=timeout, retries=2)
        except Exception as e:
            print(f"    第{n}页直链失败: {e}")
        if not _is_image(data):
            # 直链失效时回退: 抓 Stave 大图页解析真实地址
            try:
                stave_html = http_get_text(f"{BASE}/Stave-{item['music_id']}-{n}.html",
                                           timeout=timeout)
                m = RE_STAVE_BIG.search(stave_html)
                if m:
                    data = http_get(urllib.parse.urljoin(BASE, m.group(1)),
                                    timeout=timeout, retries=2)
            except Exception:
                pass
        if not _is_image(data):
            print(f"    第{n}页不是图片 (可能不存在)")
            continue
        with open(os.path.join(out_dir, f"page-{n:02d}.png"), "wb") as f:
            f.write(data)
        ok += 1
    return ok


def write_outputs(items: list, meta: dict, out_dir: str, prefix: str = "top_views"):
    os.makedirs(out_dir, exist_ok=True)

    csv_path = os.path.join(out_dir, f"{prefix}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        cols = ["rank", "views", "promoted", "title", "author", "update_date",
                "music_no", "music_id", "detail_url", "has_mp3", "mp3_id",
                "has_video", "video_id", "thumb_url"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        rank = 0
        for it in items:
            if not it.get("promoted"):
                rank += 1
            w.writerow({**it, "rank": rank if not it.get("promoted") else ""})

    json_path = os.path.join(out_dir, f"{prefix}.json")
    ranked = []
    rank = 0
    for it in items:
        if not it.get("promoted"):
            rank += 1
        ranked.append({**it, "rank": rank if not it.get("promoted") else None})
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "items": ranked}, f, ensure_ascii=False, indent=2)

    md_path = os.path.join(out_dir, f"{prefix}.md")
    n_ranked = sum(1 for it in items if not it.get("promoted"))
    title_kw = f"EveryonePiano 搜索「{meta['search']}」结果" if meta.get("search") \
        else "EveryonePiano 浏览次数最多钢琴谱"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# {title_kw} Top {n_ranked}（按浏览次数降序）\n\n")
        f.write(f"- 来源: {meta['source']}\n- 抓取时间: {meta['crawled_at']}\n")
        f.write(f"- 站点总数: {meta['total_scores']} 首 | 本次抓取: 前 {meta['pages_crawled']} 页\n")
        if meta.get("promoted_skipped"):
            f.write(f"- 已剔除推广位条目: {meta['promoted_skipped']} 个 (每页顶部轮换展示, 不属于真实排名)\n\n")
        else:
            f.write("\n")
        f.write("| 排名 | 曲名 | 作者 | 浏览次数 | 更新日期 | 详情 |\n|---:|---|---|---:|---|---|\n")
        for it in ranked:
            views = f"{it['views']:,}" if it["views"] is not None else "—(推广位)"
            name = f"**{it['title']}**" if it.get("promoted") else it["title"]
            rk = it["rank"] if it["rank"] is not None else "推广"
            f.write(f"| {rk} | {name} | {it['author']} | {views} | "
                    f"{it['update_date']} | [#{it['music_id']}]({it['detail_url']}) |\n")
    return csv_path, json_path, md_path


def main():
    ap = argparse.ArgumentParser(description="EveryonePiano 浏览次数榜单爬虫")
    ap.add_argument("--pages", type=int, default=5, help="抓取页数, 每页 10 首 (默认 5 = Top 50)")
    ap.add_argument("--delay", type=float, default=1.0, help="请求间隔秒数 (默认 1.0, 请勿低于 0.5)")
    ap.add_argument("--timeout", type=int, default=30, help="单请求超时秒数")
    ap.add_argument("--download", type=int, default=0, metavar="K", help="额外下载前 K 首的 EOP 谱面文件")
    ap.add_argument("--sheets", type=int, default=0, metavar="K",
                    help="额外下载前 K 首的五线谱高清 PNG (每首一个文件夹, 逐页保存)")
    ap.add_argument("--search", default="", metavar="关键词",
                    help="站内搜索歌名/关键词, 结果同样按浏览次数降序; 配合 --sheets/--download 使用")
    ap.add_argument("--keep-promoted", action="store_true",
                    help="保留每页顶部的推广位条目 (标记 promoted=true, 不计入排名)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"),
                    help="输出目录")
    args = ap.parse_args()

    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    mode = f"搜索「{args.search}」" if args.search else "全站浏览次数榜"
    print(f"=== EveryonePiano 爬虫: {mode}, 抓取前 {args.pages} 页 (Top {args.pages * 10}) ===")
    items, total, per_page, promoted_skipped = crawl(
        args.pages, args.delay, args.timeout, keep_promoted=args.keep_promoted,
        word=args.search)
    if not items:
        print("未抓到任何数据 (关键词无匹配?), 退出。")
        sys.exit(1)

    prefix = f"search_{sanitize_filename(args.search)}" if args.search else "top_views"
    meta = {
        "source": LIST_URL,
        "search": args.search or None,
        "sort": "canshu=clicks&paixu=desc",
        "crawled_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "total_scores": total,
        "per_page": per_page,
        "pages_crawled": args.pages,
        "promoted_skipped": promoted_skipped,
    }

    if args.download > 0:
        score_dir = os.path.join(args.out, "scores")
        os.makedirs(score_dir, exist_ok=True)
        dl_targets = [it for it in items if not it.get("promoted")][:args.download]
        print(f"\n=== 下载前 {args.download} 首的 EOP 谱面 → {score_dir} ===")
        ok = 0
        for i, it in enumerate(dl_targets, 1):
            print(f"  ({i}/{args.download}) {it['title']}")
            path = download_eop(it, score_dir, timeout=args.timeout)
            if path:
                ok += 1
                print(f"    已保存: {os.path.basename(path)} ({os.path.getsize(path):,} bytes)")
            if i < args.download:
                time.sleep(max(args.delay, 1.0))
        meta["downloaded"] = ok
        print(f"下载完成: {ok}/{args.download}")

    if args.sheets > 0:
        sheet_root = os.path.join(args.out, "sheets")
        os.makedirs(sheet_root, exist_ok=True)
        targets = [it for it in items if not it.get("promoted")][:args.sheets]
        print(f"\n=== 下载前 {args.sheets} 首的五线谱高清图 → {sheet_root} ===")
        used_folders = {}          # 歌名 → 谱号, 处理同名不同曲
        total_pages, ok_scores = 0, 0
        for i, it in enumerate(targets, 1):
            name = sanitize_filename(it["title"])
            if name in used_folders and used_folders[name] != it["music_no"]:
                name = f"{name}_{it['music_no']}"
            used_folders[name] = it["music_no"]
            print(f"  ({i}/{args.sheets}) {it['title']}")
            n = download_staves(it, os.path.join(sheet_root, name), timeout=args.timeout)
            total_pages += n
            ok_scores += 1 if n else 0
            print(f"    五线谱: {n} 页")
            if i < args.sheets:
                time.sleep(max(args.delay, 1.0))
        meta["sheets_scores"] = ok_scores
        meta["sheets_pages"] = total_pages
        print(f"五线谱下载完成: {ok_scores} 首 / 共 {total_pages} 页")

    csv_path, json_path, md_path = write_outputs(items, meta, args.out, prefix=prefix)

    ranked = [it for it in items if not it.get("promoted")]
    print(f"\n=== {mode} — Top {min(15, len(ranked))} ===")
    print(f"{'排名':>4}  {'浏览次数':>10}  曲名 — 作者")
    for i, it in enumerate(ranked[:15], 1):
        print(f"{i:>4}  {it['views']:>10,}  {it['title']} — {it['author']}")
    if promoted_skipped:
        print(f"(已剔除 {promoted_skipped} 个每页顶部的推广位条目, 用 --keep-promoted 可保留)")

    print(f"\n输出文件:\n  {csv_path}\n  {json_path}\n  {md_path}")


if __name__ == "__main__":
    main()
