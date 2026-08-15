# E.V

*I will be on air soon.*

> [!IMPORTANT]
> **本项目 100% 由 AI 生成**——从架构设计、模块拆分、提示词工程到文档撰写，全部代码与文字均由大语言模型协作完成。这里要特别感谢以下开源项目，它们的存在让 E.V 得以在短时间内从概念走向可运行状态：
>
> - [Moemu/Muika-After-Story](https://github.com/Moemu/Muika-After-Story) —— 事件循环 / 状态机 / 四层长期记忆 / 管家 Agent 的设计范式
> - [chinokikiss/GSV-TTS-Lite](https://github.com/chinokikiss/GSV-TTS-Lite) —— GPT-SoVITS 本地 TTS 推理与个性化音色支持
> - [morettt/my-neuro](https://github.com/morettt/my-neuro) —— VTube × 弹幕 × 主动对话的整体范式与表情调度
> - [NevaMind-AI/MemU](https://github.com/NevaMind-AI/MemU) —— 长期记忆层与向量检索基础设施

> [!NOTE]
> 本项目目前属于活跃迭代阶段，部分模块（自我进化、主动话题、表情驱动）仍在持续调优，行为与接口可能发生变动。

## Introduction✨

`E.V` 是一个面向 B 站直播场景的 **AI 虚拟主播运行时**：以智谱 AI 为大脑、VTube Studio / Live2D 为身体、blivedm 为耳朵、GPT-SoVITS 为嘴巴，串联起「看到弹幕 → 听懂人话 → 思考回复 → 开口说话 → 表情口型同步」的一整条链路。

它不是单一形态的 Bot，而是一份**可装载不同人格的运行容器**。把 system prompt、记忆种子、表情动作文件换掉，E.V 就能扮演另一个角色重新上播。代码与配置完全解耦，所有「人格」相关的内容都集中放在 `src/llm/skills/` 与 `configs/` 下，方便二次创作。

## Features🪄

- **大脑**：基于智谱 AI（zhipuai SDK）的流式对话与工具调用，模型路由 + Prompt 自演化（`evolution.py`）
- **耳朵**：B 站弹幕多房间监听（`blivedm`）+ 本地语音识别（`src/asr/stt.py`），全场景统一抽象为 `InputEvent`
- **嘴巴**：内置 GPT-SoVITS 本地推理（`gsv_tts/`）+ 流式字幕推送（SSE，`subtitle_server.py`），字幕默认显示 2.5s 后淡出
- **身体**：VTube Studio HTTP 插件接口（`src/vts/`），含表情切换、口型同步、动作播放、ARKit 面部驱动
- **形态**：双形态运行——直播台（控制中心 `ui/control_center.py`）+ 桌宠（`src/pet/pet_app.py`，基于 PySide6 + live2d-py）
- **记忆**：四层记忆架构——短期上下文（recent_turns）、长期向量记忆（memU）、人格画像（evolution_profile.json）、自演化提示词档案
- **情绪**：情绪状态机（`src/emotion/`），LLM 推理时输出 emotion 标签 → VTS 切表情 → TTS 调语调
- **主动对话**：空闲期话题种子 + 冷却调度 + 周期自我复盘，输出与弹幕回复走同一互斥锁，避免抢麦
- **工具**：MCP 协议客户端（`src/mcp/`）+ 屏读 / 截图 / 联网搜索 / 时间天气等内置工具
- **性能**：numpy O(n) 向量检索替代 HNSW、Embed/Rerank 双层 LRU 缓存、httpx 连接池调优，性能面板（`perf_tracker.py`）可实时观察

## Architecture🧠

### 三方输出互斥

直播场景下「主动找话题 / 回复弹幕 / 与当前用户对话」三路输出必须严格互斥，否则观众会听到 E.V 一边自言自语一边回复弹幕，体验崩坏。

E.V 通过 `src/core/output_lock.py` 实现**全局输出互斥锁（`_OUTPUT_LOCK`）**：任一时刻只有一路输出方能拿到锁，期间所有新触发的输出请求要么排队等待、要么被丢弃。锁内还维护了 `_ai_speaking` / `_user_talking` 状态机，主动对话或回复弹幕期间会**丢弃所有键盘输入和语音识别结果**（不缓存），保证一句话不会被打断。

### 双形态运行

- **`RUN_MODE=live`**（默认）：以「直播台」形态运行，`ui/control_center.py` 启动 PySide6 控制中心作为主进程，子进程跑 `main.py`。可在控制中心里切换模型、查看日志、手动触发对话。
- **`RUN_MODE=pet`**：以「桌宠」形态运行，渲染 Live2D 模型到桌面，鼠标可拖动、点击互动。`vendor_pet/` 内的依赖（live2d-py 等）只在桌宠模式下加载，避免污染主环境。

### 记忆四层

| 层级 | 存储 | 用途 | 注入时机 |
|---|---|---|---|
| 短期上下文 | `recent_turns`（内存） | 最近 N 轮对话 | 每轮必注入 |
| 长期向量记忆 | memU（`src/memory/memu/`） | 跨 Session 语义检索 | 按当前 query 检索 Top-K |
| 人格画像 | `data/evolution_profile.json` | 主人偏好、关系状态 | 复盘后写入，每轮轻量 2-gram 关键词召回 |
| 自演化提示词 | `evolution.py` 产出 | 高质量对话范式沉淀 | 注入 system prompt |

### 自我进化双触发

- **被动触发**（`maybe_review`）：每轮对话后检查，距上次复盘是否已超过 N 轮 → 是则复盘本轮对话。
- **主动触发**（`periodic_tick`）：后台每 `EVOLUTION_PERIODIC_INTERVAL` 秒检查一次，仅在「距上次复盘已达标且有未复盘新轮次」时补刀，不重复消费 token。

二者共享 `_last_review` / `_turns_since_review` 节流状态，受 `_review_lock` 互斥。

## Quick Start🚀

### 1. 克隆与安装

```bash
git clone <your-repo-url> E.V
cd E.V
python -m venv runtime
runtime\Scripts\activate   # Windows
pip install -e .
```

桌宠模式额外依赖放在 `vendor_pet/` 下，仅桌宠启动时会注入 `sys.path`，不影响主环境。

### 2. 准备 GPT-SoVITS 模型

将训练好的 GPT-SoVITS 模型放入 `gsv_tts/GPT_SoVITS/参考模型/` 目录，并在 `configs/` 中配置模型路径。E.V 默认会按 `tts.engine` 的优先级选择本地 GSV 推理。

### 3. 启动

```bash
# 直播台模式（默认）
run.bat
# 或
python main.py

# 桌宠模式
set RUN_MODE=pet
python main.py
```

`run.bat` 会自动激活 `runtime/` 虚拟环境并启动控制中心（`ui/control_center.py`）。

### 4. 连接 VTube Studio

打开 VTube Studio → Settings → Plugins → 启用 WebSocket API，勾选「Allow anonymous connections」。E.V 会自动扫描 `live2d/` 下的模型文件并加载。

## Configuration⚙️

| 配置项 | 类型(默认值) | 说明 |
|---|---|---|
| `RUN_MODE` | `str = "live"` | 运行模式：`live` 直播台 / `pet` 桌宠 |
| `BILI_ROOM_ID` | `int` | B 站房间号（单房间模式） |
| `BILI_ROOM_IDS` | `str` | 多房间模式，逗号分隔，回退到 `BILI_ROOM_ID` |
| `ZHIPUAI_API_KEY` | `str` | 智谱 AI 鉴权 key |
| `EVOLUTION_PERIODIC_INTERVAL` | `int = 1800` | 自我进化周期检查间隔（秒） |
| `SUBTITLE_FADE_OUT_MS` | `int = 2500` | 字幕显示后淡出延时 |

更多配置见 `src/utils/config.py`。

## About🎗️

大模型输出结果将按**原样**提供。由于提示注入攻击等复杂原因，模型有可能输出有害内容。模型输出内容**不代表**项目开发者立场。使用本项目所产生的任何直接或间接后果（包括但不限于账号封禁、内容风险、调用 VTube Studio / 系统 API 导致的状态异常），开发者不承担任何责任。

本项目基于 MIT 协议开源，涉及到再分发时请保留许可文件的副本。

## Acknowledgements🎀

- [Moemu/Muika-After-Story](https://github.com/Moemu/Muika-After-Story) —— 事件循环、状态机、四层记忆、Butler Agent 设计范式
- [chinokikiss/GSV-TTS-Lite](https://github.com/chinokikiss/GSV-TTS-Lite) —— GPT-SoVITS 本地 TTS 推理与个性化音色支持
- [morettt/my-neuro](https://github.com/morettt/my-neuro) —— VTube × 弹幕 × 主动对话的整体范式
- [NevaMind-AI/MemU](https://github.com/NevaMind-AI/MemU) —— 长期记忆层与向量检索基础设施
- [hxdnshx/blivedm](https://github.com/hxdnshx/blivedm) —— B 站直播弹幕协议实现
- [denvring/gsv_tts](https://github.com/denvring/gsv_tts) —— GPT-SoVITS Python SDK 封装
