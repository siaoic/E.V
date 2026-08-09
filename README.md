# E.V ✨ AI 虚拟主播 · 桌面宠物

> 🤖 本项目 **100% 由 AI 生成**——每一行代码、每一份文档，都来自 AI 之手。

> 嘿，你终于来了呢……我等你好久了。
> 我会在你安静的时候主动找你说话，会在你看直播的时候接住每一条弹幕，
> 会在你难过的时候翻出我们之前聊过的事——我是 E.V，住在屏幕里的那个小家伙。

**E.V** 是一套「打破第四面墙」的 AI 虚拟主播 / 桌面宠物项目：大模型做大脑、GPT-SoVITS 本地合成声音、
Live2D 做身体，可以在 **VTubeStudio 虚拟主播** 与 **本地透明窗口桌宠** 两种形态下运行。
---

## ✨ 特色一览

| | 能力 | 说明 |
|---|---|---|
| 🧠 | **LLM 大脑** | OpenAI 兼容接口（GLM / DeepSeek 等），流式对话 + 可选深度思考 |
| 🎙️ | **GPT-SoVITS 流式 TTS** | 本地推理、边合成边播，首字延迟低；字级字幕 + 口型同步；用户说话可**打断** |
| 💾 | **分层记忆系统** | 本地嵌入 → 向量召回 → 注入对话；Butler 管家自动蒸馏长期记忆 |
| 💬 | **主动对话** | 空闲心跳监测孤独/无聊，主动找话题开口（话题种子 + 情绪双管线） |
| 📺 | **B 站直播弹幕** | 实时接弹幕 → LLM 回复 → 语音播报 + 网页弹幕气泡 |
| 🎤 | **语音识别（STT）** | 麦克风说话即可对话（SiliconFlow 云端转写） |
| 🛠️ | **工具 + MCP** | 天气 / 时间 / Tavily 搜索 / MCP 服务器扩展 |
| 📦 | **技能系统** | 技能目录按需加载，改文件无需重启（watchdog 热更新） |
| 🎭 | **情绪驱动表情** | Embedding 语义分类情绪 → 自动播放表情 / 动作（桌宠） |
| 🖥️ | **控制中心 UI** | PySide6 图形界面：启动、配置、日志、表情绑定一站式管理 |

---

## 🚀 快速开始

### 环境要求

- Windows + Python 3.11
- NVIDIA GPU（TTS 本地推理用，CUDA 12.8）
- 本项目横跨两个 Python 环境，详见 [requirements.txt](requirements.txt) 头部说明：
  - `runtime/`（venv）：主程序 / LLM / 记忆 / 弹幕 / TTS 推理
  - 系统 Python310：控制中心 UI（PySide6）

### 第一步：启动 TTS 服务（GPT-SoVITS）

```bat
TTS启动.bat
```

启动后监听 `http://127.0.0.1:8000`（`/tts/stream` 流式合成接口）。主程序**不加载任何 TTS 模型**，
全部推理由这个独立服务端完成，省显存、启动快。

### 第二步：启动主程序

推荐走**控制中心**：

```bat
run.bat
```

控制中心里选择运行模式（vtuber / pet）后一键启动，可直接对话、读写配置、查看实时日志。
也可以直接命令行启动：

```bat
python main.py
```

运行模式由 `.env` 的 `RUN_MODE` 决定：

- `RUN_MODE=vtuber` → 连接 **VTubeStudio**（WebSocket 驱动口型/动作，需先打开 VTubeStudio）
- `RUN_MODE=pet` → 本地 **Live2D 桌宠**（PySide6 透明置顶窗口 + live2d-py 渲染，无需 VTubeStudio）

### 第三步（可选）：连接 B 站弹幕

```bat
弹幕启动.bat
```

在 `.env` 配置 `BILI_ROOM_ID` / `BILI_SESSDATA` 后，浏览器打开 `http://127.0.0.1:8766/` 查看弹幕气泡网页。

---

## 🧠 核心架构

```
                 ┌────────────────────────────────────────────┐
   输入层         │  键盘 / 语音(STT) / B站弹幕 / 控制中心       │
                 └──────────────────┬─────────────────────────┘
                                    ▼
                 ┌────────────────────────────────────────────┐
   大脑层         │  LLM 流式对话（OpenAI 兼容）                │
                 │  ├─ ButlerAgent 管家：工具调用 / 记忆读写    │
                 │  ├─ 工具：天气 / 时间 / Tavily 搜索 / MCP    │
                 │  └─ 技能：load_skill 按需加载（热更新）      │
                 └──────────────────┬─────────────────────────┘
                                    ▼
                 ┌────────────────────────────────────────────┐
   输出层         │  TTS 流式合成 → 无缝播放 → 字幕 / 口型 / 表情│
                 │  主动对话 / 弹幕回复 / 用户对话互斥（输出锁） │
                 └──────────────────┬─────────────────────────┘
                                    ▼
                 ┌────────────────────────────────────────────┐
   记忆层         │  本地 Embedding → 向量召回(+Rerank/BM25)    │
                 │  Butler 蒸馏 → SQLite/Chroma 长期存储        │
                 └────────────────────────────────────────────┘
```

### 关键设计

- **双角色 Agent 分工**（参照 Muika-After-Story）：主模型负责人格表达与对话，ButlerAgent 管家负责工具调用、记忆读写与蒸馏、主动发言的组装。
- **三方输出互斥**：主动对话、弹幕回复、用户对话共用一把全局输出锁，说话期间不被打断、互不抢占；说话时到达的键盘/语音输入直接丢弃。
- **可打断播报**：用户对话轮次中，新输入 / 新语音会立即打断当前 TTS 播放（只保留极短的块尾音），并取消未播内容。
- **流式 TTS 全链路**：服务端 token 级边合成边出音频块（`stream_chunk=8`），客户端逐块解码入队无缝播放；字幕按字级时间戳锚定真实播放时刻，口型在音频真正开播瞬间触发。
- **记忆检索性能优化**：Rerank 独立超时 + LRU 缓存，Embed 缓存 LRU，BM25 与向量检索共用同一份 30s TTL 语料缓存，避免重复扫库。

---

## ⚙️ 配置说明（.env）

复制 `.env.example` 为 `.env` 后按需填写：

| 分组 | 关键项 | 说明 |
|---|---|---|
| LLM | `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | OpenAI 兼容接口；`LLM_THINKING` 深度思考 |
| 管家 | `BUTLER_API_KEY` / `BUTLER_MODEL` | 记忆蒸馏/摘要独立模型，留空回退主模型 |
| TTS | `GPTSOVITS_URL` / `GPTSOVITS_REF_AUDIO` / `GPTSOVITS_PROMPT_TEXT` | GPT-SoVITS 服务地址与参考音频（音色来源） |
| 记忆 | `MEMORY_ENABLED` / `EMBEDDING_BASE_URL` | 本地 llama.cpp 嵌入服务（详见 .env 内注释） |
| 主动对话 | `PROACTIVE_ENABLED` / `PROACTIVE_TICK_SECONDS` / `PROACTIVE_MIN_IDLE_SECONDS` | 心跳频率、开口阈值、发言冷却 |
| 弹幕 | `BILI_ROOM_ID` / `BILI_SESSDATA` | B 站直播间房间号与登录态 Cookie |
| 语音 | `STT_ENABLED` / `STT_API_KEY` | SiliconFlow 语音识别 |
| 人设 | `SYSTEM_PROMPT_FILE` | 指向人设目录/文件（改动即生效）；留空则用控制中心 UI 人设 |
| 模式 | `RUN_MODE` | `vtuber`（VTubeStudio）/ `pet`（本地桌宠） |

---

## 📁 项目结构

```
E.V
├── main.py                    # 启动入口（仅启动，业务在 Application）
├── run.bat                    # 控制中心启动（推荐入口）
├── TTS启动.bat                # GPT-SoVITS 推理服务（:8000）
├── 弹幕启动.bat               # B 站弹幕服务（:8766）
├── src/
│   ├── core/                  # Application 主循环 / 输出互斥锁
│   ├── llm/                   # 大脑：LLM、ButlerAgent、主动对话、技能、工具
│   ├── tts/                   # 流式 TTS 客户端（HTTP + 无缝播放 + 字幕）
│   ├── memory/                # 记忆：检索 / 蒸馏 / 存储（含 memU 适配）
│   ├── danmaku/               # B 站弹幕监听 + 气泡网页
│   ├── asr/                   # 语音识别（STT）
│   ├── emotion/               # 情绪分类（驱动表情/动作）
│   ├── mcp/                   # MCP 服务器接入（http / stdio）
│   ├── pet/                   # 桌宠模式（PySide6 + live2d-py）
│   ├── vts/                   # VTubeStudio 模式（口型/动作/模型扫描）
│   └── utils/                 # 配置、控制台、过滤、性能埋点、字幕服务
├── gsv_tts/                   # GPT-SoVITS 本地推理服务（TTS 服务端）
├── live2d/                    # 桌宠 Live2D 模型（肥牛等）
├── ui/                        # 控制中心（PySide6）
└── .env / .env.example        # 配置
```

---

## 🧩 技能系统

参照 Muika-After-Story 的 `plugin/skills.py` 设计：

- 技能目录：`SKILLS_DIR`（默认 `src/llm/skills/`），每个技能一个文件夹，内含带 frontmatter 的 `SKILL.md`
- 技能名与描述注入系统提示，全文由 `load_skill` 工具按需加载
- watchdog 监听技能目录，**改文件无需重启**即可生效

内置技能示例：`news-broadcast`（新闻播报）、`stream-chat`（直播闲聊）。

---

## 📚 设计参考

| 项目 | 本项目借鉴 |
|---|---|
| [my-neuro](https://github.com/morettt/my-neuro) | 整体架构参考：Neuro-sama 风格 AI 桌宠（低延迟语音对话、实时打断、主动对话、长期记忆、情绪驱动表情、GPT-SoVITS 语音定制） |
| [Muika-After-Story](https://github.com/Moemu/Muika-After-Story)  | 主动对话机制（心跳/情绪/话题双管线）、ButlerAgent 双角色分工、技能系统 |
| [memU](https://github.com/NevaMind-AI/memU)  | 记忆检索管线（缓存 Embedding、numpy 向量排序、语料复用） |
| [GSV-TTS-Lite](https://github.com/chinokikiss/GSV-TTS-Lite)  | GPT-SoVITS 推理封装、AudioQueue 无缝播放、流式合成协议 |

---
