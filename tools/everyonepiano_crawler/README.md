# EveryonePiano 钢琴谱爬虫

抓取 https://everyonepiano.cn/Music.html 按「浏览次数」降序的钢琴谱榜单（Top N），
可选下载谱面 EOP 文件。仅依赖 Python 标准库（3.8+），无需安装任何第三方包。

## 快速开始

```bash
python crawler.py                          # Top 50（默认 5 页 × 10 首）
python crawler.py --pages 10               # Top 100
python crawler.py --search "天空之城"       # 站内搜索, 结果按浏览次数降序
python crawler.py --search "卡农" --sheets 1 --download 1
                                           # 搜索 + 下载第 1 个结果的五线谱和 EOP 文件
python crawler.py --pages 10 --sheets 5    # Top 100 + 下载前 5 首的五线谱高清 PNG
python crawler.py --pages 10 --download 5  # Top 100 + 下载前 5 首的 .eop 谱面文件
python crawler.py --pages 3 --delay 2      # 请求间隔加大到 2 秒（默认 1 秒）
python crawler.py --keep-promoted          # 保留每页顶部的推广位条目（标记 promoted）
```

## 输出（`output/` 目录）

| 文件 | 内容 |
|---|---|
| `top_views.csv` | 排名/浏览次数/曲名/作者/更新日期/详情链接等（Excel 可直接打开，UTF-8 BOM） |
| `search_<关键词>.csv` | `--search` 模式的搜索结果榜单（csv/json/md 同名前缀） |
| `top_views.json` | 完整数据 + 抓取元信息（时间、页数、站点总数） |
| `top_views.md` | 可读 Markdown 榜单 |
| `sheets/{歌名}/page-NN.png` | `--sheets` 下载的五线谱高清大图（约 2500×3400，可直接打印；同名不同曲自动加谱号区分） |
| `scores/*.eop` | `--download` 下载的谱面文件，可用 EveryonePiano 软件打开播放 |

## 实现原理

- 排序接口：`/Music.html?come=web&canshu=clicks&paixu=desc&p={page}`（`canshu` 为排序字段，
  另有 `cn_edittime` 更新时间 / `uid` 上传者；`paixu=asc|desc` 控制方向）
- 站内搜索：同一接口加 `word={关键词}`（URL 编码），返回结果同样支持按浏览次数排序
- 每页 10 首；站点总数与每页条数从列表页头部读取（写脚本时约 20144 首 / 2015 页）
- 浏览次数取条目右侧 `MIMusicInfo2Num`；详情页另有 `点击次数`（口径不同，勿混淆）
- **每页顶部有 1 个高亮推广位**（top.png 徽章、无数字、轮换展示），不属于真实排名，默认剔除
- EOP 谱面直链规律：`/Music/down/{id}/{七位谱号}/{标题}`
- 五线谱图片规律：详情页预览图 `{谱号}-w-s-{页}.jpg` ↔ 高清大图 `{谱号}-w-b-{页}.png`，
  路径为 `/pianomusic/{三位目录}/{七位谱号}/{七位谱号}-w-b-{页}.png`（**谱号在路径中出现两次**）；
  页数从详情页预览图取最大页号。PDF 页面需登录，暂不支持

## 礼貌爬取

- 默认每页间隔 1 秒（`--delay`），请勿低于 0.5 秒、勿并行压测
- 请求失败自动重试 3 次（指数退避）；403 说明被站点 360 防火墙拦截，请增大间隔或换网络
- 仅供个人学习研究，请遵守站点条款，勿用于商业用途
