# E.V 架构说明

> 本文档描述系统架构、模块依赖关系与核心数据流。
> 详细接口签名见 `docs/api.md`，配置项见 `docs/config.md`。

## 1. 总体架构

E.V 是一个事件驱动的 AI 虚拟主播程序：统一输入（键盘 / 语音识别 / B 站弹幕）→
LLM 对话 → 语音合成 → 数字人形象输出（VTube Studio / 本地桌宠），全程带记忆系统与自我进化能力。

```
+------------------------------------------------------------------------------+
|                          UI / 桌面层                                          |
|   ui/control_center.py（控制中心）  ui/字幕.html   ui/弹幕卡片.html  桌宠窗口   |
+------------------------------+-----------------------------------------------+
                               | stdin 输入 / stdout 日志 / SSE 网页
+------------------------------v-----------------------------------------------+
|                         src/core/application.py（Application 主循环）         |
|   输入等待 → 对话链路 → 会话归档 → 资源清理                          |
+-----+----------+-----------+---------+----------+----------+---------------+
      |          |           |         |          |          |
      v          v           v         v          v          v
  LLMBrain   Proactive  Danmaku   STTEngine  PluginMgr  MindcraftBridge
      |          |           |
      v          +-----------+----------+
  stream.converse / speak_text          |
      |                                  v
      v                          ButlerAgent（记忆管家）
  TTSEngine ── TTSPlayer               |
      |                                  v
      v                             MemoryManager
  FaceDriver（口型/动作）           (tools/memory/memory.py)
      |
      v
  VTSController / PetWidget（形象）
```

## 2. 目录结构（src/）

| 目录 | 职责 | 关键文件 |
|---|---|---|
| `src/core/` | 内核：主循环、输出互斥、统一异常、**事件总线、消息 Schema** | `application.py`、`output_lock.py`、`exceptions.py`、`bus.py`、`events/models.py` |
| `src/adapter/` | **统一适配器抽象层**：LLM / TTS / 形象 / 输入源的标准契约 | `llm.py`、`tts.py`、`avatar.py`、`input.py`、`base.py` |
| `src/llm/` | LLM 大脑、对话流水线、主动对话、记忆管家、自我进化 | `llm_brain.py`、`stream.py`、`proactive.py`、`agent.py`、`evolution.py` |
| `src/tts/` | 语音合成（GPT-SoVITS HTTP 客户端）+ 播放 | `engine.py`、`player.py` |
| `src/vts/` | VTube Studio 连接、口型、表情动作、模型扫描 | `controller.py`、`face_driver.py`、`emotion_actor.py` |
| `src/pet/` | 桌宠模式（live2d-py + PySide6） | `pet_app.py`、`widget.py`、`driver.py` |
| `src/danmaku/` | B 站弹幕（blivedm）、多房间 SSE、弹幕精选 | `bili_danmaku.py` |
| `src/mcp/` | MCP 工具服务器管理（stdio / HTTP） | `manager.py`、`registry.py`、`_base.py` |
| `plugins/` | 插件系统（Python 同进程 async） | `manager.py`、`base.py`、`context.py` |
| `tools/memory/` | 记忆系统（检索 + 蒸馏 + 图谱） | `memory.py`、`memory_graph.py` |
| `src/asr/` | 语音识别（SiliconFlow 云端转写） | `stt.py` |
| `src/emotion/` | 情绪状态 / 语料 / 反应（Embedding 分类） | `actor.py`、`state.py`、`corpus.py` |
| `src/utils/` | 公共工具：配置、日志、字幕、过滤、性能埋点 | `config.py`、`console.py`、`subtitle_server.py` |
| `src/mindcraft/` | MC bot 双向桥（socket.io） | `bridge.py` |

## 3. 模块依赖方向

- 内核不直接依赖外部 SDK：`src/core/application.py` 通过 `src/llm` / `src/tts` / `src/vts` / `src/mcp` 等封装层访问外部服务；
- 统一适配器层 `src/adapter/` 定义外部服务（LLM / TTS / 形象 / 输入源）的抽象契约，
  现有实现 `LLMBrain` / `TTSEngine` / `VTSController` / `STTEngine` 均继承对应基类（行为不变）；
- 依赖单向向下：`application → stream/llm_brain/proactive/plugins/mcp/memory → tts/vts/... → utils`；
- 各封装模块（`tts`、`vts`、`mcp`、`danmaku`）只暴露本模块能力，不反向引用主循环；
- `src/core/bus.py`（事件总线）与 `src/core/events/models.py`（Pydantic Schema）为内核基础设施，
  任意上层可 `subscribe` 事件消费，发布方无需感知订阅者；
- `src/utils` 为最底层公共设施（config / console / subtitle），可被任意模块引用。

## 4. 核心数据流

### 4.1 用户对话（可打断）

```
键盘 input() / 语音识别
   │  (等待输入 _wait_input / _interruptible_converse)
   v
插件 onUserInput（可注入上下文 / 改写 / 拦截）
   │
   v
LLMBrain.chat_stream（工具调用 → 思考 → 流式产句）
   │
   v
stream.converse：逐句 打印(console.chat) → 字幕(sub.push text) → TTS 排队播放 → 口型
   │
   ├─ 用户新输入到达 → TTS interrupt + 取消流 → 回到等待（打断）
   v
on_llm_done → 记忆（add_turn + ButlerAgent 提取落库）+ 自我进化 maybe_review
```

### 4.2 主动对话（LLM 自主开口）

```
互动结束 / 静默期随机间隔到点
   │  ProactiveEngine.heartbeat()
   v
（忙碌抑制 / 弹幕避让 / 话题保护 检查通过）
   │  LLM 自主决策：想不想说、说什么（不写历史）
   v
stream.speak_text → TTS 播报（owner="proactive"，此间输入被丢弃）
```

### 4.3 弹幕回复

```
blivedm 监听（每房间独立线程）
   │  清洗净化 sanitize_external → 脏话过滤（命中即弃）
   v
DanmakuPicker 精选（双堆：最高分 / 踢最低分；同窗口高分批量聚合 ≤3 条）
   │  置 danmaku_pending（主动对话避让）
   v
回复队列 → _chat_danmaku：完整 LLM→TTS→字幕→口型→记忆 链路（owner="danmaku"）
```

### 4.4 会话生命周期

```
启动（main.py → run_with_cleanup）
   → 初始化：渲染目标 → TTS（后台加载）→ 记忆 → LLMBrain → 主动对话 → 插件 → STT → 弹幕
   → 主循环：等待输入 → 对话（Ctrl+C / EOF 退出）
   → 退出：会话归档（摘要 + 蒸馏，20s 超时）→ 停插件/弹幕/字幕/MCP/TTS/面捕/VTS
```

## 5. 关键机制

### 5.1 三方输出互斥

用户对话 / 主动对话 / 弹幕回复通过全局 `asyncio.Lock`（`output_lock.get_output_lock()`）互斥：
- 同一时间只有一方播报，持有者以 `set_output_owner()` 标记身份；
- 主动对话 / 弹幕回复播报期间，键盘 / 语音输入被**直接丢弃**（`is_rejecting_input()`），不缓存；
- 全局状态机（`idle / user_talking / ai_speaking / agent_thinking`）用于忙碌抑制：非空闲状态 agent 不触发；
- 「弹幕回复待播报」标记（`danmaku_pending`）让主动对话在弹幕敲定后避让，防话痨抢话。

### 5.2 事件驱动输入等待

`_wait_input` 不固定轮询：`asyncio.wait` 同时监听 键盘输入 future / STT 识别 future /
主动对话唤醒事件（`_wakeup`）/ 播完事件（`_speak_done`），以「下一个有意义时刻」的精确超时唤醒，
避免空闲期 CPU 空转。

### 5.3 记忆系统

- 每轮对话后 `ButlerAgent` 后台提取关键信息落库（碎片 + 档案）；
- 下次检索：Embedding 向量（默认 SiliconFlow / 可换本地）+ BM25 词法，Rerank 精排（2.0s 超时回退），
  另有 2-gram 片段交集轻量召回注入系统提示；
- 碎片 ≥ 60 条触发 AI 自动蒸馏整合（成功后删旧碎片，先备份）；
- 退出时 `_archive_session` 并行执行 会话摘要 + 蒸馏写回。

### 5.4 自我进化

`EvolutionEngine` 两条触发路径共享节流状态（`_last_review` / `_turns_since_review`）并互斥：
- `maybe_review`：对话后被动触发（节流 + 轮次阈值）；
- `periodic_tick`：后台定期自我提示，仅在「距上次复盘达标且有未复盘新轮次」时补刀。

产出：技能沉淀/修补（带评估闭环，fail-open）、话题进化、行为反思、话术建议、
观众画像（owner+fact，上限 30 条）、GEPA 系统提示词进化（独立 6 小时节流）。

### 5.5 多房间弹幕架构

`BiliServiceManager`：一个 `ThreadingHTTPServer` 共享 + 每房间独立 `_Broadcaster` 与 blivedm 线程；
`/events?room_id=X` 按房间路由（主房间用 `manager.broadcaster` 兜底）；头像字节缓存全局共享。

### 5.6 插件系统

Python 同进程 async 运行时：钩子（onUserInput / onLLMRequest / onLLMResponse / onTTS*）直接以协程被调用，
事件对象直接可读写；工具以 OpenAI function calling 格式注册，与 MCP 工具统一合并（`get_merged_tools`）。

### 5.7 事件总线与适配器层

- **事件总线**（`src/core/bus.py`）：进程内 pub/sub，`bus.emit` 顺序执行订阅者、异常隔离。
  关键节点发布事件：用户/弹幕输入（`EV_USER_INPUT`）、AI 逐句回复（`EV_AI_REPLY`）、
  播报结束（`EV_SPEAKING_END`）、错误（`EV_ERROR`）、会话结束（`EV_SESSION_END`）；
  载荷为 Pydantic 强类型模型（`src/core/events/models.py`，含 `api_version` 字段）。
  新增功能只需 `subscribe`，生产方代码不改。
- **适配器层**（`src/adapter/`）：`BaseLLMAdapter` / `BaseTTSAdapter` / `BaseAvatarAdapter` /
  `BaseInputAdapter` 定义外部服务标准契约，现有实现 `LLMBrain` / `TTSEngine` /
  `VTSController` / `STTEngine` 继承对应基类（抽象方法 = 既有公共方法，行为不变）。
  切换服务（换 LLM / TTS / 数字人）只需新增实现类，上层调用不变。
- **UI 解耦**：控制中心为独立进程，经 QProcess + stdin/stdout 协议与内核通信
  （`console.chat` 的 CHAT_TAG 通道）；事件总线是内核内部契约，未来 WebUI 等前端
  可直接订阅，无需改内核。

## 6. 技术栈

- Python 3.10+（`pyproject.toml` 声明，PyInstaller 可打包）
- LLM：OpenAI 兼容 SDK（zhipuai / deepseek / 任意），多臂老虎机路由可选
- TTS：GPT-SoVITS HTTP 服务端（`tools/gsv_tts/`，`tts.bat` 启动）
- 形象：VTube Studio WebSocket（vtuber 模式）/ live2d-py + PySide6（pet 模式）
- 弹幕：blivedm（`src/danmaku/blivedm/` 内嵌依赖）
- 记忆：SQLite + numpy 向量检索（无第三方向量库）
- 网页层：HTTP + SSE 原生实现（字幕 / 弹幕卡片），无 Web 框架
