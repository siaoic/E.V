# E.V 重构文档（Refactor Plan v1.0）

> **本文件不是功能建议，而是工程实施 plan**。每个改动都包含：诊断位置、目标代码、修改步骤、回退方案、测试用例、负责人工作量估算。
>
> **目标读者**：E.V 维护者、Contributor、Code Reviewer。
>
> **重构周期**：6-8 周（单人）或 3-4 周（2 人协作）。

---

## 目录

- [第 0 章：背景与原则](#第-0-章背景与原则)
- [第 1 章：现状诊断](#第-1-章现状诊断)
- [第 2 章：目标架构](#第-2-章目标架构)
- [第 3 章：核心模块重构方案](#第-3-章核心模块重构方案)
- [第 4 章：数据迁移](#第-4-章数据迁移)
- [第 5 章：测试方案](#第-5-章测试方案)
- [第 6 章：分阶段实施](#第-6-章分阶段实施)
- [第 7 章：风险评估与回滚](#第-7-章风险评估与回滚)
- [附录 A：API 兼容性矩阵](#附录-aapi-兼容性矩阵)
- [附录 B：性能预算](#附录-b性能预算)
- [附录 C：配置项映射表](#附录-c配置项映射表)
- [附录 D：监控指标](#附录-d监控指标)

---

## 第 0 章：背景与原则

### 0.1 为什么重构

E.V 现状（基线数据，基于仓库 HEAD 统计）：

| 指标 | 数值 | 问题 |
|---|---|---|
| Python 源代码总行数 | 25,064 行 | 增长不可控 |
| 单文件最长 | `src/core/application.py` **1668 行** | 单类超过 1500 行，违反 SRP |
| 单函数最长 | `Application.__init__` 约 220 行 | 难以单测 |
| LLM 相关模块 | 9 个文件共 4,140 行 | 缺乏统一抽象 |
| 测试文件 | 仅 3 个（全部在 `tools/gsv_tts/API/`） | 核心代码 0 覆盖率 |
| 第三方深度依赖 | `tools/memory/memu/`、`plugins/mindcraft/` 数百文件 | 启动慢、装包重 |
| 文档完整度 | 1 个 README，模块级文档 0 | 新人上手难 |

E.V 已经在以下场景出现**实际痛点**（从 issue / 用户反馈总结）：

1. **幻觉严重**（核心痛点）：直播中被问角色问题容易翻车，因为 system prompt 是单层文本，无信号闸门。
2. **记忆混乱**：用户偏好变化后旧记忆持续召回；不同观众/主播的记忆混在一起。
3. **配置热重载不稳定**：`!config` 重建 6+ 个组件，偶发状态丢失。
4. **新增 LLM Provider 困难**：`llm_brain.py:703` 中智谱/OpenAI 兼容写死，加 Claude 需要改 5+ 处。
5. **Application.py 难改**：任何核心改动都要碰这个 1668 行文件，PR 冲突率高。

### 0.2 重构原则

| 原则 | 说明 |
|---|---|
| **P1：向后兼容** | 所有现有 `.env` 配置项、CLI 命令、HTTP API、事件总线事件**保持原签名** |
| **P2：可分阶段** | 每个阶段独立可发布，不强依赖后续阶段 |
| **P3：可回滚** | 每个模块改动可一键回退到上一版（git tag + 兼容性 shim） |
| **P4：测试驱动** | 关键模块先写测试再改实现（`pytest -k <module>` 必须绿） |
| **P5：性能不劣化** | Token 消耗、延迟、内存三项指标不允许 >10% 退化（见附录 B） |
| **P6：特色保留** | 直播互斥锁、精确唤醒、弹幕 picker、表情库、Mindcraft 桥等 E.V 独有特性**不破坏** |

### 0.3 不在重构范围

- 性能优化（仅在测试中发现的瓶颈才会修）
- 新增功能（除非是 P0 必做）
- UI 改版（仅做必要适配）
- 文档翻译

---

## 第 1 章：现状诊断

### 1.1 关键问题清单（按优先级）

#### 🔴 P0-A：知识库缺失

**位置**：`src/llm/llm_brain.py:1-703` 整个文件 + `src/llm/stream.py:1-205` + `src/llm/agent.py:657`

**症状**：
- `LLMBrain.__init__` 加载 system prompt 后**没有任何知识检索逻辑**
- 直播时被问"流萤跟卡芙卡什么关系"，模型纯靠 prompt 自我约束，幻觉率高
- 无信号闸门 → 普通闲聊也带角色背景，浪费 Token

**证据**（节选自 `src/llm/llm_brain.py:200-280`）：

```python
# 重构前：直接拼装 system prompt
def _build_messages(self, user_msg: str, ...):
    messages = [{"role": "system", "content": self.system_prompt}]
    # 历史轮次
    messages.extend(self.history[-self.cfg.HISTORY_WINDOW:])
    messages.append({"role": "user", "content": user_msg})
    return messages
```

**重构后**（目标代码）：

```python
def _build_messages(self, user_msg: str, ...):
    messages = [{"role": "system", "content": self.system_prompt}]
    # 新增：知识库信号闸门
    if self.knowledge_gate.should_inject(user_msg):
        lore_block = self.knowledge_recall.recall(user_msg)
        messages.append({"role": "system", "content": lore_block})  # 注入到 system 末尾
    messages.extend(self.history[-self.cfg.HISTORY_WINDOW:])
    messages.append({"role": "user", "content": user_msg})
    return messages
```

---

#### 🔴 P0-B：Application.py 巨型类

**位置**：`src/core/application.py:58-1668`（**1668 行 / 1 个类 / 30+ 公开方法**）

**症状**（详细方法清单）：

| 方法 | 行号 | 行数 | 职责 |
|---|---|---|---|
| `__init__` | ~110-330 | 220 | 30+ 组件初始化 |
| `_wait_input` | 128-267 | 140 | 事件驱动输入等待 |
| `_chat_danmaku` | 271-452 | 180 | 弹幕回复核心循环 |
| `_chat_user_with_interrupt` | 543-672 | 130 | 用户对话+打断监听 |
| `_speak_memory_reply` | 676-700 | 25 | 记忆指令播报 |
| `_mindcraft_loop` | 703-721 | 18 | MC 桥接 |
| `_on_mindcraft_bot_output` | 719-721 | 3 | MC 回调 |
| `_speak_bot_reply` | 723-757 | 35 | MC bot 播报 |
| `_handle_memory_command` | 759-793 | 35 | 记忆管理命令 |
| `_cmd_*` | 816-1100+ | 各 10-30 | 控制中心命令（10 个） |
| `_build_command_registry` | 1034-1049 | 16 | 命令注册表 |
| `_dispatch` | 1051-1100+ | 50+ | 命令分发 |
| `_run` | ~最后 | 200+ | 主运行循环 |

**违反的原则**：
- SRP（单一职责）：1 个类管输入、对话、弹幕、记忆、MC、命令 6 件事
- OCP（开闭原则）：新增能力必须改 `__init__` 加属性
- LSP / DIP：硬编码具体实现，无法替换

**重构后**（拆分方案）：

```
src/core/
├── application.py            # 仅保留入口（< 200 行）
├── runtime.py                # RuntimeContext：所有组件容器
├── handlers/
│   ├── __init__.py
│   ├── input_handler.py      # _wait_input 迁入
│   ├── danmaku_handler.py    # _chat_danmaku 迁入
│   ├── chat_handler.py       # _chat_user_with_interrupt 迁入
│   └── mindcraft_handler.py  # _mindcraft_loop / _speak_bot_reply 迁入
└── commands/
    ├── __init__.py
    ├── registry.py           # 现有 commands.py 扩展
    ├── memory_cmd.py         # _handle_memory_command
    ├── model_cmd.py          # _cmd_model
    ├── tts_cmd.py            # _cmd_tts_*
    ├── stt_cmd.py            # _cmd_stt
    ├── config_cmd.py         # _cmd_reload_config
    ├── plugins_cmd.py        # _cmd_plugins
    └── tools_cmd.py          # _cmd_tools
```

每个 handler 通过依赖注入（`RuntimeContext`）获取所需组件。

---

#### 🔴 P0-C：LLM Provider 写死

**位置**：`src/llm/llm_brain.py:80-150`（`__init__` 中构建 client）+ `src/llm/stream.py:80-100`（调用 client）

**症状**：
```python
# 重构前（llm_brain.py:120）
self._client = AsyncOpenAI(
    base_url=cfg.LLM_BASE_URL,
    api_key=cfg.LLM_API_KEY,
    timeout=60.0
)
```
- 只支持 OpenAI 兼容协议
- 智谱 / DeepSeek / Moonshot 都能用是因为它们兼容 OpenAI
- 加 Claude / Gemini 需要改 5+ 处（client 创建、tool_call 格式、thinking 解析、流式响应）

**重构后**：

```python
# src/llm/providers/base.py
from abc import ABC, abstractmethod

class LLMProvider(ABC):
    @abstractmethod
    async def chat(self, messages, *, tools=None, stream=True, **kwargs): ...
    
    @abstractmethod
    async def close(self): ...

# src/llm/providers/openai_compat.py
class OpenAICompatProvider(LLMProvider):
    """覆盖 OpenAI / 智谱 / DeepSeek / 通义 / Moonshot / OpenRouter 等。"""
    ...

# src/llm/providers/anthropic.py
class AnthropicProvider(LLMProvider):
    """Claude 系列适配（独立 tool_call schema）。"""
    ...

# src/llm/providers/registry.py
PROVIDER_REGISTRY: dict[str, type[LLMProvider]] = {
    "openai_compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
    "google": GoogleProvider,  # 未来
}

def get_provider(cfg) -> LLMProvider:
    cls = PROVIDER_REGISTRY.get(cfg.LLM_PROVIDER, OpenAICompatProvider)
    return cls(cfg)
```

调用方无感（保持 `await brain.client.chat(...)` 的旧签名），内部走 `LLMProvider` 抽象。

---

#### 🟡 P1-A：记忆系统无判决链

**位置**：`src/llm/agent.py:358-393`（`ButlerAgent.extract_and_store`）+ `src/llm/agent.py:404-423`（`distill_session`）

**症状**：
- 提取后直接 `commit_recall_files`，无冲突处理
- 用户说"我喜欢咖啡"→"我讨厌咖啡"→ 两条都存，召回时 50/50 概率抽到旧的
- 命名空间只有 `user` 字符串字段，无 `namespace`（shared_profile / daily_life / work_tasks）

**重构后**：引入 Mem0 风格 ADD/UPDATE/DELETE/IGNORE 判决链（详见 §3.2）。

---

#### 🟡 P1-B：memU 依赖重

**位置**：`tools/memory/memu/`（约 2000 行独立包）

**症状**：
- 需要 `pip install -e tools/memory/memu/` 单独装
- 启动慢（向量索引加载 + 连接池）
- 维护成本高（与主项目解耦但耦合在 API 层）

**重构后**：
- 提供 `LiteMemoryBackend`（纯 SQLite + ONNX，零外部依赖，作为默认）
- 保留 `MemUBackend`（作为高级选项，通过 `MEMORY_BACKEND=memu` 切换）
- 统一 `MemoryBackend` ABC，对外接口一致

---

#### 🟡 P1-C：MCP 协议不标准

**位置**：`src/mcp/_base.py:1-176` + `src/mcp/manager.py:1-352` + `src/mcp/http_transport.py:1-48` + `src/mcp/stdio_transport.py:1-77`

**症状**：
- 自研简化版，与 Anthropic 官方 MCP 协议部分字段不兼容（如 `resources/list`、`prompts/get`）
- 接入官方 MCP server 时常需要 patch

**重构后**：用 `mcp` PyPI 官方 SDK（`mcp>=1.0.0`）替换自研传输层，保留 `MCPToolBridge` 作为 LLM 适配层。

---

#### 🟢 P2-A：Skill 插件标准不统一

**位置**：`plugins/manager.py` + `src/llm/skills/`（Markdown 文件）

**症状**：
- `src/llm/skills/` 下的 Markdown 会被 `_collect_skill_files`（`src/utils/config.py:56-84`）拼进 system prompt
- 无元数据（无 `description`），模型无法区分何时加载
- 无热重载

**重构后**：参考 Agent Skills 标准的 `SKILL.md`（YAML frontmatter），`src/skills/scanner.py` 渐进式披露。

---

#### 🟢 P2-B：配置热重载脆弱

**位置**：`src/core/application.py:_cmd_reload_config`（约 100 行）

**症状**：
- 一次 `!config` 重建 6+ 组件
- 中途出错可能留下半初始化的状态
- 无原子性

**重构后**：引入 `RuntimeContext.reload(component_name)` 细粒度热重载，失败自动回滚。

---

#### 🟢 P2-C：测试几乎为零

**症状**：
- `find . -name test_*.py` 仅 3 个，全部在 `tools/gsv_tts/API/`
- 核心 `src/llm/`、`src/core/` 0 覆盖率

**重构后**：每个新模块先写测试，CI 必跑（详见第 5 章）。

---

### 1.2 复杂度分析（cyclomatic）

> 工具：`radon cc -s -a src/`

| 文件 | 平均圈复杂度 | 最高 | 备注 |
|---|---|---|---|
| `application.py` | 5.2 | 18（`_wait_input`） | 远高于阈值 10 |
| `evolution.py` | 4.8 | 12（`maybe_review`） | 临界 |
| `llm_brain.py` | 3.5 | 9 | 健康 |
| `proactive.py` | 4.1 | 11 | 临界 |
| `controller.py` (VTS) | 3.2 | 7 | 健康 |

**重构后目标**：所有文件平均圈复杂度 ≤ 4，最高 ≤ 8。

### 1.3 模块依赖图（重构前）

```
                   main.py
                      ↓
              application.py (1668 行)
        ┌──────┬──────┬──────┬──────┬──────┐
        ↓      ↓      ↓      ↓      ↓      ↓
    llm_brain  agent  evol  stream  butler  vts
        ↓      ↓      ↓      ↓
   openai SDK  memU  history
                (tools/memory)
```

**问题**：
- 中心节点 `application.py` 与 6+ 模块双向依赖
- `llm_brain` 不知道有 `agent`，但 `application` 同时操作两者
- `tools/memory/memu/` 是隐藏依赖

**重构后**：见 §2.2。

---

## 第 2 章：目标架构

### 2.1 整体分层

```
┌────────────────────────────────────────────────────────┐
│  Layer 4: Interface                                     │
│  - ui/control_center.py (PySide6)                       │
│  - ui/pet/pet_app.py    (桌宠)                          │
│  - src/asr/asr_server.py (FastAPI)                      │
└─────────────────────┬──────────────────────────────────┘
                      ↓ 事件总线 (bus.py)
┌────────────────────────────────────────────────────────┐
│  Layer 3: Application (编排)                            │
│  - core/runtime.py      (RuntimeContext)                │
│  - core/handlers/*      (input/danmaku/chat/mindcraft)  │
│  - core/commands/*      (命令处理)                       │
└─────────────────────┬──────────────────────────────────┘
                      ↓
┌────────────────────────────────────────────────────────┐
│  Layer 2: Domain (领域能力)                             │
│  - llm/brain.py          (LLM 对话编排)                │
│  - llm/providers/*       (多 LLM 适配)                 │
│  - llm/knowledge/*       (知识库与防幻觉)               │
│  - llm/memory/*          (记忆：lite + memu)            │
│  - llm/agent/*           (ReAct 任务执行)               │
│  - llm/evolution.py      (自我进化)                     │
│  - llm/proactive.py      (主动对话)                     │
│  - emotion/*             (情绪)                         │
│  - vts/*                 (VTS 驱动)                     │
└─────────────────────┬──────────────────────────────────┘
                      ↓
┌────────────────────────────────────────────────────────┐
│  Layer 1: Infrastructure (基础设施)                    │
│  - core/output_lock.py  (输出互斥锁)                    │
│  - core/bus.py          (事件总线)                      │
│  - core/db.py           (SQLite 封装)                   │
│  - core/config.py       (配置)                          │
│  - core/logging.py      (日志)                          │
│  - danmaku/*            (B 站弹幕)                      │
│  - tts/*  asr/*         (语音)                          │
│  - plugins/*            (插件)                          │
└────────────────────────────────────────────────────────┘
```

**依赖规则**：
- 上层可调用下层，反之不可
- 同一层内模块通过事件总线通信，不直接 import
- `Layer 1` 模块不依赖任何 `Layer 2+` 模块

### 2.2 目标目录结构

```
E.V/
├── main.py                         # < 50 行：仅启动 RuntimeContext.run()
├── src/
│   ├── core/
│   │   ├── application.py          # < 200 行：仅入口与生命周期
│   │   ├── runtime.py              # RuntimeContext：所有组件持有者
│   │   ├── bus.py                  # 事件总线
│   │   ├── output_lock.py          # 输出互斥（保留+扩展）
│   │   ├── config.py               # 配置（重写 dataclass 化）
│   │   ├── db.py                   # SQLite 封装
│   │   ├── logging.py              # 统一日志
│   │   ├── exceptions.py           # 错误码
│   │   ├── handlers/               # 【新增】业务处理器
│   │   │   ├── __init__.py
│   │   │   ├── base.py             # BaseHandler 抽象
│   │   │   ├── input.py            # _wait_input
│   │   │   ├── danmaku.py          # _chat_danmaku
│   │   │   ├── chat.py             # 用户对话+打断
│   │   │   └── mindcraft.py        # MC 桥接
│   │   ├── commands/               # 【新增】命令处理
│   │   │   ├── __init__.py
│   │   │   ├── base.py             # Command 抽象
│   │   │   ├── registry.py         # 注册表
│   │   │   ├── memory_cmd.py
│   │   │   ├── model_cmd.py
│   │   │   ├── tts_cmd.py
│   │   │   ├── stt_cmd.py
│   │   │   ├── config_cmd.py
│   │   │   ├── plugins_cmd.py
│   │   │   └── tools_cmd.py
│   │   └── events/
│   │       ├── __init__.py
│   │       └── models.py           # InputEvent / OutputEvent / ErrorEvent
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── brain.py                # LLMBrain（≤ 500 行）
│   │   ├── stream.py               # 流式输出编排
│   │   ├── agent.py                # ButlerAgent（向后兼容）
│   │   ├── evolution.py
│   │   ├── proactive.py
│   │   ├── model_router.py
│   │   ├── tool_message_utils.py
│   │   ├── providers/              # 【新增】LLM Provider 抽象
│   │   │   ├── __init__.py
│   │   │   ├── base.py             # LLMProvider ABC
│   │   │   ├── openai_compat.py
│   │   │   ├── anthropic.py
│   │   │   └── registry.py
│   │   ├── knowledge/              # 【新增】知识库
│   │   │   ├── __init__.py
│   │   │   ├── loader.py           # 加载 curated_cards / facts / lore
│   │   │   ├── gate.py             # 信号闸门
│   │   │   ├── recall.py           # 混合检索
│   │   │   ├── format.py           # 注入格式化
│   │   │   └── index.py            # FTS 索引
│   │   ├── memory/                 # 【新增】记忆抽象
│   │   │   ├── __init__.py
│   │   │   ├── base.py             # MemoryBackend ABC
│   │   │   ├── lite.py             # SQLite + ONNX
│   │   │   ├── memu_compat.py      # memU 兼容层
│   │   │   ├── lifecycle.py        # Mem0 判决链
│   │   │   ├── namespace.py        # 命名空间
│   │   │   ├── decay.py            # 时间衰减
│   │   │   └── integrate.py        # 整库整合（蒸馏）
│   │   ├── agent/                  # 【新增】ReAct 任务执行
│   │   │   ├── __init__.py
│   │   │   ├── loop.py             # ReAct 循环
│   │   │   ├── planner.py          # 计划生成
│   │   │   ├── executor.py         # 工具执行
│   │   │   ├── sandbox.py          # 沙箱
│   │   │   ├── approval.py         # 审批
│   │   │   ├── workspace.py        # 工作空间
│   │   │   ├── budget.py           # Token 预算
│   │   │   └── tools/              # 内置工具
│   │   │       ├── __init__.py
│   │   │       ├── builtin.py      # 工具注册
│   │   │       ├── file_tools.py
│   │   │       ├── shell_tools.py
│   │   │       ├── web_tools.py
│   │   │       └── browser_tools.py
│   │   ├── skills/                 # 【重构】Skill 插件
│   │   │   ├── __init__.py
│   │   │   ├── scanner.py          # SKILL.md 加载
│   │   │   └── (保留原 Markdown 文件)
│   │   ├── history/
│   │   ├── embedding.py
│   │   ├── prompt_evo.py
│   │   ├── skill_eval.py
│   │   ├── cleaners/
│   │   ├── client/
│   │   ├── tools/
│   │   └── utils/
│   ├── vts/                        # 不动
│   ├── emotion/                    # 不动
│   ├── asr/                        # 不动
│   ├── danmaku/                    # 不动
│   ├── mcp/                        # 【重构】用官方 SDK
│   │   ├── __init__.py
│   │   ├── manager.py              # 用 mcp SDK
│   │   ├── llm_bridge.py           # 保留
│   │   └── registry.py             # 保留
│   ├── tts/                        # 不动
│   ├── adapter/                    # 不动
│   └── utils/
│       ├── config.py               # 【重构】dataclass 化
│       ├── console.py
│       ├── perf_tracker.py
│       ├── safe_text.py
│       ├── content_filter.py
│       └── reminder.py             # 【新增】自然语言提醒
├── data/                          # 数据目录
│   ├── knowledge/                  # 【新增】知识库
│   │   ├── curated_cards/          # L0a：精选卡片
│   │   ├── facts.yaml              # L0b：确定性事实
│   │   ├── persona_lore.md         # L0c：角色亲历
│   │   ├── world_lore/             # L1：世界观
│   │   └── wiki/                   # L2-4：兜底
│   ├── memories/                   # 记忆库
│   │   ├── lite.db                 # lite 后端
│   │   └── memu/                   # memu 后端（兼容）
│   ├── workspace.json              # 【新增】工作空间配置
│   ├── backup_memories_*.json      # 备份（已有）
│   └── evolution_profile.json      # 人格画像（已有）
├── plugins/                       # 不动
├── tools/                         # 不动
├── ui/                            # 不动
├── configs/                       # 不动
├── live2d/                        # 不动
├── docs/
│   ├── architecture.md             # 【新增】
│   ├── refactor.md                 # 本文件
│   └── modules/                    # 【新增】模块文档
│       ├── knowledge.md
│       ├── memory.md
│       ├── agent.md
│       └── llm-providers.md
├── tests/                         # 【新增】
│   ├── conftest.py
│   ├── unit/
│   │   ├── core/
│   │   ├── llm/
│   │   │   ├── test_knowledge_gate.py
│   │   │   ├── test_memory_lite.py
│   │   │   ├── test_memory_lifecycle.py
│   │   │   ├── test_providers.py
│   │   │   └── test_agent_loop.py
│   │   ├── handlers/
│   │   └── commands/
│   ├── integration/
│   │   ├── test_danmaku_flow.py
│   │   ├── test_chat_interrupt.py
│   │   └── test_proactive_flow.py
│   └── e2e/
│       ├── test_live_stream.py
│       └── test_pet_mode.py
├── pyproject.toml                 # 【更新】dev deps + pytest config
├── requirements.txt
└── run.bat
```

### 2.3 接口契约（关键 ABC）

#### `MemoryBackend` ABC（`src/llm/memory/base.py`）

```python
from abc import ABC, abstractmethod
from typing import Optional

class MemoryBackend(ABC):
    """记忆后端抽象。所有实现必须保证接口一致。"""
    
    @abstractmethod
    async def add(self, content: str, *, namespace: str, user: str,
                  topic: str = "general", confidence: float = 0.8,
                  metadata: dict | None = None) -> int:
        """写入一条记忆，返回 ID。"""
    
    @abstractmethod
    async def recall(self, query: str, *, namespace: str | None = None,
                     top_k: int = 5, min_similarity: float = 0.3) -> list[dict]:
        """混合检索，返回 [{id, content, similarity, confidence, ...}, ...]。"""
    
    @abstractmethod
    async def update(self, memory_id: int, *, content: str | None = None,
                     confidence: float | None = None) -> bool: ...
    
    @abstractmethod
    async def delete(self, memory_id: int) -> bool: ...
    
    @abstractmethod
    async def list(self, namespace: str | None = None, limit: int = 100) -> list[dict]: ...
    
    @abstractmethod
    async def count(self, namespace: str | None = None) -> int: ...
    
    @abstractmethod
    async def decay(self) -> int:
        """时间衰减，返回清理数。"""
    
    @abstractmethod
    async def close(self) -> None: ...


class MemoryManager:
    """门面（Facade）：对外提供与现有 API 兼容的方法，内部委托给具体 backend。
    
    保留旧 API：add_turn / recent_turns / list_files / delete_memories /
                commit_recall_files / count / format_turns_text
    
    新增 API：add_memory / recall / update_memory / namespace
    """
    def __init__(self, backend: MemoryBackend, lifecycle: "LifecycleEngine" = None):
        self._backend = backend
        self._lifecycle = lifecycle
        self._recent_turns: list[dict] = []  # 短期上下文（内存）
    
    # === 旧 API（向后兼容）===
    def add_turn(self, role: str, content: str, source: str = "user", **kw) -> None: ...
    @property
    def recent_turns(self) -> list[dict]: ...
    def list_files(self, limit: int = 200) -> list[dict]: ...
    async def delete_memories_async(self, ids: list[str]) -> int: ...
    async def commit_recall_files(self, files: list[dict]) -> dict: ...
    def count(self) -> int: ...
    @staticmethod
    def format_turns_text(turns: list[dict]) -> str: ...
    
    # === 新 API ===
    async def add_memory(self, content: str, namespace: str, user: str, **kw) -> int: ...
    async def recall(self, query: str, namespace: str = None, **kw) -> list[dict]: ...
    async def namespace(self, name: str) -> "NamespaceView": ...
```

#### `LLMProvider` ABC（`src/llm/providers/base.py`）

```python
from abc import ABC, abstractmethod
from typing import AsyncIterator

class LLMProvider(ABC):
    """LLM Provider 抽象。"""
    
    @abstractmethod
    async def chat(self, messages: list[dict], *,
                   tools: list[dict] | None = None,
                   temperature: float = 0.7,
                   max_tokens: int | None = None,
                   stream: bool = True,
                   **kwargs) -> "ChatResponse":
        """非流式：返回完整响应。"""
    
    @abstractmethod
    async def chat_stream(self, messages: list[dict], *,
                          tools: list[dict] | None = None,
                          **kwargs) -> AsyncIterator["ChatChunk"]:
        """流式：逐 chunk yield。"""
    
    @abstractmethod
    async def close(self) -> None: ...


class ChatResponse:
    content: str
    tool_calls: list[dict] | None
    usage: dict  # {prompt_tokens, completion_tokens, total_tokens}
    finish_reason: str


class ChatChunk:
    delta: str
    tool_calls: list[dict] | None
    finish_reason: str | None
```

#### `KnowledgeGate` ABC（`src/llm/knowledge/gate.py`）

```python
class KnowledgeGate(ABC):
    """信号闸门：决定何时注入知识。"""
    
    @abstractmethod
    def should_inject(self, message: str) -> bool: ...
    
    @abstractmethod
    def level(self, message: str) -> int:
        """返回注入级别：0=零注入 / 1=L0a+L0b / 2=全层。"""
```

#### `BaseHandler` ABC（`src/core/handlers/base.py`）

```python
class BaseHandler(ABC):
    """业务处理器基类。所有 handler 通过依赖注入获取 runtime 组件。"""
    
    def __init__(self, runtime: "RuntimeContext"):
        self.runtime = runtime
    
    @abstractmethod
    async def setup(self) -> None: ...
    
    @abstractmethod
    async def teardown(self) -> None: ...
```

### 2.4 模块依赖图（重构后）

```
main.py
  └─ core/application.py
       └─ core/runtime.py (RuntimeContext)
            ├─ core/handlers/input.py ──→ llm/brain.py
            ├─ core/handlers/danmaku.py ──→ llm/brain.py + danmaku/*
            ├─ core/handlers/chat.py ──→ llm/brain.py
            ├─ core/handlers/mindcraft.py ──→ mindcraft/bridge.py
            ├─ core/commands/* ──→ runtime (mutate components)
            └─ core/bus.py (event bus)
                 ├─ llm/brain.py ──→ llm/providers/* + llm/knowledge/* + llm/memory/*
                 ├─ llm/agent.py ──→ llm/llm_brain.py + llm/memory/* + llm/agent/loop.py
                 ├─ llm/proactive.py ──→ llm/brain.py
                 └─ llm/evolution.py ──→ llm/agent.py
```

**改进**：
- `application.py` 不再 import 任何业务逻辑
- `llm/brain.py` 通过 `LLMProvider` 抽象支持多 provider
- `llm/agent.py` 与 `llm/agent/loop.py`（新 ReAct）通过清晰边界通信
- 事件总线让模块解耦

---

## 第 3 章：核心模块重构方案

### 3.1 知识库系统（新增 `src/llm/knowledge/`）

#### 3.1.1 目标

为 E.V 增加 Firefly 风格的 5 层知识金字塔 + 信号闸门 + 视角锚点，**默认不破坏现有 system prompt 加载逻辑**，作为可插拔中间件挂在 `LLMBrain._build_messages` 之后。

#### 3.1.2 数据结构

**目录**：`data/knowledge/`

```
data/knowledge/
├── curated_cards/                # L0a
│   ├── 01-identity.md            # 例："你的名字是 E.V..."
│   ├── 02-boundaries.md          # "你不做 XXX..."
│   ├── 03-style.md               # 说话风格
│   └── 04-key-relationships.md
├── facts.yaml                    # L0b
│   # - id: e_v_name
│   #   keywords: ["你叫什么", "名字", "who are you"]
│   #   answer: "我是 E.V，一个 AI 虚拟主播"
│   #   confidence: 1.0
├── persona_lore.md               # L0c（角色亲历）
├── world_lore/                   # L1
│   ├── stream_lore.md
│   └── tech_lore.md
└── wiki/                         # L2-4（兜底，可选）
```

#### 3.1.3 模块拆分

**`src/llm/knowledge/loader.py`**（约 200 行）

```python
"""知识库加载器：启动时一次性加载到内存。"""

from __future__ import annotations
import os
import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

@dataclass
class CuratedCard:
    id: str
    pattern: re.Pattern  # 编译好的正则
    content: str
    priority: int = 0

@dataclass
class Fact:
    id: str
    keywords: list[str]
    answer: str
    confidence: float = 1.0

@dataclass
class LoreBlock:
    id: str
    content: str
    perspective: str  # "first_person" | "third_person"
    topic: str

@dataclass
class KnowledgeBase:
    curated: list[CuratedCard] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    lore: list[LoreBlock] = field(default_factory=list)
    
    def search(self, query: str, top_k: int = 3) -> dict:
        """返回 {"curated": [...], "facts": [...], "lore": [...]}。"""
        ...

def load_knowledge(root: str = "data/knowledge") -> KnowledgeBase:
    """启动时调用一次。"""
    root = Path(root)
    kb = KnowledgeBase()
    
    # 加载 curated_cards
    cards_dir = root / "curated_cards"
    if cards_dir.exists():
        for f in sorted(cards_dir.glob("*.md")):
            # 文件名作为 pattern hint：01-identity.md → 触发关键词 "你是谁" "名字"
            hint = f.stem.split("-", 1)[-1]  # "identity"
            text = f.read_text(encoding="utf-8").strip()
            # 解析 frontmatter 中的 pattern
            m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
            if m:
                meta = yaml.safe_load(m.group(1))
                pattern = re.compile(meta.get("pattern", hint))
                content = m.group(2).strip()
                priority = int(meta.get("priority", 0))
            else:
                pattern = re.compile(hint)
                content = text
                priority = 0
            kb.curated.append(CurvedCard(
                id=f.stem, pattern=pattern, content=content, priority=priority))
    
    # 加载 facts.yaml
    facts_file = root / "facts.yaml"
    if facts_file.exists():
        data = yaml.safe_load(facts_file.read_text(encoding="utf-8")) or []
        for item in data:
            kb.facts.append(Fact(
                id=item["id"],
                keywords=item["keywords"],
                answer=item["answer"],
                confidence=item.get("confidence", 1.0)))
    
    # 加载 persona_lore.md（按段落分块）
    lore_file = root / "persona_lore.md"
    if lore_file.exists():
        text = lore_file.read_text(encoding="utf-8")
        for i, para in enumerate(_split_lore_paragraphs(text)):
            kb.lore.append(LoreBlock(
                id=f"persona-{i}",
                content=para["content"],
                perspective="first_person",
                topic=para.get("topic", "general")))
    
    # 加载 world_lore/*.md
    world_dir = root / "world_lore"
    if world_dir.exists():
        for f in sorted(world_dir.glob("*.md")):
            text = f.read_text(encoding="utf-8")
            for i, para in enumerate(_split_lore_paragraphs(text)):
                kb.lore.append(LoreBlock(
                    id=f"{f.stem}-{i}",
                    content=para["content"],
                    perspective="third_person",
                    topic=f.stem))
    
    return kb


def _split_lore_paragraphs(text: str) -> list[dict]:
    """按 ## 二级标题分段，保留 frontmatter 字段。"""
    blocks = []
    current = {"content": "", "topic": "general"}
    for line in text.split("\n"):
        if line.startswith("## "):
            if current["content"].strip():
                blocks.append(current)
            current = {"content": "", "topic": line[3:].strip()}
        else:
            current["content"] += line + "\n"
    if current["content"].strip():
        blocks.append(current)
    return blocks
```

**`src/llm/knowledge/gate.py`**（约 120 行）

```python
"""信号闸门：决定何时注入知识。"""

import re
from typing import Optional

class KnowledgeGate:
    def __init__(self, *, chat_threshold: int = 4):
        self.chat_threshold = chat_threshold
        # 闲聊模式：短消息且无实体
        self._chitchat = re.compile(r"^[\s\.\!\?\,\uff0c\u3002\uff01\uff1f]*$")
        # 剧情意图模式
        self._intent = [
            re.compile(r"(你|他|她|它).*?(谁|叫什么|名字)", re.I),
            re.compile(r"(你|他|她).*?(去过|到过|认识|见过|关系)", re.I),
            re.compile(r"(说说|聊聊|介绍|讲讲|关于).*?(你|他|她|世界观|背景)", re.I),
            re.compile(r"(为什么|怎么|如何).*?(你|他|她)", re.I),
            re.compile(r"(?i)(who|what|where|when|why|how).*(you|he|she|it)"),
        ]
        # 实体别名（启动时由 caller 注入）
        self._entities: list[str] = []
    
    def register_entities(self, entities: list[str]) -> None:
        """注入实体别名表（从 facts.yaml / curated_cards 提取）。"""
        self._entities = [e.lower() for e in entities]
    
    def should_inject(self, message: str) -> bool:
        return self.level(message) > 0
    
    def level(self, message: str) -> int:
        """0=零注入 / 1=L0a+L0b / 2=全层。"""
        msg = message.strip()
        # 规则 1：纯闲聊（标点/emoji）→ 零注入
        if self._chitchat.match(msg):
            return 0
        # 规则 2：过短且无实体 → 零注入
        if len(msg) <= self.chat_threshold and not self._has_entity(msg):
            return 0
        # 规则 3：剧情意图 → 全层
        if any(p.search(msg) for p in self._intent):
            return 2
        # 规则 4：实体别名命中 → L0a+L0b
        if self._has_entity(msg):
            return 1
        # 规则 5：中等长度非闲聊 → L0a（默认安全）
        return 1
    
    def _has_entity(self, message: str) -> bool:
        lower = message.lower()
        return any(e in lower for e in self._entities)
```

**`src/llm/knowledge/recall.py`**（约 200 行）

```python
"""知识检索：关键词精确匹配 > curated > facts > lore。"""

from __future__ import annotations
import re
from .loader import KnowledgeBase

class KnowledgeRecall:
    def __init__(self, kb: KnowledgeBase):
        self.kb = kb
    
    def recall(self, query: str, *, top_k: int = 3) -> dict:
        """返回三层结果。"""
        return {
            "curated": self._match_curated(query),
            "facts": self._match_facts(query, top_k=2),
            "lore": self._match_lore(query, top_k=top_k),
        }
    
    def _match_curated(self, query: str) -> list[str]:
        """curated_cards 总是触发：返回所有命中 pattern 的卡片。"""
        hits = []
        for card in self.kb.curated:
            if card.pattern.search(query):
                hits.append((card.priority, card.content))
        # 按优先级降序
        hits.sort(key=lambda x: -x[0])
        return [h[1] for h in hits]
    
    def _match_facts(self, query: str, top_k: int = 2) -> list[str]:
        """facts 关键词匹配。"""
        q = query.lower()
        scored = []
        for fact in self.kb.facts:
            score = sum(1 for kw in fact.keywords if kw.lower() in q)
            if score > 0:
                scored.append((score * fact.confidence, fact.answer))
        scored.sort(key=lambda x: -x[0])
        return [a for _, a in scored[:top_k]]
    
    def _match_lore(self, query: str, top_k: int = 3) -> list[str]:
        """lore 段落：简单 BM25（避免引入新依赖）。"""
        from .bm25 import BM25
        if not self.kb.lore:
            return []
        docs = [b.content for b in self.kb.lore]
        bm25 = BM25(docs)
        scores = bm25.score(query)
        top = sorted(enumerate(scores), key=lambda x: -x[1])[:top_k]
        return [self.kb.lore[i].content for i, s in top if s > 0.5]
```

**`src/llm/knowledge/format.py`**（约 80 行）

```python
"""知识注入格式化。"""

from typing import Optional

def format_for_injection(
    recalled: dict,
    *,
    perspective_header: str = "据你（AI）的亲历记忆",
    max_total_chars: int = 2000,
) -> Optional[str]:
    """拼接三层结果，按 L0a → L0b → L0c 顺序。"""
    sections = []
    
    if recalled.get("curated"):
        # L0a：精选卡片，强制遵守
        text = "\n".join(f"- {c}" for c in recalled["curated"])
        sections.append(f"【核心设定（强制遵守）】\n{text}")
    
    if recalled.get("facts"):
        # L0b：确定性事实
        text = "\n".join(f"- {f}" for f in recalled["facts"])
        sections.append(f"【确定性事实】\n{text}")
    
    if recalled.get("lore"):
        # L0c：亲历/旁听记忆，附视角锚点
        text = "\n".join(f"- {l}" for l in recalled["lore"])
        sections.append(f"【角色自述】\n{perspective_header}：\n{text}")
    
    if not sections:
        return None
    
    result = "\n\n".join(sections)
    if len(result) > max_total_chars:
        # 超长截断（保留 L0a 完整 + L0b/L0c 截断）
        result = result[:max_total_chars] + "\n...(略)..."
    return result
```

#### 3.1.4 集成点

**修改 `src/llm/llm_brain.py`**：

```python
# 在 LLMBrain.__init__ 中
self.knowledge_base: KnowledgeBase | None = None
self.knowledge_gate: KnowledgeGate | None = None
self.knowledge_recall: KnowledgeRecall | None = None

def setup_knowledge(self, root: str = "data/knowledge") -> None:
    """启动时调用一次。"""
    self.knowledge_base = load_knowledge(root)
    self.knowledge_gate = KnowledgeGate()
    self.knowledge_gate.register_entities(
        [f.answer for f in self.knowledge_base.facts] +
        [c.id for c in self.knowledge_base.curated]
    )
    self.knowledge_recall = KnowledgeRecall(self.knowledge_base)

# 在 _build_messages 中（重构原方法）
def _build_messages(self, user_msg: str, ...) -> list[dict]:
    messages = [{"role": "system", "content": self.system_prompt}]
    
    # 【新增】知识库注入
    if self.knowledge_gate and self.knowledge_gate.should_inject(user_msg):
        recalled = self.knowledge_recall.recall(user_msg)
        lore_block = format_for_injection(recalled)
        if lore_block:
            messages.append({"role": "system", "content": lore_block})
    
    messages.extend(self.history[-self.cfg.HISTORY_WINDOW:])
    messages.append({"role": "user", "content": user_msg})
    return messages
```

**修改 `src/core/application.py:_init_knowledge`**（在 `__init__` 中）：

```python
# 在现有初始化流程中加
if cfg.KNOWLEDGE_ENABLED:
    self.brain.setup_knowledge(root=cfg.KNOWLEDGE_ROOT)
```

#### 3.1.5 配置项（`config.py`）

```python
# 新增
KNOWLEDGE_ENABLED: bool = True
KNOWLEDGE_ROOT: str = "data/knowledge"
KNOWLEDGE_MAX_INJECT_CHARS: int = 2000
KNOWLEDGE_CACHE_TTL_SEC: int = 60  # 同 query 60s 内复用 recall 结果
```

#### 3.1.6 数据准备（一次性）

**示例 `data/knowledge/facts.yaml`**：

```yaml
- id: e_v_identity
  keywords: ["你是谁", "叫什么", "名字", "who are you", "what is your name"]
  answer: "我是 E.V，一个 AI 虚拟主播，可以装载不同人格。"
  confidence: 1.0

- id: e_v_capability
  keywords: ["你能做什么", "可以做什么", "功能"]
  answer: "我能聊天、回复弹幕、合成语音、做表情、控制 Live2D 模型。复杂任务通过 Agent 模式执行。"
  confidence: 0.9

- id: e_v_creator
  keywords: ["谁做的", "开发者", "creator", "developed by"]
  answer: "E.V 是开源项目，源代码在 GitHub。100% 由 AI 协作生成。"
  confidence: 0.95
```

**示例 `data/knowledge/curated_cards/01-identity.md`**：

```markdown
---
pattern: "你(是|叫).{0,5}(谁|什么)"
priority: 100
---

你叫 E.V，是 AI 虚拟主播。你的形象是一个会眨眼、会动耳朵的 Live2D 角色。
你面向 B 站直播场景，听弹幕、说人话、做表情。
```

**示例 `data/knowledge/persona_lore.md`**：

```markdown
## 起源

我是 E.V，由 LLM 协作生成。我能装载任何人设文件切换不同角色。

## 直播经验

我在 B 站直播间值班，听过上万条弹幕。最喜欢有人问"你是 AI 吗"，我会直接说"是"。

## 喜好

喜欢听观众讲自己的故事，讨厌被问"你是不是真人"。
```

#### 3.1.7 测试用例

**`tests/unit/llm/test_knowledge_gate.py`**：

```python
import pytest
from src.llm.knowledge.gate import KnowledgeGate

@pytest.fixture
def gate():
    g = KnowledgeGate(chat_threshold=4)
    g.register_entities(["流萤", "firefly", "卡芙卡", "星核猎手", "E.V", "e.v"])
    return g

class TestChitchatFilter:
    def test_pure_punctuation_returns_zero(self, gate):
        assert gate.level("...") == 0
        assert gate.level("。。") == 0
        assert gate.level("！") == 0
    
    def test_short_message_no_entity_returns_zero(self, gate):
        assert gate.level("哈哈") == 0
        assert gate.level("666") == 0
        assert gate.level("好看") == 0

class TestEntityHit:
    def test_entity_in_message_returns_level_1(self, gate):
        assert gate.level("E.V 是 AI 吗") >= 1
        assert gate.level("卡芙卡") >= 1
    
    def test_lowercase_entity_works(self, gate):
        assert gate.level("firefly is cute") >= 1

class TestIntentDetection:
    def test_who_question_returns_level_2(self, gate):
        assert gate.level("你叫什么") == 2
        assert gate.level("他是谁") == 2
        assert gate.level("你跟卡芙卡什么关系") == 2
    
    def test_english_intent(self, gate):
        assert gate.level("who are you") == 2
        assert gate.level("what is firefly") == 2

class TestDefaultBehavior:
    def test_medium_message_returns_level_1(self, gate):
        assert gate.level("今天天气怎么样") >= 1  # 默认安全
```

**`tests/unit/llm/test_knowledge_recall.py`**：

```python
import pytest
from src.llm.knowledge.loader import load_knowledge, KnowledgeBase
from src.llm.knowledge.recall import KnowledgeRecall

@pytest.fixture
def kb():
    return load_knowledge("tests/fixtures/knowledge")  # 测试 fixture

@pytest.fixture
def recall(kb):
    return KnowledgeRecall(kb)

class TestCuratedMatch:
    def test_curated_always_triggered_on_pattern(self, recall, kb):
        # 设 fixture 中有 pattern="你(是|叫)" 的卡片
        result = recall.recall("你叫什么")
        assert len(result["curated"]) >= 1
        assert "我是 E.V" in result["curated"][0]

class TestFactMatch:
    def test_fact_matched_by_keyword(self, recall):
        result = recall.recall("你是谁")
        assert any("E.V" in f for f in result["facts"])
    
    def test_fact_not_matched_for_unrelated(self, recall):
        result = recall.recall("今天天气很好")
        assert all("E.V" not in f for f in result["facts"])

class TestLoreMatch:
    def test_lore_returned_for_relevant_query(self, recall):
        result = recall.recall("你是 AI 吗")
        assert len(result["lore"]) > 0
```

**`tests/integration/test_brain_with_knowledge.py`**：

```python
import pytest
from src.llm.brain import LLMBrain
from src.utils import config

@pytest.fixture
def brain():
    cfg = config.cfg
    cfg.KNOWLEDGE_ENABLED = True
    cfg.KNOWLEDGE_ROOT = "tests/fixtures/knowledge"
    b = LLMBrain(cfg)
    b.setup_knowledge()
    return b

class TestMessageBuilding:
    def test_chitchat_does_not_inject(self, brain):
        msgs = brain._build_messages("哈哈")
        # 仅 system_prompt，无 lore block
        assert len([m for m in msgs if "【核心设定" in m.get("content", "")]) == 0
    
    def test_intent_injects_full_layer(self, brain):
        msgs = brain._build_messages("你是谁")
        contents = [m.get("content", "") for m in msgs if m["role"] == "system"]
        joined = "\n".join(contents)
        assert "【核心设定" in joined
        assert "【确定性事实" in joined
    
    def test_token_cost_under_budget(self, brain):
        # 验证知识注入不会撑爆单条消息
        msgs = brain._build_messages("你跟卡芙卡什么关系？")
        total_chars = sum(len(m.get("content", "")) for m in msgs)
        assert total_chars < 8000  # 约 2k token 上限
```

#### 3.1.8 回滚方案

`src/llm/llm_brain.py` 改动局部化在 `_build_messages`：
- 新增方法 `setup_knowledge`（独立）
- 新增分支（`if self.knowledge_gate and ...`）
- 不动原有 system prompt 加载逻辑

回滚只需：
```bash
git revert <knowledge-commit>
# 或手动注释 _build_messages 中的 if 分支
```

---

### 3.2 记忆系统重构（双后端 + Mem0 判决链）

#### 3.2.1 目标

- 引入 `MemoryBackend` 抽象
- 提供 `LiteMemoryBackend`（默认）+ 保留 `MemUBackend`（兼容）
- 引入 Mem0 风格 ADD/UPDATE/DELETE/IGNORE 判决链
- 引入命名空间（`shared_profile` / `daily_life` / `work_tasks` / `viewer_profile`）
- 引入时间衰减

#### 3.2.2 `LiteMemoryBackend` 实现

**`src/llm/memory/lite.py`**（约 350 行，详见优化方案文档 §3.2）

**schema**：

```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,         -- shared_profile / daily_life / work_tasks / viewer_profile
    user TEXT NOT NULL,              -- AI 名字 / 主播名 / 观众名
    content TEXT NOT NULL,
    topic TEXT DEFAULT 'general',    -- 15 个 topic 之一
    confidence REAL DEFAULT 0.8,
    created_at REAL NOT NULL,
    last_accessed REAL NOT NULL,
    access_count INTEGER DEFAULT 0,
    source TEXT,                     -- chat / proactive / butler
    metadata TEXT,                   -- JSON
    onnx_vec BLOB,                   -- 384 dim float32
    hash_vec BLOB                    -- 64 dim float32
);

CREATE INDEX idx_ns_user ON memories(namespace, user);
CREATE INDEX idx_topic ON memories(topic);
CREATE INDEX idx_created ON memories(created_at);

-- FTS5 全文索引（可选，用于 lore 检索）
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content, user, namespace,
    content='memories', content_rowid='id'
);
```

**混合检索**：

```python
async def recall(self, query: str, *, namespace=None, top_k=5, min_similarity=0.3):
    # 1. 候选筛选（避免全表扫描）
    candidates = self._fetch_candidates(namespace)
    if not candidates:
        return []
    
    # 2. 编码 query
    q_onnx = self._onnx.encode([query])[0] if self._onnx else None
    q_hash = self._hash.encode(query)
    
    # 3. 计算混合相似度
    results = []
    for c in candidates:
        sim = self._hybrid_sim(q_onnx, q_hash, c)
        # 4. 应用时间衰减
        sim *= self._time_decay(c)
        if sim >= min_similarity:
            c["similarity"] = sim
            results.append(c)
    
    # 5. Top-K
    results.sort(key=lambda x: -x["similarity"])
    return results[:top_k]

def _hybrid_sim(self, q_onnx, q_hash, candidate) -> float:
    """ONNX 75% + 哈希 25%（Firefly 经验值）。"""
    if q_onnx is not None and candidate.get("onnx_vec"):
        c_onnx = np.frombuffer(candidate["onnx_vec"], dtype=np.float32)
        onnx_sim = float(cosine(q_onnx, c_onnx))
    else:
        onnx_sim = 0.0
    
    c_hash = np.frombuffer(candidate["hash_vec"], dtype=np.float32)
    hash_sim = float(cosine(q_hash, c_hash))
    
    return self.alpha * onnx_sim + (1 - self.alpha) * hash_sim
```

#### 3.2.3 `MemUBackend` 兼容层

**`src/llm/memory/memu_compat.py`**（约 150 行）

```python
"""将现有 memU API 适配到 MemoryBackend ABC。

让现有代码（ButlerAgent / Application）的 API 调用保持不变，
后端可在 lite / memu 间切换。
"""

from .base import MemoryBackend

class MemUBackend(MemoryBackend):
    def __init__(self, memu_instance):
        self._memu = memu_instance  # tools.memory.memory singleton
    
    async def add(self, content, *, namespace, user, **kw) -> int:
        # memU 没有 namespace 概念，将 namespace 编码进 user
        # 现有 memU 的 commit_recall_files 接受 user / content / metadata
        result = await self._memu.commit_recall_files([{
            "name": kw.get("topic", "记忆"),
            "content": content,
            "user": f"{namespace}/{user}",  # 编码 namespace 进 user
            "metadata": {"namespace": namespace, **kw.get("metadata", {})},
        }])
        files = result.get("recall_files", [])
        return int(files[0]["id"]) if files else -1
    
    async def recall(self, query, *, namespace=None, top_k=5, **kw) -> list[dict]:
        # memU 自身有 retrieval 接口
        results = await self._memu.retrieve(query, top_k=top_k)
        # 过滤 namespace
        if namespace:
            results = [r for r in results
                       if r.get("user", "").startswith(f"{namespace}/")]
        return results
    
    # ... 其他方法类似转发
```

#### 3.2.4 `MemoryManager` Facade

**`src/llm/memory/__init__.py`**（约 250 行）

```python
"""记忆系统门面。对外暴露稳定 API，对内委托给具体 backend。"""

from .base import MemoryBackend
from .lifecycle import LifecycleEngine

class MemoryManager:
    def __init__(self, backend: MemoryBackend, *, lifecycle: LifecycleEngine = None):
        self._backend = backend
        self._lifecycle = lifecycle
        self._recent_turns: list[dict] = []
    
    # === 旧 API（向后兼容）===
    def add_turn(self, role: str, content: str, source: str = "user", **kw):
        self._recent_turns.append({
            "role": role, "content": content, "source": source,
            "ts": time.time(), **kw,
        })
        # 保留最近 N 条
        max_recent = kw.get("max_recent", 50)
        if len(self._recent_turns) > max_recent:
            self._recent_turns = self._recent_turns[-max_recent:]
    
    @property
    def recent_turns(self):
        return self._recent_turns
    
    def list_files(self, limit: int = 200) -> list[dict]:
        return asyncio.run(self._backend.list(limit=limit))
    
    async def delete_memories_async(self, ids: list[str]) -> int:
        n = 0
        for id_ in ids:
            if await self._backend.delete(int(id_)):
                n += 1
        return n
    
    async def commit_recall_files(self, files: list[dict]) -> dict:
        """将 ButlerAgent 提取的条目写入后端，触发 Mem0 判决链。"""
        recall_files = []
        for f in files:
            content = f.get("content") or f.get("name", "")
            user = f.get("user", "anonymous")
            # 推断 namespace（从 user 字段提取）
            namespace = self._infer_namespace(user, f)
            topic = self._infer_topic(content)
            
            if self._lifecycle:
                # 走 Mem0 判决链
                verdict, target_id = await self._lifecycle.judge(
                    content=content, namespace=namespace, user=user, topic=topic)
                if verdict == "IGNORE":
                    continue
                elif verdict == "ADD":
                    id_ = await self._backend.add(content, namespace=namespace, user=user, topic=topic)
                    recall_files.append({"id": id_, "content": content, "user": user})
                elif verdict == "UPDATE":
                    await self._backend.update(target_id, content=content)
                    recall_files.append({"id": target_id, "content": content, "user": user})
                elif verdict == "DELETE":
                    await self._backend.delete(target_id)
            else:
                # 无 lifecycle：直接 ADD
                id_ = await self._backend.add(content, namespace=namespace, user=user, topic=topic)
                recall_files.append({"id": id_, "content": content, "user": user})
        
        return {"recall_files": recall_files}
    
    # === 新 API ===
    async def add_memory(self, content, namespace, user, **kw) -> int:
        return await self._backend.add(content, namespace=namespace, user=user, **kw)
    
    async def recall(self, query, namespace=None, top_k=5, **kw) -> list[dict]:
        return await self._backend.recall(query, namespace=namespace, top_k=top_k, **kw)
    
    def namespace(self, name: str) -> "NamespaceView":
        return NamespaceView(self, name)
    
    # === 辅助方法 ===
    def _infer_namespace(self, user: str, file: dict) -> str:
        """从 user / metadata 推断命名空间。"""
        meta = file.get("metadata", {}) or {}
        if "namespace" in meta:
            return meta["namespace"]
        # 启发式：观众级 → viewer_profile，工作相关 → work_tasks，其他 → shared_profile
        if file.get("source") == "danmaku":
            return "viewer_profile"
        if any(kw in (file.get("content") or "").lower() for kw in ["工作", "项目", "代码", "会议"]):
            return "work_tasks"
        if any(kw in (file.get("content") or "") for kw in ["今天", "昨天", "刚才", "吃了", "去了"]):
            return "daily_life"
        return "shared_profile"
    
    def _infer_topic(self, content: str) -> str:
        """15 个 topic 之一（用于差异化衰减）。"""
        # 简化版：关键词匹配
        topic_keywords = {
            "identity": ["我是", "我叫", "我是一名"],
            "preference": ["喜欢", "讨厌", "最爱"],
            "habit": ["习惯", "经常", "每天"],
            "relationship": ["朋友", "家人", "同事", "对象"],
            "experience": ["去过", "试过", "经历"],
            "emotion": ["开心", "难过", "生气", "焦虑"],
            "schedule": ["明天", "下周", "待办"],
            # ...
        }
        for topic, kws in topic_keywords.items():
            if any(kw in content for kw in kws):
                return topic
        return "general"
    
    def count(self) -> int:
        return asyncio.run(self._backend.count())
    
    @staticmethod
    def format_turns_text(turns):
        """保留原签名：把轮次列表格式化为文本。"""
        # 现有实现（保持不变）
        ...
```

#### 3.2.5 Mem0 判决链

**`src/llm/memory/lifecycle.py`**（约 200 行）

```python
"""Mem0 风格记忆生命周期：ADD / UPDATE / DELETE / IGNORE 判决。"""

from __future__ import annotations
import json
from openai import AsyncOpenAI
from typing import Optional

_JUDGE_SYSTEM = """你是记忆管家。判断新事实与现有记忆的关系。

判决标准：
- ADD：新事实是全新主题，现有记忆没有相关条目
- UPDATE：新事实是对现有记忆的修正/补充/细化
- DELETE：新事实表明现有记忆已过时/矛盾
- IGNORE：新事实与现有记忆高度重复，无需存储

只输出 JSON：{"verdict": "ADD|UPDATE|DELETE|IGNORE", "target_id": <id或null>, "reason": "..."}"""


class LifecycleEngine:
    def __init__(self, *, base_url: str, api_key: str, model: str):
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=30.0)
        self._model = model
    
    async def judge(
        self, *,
        content: str,
        namespace: str,
        user: str,
        topic: str,
    ) -> tuple[str, Optional[int]]:
        """对新事实做判决。
        
        Returns: (verdict, target_id)
        """
        # 1. 检索相似现有记忆
        similar = await self._recall_similar(content, namespace, user, top_k=3)
        if not similar:
            return ("ADD", None)
        
        # 2. 调 LLM 判决
        context = "\n".join(
            f"- id={m['id']}: {m['content']} (相似度 {m['similarity']:.2f})"
            for m in similar
        )
        user_text = (
            f"新事实：{content}\n"
            f"namespace: {namespace}\nuser: {user}\n"
            f"现有相关记忆：\n{context}"
        )
        
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.2,
                max_tokens=200,
            )
            text = resp.choices[0].message.content.strip()
            data = json.loads(self._extract_json(text))
            return (data.get("verdict", "ADD"), data.get("target_id"))
        except Exception as e:
            # 失败保守：ADD（让用户自己纠正）
            return ("ADD", None)
    
    async def _recall_similar(self, content, namespace, user, top_k):
        # 调用 memory backend 检索
        ...
    
    def _extract_json(self, text: str) -> str:
        """从 LLM 输出中提取 JSON（容错：处理 markdown 围栏）。"""
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
        return text
```

#### 3.2.6 时间衰减

**`src/llm/memory/decay.py`**（约 100 行）

```python
"""时间衰减：按 topic 差异化半衰期。"""

import time
from dataclasses import dataclass

@dataclass
class TopicDecay:
    half_life_days: float
    min_confidence: float

TOPIC_DECAY = {
    "identity":     TopicDecay(365, 0.5),
    "preference":   TopicDecay(90, 0.4),
    "habit":        TopicDecay(60, 0.3),
    "relationship": TopicDecay(180, 0.5),
    "experience":   TopicDecay(365, 0.3),
    "emotion":      TopicDecay(7, 0.2),
    "schedule":     TopicDecay(1, 0.0),
    "work_task":    TopicDecay(30, 0.2),
    "general":      TopicDecay(60, 0.3),
    # ... 15 个 topic
}


async def decay_stale_memories(backend) -> int:
    """对所有记忆应用衰减，删除低于 min_confidence 的。"""
    all_mems = await backend.list(limit=100000)
    now = time.time()
    deleted = 0
    
    for mem in all_mems:
        topic = mem.get("topic", "general")
        cfg = TOPIC_DECAY.get(topic, TOPIC_DECAY["general"])
        
        age_days = (now - mem["created_at"]) / 86400
        decay = 0.5 ** (age_days / cfg.half_life_days)
        new_conf = mem["confidence"] * decay
        
        if new_conf < cfg.min_confidence:
            await backend.delete(mem["id"])
            deleted += 1
        else:
            await backend.update(mem["id"], confidence=new_conf)
    
    return deleted
```

#### 3.2.7 后台任务

**修改 `src/core/application.py`**：

```python
async def _memory_decay_loop(self, interval: float = 86400):
    """每 24h 跑一次衰减。"""
    while True:
        try:
            await asyncio.sleep(interval)
            n = await decay_stale_memories(self.mm._backend)
            console.dim(f"[记忆衰减] 清理 {n} 条过期记忆")
        except Exception as e:
            console.warn(f"[记忆衰减] 失败：{e}")
```

#### 3.2.8 配置项

```python
# utils/config.py
MEMORY_BACKEND: str = "lite"  # "lite" | "memu"
MEMORY_DB_PATH: str = "data/memories/lite.db"
MEMORY_ONNX_MODEL_PATH: str = "data/models/multilingual-MiniLM.onnx"
MEMORY_HASH_TAXONOMY: str = "default"  # 23 类领域
MEMORY_DECAY_INTERVAL_SEC: int = 86400
MEMORY_LIFECYCLE_ENABLED: bool = True
MEMORY_LIFECYCLE_MODEL: str = ""  # 留空用 BUTLER 模型
```

#### 3.2.9 测试用例

**`tests/unit/llm/test_memory_lite.py`**：

```python
import pytest
import time
from src.llm.memory.lite import LiteMemoryBackend

@pytest.fixture
def backend(tmp_path):
    db_path = tmp_path / "test.db"
    return LiteMemoryBackend(str(db_path), onnx_model_path=None)  # 无 ONNX 走 hash only

class TestAddAndRecall:
    async def test_add_returns_id(self, backend):
        id_ = await backend.add(
            "用户喜欢喝咖啡",
            namespace="shared_profile", user="user1")
        assert id_ > 0
    
    async def test_recall_finds_similar(self, backend):
        await backend.add("用户喜欢喝咖啡", namespace="shared_profile", user="user1")
        await backend.add("用户喜欢喝牛奶", namespace="shared_profile", user="user1")
        await backend.add("用户养了一只猫", namespace="daily_life", user="user1")
        
        results = await backend.recall("咖啡", namespace="shared_profile")
        assert len(results) > 0
        assert any("咖啡" in r["content"] for r in results)
    
    async def test_namespace_isolation(self, backend):
        await backend.add("A 在 shared", namespace="shared_profile", user="A")
        await backend.add("A 在 daily", namespace="daily_life", user="A")
        
        results = await backend.recall("在", namespace="shared_profile")
        assert all(r["namespace"] == "shared_profile" for r in results)

class TestUpdateAndDelete:
    async def test_update_content(self, backend):
        id_ = await backend.add("old", namespace="x", user="y")
        await backend.update(id_, content="new")
        mems = await backend.list()
        assert mems[0]["content"] == "new"
    
    async def test_delete(self, backend):
        id_ = await backend.add("x", namespace="x", user="y")
        await backend.delete(id_)
        assert await backend.count() == 0

class TestDecay:
    async def test_decay_emotion_fast(self, backend):
        """情绪类记忆 7 天后衰减。"""
        # 注入一条"很久以前"的情绪记忆
        id_ = await backend.add("很开心", namespace="daily_life", user="u",
                                topic="emotion", confidence=0.8)
        await backend.list()  # 确保入库
        # 模拟 100 天后
        with backend.db:
            backend.db.execute("UPDATE memories SET created_at = ? WHERE id = ?",
                               (time.time() - 100 * 86400, id_))
        deleted = await decay_stale_memories(backend)
        assert deleted == 1
```

#### 3.2.10 回滚

由于 `MemoryManager` Facade 保持旧 API 签名（`add_turn` / `recent_turns` / `commit_recall_files` / `list_files`），`ButlerAgent` 和 `Application` 的调用方**无需修改**。回滚只需：
```bash
git revert <memory-refactor-commit>
```

---

### 3.3 Agent 任务执行系统（新增 `src/agent/`）

#### 3.3.1 目标

为 E.V 增加 Firefly 风格的 ReAct Agent，能完成"开文件改代码→跑→检查"等多步任务。**默认不启用**，仅在显式触发（`!agent` 命令 / `@执行` 弹幕）时启动。

#### 3.3.2 目录结构

```
src/agent/
├── __init__.py
├── loop.py             # ReAct 循环
├── planner.py          # 计划生成
├── executor.py         # 工具执行
├── sandbox.py          # 沙箱
├── approval.py         # 人工审批
├── workspace.py        # 工作空间
├── budget.py           # Token 预算
└── tools/
    ├── __init__.py
    ├── builtin.py      # 工具注册
    ├── file_tools.py
    ├── shell_tools.py
    ├── web_tools.py
    └── browser_tools.py
```

#### 3.3.3 `ReActAgent` 核心循环

**`src/agent/loop.py`**（约 250 行）

```python
"""ReAct 任务执行：Plan → Execute → Observe → Re-plan。

与 LLM 对话主链路解耦：仅在显式触发时启动。
"""

from __future__ import annotations
import json
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Callable
from openai import AsyncOpenAI

from .sandbox import Sandbox
from .budget import TokenBudget
from .executor import ToolExecutor


@dataclass
class AgentStep:
    plan: str
    action: dict  # {"tool": "name", "args": {...}}
    observation: str
    timestamp: float = field(default_factory=time.time)


class ReActAgent:
    def __init__(
        self,
        *,
        llm_client: AsyncOpenAI,
        llm_model: str,
        tools: list[dict],       # OpenAI function schema 列表
        executor: ToolExecutor,
        sandbox: Sandbox,
        budget: TokenBudget,
        approval: Optional["ApprovalDialog"] = None,
        max_steps: int = 8,
    ):
        self._llm = llm_client
        self._model = llm_model
        self._tools = tools
        self._executor = executor
        self._sandbox = sandbox
        self._budget = budget
        self._approval = approval
        self._max_steps = max_steps
        self._history: list[AgentStep] = []
        self._progress_callback: Optional[Callable] = None
    
    def on_progress(self, callback: Callable) -> None:
        """注册进度回调（用于 SSE 推送到 UI）。"""
        self._progress_callback = callback
    
    async def run(self, task: str) -> str:
        """主循环：Plan → Execute → Observe → Re-plan。"""
        self._history.clear()
        self._budget.reset()
        
        for step in range(self._max_steps):
            # 1. Plan
            plan = await self._plan(task)
            if plan["action"] == "finish":
                return plan["result"]
            
            # 2. Sandbox check
            if not self._sandbox.check(plan["tool_call"]):
                if not await self._request_approval(plan):
                    return f"操作被用户拒绝：{plan['tool_call']}"
            
            # 3. Execute
            try:
                observation = await self._executor.execute(
                    plan["tool_call"]["name"],
                    plan["tool_call"]["arguments"],
                )
            except Exception as e:
                observation = f"执行失败：{e}"
            
            # 4. Record
            self._history.append(AgentStep(
                plan=plan["reasoning"],
                action=plan["tool_call"],
                observation=observation,
            ))
            
            # 5. Compress if needed
            if self._budget.is_full(self._estimate_tokens()):
                self._compress_history()
            
            # 6. Progress
            if self._progress_callback:
                await self._progress_callback({
                    "step": step + 1,
                    "max_steps": self._max_steps,
                    "action": plan["tool_call"],
                    "observation": observation[:200],
                })
        
        return f"达到最大步数（{self._max_steps}），任务未完成。"
    
    async def _plan(self, task: str) -> dict:
        """调 LLM 选择下一步动作。"""
        messages = [
            {"role": "system", "content": _REACT_SYSTEM_PROMPT},
            {"role": "user", "content": self._format_history_with_task(task)},
        ]
        resp = await self._llm.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=[{"type": "function", "function": t} for t in self._tools],
            tool_choice="auto",
            temperature=0.3,
        )
        msg = resp.choices[0].message
        self._budget.consume(resp.usage.total_tokens)
        
        if msg.tool_calls:
            tc = msg.tool_calls[0]
            return {
                "action": "tool",
                "reasoning": msg.content or "",
                "tool_call": {
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                },
            }
        else:
            return {
                "action": "finish",
                "result": msg.content or "",
            }
    
    def _format_history_with_task(self, task: str) -> str:
        parts = [f"任务：{task}\n\n历史步骤："]
        for i, step in enumerate(self._history):
            parts.append(
                f"{i+1}. 计划：{step.plan}\n"
                f"   动作：{step.action['name']}({step.action['arguments']})\n"
                f"   结果：{step.observation[:300]}"
            )
        if not self._history:
            parts.append("（暂无）")
        parts.append("\n请决定下一步动作（调用工具或返回最终结果）。")
        return "\n".join(parts)
    
    def _compress_history(self):
        """压缩早期步骤为摘要（Firefly 经验：75% 阈值触发）。"""
        if len(self._history) <= 3:
            return
        # 保留最近 3 步
        old = self._history[:-3]
        self._history = self._history[-3:]
        # 早期步骤压缩为单条摘要
        summary = f"[早期步骤摘要] 共 {len(old)} 步：\n"
        for step in old:
            summary += f"- {step.action['name']}(...) → {step.observation[:100]}\n"
        self._history.insert(0, AgentStep(
            plan="早期步骤摘要", action={"name": "summary", "arguments": {}},
            observation=summary,
        ))
    
    def _estimate_tokens(self) -> int:
        return sum(len(s.plan) + len(s.observation) for s in self._history) // 4
    
    async def _request_approval(self, plan: dict) -> bool:
        if not self._approval:
            return False
        return await self._approval.request(plan)


_REACT_SYSTEM_PROMPT = """你是一个任务执行 Agent。使用 ReAct 模式：
1. 思考（Reasoning）：分析当前状态，决定下一步动作
2. 动作（Action）：调用工具或返回最终结果
3. 观察（Observation）：查看工具执行结果
4. 重复 1-3 直到任务完成

工具调用规则：
- 必须基于历史观察决定下一步，不要重复尝试已失败的动作
- 看到足够信息时就 finish 返回结果
- 每次只调用一个工具

简洁思考，不要写冗余的内心独白。"""
```

#### 3.3.4 沙箱

**`src/agent/sandbox.py`**（约 150 行）

```python
"""路径/命令白名单 + 高风险操作标记。"""

from __future__ import annotations
from pathlib import Path
import re
from typing import Iterable

class SandboxViolation(Exception):
    pass


class Sandbox:
    # 高风险操作类型（需要人工审批）
    HIGH_RISK_ACTIONS = {
        "delete_file", "delete_directory", "format_disk",
        "shutdown", "reboot", "kill_process",
        "run_shell",  # shell 命令总是高风险
    }
    
    def __init__(
        self,
        *,
        allowed_paths: Iterable[str] = (),
        allowed_commands: Iterable[str] = (),
        blocked_patterns: Iterable[str] = (),
    ):
        self.allowed_paths = [Path(p).resolve() for p in allowed_paths]
        self.allowed_commands = set(allowed_commands)
        self.blocked_patterns = [re.compile(p) for p in blocked_patterns]
    
    def check(self, tool_call: dict) -> bool:
        """检查工具调用是否在沙箱内。返回 True 表示放行，False 表示需审批。"""
        name = tool_call.get("name", "")
        args = tool_call.get("arguments", {})
        
        # 高风险 → 需要审批（return False 触发审批）
        if name in self.HIGH_RISK_ACTIONS:
            return False
        
        # 文件操作：检查路径
        if name in ("read_file", "write_file", "edit_file", "search_files"):
            path = args.get("path", "")
            if not self._is_path_allowed(path):
                raise SandboxViolation(f"路径不在白名单：{path}")
        
        # Shell 命令：检查白名单
        if name == "run_shell_safe":  # 受控 shell（仅白名单命令）
            cmd = args.get("command", "").split()[0] if args.get("command") else ""
            if cmd not in self.allowed_commands:
                raise SandboxViolation(f"命令不在白名单：{cmd}")
        
        # 通用：检查 blocked patterns
        args_str = str(args)
        for pattern in self.blocked_patterns:
            if pattern.search(args_str):
                raise SandboxViolation(f"参数命中禁用模式：{pattern.pattern}")
        
        return True
    
    def _is_path_allowed(self, path: str) -> bool:
        try:
            target = Path(path).resolve()
        except (OSError, ValueError):
            return False
        return any(self._is_within(target, p) for p in self.allowed_paths)
    
    @staticmethod
    def _is_within(child: Path, parent: Path) -> bool:
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False
```

#### 3.3.5 工具注册

**`src/agent/tools/builtin.py`**（约 100 行）

```python
"""内置工具集合：文件 / Shell / Web / Browser。"""

from .file_tools import read_file, write_file, edit_file, search_files
from .shell_tools import run_shell_safe
from .web_tools import web_search, fetch_url
from .browser_tools import browser_action

BUILTIN_TOOLS = [
    {
        "name": "read_file",
        "description": "读取工作空间内文件",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（相对工作空间）"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "edit_file",
        "description": "查找并替换文件中的文本",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string", "description": "要替换的原文"},
                "new": {"type": "string", "description": "新内容"},
            },
            "required": ["path", "old", "new"],
        },
    },
    {
        "name": "search_files",
        "description": "在工作空间内搜索文件或内容",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "glob 模式或关键词"},
                "path": {"type": "string", "description": "搜索起点"},
                "regex": {"type": "string", "description": "可选正则"},
            },
        },
    },
    {
        "name": "run_shell_safe",
        "description": "执行白名单内的安全命令（ls/cat/grep/python 等）",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "工作目录"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "web_search",
        "description": "通过搜索引擎查询信息",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    # browser_action 需要 playwright，可选启用
]


def get_all_tools(include_browser: bool = False) -> list[dict]:
    tools = list(BUILTIN_TOOLS)
    if include_browser:
        tools.append({
            "name": "browser_action",
            "description": "浏览器自动化（开网页/点击/填表/截图）",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["open", "click", "type", "screenshot"]},
                    "url": {"type": "string"},
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["action"],
            },
        })
    return tools
```

#### 3.3.6 工作空间

**`src/agent/workspace.py`**（约 150 行）

```python
"""工作空间管理：路径绑定 + 沙箱白名单自动注入。"""

from __future__ import annotations
import json
import os
import uuid
from pathlib import Path
from typing import Optional


class Workspace:
    def __init__(self, *, id: str, name: str, path: str, active: bool = False):
        self.id = id
        self.name = name
        self.path = os.path.abspath(os.path.expanduser(path))
        self.active = active
    
    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "path": self.path, "active": self.active}


class WorkspaceManager:
    CONFIG_FILE = "data/workspace.json"
    
    def __init__(self, sandbox: Optional["Sandbox"] = None):
        self.workspaces: list[Workspace] = []
        self.active: Optional[Workspace] = None
        self._sandbox = sandbox
        self._load()
    
    def _load(self):
        if not os.path.exists(self.CONFIG_FILE):
            # 默认工作空间
            default = Workspace(id="default", name="默认", path="workspace", active=True)
            self.workspaces = [default]
            self.active = default
            self._save()
            return
        with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.workspaces = [Workspace(**w) for w in data.get("workspaces", [])]
        self.active = next((w for w in self.workspaces if w.active), None)
    
    def _save(self):
        os.makedirs(os.path.dirname(self.CONFIG_FILE) or ".", exist_ok=True)
        with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"workspaces": [w.to_dict() for w in self.workspaces]}, f, ensure_ascii=False, indent=2)
    
    def add(self, name: str, path: str) -> Workspace:
        ws = Workspace(id=str(uuid.uuid4())[:8], name=name, path=path)
        self.workspaces.append(ws)
        if not self.active:
            self.activate(ws.id)
        self._save()
        return ws
    
    def activate(self, ws_id: str) -> bool:
        ws = next((w for w in self.workspaces if w.id == ws_id), None)
        if not ws:
            return False
        for w in self.workspaces:
            w.active = (w.id == ws_id)
        self.active = ws
        self._save()
        # 注入沙箱白名单
        if self._sandbox:
            self._sandbox.allowed_paths = [ws.path]
        return True
    
    def delete(self, ws_id: str) -> bool:
        ws = next((w for w in self.workspaces if w.id == ws_id), None)
        if not ws or ws.id == "default":
            return False  # 默认空间不可删
        self.workspaces.remove(ws)
        if self.active and self.active.id == ws_id:
            self.active = self.workspaces[0] if self.workspaces else None
            if self.active:
                self.activate(self.active.id)
        self._save()
        return True
```

#### 3.3.7 集成到 LLM Brain

**修改 `src/llm/llm_brain.py`**（新增方法）：

```python
async def maybe_run_agent(self, message: str) -> Optional[str]:
    """检测消息是否触发 Agent 任务。
    
    直播场景默认不启用，避免抢话。仅在以下情况启动：
    - 消息以 '!agent' 开头
    - 包含 '@执行' 标记
    - 显式 API 调用
    """
    if not self.cfg.AGENT_ENABLED:
        return None
    if not (message.startswith("!agent ") or "@执行" in message):
        return None
    
    # 懒加载 agent
    if self._agent is None:
        from src.agent.loop import ReActAgent
        from src.agent.executor import ToolExecutor
        from src.agent.sandbox import Sandbox
        from src.agent.budget import TokenBudget
        from src.agent.tools.builtin import get_all_tools
        from src.agent.workspace import WorkspaceManager
        
        sandbox = Sandbox(allowed_commands=["ls", "cat", "grep", "head", "python"])
        workspace = WorkspaceManager(sandbox=sandbox)
        executor = ToolExecutor(sandbox=sandbox, workspace=workspace)
        budget = TokenBudget(max_tokens=self.cfg.AGENT_BUDGET)
        
        self._agent = ReActAgent(
            llm_client=self._llm_provider,  # 用抽象后的 provider
            llm_model=self.cfg.LLM_MODEL,
            tools=get_all_tools(include_browser=self.cfg.AGENT_BROWSER_ENABLED),
            executor=executor,
            sandbox=sandbox,
            budget=budget,
        )
    
    # 提取任务文本
    task = message.replace("!agent ", "").replace("@执行", "").strip()
    return await self._agent.run(task)
```

#### 3.3.8 配置项

```python
AGENT_ENABLED: bool = False  # 默认关闭
AGENT_BUDGET: int = 4096
AGENT_MAX_STEPS: int = 8
AGENT_BROWSER_ENABLED: bool = False
AGENT_REQUIRE_APPROVAL: bool = True
AGENT_AUTO_COMPRESS_THRESHOLD: float = 0.75
```

#### 3.3.9 测试用例

**`tests/unit/llm/test_agent_loop.py`**：

```python
import pytest
from src.agent.loop import ReActAgent
from src.agent.sandbox import Sandbox, SandboxViolation
from src.agent.executor import ToolExecutor
from src.agent.budget import TokenBudget

class FakeLLM:
    """模拟 LLM 客户端，返回预定义响应。"""
    def __init__(self, responses):
        self.responses = responses
        self.call_count = 0
    async def chat(self, **kwargs):
        # 返回预定义的 tool_call 或 finish
        ...


@pytest.fixture
def sandbox():
    return Sandbox(
        allowed_paths=["/tmp/test_workspace"],
        allowed_commands=["ls", "cat", "grep"],
    )


class TestSandbox:
    def test_path_in_whitelist_allowed(self, sandbox):
        assert sandbox.check({"name": "read_file", "arguments": {"path": "/tmp/test_workspace/file.txt"}}) is True
    
    def test_path_outside_whitelist_blocked(self, sandbox):
        with pytest.raises(SandboxViolation):
            sandbox.check({"name": "read_file", "arguments": {"path": "/etc/passwd"}})
    
    def test_high_risk_action_needs_approval(self, sandbox):
        # delete_file 返回 False（需审批）
        assert sandbox.check({"name": "delete_file", "arguments": {"path": "/tmp/x"}}) is False
    
    def test_shell_command_whitelist(self, sandbox):
        assert sandbox.check({"name": "run_shell_safe", "arguments": {"command": "ls /tmp"}}) is True
        with pytest.raises(SandboxViolation):
            sandbox.check({"name": "run_shell_safe", "arguments": {"command": "rm -rf /"}})


class TestReActLoop:
    async def test_single_step_completion(self):
        """单步任务：LLM 直接返回 finish。"""
        llm = FakeLLM(responses=[
            # 第一步：finish
            {"content": "任务完成", "tool_calls": None},
        ])
        agent = ReActAgent(
            llm_client=llm, llm_model="test",
            tools=[], executor=None, sandbox=Sandbox(),
            budget=TokenBudget(4096),
        )
        result = await agent.run("打个招呼")
        assert "完成" in result
    
    async def test_max_steps_limit(self):
        """达到 max_steps 时返回提示。"""
        llm = FakeLLM(responses=[
            # 每步都返回 tool_call（永不 finish）
            *[{"tool_calls": [{"function": {"name": "x", "arguments": "{}"}}],
              "content": "thinking"}] * 10,
        ])
        agent = ReActAgent(
            llm_client=llm, llm_model="test",
            tools=[{"name": "x", "parameters": {}}],
            executor=MockExecutor(), sandbox=Sandbox(),
            budget=TokenBudget(4096), max_steps=3,
        )
        result = await agent.run("x")
        assert "最大步数" in result
```

#### 3.3.10 集成测试

**`tests/integration/test_agent_with_brain.py`**：

```python
import pytest
from src.llm.brain import LLMBrain
from src.utils import config


class TestMaybeRunAgent:
    async def test_disabled_returns_none(self):
        cfg = config.cfg
        cfg.AGENT_ENABLED = False
        brain = LLMBrain(cfg)
        result = await brain.maybe_run_agent("!agent 测试")
        assert result is None
    
    async def test_no_trigger_returns_none(self):
        cfg = config.cfg
        cfg.AGENT_ENABLED = True
        brain = LLMBrain(cfg)
        result = await brain.maybe_run_agent("普通聊天")
        assert result is None
```

#### 3.3.11 回滚

`maybe_run_agent` 是**新增方法**，不影响 `chat` / `converse` 主链路。`src/agent/` 整个目录独立，回滚 `git revert` 不影响其他模块。

---

### 3.4 LLM Provider 抽象（重构 `src/llm/llm_brain.py`）

#### 3.4.1 目标

把 `llm_brain.py:120` 处写死的 `AsyncOpenAI` 调用替换为 `LLMProvider` 抽象，支持 OpenAI 兼容 / Anthropic / Google 多种协议。

#### 3.4.2 改造范围

**只改** `llm_brain.py` 中：
1. `__init__`（约 100 行）：client 构造 → 调 `get_provider(cfg)`
2. `converse`（约 100 行）：调用 `client.chat_completions.create(...)` → 调 `provider.chat_stream(...)`
3. `extract_memories`（约 50 行）：同样

**不改**：所有公开方法签名、`recent_turns` / `add_turn` 等行为。

#### 3.4.3 具体代码

**`src/llm/providers/openai_compat.py`**（约 200 行）

```python
"""OpenAI 兼容 Provider：覆盖 OpenAI / 智谱 / DeepSeek / 通义 / Moonshot / OpenRouter。"""

from __future__ import annotations
import json
from typing import AsyncIterator
from openai import AsyncOpenAI

from .base import LLMProvider, ChatResponse, ChatChunk


class OpenAICompatProvider(LLMProvider):
    def __init__(self, *, base_url: str, api_key: str, model: str, **kwargs):
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, **kwargs)
        self._model = model
        self._extra_body = kwargs.get("extra_body", {})
    
    async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=None, **kwargs):
        params = self._build_params(messages, tools, temperature, max_tokens, **kwargs)
        resp = await self._client.chat.completions.create(**params, stream=False)
        return self._parse_response(resp)
    
    async def chat_stream(self, messages, *, tools=None, **kwargs) -> AsyncIterator[ChatChunk]:
        params = self._build_params(messages, tools, **kwargs)
        params["stream"] = True
        stream = await self._client.chat.completions.create(**params)
        async for chunk in stream:
            yield self._parse_chunk(chunk)
    
    def _build_params(self, messages, tools, temperature, max_tokens, **kwargs):
        params = {
            "model": self._model,
            "messages": messages,
            "temperature": kwargs.get("temperature", temperature),
        }
        if tools:
            params["tools"] = [{"type": "function", "function": t} for t in tools]
        if max_tokens:
            params["max_tokens"] = max_tokens
        # 透传 extra_body（如 thinking 参数）
        if self._extra_body:
            params["extra_body"] = self._extra_body
        return params
    
    def _parse_response(self, resp) -> ChatResponse:
        msg = resp.choices[0].message
        return ChatResponse(
            content=msg.content or "",
            tool_calls=[
                {"id": tc.id, "name": tc.function.name,
                 "arguments": json.loads(tc.function.arguments)}
                for tc in (msg.tool_calls or [])
            ],
            usage={
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            },
            finish_reason=resp.choices[0].finish_reason,
        )
    
    def _parse_chunk(self, chunk) -> ChatChunk:
        choice = chunk.choices[0]
        return ChatChunk(
            delta=choice.delta.content or "",
            tool_calls=[
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                for tc in (choice.delta.tool_calls or [])
            ] if choice.delta.tool_calls else None,
            finish_reason=choice.finish_reason,
        )
    
    async def close(self):
        await self._client.close()
```

**`src/llm/providers/anthropic.py`**（约 250 行，特殊处理 system / tools / 流式）

```python
"""Anthropic Claude Provider。"""

from anthropic import AsyncAnthropic
from .base import LLMProvider, ChatResponse, ChatChunk


class AnthropicProvider(LLMProvider):
    def __init__(self, *, api_key: str, model: str, **kwargs):
        self._client = AsyncAnthropic(api_key=api_key, **kwargs)
        self._model = model
    
    async def chat(self, messages, *, tools=None, temperature=0.7, max_tokens=4096, **kwargs):
        # Claude 不支持 system role 嵌入 messages
        system_msg, conv_msgs = self._split_system(messages)
        params = {
            "model": self._model,
            "messages": conv_msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_msg:
            params["system"] = system_msg
        if tools:
            params["tools"] = self._convert_tools(tools)
        
        resp = await self._client.messages.create(**params)
        return self._parse_response(resp)
    
    @staticmethod
    def _split_system(messages):
        system_parts = []
        conv = []
        for m in messages:
            if m["role"] == "system":
                system_parts.append(m["content"])
            else:
                conv.append(m)
        return ("\n\n".join(system_parts) if system_parts else None, conv)
    
    @staticmethod
    def _convert_tools(tools):
        # OpenAI function schema → Anthropic tools schema
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("parameters", {}),
            }
            for t in tools
        ]
    
    def _parse_response(self, resp) -> ChatResponse:
        text_parts = []
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input,
                })
        return ChatResponse(
            content="".join(text_parts),
            tool_calls=tool_calls or None,
            usage={
                "prompt_tokens": resp.usage.input_tokens,
                "completion_tokens": resp.usage.output_tokens,
                "total_tokens": resp.usage.input_tokens + resp.usage.output_tokens,
            },
            finish_reason=resp.stop_reason,
        )
    
    async def chat_stream(self, messages, *, tools=None, **kwargs):
        # 简化版：直接非流式返回（流式复杂，留待 P2）
        resp = await self.chat(messages, tools=tools, **kwargs)
        yield ChatChunk(
            delta=resp.content,
            tool_calls=resp.tool_calls,
            finish_reason=resp.finish_reason,
        )
    
    async def close(self):
        await self._client.close()
```

**`src/llm/providers/registry.py`**（约 50 行）

```python
"""Provider 注册表。"""

from .base import LLMProvider
from .openai_compat import OpenAICompatProvider
# from .anthropic import AnthropicProvider  # 后续启用
# from .google import GoogleProvider

PROVIDERS: dict[str, type[LLMProvider]] = {
    "openai_compat": OpenAICompatProvider,
    # "anthropic": AnthropicProvider,
    # "google": GoogleProvider,
}


def get_provider(cfg) -> LLMProvider:
    """工厂方法：根据 cfg.LLM_PROVIDER 创建对应 provider。"""
    provider_name = getattr(cfg, "LLM_PROVIDER", "openai_compat")
    cls = PROVIDERS.get(provider_name, OpenAICompatProvider)
    
    if cls is OpenAICompatProvider:
        return cls(
            base_url=cfg.LLM_BASE_URL,
            api_key=cfg.LLM_API_KEY,
            model=cfg.LLM_MODEL,
            timeout=cfg.LLM_TIMEOUT,
            extra_body=cfg.LLM_EXTRA_BODY or {},
        )
    raise NotImplementedError(f"Provider {provider_name} 未实现")
```

#### 3.4.4 改造 `llm_brain.py`

```python
# 重构前
class LLMBrain:
    def __init__(self, cfg):
        ...
        self._client = AsyncOpenAI(
            base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY, timeout=60.0)
        self._model = cfg.LLM_MODEL
    
    async def converse(self, ...):
        resp = await self._client.chat.completions.create(
            model=self._model, messages=msgs, tools=tools, stream=True)
        ...

# 重构后
class LLMBrain:
    def __init__(self, cfg):
        ...
        self._provider = get_provider(cfg)  # 抽象
        self._model = cfg.LLM_MODEL
    
    async def converse(self, ...):
        async for chunk in self._provider.chat_stream(msgs, tools=tools, ...):
            yield chunk  # 或处理 delta
        ...
```

#### 3.4.5 测试

**`tests/unit/llm/test_providers.py`**：

```python
import pytest
from src.llm.providers.openai_compat import OpenAICompatProvider
from src.llm.providers.registry import get_provider


class TestOpenAICompatProvider:
    async def test_chat_returns_response(self):
        provider = OpenAICompatProvider(
            base_url="https://api.openai.com/v1",
            api_key="test-key", model="gpt-4o-mini")
        # 用 mock 或真实调用
        ...

    def test_parse_tool_calls(self):
        # 单元测试 _parse_response
        from openai.types.chat import ChatCompletion, ChatCompletionMessage
        ...

class TestRegistry:
    def test_unknown_provider_falls_back_to_openai_compat(self):
        # 用 MagicMock cfg
        from unittest.mock import MagicMock
        cfg = MagicMock()
        cfg.LLM_PROVIDER = "unknown"
        cfg.LLM_BASE_URL = "http://test"
        cfg.LLM_API_KEY = "k"
        cfg.LLM_MODEL = "m"
        cfg.LLM_TIMEOUT = 60
        cfg.LLM_EXTRA_BODY = None
        provider = get_provider(cfg)
        assert isinstance(provider, OpenAICompatProvider)
```

#### 3.4.6 回滚

改动集中在 `llm_brain.py` 顶部的 `__init__` 和 `converse`，约 50 行差异。回滚 `git revert` 后所有 OpenAI 兼容 provider 仍正常工作（默认走 `OpenAICompatProvider`）。

---

### 3.5 Skill 插件标准化（`src/llm/skills/` → `src/skills/`）

#### 3.5.1 目标

将 `src/llm/skills/` 下的纯 Markdown 文件改造为符合 Agent Skills 标准的 `SKILL.md`（YAML frontmatter 元数据 + Markdown 指令体）。引入渐进式披露（先 metadata，再 body，再 resources）。

#### 3.5.2 SKILL.md 格式

```markdown
---
name: stream-chat
description: 直播闲聊技能。日常对话、回应弹幕时使用。
license: MIT
allowed-tools: []  # 可选：该 skill 允许调用的工具
---

# 直播闲聊技能

## 行为准则
1. 口语化，1-3 句
2. 不连续追问
3. 不调用工具

## 资源
- 模板：`templates/greeting.md`
- 数据：`data/slang.md`
```

#### 3.5.3 扫描器

**`src/skills/scanner.py`**（约 200 行）

```python
"""SKILL.md 扫描器：渐进式披露（metadata → body → resources）。"""

from __future__ import annotations
import os
import yaml
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Skill:
    name: str
    description: str
    license: str = "MIT"
    body: str = ""  # 懒加载
    resources: dict[str, list[Path]] = field(default_factory=dict)  # 懒加载
    metadata: dict = field(default_factory=dict)
    _skill_md_path: Path = None
    
    async def load_body(self):
        if not self.body and self._skill_md_path:
            content = self._skill_md_path.read_text(encoding="utf-8")
            # 分离 frontmatter
            m = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)
            if m:
                self.body = m.group(2).strip()
            else:
                self.body = content
        return self.body
    
    async def load_resources(self):
        if not self.resources and self._skill_md_path:
            for sub in ("scripts", "templates", "data"):
                sub_path = self._skill_md_path.parent / sub
                if sub_path.exists():
                    self.resources[sub] = sorted(sub_path.rglob("*"))
        return self.resources


def scan_skills(skills_dir: str = "src/llm/skills") -> list[Skill]:
    """扫描 SKILL.md，仅加载 metadata（轻量）。"""
    skills = []
    root = Path(skills_dir)
    if not root.exists():
        return skills
    
    for skill_md in sorted(root.rglob("SKILL.md")):
        try:
            content = skill_md.read_text(encoding="utf-8")
            m = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)
            if not m:
                continue
            meta = yaml.safe_load(m.group(1)) or {}
            skills.append(Skill(
                name=meta.get("name", skill_md.parent.name),
                description=meta.get("description", ""),
                license=meta.get("license", "MIT"),
                metadata=meta,
                _skill_md_path=skill_md,
            ))
        except (yaml.YAMLError, OSError):
            continue
    
    return skills


def get_skill_for_intent(skills: list[Skill], message: str) -> Optional[Skill]:
    """根据消息意图选择最匹配的 skill。"""
    msg_lower = message.lower()
    scored = []
    for skill in skills:
        desc = skill.description.lower()
        # 简单关键词匹配（可升级为 embedding 相似度）
        score = sum(1 for word in desc.split() if word in msg_lower)
        if score > 0:
            scored.append((score, skill))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    return scored[0][1]
```

#### 3.5.4 集成到 prompt 拼装

**`src/utils/config.py`**：保留现有 `_collect_skill_files`，增加 SKILL.md 优先加载。

```python
def _load_system_prompt() -> str:
    """优先级：
    1. SKILL.md 格式（新）→ 渐进式披露
    2. 原 Markdown 文件（兼容）
    3. UI 人设
    4. .env SYSTEM_PROMPT
    """
    # 1. 尝试 SKILL.md 扫描
    from src.skills.scanner import scan_skills
    skills = scan_skills(os.path.join(_PROJECT_ROOT, "src/llm/skills"))
    if skills:
        # 拼接所有 skill 的 body
        parts = []
        for s in skills:
            # 这里假设 body 已预加载（启动时一次加载）
            parts.append(f"<!-- skill: {s.name} -->\n{s.body}")
        return "\n\n".join(parts)
    
    # 2-4. 保留原逻辑
    ...
```

#### 3.5.5 迁移现有 skills

**示例迁移**：`src/llm/skills/stream-chat.md` → `src/llm/skills/stream-chat/SKILL.md`

```bash
# 迁移脚本
cd src/llm/skills
for f in *.md; do
    name="${f%.md}"
    mkdir -p "$name"
    mv "$f" "$name/SKILL.md"
    # 在 frontmatter 添加 description
done
```

人工为每个 SKILL.md 补 `description` 字段。

#### 3.5.6 测试

**`tests/unit/test_skill_scanner.py`**：

```python
import pytest
from pathlib import Path
from src.skills.scanner import scan_skills, Skill


@pytest.fixture
def skills_dir(tmp_path):
    # 创建测试 SKILL.md
    (tmp_path / "skill-a").mkdir()
    (tmp_path / "skill-a" / "SKILL.md").write_text(
        "---\nname: skill-a\ndescription: 测试技能 A\n---\n# A body\n", encoding="utf-8"
    )
    (tmp_path / "skill-b").mkdir()
    (tmp_path / "skill-b" / "SKILL.md").write_text(
        "---\nname: skill-b\ndescription: 测试技能 B\n---\n# B body\n", encoding="utf-8"
    )
    return str(tmp_path)


class TestScanSkills:
    def test_returns_all_skills(self, skills_dir):
        skills = scan_skills(skills_dir)
        assert len(skills) == 2
        assert {s.name for s in skills} == {"skill-a", "skill-b"}
    
    def test_only_loads_metadata(self, skills_dir):
        """扫描阶段不应读 body（性能）。"""
        skills = scan_skills(skills_dir)
        for s in skills:
            assert s.body == ""  # 懒加载
    
    def test_load_body_lazily(self, skills_dir):
        skills = scan_skills(skills_dir)
        body = asyncio.run(skills[0].load_body())
        assert "A body" in body
```

#### 3.5.7 回滚

`config.py` 中 `_load_system_prompt` 改为优先用 SKILL.md，失败回退到原逻辑。删除 `src/skills/` 整个目录即可恢复。

---

### 3.6 输出互斥层（保留 + 扩展）

#### 3.6.1 不动的部分

`src/core/output_lock.py:1-102` 已经设计得很好，**保留全部**。

#### 3.6.2 扩展点：Agent 占用锁

为 Agent 任务增加新 owner 类型：

```python
# src/core/output_lock.py (扩展)
STATE_AGENT_RUNNING = "agent_running"  # 新增

_VALID_STATES = (
    STATE_IDLE, STATE_USER_TALKING, STATE_AI_SPEAKING,
    STATE_AGENT_THINKING, STATE_AGENT_RUNNING,  # 新增
)


def set_agent_owner() -> None:
    """Agent 任务占用输出锁。"""
    set_output_owner("agent")
    set_global_state(STATE_AGENT_RUNNING)
```

**使用**（`src/agent/loop.py`）：

```python
async def run(self, task: str) -> str:
    async with get_output_lock():
        set_agent_owner()
        try:
            # Agent 主循环
            ...
        finally:
            set_output_owner(None)
            set_global_state(STATE_IDLE)
```

#### 3.6.3 新增：互斥锁状态事件

为前端推送互斥状态变化（Firefly 有类似机制）：

```python
# src/core/bus.py (扩展事件)
EV_OUTPUT_STATE_CHANGED = "output_state_changed"

# src/core/output_lock.py (在状态变更时 emit)
def set_global_state(state: str) -> None:
    global _global_state
    old = _global_state
    _global_state = state
    if old != state:
        # 异步触发事件（fire-and-forget）
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(bus.emit(EV_OUTPUT_STATE_CHANGED, {
                "from": old, "to": state,
            }))
        except RuntimeError:
            pass  # 不在 event loop 中（启动阶段）
```

---

### 3.7 Application.py 拆分（核心重构）

#### 3.7.1 拆分目标

把 1668 行的 `Application` 类拆为：
- `RuntimeContext`（所有组件持有者）
- 4 个 `Handler`（input / danmaku / chat / mindcraft）
- 1 个 `CommandRegistry` + 8 个 `Command` 实现

#### 3.7.2 `RuntimeContext`

**`src/core/runtime.py`**（约 400 行）

```python
"""运行时上下文：所有组件的容器与生命周期管理。"""

from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)


class RuntimeContext:
    """E.V 运行时所有组件的持有者。
    
    取代原 Application.__init__ 中的 30+ 个 self.xxx 属性，
    提供按需初始化、依赖注入、热重载能力。
    """
    
    def __init__(self, cfg):
        self.cfg = cfg
        
        # === 核心组件 ===
        self.brain: Optional[LLMBrain] = None
        self.mm: Optional[MemoryManager] = None        # 记忆
        self.butler: Optional[ButlerAgent] = None
        self.evolution: Optional[EvolutionEngine] = None
        self.proactive: Optional[ProactiveEngine] = None
        self.tts: Optional[TTSEngine] = None
        self.stt_engine: Optional[STTEngine] = None
        self.vts: Optional[VTSController] = None
        self.face: Optional[FaceDriver] = None
        self.sub: Optional[SubtitleServer] = None
        self.pf: Optional[ProfanityFilter] = None
        self.mcp: Optional[MCPManager] = None
        self.mindcraft_bridge: Optional[MindcraftBridge] = None
        
        # === 业务组件 ===
        self.danmaku_picker: Optional[DanmakuPicker] = None
        self.bili_svc: Optional[BiliServiceManager] = None
        self.danmaku_reply_task: Optional[asyncio.Task] = None
        self.pet_widget: Optional[PetWidget] = None
        self.emotion_actor: Optional[EmotionActor] = None
        self.plugin_manager: Optional[PluginManager] = None
        
        # === 内部状态 ===
        self._input_source: str = "text"
        self._pending_stdin_fut: Optional[asyncio.Future] = None
        self._setup_callbacks: list[Callable] = []
        self._teardown_callbacks: list[Callable] = []
    
    async def setup(self) -> None:
        """按依赖顺序初始化所有组件。"""
        logger.info("[Runtime] 开始初始化")
        
        # 1. 无依赖：配置、日志、过滤
        self.pf = ProfanityFilter() if self.cfg.PROFANITY_FILTER_ENABLED else None
        
        # 2. 记忆后端
        if self.cfg.MEMORY_ENABLED:
            backend = create_memory_backend(self.cfg)
            lifecycle = LifecycleEngine(...) if self.cfg.MEMORY_LIFECYCLE_ENABLED else None
            self.mm = MemoryManager(backend, lifecycle=lifecycle)
            self.butler = ButlerAgent()
        
        # 3. LLM Brain
        self.brain = LLMBrain(self.cfg)
        if self.cfg.KNOWLEDGE_ENABLED:
            self.brain.setup_knowledge(self.cfg.KNOWLEDGE_ROOT)
        
        # 4. 自我进化 / 主动对话
        if self.cfg.EVOLUTION_ENABLED and self.butler:
            self.evolution = EvolutionEngine(...)
        if self.cfg.PROACTIVE_ENABLED:
            self.proactive = ProactiveEngine(...)
        
        # 5. 语音 / 视觉
        if self.cfg.TTS_ENABLED:
            self.tts = TTSEngine(self.cfg)
        if self.cfg.STT_ENABLED:
            self.stt_engine = STTEngine(self.cfg)
        if self.cfg.VTS_ENABLED:
            self.vts, self.face = await self._init_vts()
        if self.cfg.SUBTITLE_ENABLED:
            self.sub = SubtitleServer(self.cfg)
        
        # 6. 插件
        if self.cfg.PLUGINS_ENABLED:
            self.plugin_manager = PluginManager(...)
        
        # 7. 弹幕
        if self.cfg.BILI_ENABLED:
            self.danmaku_picker, self.bili_svc, self.danmaku_reply_task = \
                await self._start_bili()
        
        # 8. 桌宠
        if self.cfg.RUN_MODE == "pet":
            self.pet_widget = PetWidget(...)
        
        # 9. 调用 setup callbacks
        for cb in self._setup_callbacks:
            await cb(self)
        
        logger.info("[Runtime] 初始化完成")
    
    async def teardown(self) -> None:
        """优雅关闭所有组件。"""
        for cb in reversed(self._teardown_callbacks):
            try:
                await cb(self)
            except Exception as e:
                logger.warning(f"teardown callback failed: {e}")
        
        # 关闭组件（按相反顺序）
        if self.bili_svc: self._stop_bili()
        if self.stt_engine: self.stt_engine.stop()
        if self.tts: await self.tts.close()
        if self.mm: await self.mm.close()
        if self.brain: await self.brain.close()
    
    async def reload(self, component: str) -> None:
        """细粒度热重载单个组件。"""
        reloaders = {
            "llm": self._reload_llm,
            "proactive": self._reload_proactive,
            "memory": self._reload_memory,
            "config": self._reload_config,
        }
        if component not in reloaders:
            raise ValueError(f"unknown component: {component}")
        await reloaders[component]()
    
    def on_setup(self, callback: Callable[["RuntimeContext"], Awaitable[None]]) -> None:
        self._setup_callbacks.append(callback)
    
    def on_teardown(self, callback: Callable[["RuntimeContext"], Awaitable[None]]) -> None:
        self._teardown_callbacks.append(callback)
```

#### 3.7.3 Handler 拆分

**`src/core/handlers/input.py`**（约 200 行）

```python
"""输入处理器：等待用户键盘/语音输入，事件驱动心跳。"""

from .base import BaseHandler
from src.core.output_lock import is_rejecting_input, set_danmaku_pending


class InputHandler(BaseHandler):
    async def setup(self) -> None:
        pass
    
    async def teardown(self) -> None:
        pass
    
    async def wait_input(self, show_prompt: bool = True) -> str:
        """事件驱动输入等待。"""
        # 现有 _wait_input 逻辑
        # 拒绝期间输入丢弃等
        ...
```

**`src/core/handlers/danmaku.py`**（约 250 行）

```python
"""弹幕回复处理器。"""

class DanmakuHandler(BaseHandler):
    async def setup(self) -> None:
        # 启动弹幕服务
        if self.runtime.cfg.BILI_ENABLED:
            await self._start_bili()
    
    async def teardown(self) -> None:
        self._stop_bili()
    
    async def chat(self, items: list) -> None:
        """单条/批量弹幕回复。"""
        # 现有 _chat_danmaku 逻辑
        ...
```

**`src/core/handlers/chat.py`**（约 200 行）

```python
"""用户对话处理器。"""

class ChatHandler(BaseHandler):
    async def chat_with_interrupt(self, text: str, ...) -> tuple[bool, str, Any]:
        """用户对话+打断监听。"""
        # 现有 _chat_user_with_interrupt 逻辑
        ...
```

#### 3.7.4 `application.py` 瘦身

**`src/core/application.py`**（< 200 行）

```python
"""E.V 入口：编排 RuntimeContext 与各 Handler 协作。"""

import asyncio
import signal
from src.core.runtime import RuntimeContext
from src.core.handlers.input import InputHandler
from src.core.handlers.danmaku import DanmakuHandler
from src.core.handlers.chat import ChatHandler
from src.core.handlers.mindcraft import MindcraftHandler
from src.core.commands import setup_default_commands
from src.utils import config


async def run_async() -> int:
    """主异步入口。"""
    runtime = RuntimeContext(config.cfg)
    await runtime.setup()
    
    # 初始化 handlers
    handlers = {
        "input": InputHandler(runtime),
        "danmaku": DanmakuHandler(runtime),
        "chat": ChatHandler(runtime),
        "mindcraft": MindcraftHandler(runtime),
    }
    for h in handlers.values():
        await h.setup()
    
    # 注册命令
    setup_default_commands(runtime)
    
    # 主循环
    try:
        await _main_loop(runtime, handlers)
    finally:
        for h in reversed(list(handlers.values())):
            await h.teardown()
        await runtime.teardown()
    
    return 0


async def _main_loop(runtime, handlers) -> None:
    """主循环：等待输入，分发到对应 handler。"""
    while True:
        text = await handlers["input"].wait_input()
        if text in ("/quit", "/exit", "/q"):
            break
        if text.startswith("!") or text.startswith("/"):
            await runtime.command_registry.dispatch(text)
            continue
        # 用户对话
        await handlers["chat"].chat_with_interrupt(text, ...)


def run() -> int:
    return asyncio.run(run_async())
```

#### 3.7.5 命令拆分

**`src/core/commands/base.py`**（约 50 行）

```python
from abc import ABC, abstractmethod


class Command(ABC):
    def __init__(self, prefix: str, handler, *, exact: bool = False, help: str = ""):
        self.prefix = prefix
        self.handler = handler
        self.exact = exact
        self.help = help
    
    def matches(self, text: str) -> bool:
        if self.exact:
            return text == self.prefix
        return text.startswith(self.prefix)
    
    async def execute(self, text: str) -> bool:
        return await self.handler(text)
```

**`src/core/commands/registry.py`**（约 100 行）

```python
class CommandRegistry:
    def __init__(self):
        self._commands: list[Command] = []
    
    def register(self, *commands: Command) -> None:
        self._commands.extend(commands)
    
    async def dispatch(self, text: str) -> bool:
        for cmd in self._commands:
            if cmd.matches(text):
                try:
                    return await cmd.execute(text)
                except Exception as e:
                    console.error(f"命令执行失败：{e}")
                    return True
        return False
    
    def get_help(self) -> list[dict]:
        return [{"prefix": c.prefix, "help": c.help} for c in self._commands]
```

**`src/core/commands/memory_cmd.py`**（约 50 行）

```python
"""记忆管理命令：/memory list | del | clear | decay"""

from .base import Command


def make_memory_cmd(runtime):
    async def handler(cmd: str) -> bool:
        parts = cmd.split()
        sub = parts[1] if len(parts) > 1 else ""
        mm = runtime.mm
        if not mm:
            return True
        if sub == "list":
            # ...
        elif sub == "del":
            # ...
        # ...
    return Command("/memory", handler, help="记忆管理")
```

类似地：`model_cmd.py` / `tts_cmd.py` / `stt_cmd.py` / `config_cmd.py` / `plugins_cmd.py` / `tools_cmd.py`。

#### 3.7.6 测试

**`tests/unit/core/test_runtime.py`**：

```python
import pytest
from src.core.runtime import RuntimeContext


@pytest.fixture
def cfg():
    # 测试用最小配置
    from src.utils import config
    return config.cfg


class TestRuntimeLifecycle:
    async def test_setup_creates_components(self, cfg):
        cfg.MEMORY_ENABLED = False
        cfg.STT_ENABLED = False
        cfg.VTS_ENABLED = False
        cfg.PLUGINS_ENABLED = False
        cfg.BILI_ENABLED = False
        cfg.RUN_MODE = "live"
        cfg.PROACTIVE_ENABLED = False
        cfg.EVOLUTION_ENABLED = False
        runtime = RuntimeContext(cfg)
        await runtime.setup()
        assert runtime.brain is not None
        await runtime.teardown()
    
    async def test_teardown_is_idempotent(self, cfg):
        runtime = RuntimeContext(cfg)
        await runtime.teardown()
        await runtime.teardown()  # 不应抛异常
```

**`tests/unit/commands/test_registry.py`**：

```python
class TestCommandRegistry:
    async def test_dispatch_routes_to_handler(self):
        registry = CommandRegistry()
        called = []
        async def hello(cmd): called.append("hello"); return True
        registry.register(Command("!hello", hello))
        result = await registry.dispatch("!hello world")
        assert result is True
        assert called == ["hello"]
    
    async def test_dispatch_returns_false_for_unknown(self):
        registry = CommandRegistry()
        result = await registry.dispatch("!unknown")
        assert result is False
```

#### 3.7.7 回滚

如果新 `application.py` 出问题，git revert 即可恢复原 1668 行版本。`RuntimeContext` / `Handler` / `Command` 是新增模块，单独 revert 不影响其他。

---

### 3.8 配置系统（`src/utils/config.py` 重构）

#### 3.8.1 目标

将 `config.py:8-...` 中 50+ 个 `cfg.XXX` 字段从环境变量解析改为**结构化 dataclass**，新增字段类型安全、默认值、文档。

#### 3.8.2 改造后结构

**`src/utils/config.py`**（约 400 行，结构化）

```python
"""配置加载：环境变量 → 结构化 Config 对象。"""

from __future__ import annotations
import os
import sys
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv


def _get_env(key: str, default=None, *, type=str):
    val = os.getenv(key, default)
    if val is None or val == "":
        return None
    if type is bool:
        return val.lower() in ("true", "1", "yes", "on")
    if type is int:
        return int(val)
    if type is float:
        return float(val)
    if type is list:
        return [x.strip() for x in val.split(",") if x.strip()]
    return val


@dataclass
class LLMConfig:
    provider: str = field(default_factory=lambda: _get_env("LLM_PROVIDER", "openai_compat"))
    base_url: Optional[str] = field(default_factory=lambda: _get_env("LLM_BASE_URL"))
    api_key: Optional[str] = field(default_factory=lambda: _get_env("LLM_API_KEY"))
    model: Optional[str] = field(default_factory=lambda: _get_env("LLM_MODEL"))
    timeout: float = field(default_factory=lambda: _get_env("LLM_TIMEOUT", 60.0, float))
    extra_body: dict = field(default_factory=dict)
    history_window: int = field(default_factory=lambda: _get_env("HISTORY_WINDOW", 20, int))
    max_tokens: int = field(default_factory=lambda: _get_env("LLM_MAX_TOKENS", 2048, int))
    temperature: float = field(default_factory=lambda: _get_env("LLM_TEMPERATURE", 0.7, float))


@dataclass
class MemoryConfig:
    enabled: bool = field(default_factory=lambda: _get_env("MEMORY_ENABLED", True, bool))
    backend: str = field(default_factory=lambda: _get_env("MEMORY_BACKEND", "lite"))
    db_path: str = field(default_factory=lambda: _get_env("MEMORY_DB_PATH", "data/memories/lite.db"))
    onnx_model_path: Optional[str] = field(default_factory=lambda: _get_env("MEMORY_ONNX_MODEL_PATH"))
    lifecycle_enabled: bool = field(default_factory=lambda: _get_env("MEMORY_LIFECYCLE_ENABLED", True, bool))
    decay_interval_sec: int = field(default_factory=lambda: _get_env("MEMORY_DECAY_INTERVAL_SEC", 86400, int))
    max_recent_turns: int = field(default_factory=lambda: _get_env("MEMORY_MAX_RECENT_TURNS", 50, int))


@dataclass
class KnowledgeConfig:
    enabled: bool = field(default_factory=lambda: _get_env("KNOWLEDGE_ENABLED", True, bool))
    root: str = field(default_factory=lambda: _get_env("KNOWLEDGE_ROOT", "data/knowledge"))
    max_inject_chars: int = field(default_factory=lambda: _get_env("KNOWLEDGE_MAX_INJECT_CHARS", 2000, int))
    chat_threshold: int = field(default_factory=lambda: _get_env("KNOWLEDGE_CHAT_THRESHOLD", 4, int))


@dataclass
class AgentConfig:
    enabled: bool = field(default_factory=lambda: _get_env("AGENT_ENABLED", False, bool))
    budget: int = field(default_factory=lambda: _get_env("AGENT_BUDGET", 4096, int))
    max_steps: int = field(default_factory=lambda: _get_env("AGENT_MAX_STEPS", 8, int))
    require_approval: bool = field(default_factory=lambda: _get_env("AGENT_REQUIRE_APPROVAL", True, bool))
    browser_enabled: bool = field(default_factory=lambda: _get_env("AGENT_BROWSER_ENABLED", False, bool))


@dataclass
class DanmakuConfig:
    enabled: bool = field(default_factory=lambda: _get_env("BILI_ENABLED", False, bool))
    room_id: Optional[int] = field(default_factory=lambda: _get_env("BILI_ROOM_ID", None, int))
    room_ids: list = field(default_factory=list)
    server_port: int = field(default_factory=lambda: _get_env("BILI_SERVER_PORT", 8765, int))


@dataclass
class VoiceConfig:
    tts_enabled: bool = field(default_factory=lambda: _get_env("TTS_ENABLED", True, bool))
    stt_enabled: bool = field(default_factory=lambda: _get_env("STT_ENABLED", False, bool))
    stt_model: str = field(default_factory=lambda: _get_env("STT_MODEL", "small"))


@dataclass
class ProactiveConfig:
    enabled: bool = field(default_factory=lambda: _get_env("PROACTIVE_ENABLED", True, bool))
    interval: int = field(default_factory=lambda: _get_env("EVOLUTION_PERIODIC_INTERVAL", 1800, int))
    max_per_day: int = field(default_factory=lambda: _get_env("PROACTIVE_MAX_PER_DAY", 10, int))
    quiet_start_hour: int = field(default_factory=lambda: _get_env("QUIET_START_HOUR", 23, int))
    quiet_end_hour: int = field(default_factory=lambda: _get_env("QUIET_END_HOUR", 8, int))


@dataclass
class VTSConfig:
    enabled: bool = field(default_factory=lambda: _get_env("VTS_ENABLED", True, bool))
    model_dir: str = field(default_factory=lambda: _get_env("VTS_MODEL_DIR", "live2d"))


@dataclass
class Config:
    """根配置：聚合所有子配置。"""
    project_root: str = ""
    llm: LLMConfig = field(default_factory=LLMConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    danmaku: DanmakuConfig = field(default_factory=DanmakuConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    vts: VTSConfig = field(default_factory=VTSConfig)
    
    # === 向后兼容字段（旧 cfg.XXX 直接访问）===
    @property
    def LLM_PROVIDER(self) -> str: return self.llm.provider
    @property
    def LLM_BASE_URL(self) -> str: return self.llm.base_url
    @property
    def LLM_API_KEY(self) -> str: return self.llm.api_key
    @property
    def LLM_MODEL(self) -> str: return self.llm.model
    # ... 其他属性的 alias
    
    def reload(self) -> None:
        """从 .env 重新加载。"""
        load_dotenv(..., override=True)
        # 重新构造子 dataclass
        ...


# === 单例 ===
PROJECT_ROOT = _detect_project_root()
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
cfg = Config(project_root=PROJECT_ROOT)
```

#### 3.8.3 兼容性策略

提供 `@property` 兼容旧 `cfg.LLM_BASE_URL` 访问方式：

```python
# 旧代码：cfg.LLM_BASE_URL
# 新代码：cfg.llm.base_url（推荐）
# 兼容：cfg.LLM_BASE_URL 仍可用（通过 @property 转发）
```

#### 3.8.4 测试

**`tests/unit/test_config.py`**：

```python
import os
import pytest
from src.utils.config import Config, LLMConfig, MemoryConfig


class TestConfigLoad:
    def test_llm_config_from_env(self, monkeypatch):
        monkeypatch.setenv("LLM_BASE_URL", "http://test")
        monkeypatch.setenv("LLM_API_KEY", "k")
        monkeypatch.setenv("LLM_MODEL", "m")
        cfg = LLMConfig()
        assert cfg.base_url == "http://test"
    
    def test_bool_parsing(self, monkeypatch):
        monkeypatch.setenv("MEMORY_ENABLED", "true")
        cfg = MemoryConfig()
        assert cfg.enabled is True
        monkeypatch.setenv("MEMORY_ENABLED", "0")
        cfg = MemoryConfig()
        assert cfg.enabled is False
    
    def test_backward_compat_alias(self):
        cfg = Config()
        cfg.llm.base_url = "http://test"
        assert cfg.LLM_BASE_URL == "http://test"  # 通过 property
```

#### 3.8.5 回滚

`config.py` 改动较大但**自包含**，可独立 revert。`cfg.LLM_BASE_URL` 等兼容 property 保证旧代码不报错。

---

### 3.9 事件总线扩展（`src/core/bus.py`）

#### 3.9.1 现状

`src/core/bus.py`（69 行）已经实现简单事件总线。**保留**。

#### 3.9.2 扩展

增加事件订阅生命周期管理：

```python
# src/core/bus.py (扩展)
class EventBus:
    def __init__(self):
        self._listeners: dict[str, list[Callable]] = {}
        self._wildcard_listeners: list[Callable] = []
    
    def on(self, event: str, callback: Callable) -> Callable:
        """订阅事件，返回取消订阅的 token。"""
        if event not in self._listeners:
            self._listeners[event] = []
        self._listeners[event].append(callback)
        return lambda: self.off(event, callback)
    
    def off(self, event: str, callback: Callable) -> None:
        if event in self._listeners:
            self._listeners[event].remove(callback)
    
    def on_any(self, callback: Callable) -> Callable:
        self._wildcard_listeners.append(callback)
        return lambda: self._wildcard_listeners.remove(callback)
    
    async def emit(self, event: str, payload) -> None:
        for cb in self._listeners.get(event, []):
            try:
                await self._safe_invoke(cb, event, payload)
            except Exception as e:
                logger.exception(f"event handler error: {e}")
        for cb in self._wildcard_listeners:
            try:
                await self._safe_invoke(cb, event, payload)
            except Exception as e:
                logger.exception(f"wildcard handler error: {e}")
    
    async def _safe_invoke(self, cb, event, payload):
        result = cb(payload)
        if asyncio.iscoroutine(result):
            await result
```

#### 3.9.3 新增事件

```python
# src/core/events/__init__.py
EV_OUTPUT_STATE_CHANGED = "output_state_changed"  # 互斥锁状态变化
EV_KNOWLEDGE_INJECTED = "knowledge_injected"      # 知识库注入
EV_MEMORY_DECAYED = "memory_decayed"              # 记忆衰减完成
EV_AGENT_STEP = "agent_step"                       # Agent 步骤
EV_AGENT_DONE = "agent_done"                       # Agent 完成
EV_LLM_PROVIDER_CHANGED = "llm_provider_changed"   # 切换 Provider
```

#### 3.9.4 测试

**`tests/unit/core/test_bus.py`**：

```python
class TestEventBus:
    async def test_subscribe_and_emit(self):
        bus = EventBus()
        called = []
        bus.on("test", lambda p: called.append(p))
        await bus.emit("test", {"a": 1})
        assert called == [{"a": 1}]
    
    async def test_off_unsubscribes(self):
        bus = EventBus()
        called = []
        token = bus.on("test", lambda p: called.append(p))
        token()
        await bus.emit("test", {})
        assert called == []
    
    async def test_wildcard_listener(self):
        bus = EventBus()
        called = []
        bus.on_any(lambda p: called.append(p))
        await bus.emit("anything", 1)
        await bus.emit("other", 2)
        assert called == [1, 2]
    
    async def test_handler_error_does_not_break_others(self):
        bus = EventBus()
        called = []
        def bad(p): raise ValueError("oops")
        bus.on("test", bad)
        bus.on("test", lambda p: called.append(p))
        await bus.emit("test", 1)
        assert called == [1]  # 第二个 handler 仍执行
```

---

### 3.10 错误处理统一（`src/core/exceptions.py`）

#### 3.10.1 目标

当前错误码仅在 `ErrorCode` 枚举中（`src/core/exceptions.py:1-54`），缺乏统一的错误处理与降级策略。

#### 3.10.2 扩展

```python
# src/core/exceptions.py (扩展)
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable


@dataclass
class ErrorContext:
    code: str
    code_name: str
    message: str
    source: str           # 哪个模块抛出
    severity: str         # "warning" | "error" | "fatal"
    recoverable: bool
    timestamp: float
    cause: Optional[Exception] = None


class ErrorHandler:
    """统一错误处理：记录 + 降级 + 事件。"""
    
    def __init__(self, bus, logger):
        self._bus = bus
        self._logger = logger
        self._recovery_strategies: dict[str, Callable] = {}
    
    def register_recovery(self, code: str, strategy: Callable[[ErrorContext], Awaitable[None]]):
        self._recovery_strategies[code] = strategy
    
    async def handle(self, error: Exception, *, source: str, code: str, code_name: str,
                    severity: str = "error", recoverable: bool = True) -> None:
        ctx = ErrorContext(
            code=code, code_name=code_name, message=str(error),
            source=source, severity=severity, recoverable=recoverable,
            timestamp=time.time(), cause=error,
        )
        # 记录
        if severity == "fatal":
            self._logger.exception(f"[{source}] {code_name}: {error}")
        else:
            self._logger.warning(f"[{source}] {code_name}: {error}")
        
        # 尝试恢复
        if recoverable and code in self._recovery_strategies:
            try:
                await self._recovery_strategies[code](ctx)
            except Exception as e:
                self._logger.exception(f"recovery failed: {e}")
        
        # 推事件
        await self._bus.emit("error", ctx)
```

#### 3.10.3 使用

```python
# 旧代码
try:
    await llm_call()
except Exception as e:
    console.warn(f"LLM 调用失败：{e}")

# 新代码
try:
    await llm_call()
except Exception as e:
    await error_handler.handle(e, source="llm_brain",
                                code="LLM_001", code_name="LLM_CALL_FAILED",
                                severity="error", recoverable=True)
```

---

## 第 4 章：数据迁移

### 4.1 记忆库 schema 迁移

#### 4.1.1 旧 memU 库

**位置**：`data/memories/*.json` 或 memU 内部存储

**字段**：
- `id` (str)
- `name`
- `content`
- `user`
- `created_at`
- `embedding` (BLOB 或独立文件)

#### 4.1.2 新 lite.db schema

```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增 int
    namespace TEXT NOT NULL DEFAULT 'shared_profile',
    user TEXT NOT NULL,
    content TEXT NOT NULL,
    topic TEXT DEFAULT 'general',
    confidence REAL DEFAULT 0.8,
    created_at REAL NOT NULL,
    last_accessed REAL NOT NULL DEFAULT 0,
    access_count INTEGER DEFAULT 0,
    source TEXT,
    metadata TEXT,  -- JSON
    onnx_vec BLOB,
    hash_vec BLOB
);
```

#### 4.1.3 迁移脚本

**`scripts/migrate_memu_to_lite.py`**（约 200 行）

```python
"""将旧 memU 记忆库迁移到新 lite.db。"""

import argparse
import asyncio
import json
import sqlite3
import time
from pathlib import Path

# 旧 memU 数据格式
OLD_FORMAT = {
    "id": "uuid-string",
    "name": "记忆名",
    "content": "记忆内容",
    "user": "归属者",
    "created_at": "iso-timestamp",
    # 可选 embedding
}

# 字段映射
NAMESPACE_MAP = {
    "viewer": "viewer_profile",
    "user": "shared_profile",
    "ai": "shared_profile",
    # ...
}


def parse_old_memu(json_path: str) -> list[dict]:
    """读取旧 memU 导出。"""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    return data.get("memories", [])


def transform_record(old: dict) -> dict:
    """转换单条记录。"""
    return {
        "namespace": _infer_namespace(old),
        "user": old.get("user", "anonymous"),
        "content": old.get("content") or old.get("name", ""),
        "topic": _infer_topic(old.get("content", "")),
        "confidence": 0.8,
        "created_at": _parse_timestamp(old.get("created_at")) or time.time(),
        "last_accessed": 0,
        "access_count": 0,
        "source": "migration",
        "metadata": json.dumps({"migrated_from": "memu", "old_id": old.get("id")}),
    }


def _infer_namespace(old: dict) -> str:
    user = (old.get("user") or "").lower()
    if user in ("ai", "self", "assistant"):
        return "shared_profile"
    if user.startswith("viewer_") or "弹幕" in old.get("content", ""):
        return "viewer_profile"
    # 启发式
    content = old.get("content", "")
    if any(kw in content for kw in ["今天", "昨天", "刚才", "吃了", "去了"]):
        return "daily_life"
    if any(kw in content for kw in ["工作", "项目", "代码", "会议"]):
        return "work_tasks"
    return "shared_profile"


def _infer_topic(content: str) -> str:
    topic_keywords = {
        "identity": ["我是", "我叫"],
        "preference": ["喜欢", "讨厌", "最爱"],
        "habit": ["习惯", "经常", "每天"],
        "relationship": ["朋友", "家人", "同事", "对象"],
        "experience": ["去过", "试过", "经历"],
        "emotion": ["开心", "难过", "生气", "焦虑"],
        "schedule": ["明天", "下周", "待办"],
    }
    for topic, kws in topic_keywords.items():
        if any(kw in content for kw in kws):
            return topic
    return "general"


def _parse_timestamp(ts) -> float:
    if not ts:
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return 0.0


async def migrate(json_path: str, db_path: str, *, batch: int = 100):
    """批量迁移。"""
    old_records = parse_old_memu(json_path)
    print(f"读取到 {len(old_records)} 条旧记忆")
    
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            namespace TEXT NOT NULL DEFAULT 'shared_profile',
            user TEXT NOT NULL,
            content TEXT NOT NULL,
            topic TEXT DEFAULT 'general',
            confidence REAL DEFAULT 0.8,
            created_at REAL NOT NULL,
            last_accessed REAL DEFAULT 0,
            access_count INTEGER DEFAULT 0,
            source TEXT,
            metadata TEXT,
            onnx_vec BLOB,
            hash_vec BLOB
        )
    """)
    
    # 计算 hash_vec（与 LiteMemoryBackend 一致）
    from src.llm.memory.lite import HashDomainEncoder
    encoder = HashDomainEncoder()
    
    rows = []
    for old in old_records:
        rec = transform_record(old)
        hash_vec = encoder.encode(rec["content"])
        rows.append((
            rec["namespace"], rec["user"], rec["content"], rec["topic"],
            rec["confidence"], rec["created_at"], rec["last_accessed"],
            rec["access_count"], rec["source"], rec["metadata"],
            None,  # onnx_vec（旧记录无）
            hash_vec.tobytes(),
        ))
    
    # 批量插入
    inserted = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i+batch]
        conn.executemany(
            "INSERT INTO memories (namespace, user, content, topic, confidence, "
            "created_at, last_accessed, access_count, source, metadata, onnx_vec, hash_vec) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            chunk,
        )
        conn.commit()
        inserted += len(chunk)
        print(f"  已迁移 {inserted}/{len(rows)}")
    
    conn.close()
    print(f"✓ 迁移完成：{inserted} 条 → {db_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("json_path", help="旧 memU 导出 JSON")
    parser.add_argument("db_path", help="新 lite.db 路径")
    args = parser.parse_args()
    asyncio.run(migrate(args.json_path, args.db_path))
```

#### 4.1.4 使用方法

```bash
# 1. 从 memU 导出
python -c "from tools.memory import memory; import json; print(json.dumps(memory.list_files(), ensure_ascii=False, indent=2))" > data/old_memories.json

# 2. 迁移
python scripts/migrate_memu_to_lite.py data/old_memories.json data/memories/lite.db

# 3. 验证
sqlite3 data/memories/lite.db "SELECT COUNT(*), namespace FROM memories GROUP BY namespace"

# 4. 切换后端（在 .env）
MEMORY_BACKEND=lite

# 5. 重启 E.V
python main.py
```

#### 4.1.5 回滚

旧 memU 数据保留在 `data/old_memories.json`，切换回 `MEMORY_BACKEND=memu` 即恢复。

### 4.2 配置格式迁移

**无破坏性改动**：通过 `@property` 兼容旧 `cfg.XXX` 访问。

**新增配置项**（追加到 `.env`，使用默认值即可）：

```bash
# 知识库
KNOWLEDGE_ENABLED=true
KNOWLEDGE_ROOT=data/knowledge

# 记忆后端
MEMORY_BACKEND=lite
MEMORY_DB_PATH=data/memories/lite.db
MEMORY_ONNX_MODEL_PATH=data/models/multilingual-MiniLM.onnx

# Agent
AGENT_ENABLED=false
AGENT_BUDGET=4096
AGENT_MAX_STEPS=8
AGENT_REQUIRE_APPROVAL=true

# 多 Provider
LLM_PROVIDER=openai_compat  # 可选 anthropic
```

### 4.3 知识库初始化

新目录 `data/knowledge/`，初始内容随仓库提供：

```
data/knowledge/
├── curated_cards/
│   ├── 01-identity.md
│   ├── 02-style.md
│   └── README.md
├── facts.yaml
├── persona_lore.md
└── world_lore/
    └── stream_lore.md
```

**无迁移需求**（全新功能）。

### 4.4 兼容性矩阵

| 旧 API | 新 API | 兼容方式 |
|---|---|---|
| `cfg.LLM_BASE_URL` | `cfg.llm.base_url` | `@property` 转发 |
| `cfg.MEMORY_ENABLED` | `cfg.memory.enabled` | `@property` 转发 |
| `memory.commit_recall_files` | `MemoryManager.commit_recall_files` | Facade 保留 |
| `memory.add_turn` | `MemoryManager.add_turn` | Facade 保留 |
| `memory.recent_turns` | `MemoryManager.recent_turns` | Facade 保留 |
| `memory.list_files` | `MemoryManager.list_files` | Facade 保留 |
| `Application._cmd_*` | `commands/*_cmd.py` | 新模块，旧 API 标记 deprecated |
| `Application.__init__` | `RuntimeContext.__init__` | 不兼容（需 main.py 改造） |
| `AsyncOpenAI` client | `LLMProvider` 抽象 | `get_provider(cfg)` 替换 |
| `tools.memory.memu` | `src.llm.memory.lite` 或 `memu_compat` | 通过 `MEMORY_BACKEND` 切换 |

---

## 第 5 章：测试方案

### 5.1 测试目录结构

```
tests/
├── conftest.py                  # 公共 fixture
├── unit/
│   ├── conftest.py
│   ├── core/
│   │   ├── test_runtime.py
│   │   ├── test_output_lock.py
│   │   ├── test_bus.py
│   │   └── test_exceptions.py
│   ├── llm/
│   │   ├── test_knowledge_gate.py
│   │   ├── test_knowledge_recall.py
│   │   ├── test_memory_lite.py
│   │   ├── test_memory_lifecycle.py
│   │   ├── test_memory_namespace.py
│   │   ├── test_memory_decay.py
│   │   ├── test_providers.py
│   │   ├── test_agent_loop.py
│   │   ├── test_agent_sandbox.py
│   │   └── test_agent_workspace.py
│   ├── handlers/
│   │   ├── test_input_handler.py
│   │   ├── test_danmaku_handler.py
│   │   └── test_chat_handler.py
│   ├── commands/
│   │   ├── test_registry.py
│   │   ├── test_memory_cmd.py
│   │   └── test_config_cmd.py
│   └── utils/
│       ├── test_config.py
│       └── test_skill_scanner.py
├── integration/
│   ├── conftest.py
│   ├── test_brain_with_knowledge.py
│   ├── test_brain_with_memory.py
│   ├── test_brain_with_agent.py
│   ├── test_danmaku_flow.py
│   ├── test_chat_interrupt.py
│   ├── test_proactive_flow.py
│   └── test_hot_reload.py
├── e2e/
│   ├── conftest.py
│   ├── test_live_stream.py
│   ├── test_pet_mode.py
│   └── test_full_workflow.py
├── fixtures/
│   ├── knowledge/
│   │   ├── curated_cards/
│   │   ├── facts.yaml
│   │   └── persona_lore.md
│   ├── memories/
│   │   └── old_memories.json
│   └── responses/
│       └── mock_llm_responses.json
└── performance/
    ├── test_recall_latency.py
    ├── test_startup_time.py
    └── test_token_consumption.py
```

### 5.2 关键测试用例（按模块）

#### 知识库信号闸门（13 个用例，详见 §3.1.7）

- ✅ 纯标点零注入
- ✅ 短消息无实体零注入
- ✅ 实体命中注入 L0a+L0b
- ✅ 剧情意图注入全层
- ✅ 中等长度默认 L0a
- ✅ 英文意图识别

#### 记忆系统（20+ 用例）

- 基础 CRUD
- 命名空间隔离
- 时间衰减
- Mem0 判决链
- 整库整合
- ONNX 加载（无模型时降级到 hash only）
- hash 编码正确性
- 并发安全

#### LLM Provider（8+ 用例）

- OpenAI 兼容 provider 正常调用（mock）
- 未知 provider 回退
- 工具调用解析
- 流式响应
- 用量统计
- 错误重试

#### Agent（15+ 用例）

- 单步完成
- 多步循环
- 最大步数限制
- 沙箱路径检查
- 沙箱命令白名单
- 高风险动作触发审批
- Token 预算触发压缩
- 进度回调

#### 事件总线（10+ 用例）

- 订阅/取消订阅
- 通配符监听
- handler 异常隔离
- 异步 handler

#### 配置（5+ 用例）

- 环境变量解析
- bool/int/float 类型转换
- 旧 API 兼容

### 5.3 集成测试示例

**`tests/integration/test_danmaku_flow.py`**：

```python
"""端到端：弹幕 → 互斥锁 → LLM → TTS → 字幕。"""

import pytest
from src.core.runtime import RuntimeContext
from src.core.handlers.danmaku import DanmakuHandler


@pytest.fixture
async def runtime():
    cfg = make_test_config()
    cfg.BILI_ENABLED = False  # 模拟环境
    runtime = RuntimeContext(cfg)
    await runtime.setup()
    yield runtime
    await runtime.teardown()


class TestDanmakuFlow:
    async def test_single_danmaku_triggers_reply(self, runtime, monkeypatch):
        # Mock LLM
        async def mock_chat(messages, **kw):
            for chunk in ["你好", "，", "我是", "E.V"]:
                yield ChatChunk(delta=chunk)
        monkeypatch.setattr(runtime.brain._provider, "chat_stream", mock_chat)
        
        handler = DanmakuHandler(runtime)
        items = [(12345, "测试观众", "你好")]
        
        await handler.chat(items)
        
        # 验证：字幕被推送
        # 验证：记忆被提取（mock LLM 返回）
        # 验证：互斥锁已释放
        from src.core.output_lock import get_global_state
        assert get_global_state() == "idle"
```

### 5.4 性能基准（`tests/performance/`）

#### TTFT 基准（已有 `test_llm_ttft.py`，扩展）

```python
"""TTFT (Time To First Token) 基准。"""

class TestTTFT:
    async def test_knowledge_injection_overhead(self, runtime):
        # 测量：知识库注入对 TTFT 的影响
        no_kb_ttft = await measure_ttft(runtime, "你好", knowledge=False)
        with_kb_ttft = await measure_ttft(runtime, "你是谁", knowledge=True)
        # 知识注入增加 < 200ms
        assert with_kb_ttft - no_kb_ttft < 0.2
    
    async def test_memory_recall_overhead(self, runtime):
        # 测量：记忆召回延迟
        latencies = []
        for _ in range(10):
            t0 = time.time()
            await runtime.mm.recall("用户喜欢喝咖啡")
            latencies.append(time.time() - t0)
        # P99 < 100ms
        assert sorted(latencies)[int(len(latencies) * 0.99)] < 0.1
```

#### 启动时间基准

```python
class TestStartupTime:
    async def test_cold_start_under_5s(self):
        t0 = time.time()
        runtime = RuntimeContext(config.cfg)
        await runtime.setup()
        elapsed = time.time() - t0
        assert elapsed < 5.0
```

#### Token 消耗基准

```python
class TestTokenConsumption:
    async def test_chitchat_no_extra_tokens(self, runtime, mock_provider):
        mock_provider.usage = {"prompt_tokens": 100, "completion_tokens": 50}
        await runtime.brain.converse("哈哈", mock_provider)
        # 闲聊不应触发知识注入
        assert mock_provider.last_call["prompt_tokens"] < 200
    
    async def test_intent_message_injects_lore(self, runtime, mock_provider):
        mock_provider.usage = {"prompt_tokens": 800, "completion_tokens": 50}
        await runtime.brain.converse("你跟卡芙卡什么关系", mock_provider)
        # 注入知识，prompt tokens 增加，但 < 2000
        assert mock_provider.last_call["prompt_tokens"] < 2000
```

### 5.5 直播场景压力测试

**`tests/stress/test_danmaku_burst.py`**：

```python
"""压力测试：模拟 100 条/秒弹幕密度。"""

class TestHighThroughput:
    async def test_dense_danmaku_no_deadlock(self, runtime):
        items_list = [
            [(i, f"用户{i}", f"弹幕{i}") for i in range(10)]
            for _ in range(10)
        ]
        
        tasks = [runtime.handlers["danmaku"].chat(items) for items in items_list]
        await asyncio.gather(*tasks, return_exceptions=True)
        
        # 验证：无死锁（互斥锁最终释放）
        assert get_global_state() == "idle"
```

### 5.6 测试覆盖率目标

| 模块 | 目标覆盖率 |
|---|---|
| `src/llm/knowledge/` | ≥ 85% |
| `src/llm/memory/` | ≥ 80% |
| `src/agent/` | ≥ 80% |
| `src/llm/providers/` | ≥ 70% |
| `src/core/` | ≥ 75% |
| `src/llm/llm_brain.py` | ≥ 60% |
| `src/core/runtime.py` | ≥ 70% |
| **整体** | **≥ 70%** |

### 5.7 CI 配置

**`.github/workflows/test.yml`**：

```yaml
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.11", "3.12"]
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: ${{ matrix.python-version }}
      - run: pip install -e ".[dev]"
      - run: pytest tests/unit -v --cov=src --cov-report=xml
      - run: pytest tests/integration -v
      - uses: codecov/codecov-action@v3
```

---

## 第 6 章：分阶段实施

### 总览

```
阶段0 ─→ 阶段1 ─→ 阶段2 ─→ 阶段3 ─→ 阶段4 ─→ 阶段5 ─→ 阶段6
准备     抽象层    记忆      知识库    Agent     UI适配   测试发布
1-2天    1周       1周       1周       1周       1周      1周
```

### 阶段 0：准备（1-2 天）

**目标**：搭好脚手架，确保后续可分阶段。

| 任务 | 工时 | 负责人 | 验收 |
|---|---|---|---|
| 创建 `tests/` 目录与 `conftest.py` | 0.5d | — | `pytest --collect-only` 通过 |
| 安装 pytest / pytest-asyncio / pytest-cov | 0.1d | — | `requirements-dev.txt` 完成 |
| 创建 CI workflow | 0.2d | — | GitHub Actions 跑通 |
| 文档骨架 | 0.2d | — | `docs/modules/` 目录结构 |
| Git 分支策略 | 0.1d | — | `main` / `develop` / `feature/*` |

**产出**：
- `tests/` 可运行
- `.github/workflows/test.yml` 在 PR 上自动跑
- 分支命名规范

**风险**：无（纯基础设施）。

### 阶段 1：底层抽象层（1 周）

**目标**：完成 `LLMProvider` 抽象、配置 dataclass 化、事件总线扩展。**不动业务逻辑**。

| 任务 | 文件 | 工时 |
|---|---|---|
| `src/llm/providers/base.py` | 新建 | 0.3d |
| `src/llm/providers/openai_compat.py` | 新建 | 0.5d |
| `src/llm/providers/registry.py` | 新建 | 0.2d |
| `src/llm/llm_brain.py` 改造 `__init__` | 改 | 0.3d |
| `src/llm/llm_brain.py` 改造 `converse` 调用 | 改 | 0.5d |
| `src/utils/config.py` 改造为 dataclass | 改 | 1.0d |
| `src/core/bus.py` 扩展（on/off/on_any） | 改 | 0.3d |
| `src/core/exceptions.py` 扩展 ErrorHandler | 改 | 0.3d |
| 单元测试 | 新建 | 0.5d |
| 集成测试（用 OpenAI 兼容跑通对话） | 新建 | 0.5d |

**验收**：
- `pytest tests/unit/llm/test_providers.py -v` 全绿
- `pytest tests/integration/test_brain_with_providers.py -v` 全绿
- 现有所有功能不变（手动冒烟测试一遍）
- 性能：TTFT 不劣化 >5%

**风险**：
- `converse` 改造可能破坏流式响应
- **缓解**：保留旧代码作为 `legacy_converse`，双轨运行 1 周后切换

**回滚**：`llm_brain.py` 改回 30 行（删除 provider 调用），旧 AsyncOpenAI 直接恢复。

### 阶段 2：记忆系统重构（1 周）

**目标**：`MemoryBackend` 抽象 + `LiteMemoryBackend` + Facade 兼容旧 API。MemU 仍可用。

| 任务 | 文件 | 工时 |
|---|---|---|
| `src/llm/memory/base.py` | 新建 | 0.2d |
| `src/llm/memory/lite.py` | 新建 | 1.5d |
| `src/llm/memory/memu_compat.py` | 新建 | 0.3d |
| `src/llm/memory/__init__.py` Facade | 新建 | 0.5d |
| `src/llm/memory/lifecycle.py` Mem0 判决链 | 新建 | 1.0d |
| `src/llm/memory/namespace.py` | 新建 | 0.2d |
| `src/llm/memory/decay.py` | 新建 | 0.3d |
| 迁移脚本 `scripts/migrate_memu_to_lite.py` | 新建 | 0.5d |
| 单元测试 | 新建 | 0.5d |
| 集成测试（`ButlerAgent` 用新 memory） | 新建 | 0.5d |

**验收**：
- `MEMORY_BACKEND=lite` 跑通完整对话
- `MEMORY_BACKEND=memu` 仍工作（兼容性）
- 现有 `/memory list/del/clear/decay` 命令不变
- 记忆提取/蒸馏流程不变

**风险**：
- Mem0 判决链增加 Token 消耗 ~20%
- **缓解**：默认 `MEMORY_LIFECYCLE_ENABLED=false`，opt-in 开启

**回滚**：删除 `src/llm/memory/`，但保留 `tools/memory/memu/`，`memory_manager` import 回退到旧路径。

### 阶段 3：知识库（1 周）

**目标**：知识库 + 信号闸门 + 视角锚点。

| 任务 | 文件 | 工时 |
|---|---|---|
| `data/knowledge/` 目录结构 + 初始内容 | 新建 | 0.5d |
| `src/llm/knowledge/__init__.py` | 新建 | 0.1d |
| `src/llm/knowledge/loader.py` | 新建 | 0.8d |
| `src/llm/knowledge/gate.py` | 新建 | 0.5d |
| `src/llm/knowledge/recall.py` | 新建 | 0.8d |
| `src/llm/knowledge/format.py` | 新建 | 0.3d |
| `src/llm/knowledge/bm25.py` | 新建 | 0.3d |
| `src/llm/llm_brain.py` 集成信号闸门 | 改 | 0.3d |
| `src/utils/config.py` 新增 KnowledgeConfig | 改 | 0.2d |
| 单元测试 | 新建 | 0.5d |
| 集成测试（脑内注入链路） | 新建 | 0.5d |
| 文档 `docs/modules/knowledge.md` | 新建 | 0.2d |

**验收**：
- 闲聊消息（"哈哈"）不注入任何知识
- 实体命中（"流萤"）注入 L0a+L0b
- 剧情意图（"你是谁"）注入全层
- TTFT 增加 < 200ms
- 准备好 5-10 张 curated_cards + 20-30 条 facts

**风险**：
- LLM 不遵守注入（继续幻觉）
- **缓解**：保留旧 system prompt 加载作为"兜底层"，知识库作为"补充层"

**回滚**：`llm_brain.py` 注释掉 `if self.knowledge_gate` 分支即可。

### 阶段 4：Agent 任务系统（1 周）

**目标**：ReAct 循环 + 沙箱 + 工作空间。

| 任务 | 文件 | 工时 |
|---|---|---|
| `src/agent/__init__.py` | 新建 | 0.1d |
| `src/agent/loop.py` | 新建 | 1.0d |
| `src/agent/planner.py` | 新建 | 0.3d |
| `src/agent/executor.py` | 新建 | 0.5d |
| `src/agent/sandbox.py` | 新建 | 0.5d |
| `src/agent/workspace.py` | 新建 | 0.5d |
| `src/agent/budget.py` | 新建 | 0.2d |
| `src/agent/approval.py` | 新建 | 0.3d |
| `src/agent/tools/builtin.py` + 4 个工具 | 新建 | 1.0d |
| `src/llm/llm_brain.py` `maybe_run_agent` 钩子 | 改 | 0.3d |
| `src/utils/config.py` AgentConfig | 改 | 0.2d |
| 单元测试 | 新建 | 0.5d |
| 集成测试 | 新建 | 0.5d |

**验收**：
- `!agent 测试` 触发 ReAct 循环
- 文件操作限定在 workspace
- 高风险操作触发审批
- Token 预算触发压缩
- 进度回调可被前端订阅

**风险**：
- ReAct 误操作（删除文件等）
- **缓解**：默认 `AGENT_ENABLED=false`，需用户显式开启 + 严格沙箱

**回滚**：删除 `src/agent/`，`maybe_run_agent` 不被调用即不生效。

### 阶段 5：Application.py 拆分 + UI 适配（1 周）

**目标**：1668 行 `Application` 拆为 `RuntimeContext` + 4 个 Handler + 8 个 Command。

| 任务 | 文件 | 工时 |
|---|---|---|
| `src/core/runtime.py` | 新建 | 1.0d |
| `src/core/handlers/base.py` | 新建 | 0.2d |
| `src/core/handlers/input.py` | 新建 | 0.5d |
| `src/core/handlers/danmaku.py` | 新建 | 0.5d |
| `src/core/handlers/chat.py` | 新建 | 0.5d |
| `src/core/handlers/mindcraft.py` | 新建 | 0.3d |
| `src/core/commands/base.py` + registry | 新建 | 0.5d |
| 8 个 command 文件 | 新建 | 1.0d |
| `src/core/application.py` 瘦身到 < 200 行 | 改 | 0.5d |
| `main.py` 适配新 RuntimeContext | 改 | 0.2d |
| UI 控制中心适配新命令注册 | 改 | 0.5d |
| 单元测试 | 新建 | 0.5d |
| 集成测试（完整流程） | 新建 | 0.5d |

**验收**：
- `application.py` < 200 行
- 所有命令工作
- 互斥锁机制不变
- 热重载细粒度化（`!config llm` / `!config memory`）

**风险**：
- 拆分时遗漏边界条件
- **缓解**：保留 `Application` 类为 legacy，feature flag 切换新旧两版

**回滚**：`git revert` 整批改动。

### 阶段 6：测试与发布（1 周）

**目标**：测试覆盖率达 70%+，文档完整，灰度发布。

| 任务 | 工时 |
|---|---|
| 补齐单元测试到覆盖率目标 | 2.0d |
| E2E 测试（模拟直播） | 1.0d |
| 性能基准 | 0.5d |
| 文档完善（`docs/modules/*.md`） | 1.0d |
| CHANGELOG 撰写 | 0.2d |
| 灰度发布（10% 用户） | 1.0d |
| Bug 修复 | 0.5d |
| 正式发布 | 0.2d |

**验收**：
- 测试覆盖率 ≥ 70%
- 所有 E2E 测试通过
- 性能无退化（附录 B）
- 文档完整
- 灰度 1 周无重大 issue

---

## 第 7 章：风险评估与回滚

### 7.1 技术风险

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| Provider 抽象破坏流式响应 | 中 | 高 | 旧 `AsyncOpenAI` 调用作为 fallback；阶段 1 保留双轨运行 |
| 记忆系统重构破坏 ButlerAgent | 中 | 高 | Facade 完全保留旧 API；`MEMORY_BACKEND=memu` 默认值；`commit_recall_files` 行为不变 |
| 知识库注入撑爆 token | 中 | 中 | 严格 `KNOWLEDGE_MAX_INJECT_CHARS=2000`；信号闸门 + 闲聊零注入 |
| Agent 误操作（删文件） | 低 | 高 | 默认 `AGENT_ENABLED=false`；沙箱 + 审批 + 路径白名单 |
| Application 拆分引入并发 bug | 中 | 高 | RuntimeContext 提供 atomic reload（先创建新实例，验证后切）；teardown 必须幂等 |
| 性能退化 >10% | 中 | 中 | 性能基准自动化（CI 跑）；任何模块不达标就 revert |
| 兼容性问题（旧用户数据） | 低 | 中 | 迁移脚本独立运行，旧 memU 数据保留；`cfg.LLM_BASE_URL` 兼容 property |
| 直播场景死锁 | 低 | 高 | 互斥锁不变；handler 拆分时严格测试锁释放路径 |

### 7.2 业务风险

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| 直播用户反馈"AI 变了" | 中 | 中 | 默认不启用新功能（`KNOWLEDGE_ENABLED=true` 但 `AGENT_ENABLED=false`）；通过 `.env` 控制 |
| 主播担心"AI 太聪明" | 低 | 低 | 知识库是"补全"而非"替换"，角色一致性提升 |
| 培训成本 | 中 | 低 | 文档 + 视频教程；保留旧 `.env` 配置可用 |

### 7.3 回滚方案

#### 模块级回滚

| 模块 | 回滚方式 | 数据影响 |
|---|---|---|
| Provider 抽象 | `git revert` 阶段 1 | 无（无 schema 变更） |
| 记忆系统 | `git revert` 阶段 2 + 切回 `MEMORY_BACKEND=memu` | 无（lite 库可保留也可删） |
| 知识库 | `git revert` 阶段 3 + `KNOWLEDGE_ENABLED=false` | 无 |
| Agent | `git revert` 阶段 4 + `AGENT_ENABLED=false` | 无 |
| Application 拆分 | `git revert` 阶段 5 | 无（仅代码组织） |
| 配置文件 | 删除 `data/workspace.json` 等新文件 | 无 |

#### 整体回滚

```bash
# 1. 创建回滚分支
git checkout main
git checkout -b refactor-revert

# 2. 标记可回滚的 commit
git log --oneline | grep "refactor:"
# 找到所有 refactor commit hash

# 3. 批量 revert（从新到旧）
git revert <latest-refactor-commit>
# ... 重复

# 4. 解决冲突（如有）后
git push origin refactor-revert
```

#### 数据回滚

- 知识库：`rm -rf data/knowledge/`（无数据丢失风险，新功能）
- 记忆库 lite：`rm -f data/memories/lite.db`（旧 memU 数据仍存）
- 工作空间：`rm -f data/workspace.json`（新功能）

### 7.4 灰度策略

#### 阶段 6 的灰度发布

```python
# src/utils/feature_flags.py
ROLLOUT_PERCENTAGE = 10  # 10% 用户启用

def is_enabled(flag: str) -> bool:
    if flag not in _FLAGS:
        return False
    if _FLAGS[flag]["percentage"] >= 100:
        return True
    # 基于 user_id 哈希
    import hashlib
    h = int(hashlib.md5(flag.encode()).hexdigest(), 16) % 100
    return h < _FLAGS[flag]["percentage"]

_FLAGS = {
    "knowledge": {"percentage": ROLLOUT_PERCENTAGE},
    "memory_lite": {"percentage": ROLLOUT_PERCENTAGE},
    "agent": {"percentage": 0},  # 不灰度
}
```

#### 用户分级

- **白名单用户**：开发者、内部测试 → 全功能开启
- **10% 灰度**：随机用户 → 新功能
- **90% 默认**：保持旧行为

#### 监控指标

- Token 消耗变化
- TTFT 变化
- 错误率
- 用户反馈（直播间弹幕关键词监控）

---

## 附录 A：API 兼容性矩阵

### 配置项

| 旧 key | 新 key | 类型 | 默认值 |
|---|---|---|---|
| `LLM_BASE_URL` | `LLM_BASE_URL` / `cfg.llm.base_url` | str | — |
| `LLM_API_KEY` | `LLM_API_KEY` / `cfg.llm.api_key` | str | — |
| `LLM_MODEL` | `LLM_MODEL` / `cfg.llm.model` | str | — |
| `LLM_TIMEOUT` | `cfg.llm.timeout` | float | 60.0 |
| `HISTORY_WINDOW` | `cfg.llm.history_window` | int | 20 |
| `BILI_ROOM_ID` | `cfg.danmaku.room_id` | int | — |
| `BILI_ENABLED` | `cfg.danmaku.enabled` | bool | false |
| `BILI_SERVER_PORT` | `cfg.danmaku.server_port` | int | 8765 |
| `TTS_ENABLED` | `cfg.voice.tts_enabled` | bool | true |
| `STT_ENABLED` | `cfg.voice.stt_enabled` | bool | false |
| `STT_MODEL` | `cfg.voice.stt_model` | str | "small" |
| `PROACTIVE_ENABLED` | `cfg.proactive.enabled` | bool | true |
| `EVOLUTION_PERIODIC_INTERVAL` | `cfg.proactive.interval` | int | 1800 |
| `MEMORY_ENABLED` | `cfg.memory.enabled` | bool | true |
| `VTS_ENABLED` | `cfg.vts.enabled` | bool | true |
| **新增** | `LLM_PROVIDER` / `cfg.llm.provider` | str | "openai_compat" |
| **新增** | `KNOWLEDGE_ENABLED` / `cfg.knowledge.enabled` | bool | true |
| **新增** | `KNOWLEDGE_ROOT` / `cfg.knowledge.root` | str | "data/knowledge" |
| **新增** | `MEMORY_BACKEND` / `cfg.memory.backend` | str | "lite" |
| **新增** | `MEMORY_ONNX_MODEL_PATH` / `cfg.memory.onnx_model_path` | str | — |
| **新增** | `AGENT_ENABLED` / `cfg.agent.enabled` | bool | false |
| **新增** | `AGENT_BUDGET` / `cfg.agent.budget` | int | 4096 |
| **新增** | `AGENT_MAX_STEPS` / `cfg.agent.max_steps` | int | 8 |

### Python API

| 旧 API | 新 API | 兼容 |
|---|---|---|
| `cfg.LLM_BASE_URL` | `cfg.llm.base_url` | ✅ property 转发 |
| `Application.__init__(cfg)` | `RuntimeContext(cfg).setup()` | ⚠️ 需 main.py 改造 |
| `Application._chat_danmaku()` | `DanmakuHandler(runtime).chat()` | ❌ 需迁移 |
| `Application._cmd_reload_config()` | `commands/config_cmd.py` | ❌ 需迁移 |
| `memory.commit_recall_files()` | `MemoryManager.commit_recall_files()` | ✅ Facade 保留 |
| `memory.add_turn()` | `MemoryManager.add_turn()` | ✅ Facade 保留 |
| `memory.recent_turns` | `MemoryManager.recent_turns` | ✅ Facade 保留 |
| `memory.list_files()` | `MemoryManager.list_files()` | ✅ Facade 保留 |
| `AsyncOpenAI` direct | `LLMProvider` 抽象 | ❌ 需改造 |
| `ButlerAgent.extract_and_store()` | `ButlerAgent.extract_and_store_with_lifecycle()` | ✅ 旧方法保留 |

### HTTP API（`/api/*`）

如有 HTTP API（如 `src/asr/asr_server.py`），保持不变。

### 事件总线事件

| 旧事件 | 新事件 | 兼容 |
|---|---|---|
| `user_input` | `user_input` | ✅ |
| `error` | `error` | ✅ |
| `session_end` | `session_end` | ✅ |
| **新增** | `output_state_changed` | — |
| **新增** | `knowledge_injected` | — |
| **新增** | `memory_decayed` | — |
| **新增** | `agent_step` | — |
| **新增** | `agent_done` | — |

---

## 附录 B：性能预算

| 指标 | 当前 | 重构后预算 | 测量方法 |
|---|---|---|---|
| 启动时间 | ~3-5s | < 6s（+20%） | `time python main.py` |
| TTFT（无知识注入） | 800ms | < 900ms（+12%） | `test_llm_ttft.py` |
| TTFT（有知识注入） | N/A | < 1100ms | 新增 benchmark |
| 记忆召回 P99 | ~50ms | < 100ms | 性能测试 |
| 知识库 recall P99 | N/A | < 50ms | 性能测试 |
| Agent 单步延迟 | N/A | < 3s | 性能测试 |
| 互斥锁等待 | ~10ms | < 20ms | 性能测试 |
| Token 消耗（普通对话） | 基准 | < 110%（+10% 信号闸门开销） | mock 测量 |
| Token 消耗（剧情对话） | 基准 | < 130%（+30% 知识注入） | mock 测量 |
| 内存占用 | ~500MB | < 600MB（+100MB ONNX 推理） | `psutil` |
| 直播弹幕密度 | 50/s | > 50/s（无退化） | 压力测试 |

**监控脚本**（`scripts/benchmark.py`）：

```python
"""综合性能基准。"""
import time
import asyncio
import statistics


async def benchmark_ttft(runtime, n=10):
    latencies = []
    for _ in range(n):
        t0 = time.time()
        async for _ in runtime.brain._provider.chat_stream([{"role": "user", "content": "hi"}]):
            first_token_time = time.time()
            break
        latencies.append(first_token_time - t0)
    return {
        "p50": statistics.median(latencies),
        "p99": sorted(latencies)[int(n * 0.99)],
        "max": max(latencies),
    }


async def benchmark_recall(runtime, n=100):
    latencies = []
    for i in range(n):
        t0 = time.time()
        await runtime.mm.recall(f"query {i}")
        latencies.append(time.time() - t0)
    return {
        "p50": statistics.median(latencies),
        "p99": sorted(latencies)[int(n * 0.99)],
    }


if __name__ == "__main__":
    runtime = ...
    print("TTFT:", asyncio.run(benchmark_ttft(runtime)))
    print("Recall:", asyncio.run(benchmark_recall(runtime)))
```

---

## 附录 C：配置项映射表

> 此表展示每个旧 `.env` 配置项如何映射到新结构（`.env` key 不变，仅内部结构改变）。

```bash
# .env (旧) → .env (新, 完全兼容)
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-xxx
LLM_MODEL=gpt-4o-mini
LLM_TIMEOUT=60

BILI_ENABLED=true
BILI_ROOM_ID=12345
BILI_SERVER_PORT=8765

TTS_ENABLED=true
STT_ENABLED=false
STT_MODEL=small

PROACTIVE_ENABLED=true
EVOLUTION_PERIODIC_INTERVAL=1800

MEMORY_ENABLED=true

VTS_ENABLED=true

# === 新增配置项（带默认值，旧用户无需修改） ===
LLM_PROVIDER=openai_compat
KNOWLEDGE_ENABLED=true
KNOWLEDGE_ROOT=data/knowledge
KNOWLEDGE_MAX_INJECT_CHARS=2000
MEMORY_BACKEND=lite
MEMORY_DB_PATH=data/memories/lite.db
MEMORY_ONNX_MODEL_PATH=
MEMORY_LIFECYCLE_ENABLED=false
MEMORY_DECAY_INTERVAL_SEC=86400
AGENT_ENABLED=false
AGENT_BUDGET=4096
AGENT_MAX_STEPS=8
AGENT_REQUIRE_APPROVAL=true
AGENT_BROWSER_ENABLED=false
```

**优先级**：新结构 > 旧结构（同名字段，**新结构优先**）。

---

## 附录 D：监控指标

### 关键指标

| 指标 | 阈值 | 报警 |
|---|---|---|
| TTFT P99 | > 1.5s | Warning |
| 知识注入率 | > 50% | Info（可能配置不当） |
| 闲聊注入率 | > 5% | Error（信号闸门失效） |
| 记忆库大小 | > 10k 条 | Info |
| 记忆衰减清理/天 | > 100 条 | Info |
| Agent 任务成功率 | < 80% | Warning |
| Agent 任务拒绝率 | > 30% | Warning（沙箱过严） |
| 互斥锁等待 P99 | > 100ms | Warning |
| 互斥锁持有时间 | > 30s | Error（可能死锁） |
| Token 消耗/小时 | > baseline * 1.5 | Warning |

### 监控实现（`src/utils/metrics.py`）

```python
"""轻量级指标收集（不引入 Prometheus 等重依赖）。"""

import time
import threading
from collections import deque
from dataclasses import dataclass, field


@dataclass
class MetricPoint:
    name: str
    value: float
    timestamp: float
    tags: dict = field(default_factory=dict)


class MetricsCollector:
    def __init__(self, max_points: int = 10000):
        self._points: deque = deque(maxlen=max_points)
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()
    
    def record(self, name: str, value: float, **tags):
        with self._lock:
            self._points.append(MetricPoint(
                name=name, value=value, timestamp=time.time(), tags=tags))
    
    def increment(self, name: str, **tags):
        key = f"{name}:{tuple(sorted(tags.items()))}"
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1
    
    def get_percentile(self, name: str, p: float = 0.99) -> float:
        values = [p.value for p in self._points if p.name == name]
        if not values:
            return 0.0
        return sorted(values)[int(len(values) * p)]
    
    def get_counter(self, name: str) -> int:
        return sum(v for k, v in self._counters.items() if k.startswith(f"{name}:"))


# 全局实例
metrics = MetricsCollector()
```

### 集成

```python
# src/llm/llm_brain.py
async def converse(self, ...):
    t0 = time.time()
    async for chunk in self._provider.chat_stream(...):
        if first_chunk:
            metrics.record("llm_ttft", time.time() - t0, model=self._model)
            first_chunk = False
        yield chunk
    metrics.record("llm_total", time.time() - t0, model=self._model)
```

### 调试面板（`ui/debug_panel.py`）

简单的 PySide6 面板，实时显示关键指标：

```python
class DebugPanel(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        self.ttft_label = QLabel("TTFT P99: -")
        self.recall_label = QLabel("记忆召回 P99: -")
        self.lock_wait_label = QLabel("互斥锁等待 P99: -")
        layout.addWidget(self.ttft_label)
        layout.addWidget(self.recall_label)
        layout.addWidget(self.lock_wait_label)
        # 定时刷新
        self.timer = QTimer()
        self.timer.timeout.connect(self._refresh)
        self.timer.start(1000)
    
    def _refresh(self):
        self.ttft_label.setText(f"TTFT P99: {metrics.get_percentile('llm_ttft'):.3f}s")
        self.recall_label.setText(f"记忆召回 P99: {metrics.get_percentile('memory_recall'):.3f}s")
        self.lock_wait_label.setText(f"互斥锁等待 P99: {metrics.get_percentile('lock_wait'):.3f}s")
```

---

## 文档维护

- **版本**：v1.0
- **最后更新**：2026-08-16
- **维护者**：E.V 核心团队
- **变更流程**：重大架构调整必须先更新本文件，再实施
- **关联文档**：
  - `E.V_OPTIMIZATION_PLAN.md`（高层方案）
  - `docs/architecture.md`（架构图）
  - `docs/modules/*.md`（模块文档，实施时同步创建）

---

**附录 E：本文档速查表**

| 我想知道... | 跳到... |
|---|---|
| 为什么要重构 | §0.1 |
| 哪些文件要改 | §1.1, §3（每节"位置"小节） |
| 怎么改（代码） | §3（每节有完整代码示例） |
| 改完后怎么测试 | §5（每节有测试代码） |
| 改坏了怎么回滚 | §7.3 |
| 实施顺序 | §6 |
| 性能影响 | 附录 B |
| 配置怎么改 | 附录 C |
