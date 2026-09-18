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

## TS 侧重构（进行中）

`server/` 是 TypeScript 应用层（迁移方案 B：TS 接管 HTTP/WS/鉴权/数据层；Python 保留
AI 推理 / 向量 / TTS / 插件 Runner，见 `迁移前置调研.md`）。阶段①已落地：

- `server/src/auth/`：webui.json Token 管理 + Cookie + WS 临时 token（与 Python 逐字对齐，29 项契约测试锁定）
- `server/src/db/`：真实 DDL（`schema.sql` 只读导出）+ Drizzle schema（由 `scripts/generate_drizzle_schema.py` 生成）
- `server/src/http/`：/api/webui 默认鉴权守卫 + health / version-compatibility / auth / ws-token 路由
- `server/src/main.ts`：进程入口，退出码 42 = 重启（与 bot.py 对齐）

```bash
cd server
npm install          # Node >= 22；原生模块脚本批准已写入 package.json 的 allowScripts
npm test             # 29 项契约测试
npm run dev          # MAIBOT_ROOT 可指定仓库根；MAIBOT_DB_FILE 可覆盖主库路径
```

## 快速启动（概要）

```bash
uv sync                       # Python 依赖
uv run python bot.py          # 主程序（WebUI 默认 127.0.0.1:8001）
cd plugins/world-minecraft/node && npm install   # 仅 Minecraft 世界需要
cd dashboard && npm install && npm run build     # 仅改前端后需要
```
