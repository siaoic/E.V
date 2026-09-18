# E.V — MaiBot 直播实例

MaiBot（Python 3.12 + FastAPI + SQLite）的 B 站直播实例，含事件驱动多 World 框架与
VTuber 表演层桥接：

- `src/worlds/`：世界框架基座（事件总线 / 状态缓存 / 变更轮询 / 延迟双视图）
- `plugins/world-bilibili`：B 站弹幕/礼物接入（已内置）
- `plugins/world-asr`：麦克风语音世界（VAD 分段 → ASR 转写 → 世界事件）
- `plugins/world-minecraft`：Minecraft 世界（mineflayer 桥，`node/` 下需 `npm install`）
- `plugins/world-pvz`：植物大战僵尸世界（帧差 + 模板匹配，模板需实机标定，见其 `templates/README.md`）
- `src/maisaka/vtuber_bridge/`：Live2D 口型 + 表情桥（VTube Studio，`config/bilibili_live.toml` 的 `[vtuber]`）
- `dashboard/`：WebUI 前端（React + Vite，`npm run build` 产出供主程序 8001 端口使用）
- 设计文档：`多World与VTuber表演层-未实施部分实施计划.md`、`迁移前置调研.md`

## 本仓库刻意排除的内容（见 `.gitignore`）

| 排除项 | 原因 | 本地恢复方式 |
| --- | --- | --- |
| `config/model_config.toml` | 含真实 LLM API Key | 复制 `config/model_config.example.toml` 并填入自己的 Key |
| `plugins/world-bilibili/config.toml` | 含 B 站账号 SESSDATA | 复制 `plugins/world-bilibili/config.example.toml` 并填入房间号与 SESSDATA |
| `data/`、`logs/`、`tts_service/models/` | 用户数据 / 运行时产物 / 模型权重 | 首次运行自动生成 |
| `.venv/`、`node_modules/`、`dashboard/dist/` | 可重建的虚拟环境 / 依赖 / 构建产物 | `uv sync`、`npm install`、`cd dashboard && npm run build` |

## 快速启动（概要）

```bash
uv sync                       # Python 依赖
uv run python bot.py          # 主程序（WebUI 默认 127.0.0.1:8001）
cd plugins/world-minecraft/node && npm install   # 仅 Minecraft 世界需要
cd dashboard && npm install && npm run build     # 仅改前端后需要
```
