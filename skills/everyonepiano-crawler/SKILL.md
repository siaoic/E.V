---
name: everyonepiano-crawler
description: 抓取 everyonepiano.cn (EveryonePiano) 钢琴谱: 全站"浏览次数最多"榜单或按歌名搜索, 可下载五线谱高清 PNG 与 EOP 谱面文件. Use when the user wants EveryonePiano piano scores, 五线谱/谱子/钢琴谱, sheet-music downloads, or most-viewed piano score rankings from everyonepiano.cn.
---

# EveryonePiano 钢琴谱爬虫

抓取 everyonepiano.cn 的钢琴谱：按浏览次数排序的全站榜单，或按歌名搜索；可下载五线谱高清 PNG（可直接打印）与 EOP 谱面文件。纯 Python 标准库（3.8+），**无需安装任何第三方包**。

## 何时使用

- 用户想要 EveryonePiano 网站的谱子 / 五线谱 / EOP 文件
- 用户要"浏览次数最多 / 最热门"的钢琴谱榜单
- 用户给出歌名，想找谱并下载

## 快速命令

在本 skill 目录下执行（Windows 用 `python`，Linux/macOS 用 `python3`）：

```bash
# 搜索歌名 + 下载第 1 个结果(=浏览次数最高版本)的五线谱和 EOP —— 最常用
python crawler.py --search "天空之城" --pages 1 --sheets 1 --download 1

# 只看搜索结果榜单, 不下载
python crawler.py --search "卡农" --pages 1

# 全站浏览次数 Top 50 (默认 5 页 × 10 首)
python crawler.py --pages 5

# Top 100 + 前 5 首的五线谱
python crawler.py --pages 10 --sheets 5
```

输出目录默认为 `<本skill目录>/output`；写入受限时用 `--out` 指到可写目录。

## 参数速查

| 参数 | 说明 |
|---|---|
| `--search 关键词` | 站内搜索歌名/关键词，结果按浏览次数降序 |
| `--pages N` | 抓取页数，每页 10 首（默认 5 = Top 50） |
| `--sheets K` | 下载前 K 首的五线谱高清 PNG，每首一个以歌名命名的文件夹 |
| `--download K` | 下载前 K 首的 EOP 谱面文件（EveryonePiano 软件可播放） |
| `--delay 秒` | 请求间隔（默认 1.0，**勿低于 0.5**） |
| `--out 目录` | 输出目录 |
| `--keep-promoted` | 保留每页顶部推广位条目（默认剔除） |
| `--timeout 秒` | 单请求超时（默认 30） |

## 输出结构

```
output/
├── top_views.csv|json|md            # 全站榜单 (搜索模式为 search_<关键词>.*)
├── sheets/{歌名}/page-NN.png        # 五线谱高清大图, 约 2500×3400, 可直接打印
└── scores/{谱号}_{歌名}.eop         # EOP 谱面文件
```

## 结果解读（汇报给用户时）

- CSV/JSON 的 `rank` 只统计真实条目，`views` 为总浏览次数（列表页数值，排序依据）
- 同一首歌常有多个版本（超简单版/简单版/C调版…），各自独立计数；搜索结果即按浏览次数排序，**第 1 个结果 = 浏览次数最高的版本**
- 每页顶部的"推广位"条目（TOP 徽章、无浏览数字）已被默认剔除，不属于真实排名

## 踩坑与注意

- **礼貌爬取**：保持默认 `--delay ≥ 1`；出现 403 = 被站点 360 防火墙拦截 → 加大 delay 或换网络，**禁止高频重试**
- **PDF 不可用**：站点 PDF 下载需登录，本 skill 不支持；五线谱 PNG 即打印件效果
- 五线谱 URL 中谱号出现两次（目录级+文件名前缀），脚本已处理；直链失效会自动回退 Stave 大图页
- EOP 详情页的"点击次数"与榜单"浏览次数"口径不同，勿混用
- 仅限个人学习研究用途
