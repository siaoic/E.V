# E.V 重构方案：参考 DeepSeek Harness 实现「一切皆插件」

> **副标题**：把 E.V 从「核心硬编码 + 工具插件化」重构为「微内核 + 一切皆插件」，做到"不修改代码就能直接接入新 LLM / TTS / Avatar / 弹幕协议 / 记忆后端"。
>
> **参考基线**：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) 的"Everything is a Plugin"架构（Cordis 微内核 + 配置层自由组合 + 插件生态）。
>
> **目标版本**：E.V 5.0（基于 E.V 当前 main 分支，`main.py` + `src/core/` + `plugins/`）。

---

## 0. TL;DR（一页纸摘要）

| 维度 | 当前 E.V（4.x） | 目标 E.V（5.0） | 借鉴 deepseek-harness |
|---|---|---|---|
| 内核定位 | RuntimeContext 持有 11 个硬编码组件 | 纯插件生命周期 + 事件分发，零业务逻辑 | Cordis kernel（只管挂载/卸载/依赖） |
| LLM 切换 | 改 `LLMBrain`，加 openai 兼容端点要写代码 | `model.openai_compat` 插件即装即用 | `plugin-model-openai`、`plugin-model-anthropic` |
| TTS 切换 | `TTSEngine` 写死 GPT-SoVITS | `tts.gpt_sovits` / `tts.voicevox` / `tts.edge` 插件 | `plugin-tts-*` |
| Avatar 切换 | `VTSController` 写死 VTube Studio | `avatar.vts` / `avatar.live2d_py` / `avatar.static` 插件 | `plugin-avatar-*` |
| 弹幕协议 | `src/danmaku/` 写死 B 站 blivedm | `danmaku.bilibili` / `danmaku.twitch` / `danmaku.youtube` 插件 | 一类能力 = 一类插件包 |
| 记忆后端 | `tools/memory/memu/` 写死 | `memory.memu` / `memory.chroma` / `memory.lance` 插件 | `plugin-memory-*` |
| 主动对话 | `src/llm/proactive.py` 写死 | `proactive.idle_topic` / `proactive.event_driven` 插件 | 行为逻辑也可插拔 |
| 配置组合 | `.env` + `config.yaml`（按字段覆盖） | `profiles/<name>/profile.yaml` 声明插件集合 | 4 种 preset 自由组合 |
| 插件安装 | 只能放在 `plugins/<dir>/`，重启才生效 | `ev plugin add github:user/repo`，热加载 | `dsh plugin --profile web add ...` |
| 会话可追溯 | `console` 打印，无结构化日志 | append-only session log（每条 context 注入都记） | Trajectory 视图 |
| 工作量估算 | — | 约 6-8 个 Sprint，2-3 人/月 | — |

**一句话总结**：把 E.V 当成一个 AI 虚拟主播运行时 SDK，零业务、零默认实现；所有"会用到的能力"都是插件，由用户通过 profile 装配。

---

## 1. 现状诊断：E.V 已经"半插件化"，但骨架硬编码

### 1.1 已有的好设计（要保留）

E.V 的 `plugins/` 目录已经有相当完整的插件框架：

- `plugins/base.py` 定义了 `Plugin` 基类和 9 个钩子点（`on_init`/`on_start`/`on_user_input`/`on_llm_request`/`on_llm_response`/`on_tts_text`/`on_tts_start`/`on_tts_end`）
- `plugins/manager.py` 实现了扫描/加载/卸载/热重载/启停/钩子分发/工具聚合/事件总线/系统提示注入
- `plugins/context.py` 暴露 `register_tool`/`on`/`emit`/`add_system_prompt_patch`/`call_llm`/`send_message`/`trigger_emotion` 等 17 个 API
- `plugins/tools/` 下已经有 11 个工具插件（screen/sfx/weather/time/skill_loader/curated_memory/memory_tools/diary/session_search/skills）
- 支持编程式 `register(ctx)` 入口（3.11 引入）
- 启用清单在 `enabled_plugins.json`，支持 disabled 显式禁用

**这些是金子，要全保留。** deepseek-harness 的设计哲学就是把这套做对，然后扩展到所有能力。

### 1.2 11 处硬编码（必须打散）

我过了一遍 `src/core/runtime.py`（1270 行）、`src/core/application.py`（303 行）和 `src/adapter/*.py`，列出来 11 处"骨架硬编码"：

| # | 能力 | 当前实现 | 位置 | 硬编码点 |
|---|---|---|---|---|
| 1 | **LLM 大脑** | `LLMBrain` | `src/llm/llm_brain.py` (50KB) | 写死 openai 客户端构造、`LLMBrain` 直接耦合 Butler/Evolution/Memory |
| 2 | **TTS 引擎** | `TTSEngine` | `src/tts/engine.py` | 写死 GPT-SoVITS HTTP 客户端，ref audio 处理 |
| 3 | **Avatar 形象** | `VTSController` | `src/vts/controller.py` | 写死 VTube Studio WebSocket 协议 |
| 4 | **Input 输入源** | `STTEngine` | `src/asr/stt.py` | 写死 SiliconFlow API |
| 5 | **弹幕协议** | `BiliServiceManager` + `bili_loop` | `src/danmaku/` (7 文件) | 写死 B 站 blivedm |
| 6 | **记忆后端** | `memory` (memU) | `tools/memory/memu/` | 写死 memU + numpy O(n) |
| 7 | **情绪引擎** | `VtsEmotionActor` / `PetEmotionActor` | `src/emotion/actor.py` | 写死 Embedding 分类 + VTS 热键映射 |
| 8 | **主动对话** | `ProactiveEngine` | `src/llm/proactive.py` (35KB) | 写死空闲话题种子 + 冷却调度 |
| 9 | **MCP 客户端** | `MCPManager` | `src/mcp/manager.py` | 写死 stdio/HTTP 传输 |
| 10 | **ButlerAgent / 自我进化** | `ButlerAgent` / `EvolutionEngine` | `src/llm/butler_agent.py` / `src/llm/evolution/` | 写死提取/复盘/蒸馏逻辑 |
| 11 | **Agent 调度** | `AgentScheduler` | `src/agent/scheduler.py` | 写死定时任务模型 |

更糟的是 `src/adapter/` 下虽然定义了 4 个抽象基类（`BaseLLMAdapter` / `BaseTTSAdapter` / `BaseAvatarAdapter` / `BaseInputAdapter`），**但没有任何代码真的在用**——`LLMBrain` / `TTSEngine` 等都不继承这些基类，也没人注册到任何 registry 里。所以现状是"基类 + 写死实现 + 不挂桥"。

### 1.3 三个最痛的"用户场景"

1. **想换 LLM provider**（比如从智谱换到 DeepSeek 本地部署）：要改 `src/llm/llm_brain.py` 里的 openai client 构造、可能要改工具调用协议、还要改 ButlerAgent 的描述图像调用。
2. **想接 YouTube/Twitch 弹幕**：要在 `src/danmaku/` 加一整个目录、改 `runtime.py` 的 `start_bili`、改 `output_lock` 的 owner 名、改 UI 的弹幕卡片 HTML。
3. **想换成 Live2D-py 自渲染而不用 VTS**：要在 `src/pet/` 之外开新分支、改 `runtime.py` 的 `setup` 大分支、改所有 `self.vts.xxx` 调用点（20+ 处）。

这三个场景在 5.0 之后，都应该是"装一个插件 + 改 profile.yaml"的事。

---

## 2. 目标架构：5.0 的运行时形状

### 2.1 整体架构图

```
┌────────────────────────────────────────────────────────────────────┐
│                       E.V 5.0 运行时                                │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │              KERNEL  (src/core/kernel.py)                    │  │
│  │   ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐  │  │
│  │   │ PluginRegistry│  │ EventBus    │  │ SlotRegistry     │  │  │
│  │   │ (mount/unmount│  │ (event/hooks│  │ (按能力槽位)    │  │  │
│  │   │  /dependency) │  │   /lifecycle│  │ model/tts/avatar │  │  │
│  │   └──────┬──────┘  └──────┬──────┘  │ memory/danmaku/..│  │  │
│  │          │                │         └──────┬─────────────┘  │  │
│  │          └────────┬───────┘                │                │  │
│  │                   ▼                        ▼                │  │
│  │            ┌──────────────────────────────────┐             │  │
│  │            │       Application (编排层)       │             │  │
│  │            │   wait_input → converse → flush  │             │  │
│  │            └─────────────┬────────────────────┘             │  │
│  └──────────────────────┼────────────────────────────────────┘  │
│                         │                                         │
│            ┌────────────┴────────────┐                           │
│            ▼                         ▼                           │
│  ┌──────────────────┐       ┌──────────────────────┐            │
│  │ Profile 配置     │       │ Session Log          │            │
│  │ (profile.yaml)   │       │ (append-only JSONL)  │            │
│  │ + enabled list   │       │ - system prompt      │            │
│  └──────────────────┘       │ - LLM request/reply  │            │
│                              │ - tool call/result   │            │
│                              │ - context injection  │            │
│                              │ - plugin events      │            │
│                              └──────────────────────┘            │
└────────────────────────────────────────────────────────────────────┘
                              │
                              │ 由 profile.yaml 装配
                              ▼
   ┌────────────────────────────────────────────────────────────┐
   │  插件集（plugins/ 或 ~/.ev/plugins/ 或 pip 装的 ev-plugin-*）│
   │                                                            │
   │  必装（提供默认 profile 即可用）：                           │
   │  - ev-plugin-llm-zhipu / ev-plugin-llm-openai-compat        │
   │  - ev-plugin-tts-gpt-sovits / ev-plugin-tts-edge            │
   │  - ev-plugin-avatar-vts / ev-plugin-avatar-live2d-py        │
   │  - ev-plugin-input-keyboard / ev-plugin-input-stt-sensevoice │
   │  - ev-plugin-danmaku-bilibili                               │
   │  - ev-plugin-memory-memu / ev-plugin-memory-chroma           │
   │                                                            │
   │  可选：                                                     │
   │  - ev-plugin-emotion-vts-embedding / ev-plugin-emotion-llm │
   │  - ev-plugin-proactive-idle / ev-plugin-proactive-event     │
   │  - ev-plugin-mcp-stdio / ev-plugin-mcp-http                 │
   │  - ev-plugin-butler / ev-plugin-evolution                   │
   │  - ev-plugin-scheduler / ev-plugin-pet-renderer             │
   │  - ev-plugin-tools-* （沿用现有 plugins/tools）            │
   │  - ev-plugin-persona-* （人格 SKILL.md）                   │
   └────────────────────────────────────────────────────────────┘
```

### 2.2 与 deepseek-harness 的逐项对照

| deepseek-harness 概念 | E.V 5.0 对应 | 备注 |
|---|---|---|
| **Cordis kernel** | `src/core/kernel.py`（新） | 只做 mount/unmount/dependency/lifecycle，不持有任何业务对象 |
| **Cordis services** | `SlotRegistry` | 按 slot 名（`model`/`tts`/`avatar`/...）注册实现，运行时按 profile 选一个 active |
| **Cordis events** | `EventBus`（升级 `src/core/bus.py`） | 同步 + 异步 handler，加 hook extension point |
| **plugin-model-openai** | `ev-plugin-llm-openai-compat` | 通过 OpenAI 兼容协议接 DeepSeek/智谱/Moonshot/Ollama/... |
| **plugin-tts-* / plugin-avatar-*** | `ev-plugin-tts-*` / `ev-plugin-avatar-*` | 沿用 BaseTTSAdapter / BaseAvatarAdapter 抽象 |
| **plugin-str-replace-editor / plugin-bash** | `ev-plugin-tool-*`（沿用 plugins/tools/） | 工具插件保持原样 |
| **plugin-subagents / plugin-workflows** | `ev-plugin-agent-*` | Agent 调度 / 委派 worker 全部下沉 |
| **plugin-web-ui** | `ev-plugin-ui-control-center` | 控制中心 PySide6 UI 变成"可选插件" |
| **plugin-skills** | `ev-plugin-skill-loader`（沿用 plugins/tools/skills.py） | SKILL.md 按目录扫描已实现 |
| **plugin-planning / plugin-goals** | `ev-plugin-proactive-*` | 主动对话引擎抽象成多实现 |
| **plugin-retrieval** | `ev-plugin-memory-*` | 记忆后端抽象 |
| **plugin-sandbox** | `ev-plugin-sandbox-local` | 当前是裸运行，未来 sandbox 隔离可插拔 |
| **~/.dsh/profiles/<name>/** | `~/.ev/profiles/<name>/profile.yaml` | profile 声明插件集合 + slot 绑定 |
| **`dsh plugin add`** | `ev plugin add <github:user/repo \| pypi:pkg>` | 装完热加载，复制到用户插件目录 |
| **Trajectory view** | `~/.ev/sessions/<id>.jsonl` + `ev session show <id>` | append-only 日志 + 检索/重放 |
| **Standard / Code / Minimal / Creator** | `live` / `pet` / `headless` / `creator` | 4 个内置 profile，复用 dsh 命名 |
| **`@dsh/plugin-*` npm 包** | `ev-plugin-*` pip 包 | 同名规范 |
| **`$DSH_HOME/.credentials.yaml`** | `~/.ev/credentials.yaml` | 加密可后续迭代 |

---

## 3. 核心抽象：Slot Registry（一切皆插槽）

这是整个重构的"心脏"，从 deepseek-harness 的"插件可挂载到不同 slot"抽象而来。

### 3.1 Slot 类型定义（`src/core/slots.py`，新文件）

```python
"""Slot Registry：按能力槽位注册实现，运行时按 profile 选 active。

每个 slot 对应一类可替换的能力（model / tts / avatar / memory / ...）。
插件在 on_init 里调 ctx.slots.register(SLOT_NAME, instance)；
编排层调 ctx.slots.get(SLOT_NAME) 拿到当前 profile 选中的实现。

对照 deepseek-harness：Cordis 允许一个插件注册多个 service/extension；
E.V 5.0 用 Slot 把这种"多实现并存+按需选一"的模式显式化。
"""

from __future__ import annotations
from enum import Enum
from typing import Any, Callable


class SlotName(str, Enum):
    LLM = "model"            # 取代 LLMBrain → BaseLLMAdapter 实例
    TTS = "tts"              # 取代 TTSEngine → BaseTTSAdapter 实例
    AVATAR = "avatar"        # 取代 VTSController → BaseAvatarAdapter 实例
    INPUT = "input"          # 取代 STTEngine → BaseInputAdapter 实例（可选）
    DANMAKU = "danmaku"      # 取代 BiliServiceManager → DanmakuSource 实例
    MEMORY = "memory"        # 取代 tools/memory → MemoryBackend 实例
    EMOTION = "emotion"      # 取代 emotion_actor → EmotionEngine 实例
    PROACTIVE = "proactive"  # 取代 ProactiveEngine → ProactiveEngine 实例
    MCP = "mcp"              # 取代 MCPManager → MCPClientBackend 实例
    BUTLER = "butler"        # 取代 ButlerAgent → ButlerAgent 实例
    EVOLUTION = "evolution"  # 取代 EvolutionEngine → EvolutionEngine 实例
    SCHEDULER = "scheduler"  # 取代 AgentScheduler → SchedulerBackend 实例
    SANDBOX = "sandbox"      # 文件/命令执行隔离（可选，未来）
    UI = "ui"                # 控制中心 PySide6（可选，headless 模式不装）
    PET_RENDERER = "pet"     # 桌宠渲染（可选，live 模式不装）
    SESSION = "session"      # 会话存储后端（默认 JSONL，可换 SQLite/Postgres）
    CREDENTIALS = "credentials"  # 凭证存储（默认 yaml，可换 keyring）


# 每个 slot 有"接口契约"——即期望的 Python Protocol（鸭子类型）
# 不强制继承，只要满足这些方法即可注册（深 seek-harness 风格）：
class LLMContract(Protocol):
    name: str
    async def chat_stream(self, text: str, *, proactive=False, history=None): ...
    def push_turn_context(self, contexts: list[str]) -> None: ...
    def reload_client(self) -> None: ...
    # （其他由插件按需实现：tool_call、image_understand 等）

class TTSContract(Protocol):
    async def start(self) -> bool: ...
    async def speak(self, text: str) -> None: ...
    # ... 沿用 src/adapter/tts.py 的抽象方法

# 其他契约用同样的方式定义（不强制继承 base，Protocol 即可）


class SlotRegistry:
    """每个 slot 允许多个实现注册；运行时根据 profile.slot_bindings 选 active。"""

    def __init__(self):
        # slot_name -> {impl_name: instance}
        self._registry: dict[SlotName, dict[str, Any]] = {}
        # profile 选中的当前活跃实现：slot_name -> impl_name
        self._active: dict[SlotName, str] = {}
        # 切换 active 的回调（hot reload 时清旧引用、建新连接）
        self._on_activate: list[Callable] = []

    def register(self, slot: SlotName, impl_name: str, instance: Any) -> None:
        if slot not in self._registry:
            self._registry[slot] = {}
        if impl_name in self._registry[slot]:
            raise ValueError(f"{slot.value} 已存在实现 {impl_name}")
        self._registry[slot][impl_name] = instance

    def unregister(self, slot: SlotName, impl_name: str) -> None:
        if impl_name in self._registry.get(slot, {}):
            del self._registry[slot][impl_name]

    def activate(self, slot: SlotName, impl_name: str) -> Any:
        impls = self._registry.get(slot, {})
        if impl_name not in impls:
            raise KeyError(f"{slot.value} 没有实现 {impl_name}（已注册：{list(impls)}）")
        # 旧的：通知（让编排层释放资源，如断开旧 VTS）
        if slot in self._active:
            old = self.get(slot)
            if old is not None and hasattr(old, "on_deactivate"):
                try: old.on_deactivate()
                except Exception: pass
        self._active[slot] = impl_name
        new = impls[impl_name]
        if hasattr(new, "on_activate"):
            try: new.on_activate()
            except Exception: pass
        for cb in self._on_activate:
            try: cb(slot, impl_name, new)
            except Exception: pass
        return new

    def get(self, slot: SlotName) -> Any | None:
        name = self._active.get(slot)
        if name is None: return None
        return self._registry.get(slot, {}).get(name)

    def get_all(self, slot: SlotName) -> dict[str, Any]:
        return dict(self._registry.get(slot, {}))

    def active_names(self) -> dict[SlotName, str]:
        return dict(self._active)
```

### 3.2 编排层不再 import 任何具体类

迁移后，`src/core/runtime.py` 不再写：

```python
# 老代码
from src.llm.llm_brain import LLMBrain
from src.tts.engine import TTSEngine
from src.vts.controller import VTSController
self.brain = LLMBrain(...)
self.tts = TTSEngine()
self.vts = VTSController()
```

而是从 slot 里取：

```python
# 新代码
self.llm = self.kernel.slots.get(SlotName.LLM)
self.tts = self.kernel.slots.get(SlotName.TTS)
self.avatar = self.kernel.slots.get(SlotName.AVATAR)
```

应用代码（`stream.converse` / `output_lock` / `proactive`）的接口签名不变（`tts.speak(text)` / `face.inject_parameters` 等等），只是从"指向 TTSEngine"变成"指向 SlotRegistry.get(SlotName.TTS)"，调用方完全无感。

### 3.3 Profile 决定激活谁（`src/core/profile.py`，新文件）

```yaml
# ~/.ev/profiles/bili-live/profile.yaml
name: bili-live
description: 智谱 + GPT-SoVITS + VTS + B 站弹幕的标准直播配置
extends: builtin:standard    # 继承内置 standard profile

# 1) 装哪些插件（路径 / 远端 / pip 包）
plugins:
  builtin:                  # 仓库内置 plugins/ 下的目录
    - tools                 # 沿用现有 plugins/tools（screen/sfx/weather/...）
    - mindcraft
    - neuro-sdk
  pypi:
    - ev-plugin-llm-openai-compat
    - ev-plugin-tts-gpt-sovits
    - ev-plugin-avatar-vts
    - ev-plugin-input-stt-sensevoice
    - ev-plugin-danmaku-bilibili
    - ev-plugin-memory-memu
    - ev-plugin-emotion-vts-embedding
    - ev-plugin-proactive-idle
    - ev-plugin-mcp-stdio
    - ev-plugin-butler-default
    - ev-plugin-evolution-default
    - ev-plugin-scheduler-file
    - ev-plugin-ui-control-center
  git:
    - github:mycorp/ev-plugin-llm-deepseek-local

# 2) 每个 slot 选哪个实现
slots:
  model: openai_compat-zhipu
  tts: gpt_sovits-main
  avatar: vts-8001
  input: stt-sensevoice-cloud
  danmaku: bilibili-room-123456
  memory: memu-numpy
  emotion: vts_embedding-qwen3
  proactive: idle_topic
  mcp: stdio-python
  butler: default
  evolution: default
  scheduler: file-json
  ui: control_center
  pet_renderer: null          # live 模式不装桌宠

# 3) 插件实例化参数（透传给插件 on_init 的 config dict）
plugin_config:
  "ev-plugin-llm-openai-compat":
    base_url: "${ZHIPUAI_BASE_URL}"
    api_key: "${ZHIPUAI_API_KEY}"
    model: "glm-4.7-flash"
    thinking: true
  "ev-plugin-tts-gpt-sovits":
    server_url: "http://127.0.0.1:8000"
    ref_audio: "tools/gsv_tts/参考模型/ref.wav"
    ref_text: "参考文本"
  "ev-plugin-danmaku-bilibili":
    room_ids: [123456]
    sessdata: "${BILI_SESSDATA}"
  # ... 其他插件的配置
```

加载流程：

```python
# src/core/profile.py
class Profile:
    def __init__(self, path: str): ...
    def resolve(self) -> dict:  # 处理 extends + env var 替换
        ...

# src/core/kernel.py
class Kernel:
    def __init__(self, profile_path: str):
        self.profile = Profile(profile_path).resolve()
        self.slots = SlotRegistry()
        self.event_bus = EventBus()
        self.session_log = SessionLog()  # append-only JSONL
        self.plugin_manager = PluginManager(self)
        self.lifecycle = Lifecycle(self)  # on_init/on_start/on_stop 编排

    async def boot(self):
        # 1. 解析 profile
        # 2. 装插件（远端先 pip install / git clone 到 ~/.ev/plugins/）
        # 3. 启动插件：on_init → register(slot) → on_start
        # 4. 按 profile.slots 激活每个 slot
        # 5. 启动后台任务（弹幕监听、proactive tick、evolution tick）
        ...
```

---

## 4. 插件接口规范：从 "Plugin 基类" 进化为 "apply(ctx)"

### 4.1 两种入口都支持（向后兼容）

deepseek-harness 的精髓之一：插件就是"调 `ctx.xxx` 的代码"。E.V 5.0 沿用这个思路，并兼容 E.V 4.x 现有的 `Plugin` 基类。

```python
# 风格 1：编程式 register(ctx)（deepseek-harness 风格，新增）
def register(ctx: PluginContext) -> None:
    """插件入口：ctx 是当前插件可用的全部能力。"""
    # 1) 把自己注册到对应 slot
    ctx.slots.register(
        SlotName.LLM,
        impl_name="openai_compat-zhipu",
        instance=OpenAICompatAdapter(ctx.config),
    )
    # 2) 注册工具
    ctx.tools.register({
        "type": "function",
        "function": {
            "name": "search_danmu",
            "description": "回查最近弹幕",
            "parameters": {"type": "object", "properties": {...}},
        }
    })
    # 3) 注册事件订阅
    ctx.on("danmaku.received", on_danmaku)
    # 4) 注册钩子
    ctx.register_hook("on_user_input", enrich_with_memory)
    # 5) 注册周期任务
    ctx.jobs.every(1800).do(periodic_review)


# 风格 2：继承 Plugin 基类（E.V 4.x 风格，保留）
class MyPlugin(Plugin):
    async def on_init(self):
        self.context.slots.register(SlotName.TTS, "voicevox-default",
                                    VoicevoxAdapter(...))
    async def on_start(self):
        self.context.jobs.every(60).do(self.tick)
    async def on_user_input(self, event):
        event.add_context("背景知识：...")
```

### 4.2 PluginContext 升级（`plugins/context.py`）

把现有 17 个方法保留，**新增 6 个核心 API**：

```python
class PluginContext:
    # === 已有（保留） ===
    def log(self, level, message): ...
    def get_config(self): ...            # 全局 .env + config.yaml
    def get_plugin_config(self) -> dict: ...  # 本插件专属配置
    async def send_message(self, text): ...   # 主动说话（走输出锁）
    async def get_messages(self) -> list: ...
    def add_system_prompt_patch(self, pid, text): ...
    async def call_llm(self, prompt, options=None): ...
    async def show_subtitle(self, text, duration=3000): ...
    async def trigger_emotion(self, emotion): ...
    def register_tool(self, tool_def): ...
    def unregister_tool(self, name): ...
    def register_hook(self, name, fn): ...
    def register_memory_provider(self, provider): ...
    def get_plugin(self, name): ...
    def on(self, event, handler): ...
    def off(self, event, handler): ...
    async def emit(self, event, data): ...

    # === 新增 5.0 ===
    @property
    def slots(self) -> SlotRegistry:
        """把当前实现注册到对应能力槽位，或查询当前 profile 选中的实现。"""
        return self._manager.kernel.slots

    @property
    def jobs(self) -> JobScheduler:
        """声明式周期任务：ctx.jobs.every(30).do(fn)；替代 plugin 自己 asyncio.create_task 循环。"""
        return self._manager.kernel.jobs

    @property
    def session(self) -> SessionLog:
        """append-only 会话日志：ctx.session.append({type:..., payload:...})。"""
        return self._manager.kernel.session_log

    @property
    def config(self) -> ConfigView:
        """本插件专属配置（已合并 env 变量、yaml 字段、default）。"""
        return ConfigView(self._manager.kernel.profile.plugin_config.get(self._plugin_name, {}))

    def register_subcommand(self, name: str, handler, help_text: str = "") -> None:
        """注册 `!name` 控制台子命令（替代在 main_loop 里堆 if user_text.startswith("!xxx")）。"""
        self._manager.kernel.console.register(name, handler, help_text,
                                              plugin=self._plugin_name)
```

### 4.3 钩子扩展点（Hook Extension Points）

E.V 4.x 已经有 9 个钩子；5.0 在 deepseek-harness 风格上扩展到 17 个：

| 钩子名 | 触发时机 | 新增/保留 | 借鉴 dsh 的 |
|---|---|---|---|
| `on_init` | 插件加载 | 保留 | plugin apply 第一阶段 |
| `on_start` | 编排就绪后 | 保留 | plugin ready |
| `on_stop` | 编排关闭前 | 保留 | plugin dispose |
| `on_destroy` | 卸载时 | 保留 | — |
| `on_user_input` | 用户消息发给 LLM 前 | 保留 | tools/pre-execute 思路 |
| `on_llm_request` | LLM 调用前 | 保留 | agent/pre-step |
| `on_llm_response` | LLM 回复后 | 保留 | agent/post-step |
| `on_tts_text` / `on_tts_start` / `on_tts_end` | TTS 三段 | 保留 | — |
| **`on_slot_activate`** | slot 切换时（hot reload） | 新增 | Cordis 切换 service |
| **`on_session_start` / `on_session_end`** | 会话开始/结束 | 新增 | 归档 trigger |
| **`on_danmaku`** | 弹幕到达 | 新增 | 跨插件通信 |
| **`on_emotion_decide`** | 情绪分类时 | 新增 | 让插件覆盖默认分类器 |
| **`on_proactive_decide`** | 主动对话决策时 | 新增 | 决策可被插件覆盖 |
| **`on_config_reload`** | 配置热重载 | 新增 | dynamic config |

---

## 5. 重构蓝图：5 个 Sprint

### Sprint 1（1 周）：微内核 + Slot Registry + 第一个示例插件

**目标**：把"运行时可以没有硬编码组件"这件事跑通，只留 LLM 能力做 demo。

1. 新建 `src/core/kernel.py`（Kernel 类，~200 行）
2. 新建 `src/core/slots.py`（SlotRegistry + SlotName + Contract Protocol，~150 行）
3. 新建 `src/core/profile.py`（Profile 加载 + extends 解析，~200 行）
4. 新建 `src/core/session_log.py`（append-only JSONL，~100 行）
5. 改造 `plugins/context.py`，新增 `slots` / `jobs` / `session` / `config` 4 个属性
6. 改造 `plugins/manager.py`，把 `load_all` 改为"按 profile 装插件"而非"扫目录全装"
7. 写一个示例插件 `plugins/builtin/echo_llm/`，只注册 LLM slot，提供"回声"功能
8. 写 `tests/test_kernel.py` 验证：装插件 → 注册 slot → 切换 active → 卸载

**不破坏现状**：`src/core/runtime.py` 仍能跑（不改），只是新加一个 `ev5` 命令走 Kernel 路径。

### Sprint 2（1.5 周）：把 4 个 Adapter 真接到 Slot

**目标**：LLM / TTS / Avatar / Input 都能通过插件换实现。

1. 改造 `src/adapter/llm.py`：把 `BaseLLMAdapter` 升级为 Protocol（不强制继承）
2. 改造 `src/llm/llm_brain.py`：让 `LLMBrain` 接受一个 `LLMContract` 而不是内部 new OpenAI 客户端
3. 抽 `src/llm/llm_brain.py` 里的"openai 客户端构造"到独立 `ev_plugin_llm_openai_compat/openai_client.py`
4. 同理：抽 `TTSEngine` 里的 HTTP 客户端到 `ev_plugin_tts_gpt_sovits/client.py`
5. 同理：抽 `VTSController` 里的 WebSocket 客户端到 `ev_plugin_avatar_vts/protocol.py`
6. 同理：抽 `STTEngine` 里的音频管线到 `ev_plugin_input_stt_sensevoice/`
7. `src/adapter/avatar.py` / `tts.py` / `input.py` 全部转 Protocol
8. 写 4 个 `ev-plugin-*` 插件包骨架（pypi 化），本地 path 安装测试
9. 写 `tests/test_slot_swap.py`：用 mock 实现切换，验证主流程无感

**兼容性保证**：`LLMBrain` 还能直接被老代码 new（默认参数 + lazy slot fallback），老 .env 不用改。

### Sprint 3（1.5 周）：把 7 个外部服务变成插件

**目标**：弹幕 / 记忆 / 情绪 / 主动对话 / MCP / Butler / 进化都能装卸。

| Sprint 3 子任务 | 抽出的 slot 实现 | 留下的内置插件 |
|---|---|---|
| 弹幕 | `ev-plugin-danmaku-bilibili`（继承 `src/danmaku/`） | 内置保留，作为默认 |
| 记忆 | `ev-plugin-memory-memu` / `ev-plugin-memory-chroma` | memu 抽出来 |
| 情绪 | `ev-plugin-emotion-vts-embedding` | 沿用现有 actor |
| 主动对话 | `ev-plugin-proactive-idle` | 沿用 ProactiveEngine |
| MCP | `ev-plugin-mcp-stdio` / `ev-plugin-mcp-http` | stdio 抽出来 |
| Butler | `ev-plugin-butler-default` | 沿用 ButlerAgent |
| 进化 | `ev-plugin-evolution-default` | 沿用 EvolutionEngine |

每个 slot 的设计：
```python
# ev-plugin-danmaku-bilibili/register.py
class BilibiliDanmakuSource:
    name = "bilibili"

    def __init__(self, config):
        self.room_ids = config["room_ids"]
        self.sessdata = config.get("sessdata")
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._task = None

    async def start(self):
        """抽象方法：启动监听 + 后台任务入队。"""
        from src.danmaku.bili_danmaku import BiliServiceManager
        from src.danmaku.client import bili_loop
        self._mgr = BiliServiceManager(self.room_ids, 8766, html)
        self._mgr.attach_client_starter(bili_loop)
        self._mgr.start()
        # 监听 → 推 ctx.session.append({type:"danmaku", payload:...}) + emit("danmaku.received")
        self._task = asyncio.create_task(self._loop())

    async def stop(self): ...
    def on_danmaku(self, callback): ...   # 让编排层 / 其他插件订阅
```

主循环改为订阅 `danmaku.received` 事件：

```python
# src/core/application.py（5.0）
async def _main_loop(self, ...):
    while True:
        user_text = await input_handler.wait_input(show_prompt=True)
        # ... 用户/语音/键盘 都是 InputEvent 走同一管线

# 弹幕不再硬编码在 runtime.start_bili，而是由 danmaku 插件 emit 事件
@bus.on("danmaku.received")
async def on_danmaku(item):
    await chat_handler.converse_danmaku(item)
```

### Sprint 4（1 周）：Profile + 插件生态 CLI

1. 新建 `src/core/profile.py`（Profile 加载 + extends + env 替换）
2. 新建 `src/cli/ev.py`（命令行入口）
3. 命令清单：
   ```
   ev profile list                    # 列出可用 profile
   ev profile use <name>              # 切换默认 profile
   ev profile new <name>              # 基于当前 profile 派生
   ev plugin list                     # 列出已装插件
   ev plugin add <github:u/r | pypi:pkg | path>
   ev plugin remove <name>
   ev plugin enable / disable <name>
   ev plugin reload <name>            # 热重载
   ev plugin info <name>              # 显示 metadata + 实现的 slot
   ev session show <id>               # 查看会话日志
   ev session replay <id>             # 回放
   ev run [--profile <name>]          # 启动主程序
   ```
4. `ev plugin add` 实现：从 GitHub 拉（`git clone --depth 1` 到 `~/.ev/plugins/`） / 从 PyPI 装（`pip install`） / 从本地 path 装（软链） / 从 git URL 装
5. 内置 4 个 profile：
   - `live`（vTuber 直播台）—— 当前 main 分支
   - `pet`（桌宠）—— 当前 RUN_MODE=pet
   - `headless`（无 UI，纯 CLI）—— 跑一次性任务
   - `creator`（Creator Mode：runtime inspection + 插件实验）—— 借鉴 dsh

### Sprint 5（1 周）：会话可追溯 + 兼容性收尾

1. `SessionLog` 把每条系统提示 / 消息 / 工具调用 / 上下文注入 / 插件事件都写入 `~/.ev/sessions/<id>.jsonl`
2. `ev session show <id>` 提供树形/时间线视图
3. `ev session replay <id>` 重新跑一次（可指定不同 profile 跨环境对照）
4. 保留 4.x 行为：`main.py` 不传 profile 时默认用 `live`，跑出来的结果与 4.x 完全一致
5. 文档站：`docs/5.0/` 写 6 篇：
   - "如何写一个 LLM provider 插件"
   - "如何写一个 TTS 插件"
   - "如何写一个弹幕源插件"
   - "Profile 怎么写"
   - "插件发布到 PyPI 的流程"
   - "从 4.x 迁移到 5.0"

**总计**：6.5 周（按 1 人计算）；2 人 3 周可并行做完。

---

## 6. 关键代码示例：5 个最有代表性的迁移

### 示例 1：写一个 LLM provider 插件（10 行核心 + 100 行实现）

```python
# ev-plugin-llm-openai-compat/ev_plugin_llm_openai_compat/__init__.py
from .adapter import OpenAICompatAdapter

def register(ctx):
    cfg = ctx.config
    adapter = OpenAICompatAdapter(
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        model=cfg["model"],
        thinking=cfg.get("thinking", False),
    )
    impl_name = f"openai_compat-{cfg['model']}"
    ctx.slots.register(SlotName.LLM, impl_name, adapter)
    ctx.log("ok", f"已注册 LLM 槽位：{impl_name}")


# ev-plugin-llm-openai-compat/ev_plugin_llm_openai_compat/adapter.py
from openai import AsyncOpenAI
from typing import AsyncIterator

class OpenAICompatAdapter:
    """所有 OpenAI 兼容端点都走这个：DeepSeek/智谱/Moonshot/Ollama/...。"""
    name = "openai_compat"

    def __init__(self, base_url, api_key, model, thinking=False):
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.thinking = thinking
        self._turn_contexts: list[str] = []

    async def chat_stream(self, text, *, proactive=False, history=None):
        messages = list(history or [])
        if self._turn_contexts:
            sys_patch = "\n".join(self._turn_contexts)
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] += "\n" + sys_patch
            else:
                messages.insert(0, {"role": "system", "content": sys_patch})
        self._turn_contexts.clear()
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            extra_body={"thinking": {"type": "enabled"}} if self.thinking else None,
        )
        async for chunk in resp:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def push_turn_context(self, contexts):
        self._turn_contexts.extend(contexts)

    def reload_client(self):
        # 配置热更新时由编排层调
        self.client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)
```

**对比 4.x**：用户换 LLM provider 不再需要 `git clone` E.V 仓库、找 `LLMBrain.chat_stream` 改几处——`pip install ev-plugin-llm-deepseek-local` + 改 profile.yaml 一行，重启即可。

### 示例 2：写一个弹幕源插件（Twitch）

```python
# ev-plugin-danmaku-twitch/ev_plugin_danmaku_twitch/__init__.py
import twitchio
from twitchio.ext import commands

def register(ctx):
    cfg = ctx.config
    src = TwitchDanmakuSource(
        oauth=cfg["oauth"], channel=cfg["channel"],
        on_danmaku=lambda item: ctx.emit("danmaku.received", item),
    )
    ctx.slots.register(SlotName.DANMAKU, f"twitch-{cfg['channel']}", src)


class TwitchDanmakuSource:
    name = "twitch"
    def __init__(self, oauth, channel, on_danmaku):
        self.bot = commands.Bot(token=oauth, prefix="!", initial_channels=[channel])
        self.bot.listen("message")(self._wrap(on_danmaka))
        self.on_danmaku = on_danmaku
    async def start(self):
        await self.bot.start()
    async def stop(self):
        await self.bot.close()
```

**主循环无感**：`@bus.on("danmaku.received")` 已经接管，原来 B 站专用代码全部砍掉。

### 示例 3：Profile 文件 + ev 命令

```bash
# 用户视角的完整流程
$ pip install ev-cli
$ ev profile new my-bili-live --base standard
$ ev plugin add github:mycorp/ev-plugin-tts-voicevox
$ ev plugin add pypi:ev-plugin-danmaku-youtube
$ vim ~/.ev/profiles/my-bili-live/profile.yaml
  # 改 slots.tts: voicevox-default
  # 改 slots.danmaku: youtube-channel-UCxxx
  # 加 plugin_config."ev-plugin-tts-voicevox": { speaker: 42 }
$ ev run --profile my-bili-live
```

### 示例 4：保留 4.x 兼容路径

```python
# src/core/runtime.py（5.0 之后）
class RuntimeContext:
    """5.0 仍保留为 fallback：当没装 Kernel 模式插件时，仍能跑 4.x 行为。"""

    def __init__(self, cfg, kernel=None):
        self.cfg = cfg
        self.kernel = kernel
        # === 兼容 4.x：把"slot 里的实现"反映到 self.xxx 上 ===
        # 这样 src/core/handlers/*.py、src/llm/stream.py 等老代码不用改
        self.brain = LLMBrain(mcp=None)   # 老代码路径
        self.tts = TTSEngine() if cfg.GPTSOVITS_REF_AUDIO else None
        self.vts = VTSController()
        # ... 其他组件默认构造

    def _hydrate_from_slots(self):
        """编排层启动时调一次：用 slot 里的实现覆盖 self.xxx。"""
        if not self.kernel: return
        for slot, attr in [
            (SlotName.LLM, "brain"),
            (SlotName.TTS, "tts"),
            (SlotName.AVATAR, "vts"),
            (SlotName.DANMAKU, "danmaku_source"),
        ]:
            impl = self.kernel.slots.get(slot)
            if impl is not None:
                setattr(self, attr, impl)
```

这样 4.x 的 `stream.converse(self.brain, ...)` / `await self.tts.speak(...)` 全部还能跑——只是 `self.brain` 现在可能是 `OpenAICompatAdapter` 也可能是 `LLMBrain`，调用方不关心。

### 示例 5：Creator Mode（运行时检视）

借鉴 deepseek-harness 的 Creator mode，提供 `ev inspect` 子命令：

```python
# src/cli/inspect.py
def cmd_inspect(kernel):
    print("=== Active Slots ===")
    for slot, name in kernel.slots.active_names().items():
        impl = kernel.slots.get(slot)
        print(f"  {slot.value:15s} -> {name} ({type(impl).__module__}.{type(impl).__name__})")
    print("\n=== All Registered Implementations ===")
    for slot in SlotName:
        impls = kernel.slots.get_all(slot)
        if impls:
            print(f"  {slot.value}: {list(impls)}")
    print("\n=== Loaded Plugins ===")
    for p in kernel.plugin_manager.get_plugin_list():
        print(f"  {p['name']} v{p['version']} ({p['rel']})")
    print("\n=== Event Subscribers ===")
    for ev, subs in kernel.event_bus.subscribers().items():
        print(f"  {ev}: {len(subs)} handler(s)")

def cmd_doctor(kernel):
    """借鉴 dsh：探测每个 slot 实现的健康度。"""
    for slot in SlotName:
        impl = kernel.slots.get(slot)
        if impl is None:
            print(f"  {slot.value}: (not active)")
            continue
        if hasattr(impl, "health_check"):
            try:
                ok, msg = await impl.health_check()
                mark = "✓" if ok else "✗"
                print(f"  {mark} {slot.value}: {msg}")
            except Exception as e:
                print(f"  ✗ {slot.value}: {e}")
```

`ev inspect` 跑一下，直接看到"我到底在用什么实现"——这是 deepseek-harness 的设计精髓之一。

---

## 7. 兼容与迁移策略

### 7.1 4.x → 5.0 的兼容矩阵

| 4.x 用法 | 5.0 是否兼容 | 迁移路径 |
|---|---|---|
| `.env` 配置 | 完全兼容 | 不变 |
| `config.yaml`（可选） | 完全兼容 | 不变 |
| `python main.py` 启动 | 完全兼容 | 不变（默认 profile=live 跑老路径） |
| `RUN_MODE=pet` | 完全兼容 | profile=pet 自动路由 |
| `plugins/<name>/metadata.json + index.py` | 完全兼容 | 内部走新 PluginManager |
| `register(ctx)` 编程式入口 | 完全兼容 | ctx API 全部沿用 + 新增 |
| `Plugin` 基类继承 | 完全兼容 | 钩子列表 + 新增 |
| `enabled_plugins.json` | 完全兼容 | 不变 |
| 改 `LLMBrain` 换 LLM | **不再需要** | 装插件 + 改 profile |
| 加 `src/danmaku/<site>.py` 接入新弹幕 | **不再需要** | 装 `ev-plugin-danmaku-<site>` |
| 改 `runtime.py` 切换运行模式 | **不再需要** | `ev profile use <name>` |

### 7.2 灰度切换策略

- **阶段 1**（Sprint 1-2）：Kernel 模式作为 opt-in 入口（`ev run` 命令），main.py 不变
- **阶段 2**（Sprint 3-4）：默认 profile 写好，覆盖 4.x 行为
- **阶段 3**（Sprint 5）：发 5.0 标签，4.x 仍接受 bug fix 至 6.0 发布

### 7.3 风险与权衡

| 风险 | 应对 |
|---|---|
| 重构期打断主流程 | Sprint 1-2 不动 `src/core/runtime.py`；新能力走 `ev run` 命令 |
| Slot 切换的冷启动（VTS 重连 5s） | `SlotRegistry.activate` 提供 `graceful=True` 选项，老连接不立即断 |
| 插件依赖地狱 | Slot 契约用 Protocol（鸭子类型），不强制抽象基类；插件按需 import |
| `enabled_plugins.json` 空 | 提供"first-run wizard"：`ev init` 帮用户装默认插件集 |
| PyPI 包名 `ev-plugin-*` 被占 | 加 `ev-` 前缀预防；改用 `evplug-*` 作为备用 |
| 现有 mindcraft / neuro-sdk 插件 | 内置在 `plugins/builtin/` 下，按新规范适配 metadata.json |

---

## 8. 一图总结：E.V 5.0 的运行时

```
                        User → text/voice/danmaku
                                │
                                ▼
              ┌──────────────────────────────────┐
              │   Application (编排，无业务)     │
              │   wait_input → on_user_input     │
              │   → slot(LLM).chat_stream        │
              │   → slot(TTS).speak              │
              │   → slot(AVATAR).inject          │
              └──────────────────────────────────┘
                                │
                ┌───────────────┼───────────────┐
                ▼               ▼               ▼
        ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
        │ Slot:LLM     │ │ Slot:TTS     │ │ Slot:AVATAR  │  ...
        │ impl:        │ │ impl:        │ │ impl:        │
        │ openai_compat│ │ gpt_sovits   │ │ vts-8001     │
        │ -zhipu       │ │ -main        │ │              │
        │ (plugin)     │ │ (plugin)     │ │ (plugin)     │
        └──────────────┘ └──────────────┘ └──────────────┘
                                │
                                ▼
              ┌──────────────────────────────────┐
              │  Kernel: 只管 mount/unmount      │
              │  + 事件分发 + 依赖解析 + 日志    │
              │  + Profile 加载 + 插件 CLI      │
              └──────────────────────────────────┘
                                │
                                ▼
            ~/.ev/profiles/<name>/profile.yaml
            (用户编辑，决定装哪些插件、激活哪个实现)
```

**把 E.V 从"AI 虚拟主播项目"变成"AI 虚拟主播运行时 SDK"。**

用户拿到手的是：
- 一个能跑的空壳（Kernel + 4 个内置 profile）
- 一组官方维护的插件（覆盖 90% 场景）
- 一份写插件的规范（Plugin Contract + Slot Protocol + Profile Spec）
- 一条 `ev plugin add` 命令（接入第三方贡献）

这就是 deepseek-harness 在做的事情，E.V 5.0 也这么做。

---

## 附录 A：完整 Sprint 排期

| Sprint | 周数 | 主要交付 | 关键里程碑 |
|---|---|---|---|
| 1 | W1 | Kernel + Slot + Profile + 1 个示例插件 | `ev run --profile demo` 能跑通回声机器人 |
| 2 | W1.5 | 4 个 Adapter 真接 Slot（LLM/TTS/Avatar/Input） | 装不同 LLM/TTS 插件能切换，老 .env 不变 |
| 3 | W1.5 | 7 个外部服务拆插件（弹幕/记忆/情绪/主动/MCP/Butler/进化） | profile=bilibili 跑出与 4.x 一致行为 |
| 4 | W1 | Profile + CLI + 4 个内置 profile + 插件安装 | `ev plugin add github:u/r` 能跑通 |
| 5 | W1 | SessionLog + 兼容性收尾 + 文档 | 4.x 行为 100% 复现，发 5.0 标签 |

**2 人 × 3 周 = 6 人周 = 1.5 人月**

## 附录 B：参考资料

- [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) —— Everything is a Plugin
- [Cordis](https://github.com/koishijs/koishi/tree/master/packages/core) —— 微内核原型
- [Moemu/Muika-After-Story](https://github.com/Moemu/Muika-After-Story) —— E.V 当前的参考实现（事件循环/状态机/四层记忆/Butler Agent）
- [spatiotemporal composability paper](https://koishijs.com/) —— Cordis 团队的设计哲学
- [EleutherAI/lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness) —— 名字撞车但和本项目无关

---

> **写在最后**：重构最大的风险不是技术，是"想做的事情太多"。本方案最关键的判断是：**只把"切换能力需要的代码"抽出来，不重写业务逻辑**。LLMBrain 里的工具调用循环、EvolutionEngine 里的复盘策略、ProactiveEngine 里的冷却调度——这些都是 E.V 团队的领域知识，不该在重构里动。重构只解决"如何不修改它们就能换实现"的问题。
