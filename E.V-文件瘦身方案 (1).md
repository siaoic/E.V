# E.V 文件瘦身方案：从 788 个文件砍到 300 个以内

> **核心判断**：E.V 不需要换语言、不需要重写逻辑，只需要做 3 件事——**砍上帝目录、拆超级文件、收编分散配置**。
>
> **目标**：788 → 300 个文件、8 个 700+ 行文件 → 0 个、最大单文件 ≤ 400 行。
>
> **工作量**：1 人 × 1 周（不依赖 5.0 重构，可独立交付；与 5.0 配合做减半）。

---

## 0. 体检报告

| 指标 | 现状 | 严重度 |
|---|---|---|
| 文件总数 | 788（src 181 / tools 174 / plugins 119 / ui 37 / 其他 277）| 🔴 |
| `src/llm/` 子目录数 | 11（**上帝目录**：对话、记忆、进化、主动、技能、知识、stream、providers、sessiondb、tools、topics.yml 全塞一起） | 🔴 |
| `src/llm/` 被依赖次数 | 42 处 `from src.llm` import | 🔴 |
| 700+ 行文件 | 8 个（runtime 1270 / config 1534 / butler_agent 1011 / proactive 1029 / corpus 982 / widget 967 / llm_brain 935 / engine 827） | 🔴 |
| 配置文件散落 | 14+ 个：`.env` / `config.yaml` / `topics.yml` / `system_prompt.txt` / `evolution_profile.json` / `mcp_config.json` / `enabled_plugins.json` / `pyproject.toml` / `lefthook.yml` / 各 plugin 内 ... | 🟡 |
| 死代码 | `src/adapter/` 4 个 Base class 没人继承（5.0 规划里要重做） | 🟡 |
| `tools/` 混血 | 既是第三方包（gsv_tts、memu）又是自研（mcp_bing、memory）| 🟡 |
| 命名风格 | snake_case 与 camelCase 混用（`BaseLLMAdapter` vs `self.brain`） | 🟢 |

**总判断**：架构思路没问题，**组织能力是瓶颈**。

---

## 1. 瘦身三原则（先看再动手）

### 原则 1：**先砍后建**
`src/adapter/` 4 个文件、`src/llm/topics.yml`、`src/llm/utils/` 是死代码/可有可无，先删后加。

### 原则 2：**一个目录只管一件事**
`src/llm/` 把 11 件不同的事塞一起——拆。改完一个目录应该一眼能看出"这是干啥的"。

### 原则 3：**大文件 = 烂代码的信号**
1270 行的 `runtime.py` 不会是真的"都在做对的事"——里面至少有 400 行是流程控制本不该在内核里。

---

## 2. 目标目录结构（瘦身版）

```
E.V/
├── main.py                      # 30 行：最薄入口
├── pyproject.toml               # 单一项目元数据
├── requirements/                # 依赖分层（按 profile 装）
│   ├── base.txt
│   ├── llm.txt / tts-gsv.txt / tts-edge.txt
│   ├── stt.txt / pet.txt / ui.txt
│   └── dev.txt
├── configs/                     # 所有配置集中
│   ├── .env.example             # 密钥模板
│   ├── profiles/                # ← 新增：profile 目录
│   │   ├── live/profile.yaml
│   │   ├── pet/profile.yaml
│   │   └── headless/profile.yaml
│   ├── personas/                # ← 收纳：人格 SKILL.md
│   │   ├── default/SKILL.md
│   │   └── feiniu/SKILL.md
│   ├── plugins/                 # ← 收纳：插件清单
│   │   ├── enabled.json
│   │   └── disabled.json
│   ├── mcp.json                 # ← 收纳：MCP 配置
│   └── data/                    # ← 运行时数据（gitignore）
│       ├── evolution_profile.json
│       ├── session.jsonl
│       └── memory/
├── ev/                          # ← 替代 src/core/，E.V 自己的代码
│   ├── __init__.py
│   ├── kernel/                  # 微内核（5.0 时落地）
│   │   ├── __init__.py
│   │   ├── runtime.py           # 200 行
│   │   ├── slot.py              # Slot Registry
│   │   ├── profile.py           # Profile 加载
│   │   ├── event_bus.py         # 事件总线（升级 src/core/bus.py）
│   │   ├── session_log.py       # append-only 日志
│   │   ├── output_lock.py       # 三方输出互斥（沿用 src/core/）
│   │   └── commands.py          # 控制台命令注册
│   ├── domains/                 # ← 替代 src/llm/，按领域拆开
│   │   ├── chat/                # 对话
│   │   │   ├── brain.py         # 350 行（拆自 llm_brain.py）
│   │   │   ├── stream.py        # 流式管线
│   │   │   ├── history.py       # 会话历史
│   │   │   └── tools.py         # function calling 聚合
│   │   ├── memory/              # 记忆（沿用 src/llm/memory/，改名）
│   │   │   ├── store.py         # 记忆后端
│   │   │   ├── vector.py        # 向量检索
│   │   │   ├── lifecycle.py     # 增删改查
│   │   │   └── extract.py       # 信息提取（拆自 butler_agent）
│   │   ├── proactive/           # 主动对话
│   │   │   ├── engine.py
│   │   │   ├── topics.py        # 话题种子（从 llm/topics.yml 转）
│   │   │   └── scheduler.py
│   │   ├── evolution/           # 自我进化
│   │   │   ├── engine.py
│   │   │   ├── review.py        # 复盘
│   │   │   ├── feedback.py      # 反馈
│   │   │   └── profile.py       # 人格画像
│   │   ├── persona/             # 人格管理
│   │   │   ├── loader.py        # 加载 SKILL.md
│   │   │   └── manager.py
│   │   └── skills/              # 技能（SKILL.md 工具）
│   │       ├── manager.py
│   │       └── loader.py
│   ├── adapters/                # ← 替代 src/adapter/，强制使用
│   │   ├── llm.py               # Protocol 定义
│   │   ├── tts.py
│   │   ├── avatar.py
│   │   ├── input.py
│   │   └── danmaku.py
│   ├── io/                      # ← 替代 src/danmaku/ + src/asr/ + src/tts/
│   │   ├── tts/
│   │   ├── asr/
│   │   ├── danmaku/
│   │   └── input/
│   ├── render/                  # ← 替代 src/vts/ + src/pet/ + src/emotion/
│   │   ├── vts/
│   │   ├── pet/
│   │   ├── emotion/
│   │   └── avatar.py
│   ├── agent/                   # ← 沿用 src/agent/
│   ├── mcp/                     # ← 沿用 src/mcp/
│   └── utils/                   # 工具（减少到 5 个文件）
│       ├── paths.py             # 路径常量
│       ├── console.py           # 日志
│       ├── config.py            # ← 拆自 1534 行的 config.py
│       │                         #     拆成：env_loader + schema + reloader
│       ├── safety.py            # 内容过滤 + 安全文本（合并两个文件）
│       └── text.py              # 字幕 / safe_text / repetition_guard
├── builtin/                     # ← 替代 plugins/tools/，E.V 官方内置插件
│   ├── llm-zhipu/               # 一个 LLM provider = 一个目录
│   ├── llm-openai-compat/
│   ├── tts-gpt-sovits/
│   ├── tts-edge/
│   ├── avatar-vts/
│   ├── avatar-live2d/
│   ├── danmaku-bilibili/
│   ├── danmaku-twitch/
│   ├── memory-memu/
│   ├── memory-chroma/
│   ├── emotion-vts/
│   ├── pet-renderer/
│   ├── ui-control-center/
│   ├── tools/                   # 沿用 plugins/tools/
│   │   ├── screen.py
│   │   ├── weather.py
│   │   └── ...
│   └── personas/                # 默认人格（也可放 configs/personas/）
├── plugins/                     # ← 缩小：只放用户/第三方插件
│   ├── mindcraft/               # 第三方插件原样
│   ├── neuro-sdk/
│   └── _third_party/            # 第三方插件隔离
├── tools/                       # ← 缩小：只放第三方包（git submodule）
│   ├── gsv_tts/                 # git submodule
│   └── memu/                    # git submodule
├── live2d/                      # 资源（不变）
├── docs/                        # 文档（沿用）
├── scripts/                     # 脚本（沿用）
├── tests/                       # 测试（沿用）
└── ui/                          # 控制中心（沿用）
```

**对比**：
| 指标 | 现在 | 目标 | 减幅 |
|---|---|---|---|
| 顶层目录 | 11 个语义不清 | 9 个语义清晰 | -18% |
| `src/llm/` 子目录 | 11 | 6 个 domain | -45% |
| `src/` 顶层子目录 | 13 | 8 个（ev/domains、ev/io、ev/render ...）| -38% |
| 单文件最大行数 | 1534（config.py） | ≤ 400 | -74% |
| 文件总数 | 788 | **~300** | **-62%** |

---

## 3. 8 个超级文件拆解方案

按"先拆影响最大的"排序：

### 3.1 `src/utils/config.py`（1534 行）→ 3 文件

**问题**：单文件混了 .env 加载、17 个 dataclass、reload 逻辑、字段 loader 表、路径计算。

**拆法**：
```
ev/utils/config/
├── __init__.py          # 重新导出，保持 `from src.utils import config` 兼容
├── env_loader.py        # 200 行：load_dotenv + 字段 loader 表
├── schema.py            # 300 行：17 个 dataclass 拆分到这里
└── reloader.py          # 250 行：热更新逻辑
```

**兼容性**：外部仍 `from src.utils import config`，内部用 `from ev.utils.config import cfg`。

### 3.2 `src/core/runtime.py`（1270 行）→ 7 文件

**问题**：1270 行里有 11 个职责——LLM/TTS/VTS/弹幕/记忆/情绪/主动对话/MCP/插件/Agent/UI 的 setup。

**拆法**：
```
ev/kernel/runtime.py          # 200 行：纯 setup/teardown 骨架
ev/kernel/components/
├── llm.py                    # 100 行：LLM 初始化
├── tts.py                    # 80 行：TTS 初始化
├── avatar.py                 # 80 行：Avatar 初始化
├── danmaku.py                # 200 行：弹幕启动 + 回复循环（拆自 _chat_danmaku）
├── memory.py                 # 100 行：记忆初始化 + 整合循环
├── proactive.py              # 100 行：主动对话循环
├── evolution.py              # 80 行：进化循环
├── agent.py                  # 150 行：Agent 调度循环
├── plugin.py                 # 100 行：插件系统初始化
└── ui.py                     # 80 行：UI 初始化
```

每个 component 独立函数 `async def setup(runtime) -> None` + `async def teardown(runtime) -> None`。

**主调用**：
```python
# ev/kernel/runtime.py
class RuntimeContext:
    async def setup(self):
        for comp in self._components:
            await comp.setup(self)
    async def teardown(self):
        for comp in reversed(self._components):
            await comp.teardown(self)
```

### 3.3 `src/llm/llm_brain.py`（935 行）→ 4 文件

```
ev/domains/chat/
├── brain.py           # 350 行：核心 chat_stream
├── tools.py           # 200 行：function calling 聚合
├── context.py         # 200 行：上下文管理（turn_contexts、system_patches）
└── history.py         # 200 行：历史消息管理
```

### 3.4 `src/llm/butler_agent.py`（1011 行）→ 4 文件

```
ev/domains/memory/
├── store.py           # 300 行：记忆存储抽象
├── extract.py         # 250 行：信息提取（原 butler 核心）
├── integrate.py       # 200 行：记忆蒸馏
└── judge.py           # 200 行：增删改判
```

### 3.5 `src/llm/proactive.py`（1029 行）→ 3 文件

```
ev/domains/proactive/
├── engine.py          # 400 行：主循环
├── topics.py          # 250 行：话题种子（原 llm/topics.yml 移过来）
└── scheduler.py       # 300 行：冷却 + 调度
```

### 3.6 `src/tts/engine.py`（827 行）→ 2 文件

```
ev/io/tts/
├── gsv_client.py      # 400 行：GPT-SoVITS HTTP 客户端
└── engine.py          # 400 行：播放队列 + 字幕
```

（这个会和 plugins/tts-gpt-sovits 一起重构）

### 3.7 `src/pet/widget.py`（967 行）→ 3 文件

```
ev/render/pet/
├── widget.py          # 300 行：PySide6 widget
├── renderer.py        # 350 行：Live2D 渲染
└── driver.py          # 250 行：动作/表情驱动
```

### 3.8 `src/emotion/corpus.py`（982 行）→ 2 文件

```
ev/render/emotion/
├── classifier.py      # 500 行：Embedding 分类
└── corpus.py          # 400 行：语料
```

**8 个文件全部拆完，单文件最大行数 ≤ 500。**

---

## 4. 配置收编

### 4.1 现在的混乱

```
.env                              # 主配置（dotenv）
configs/config.example.yaml       # 覆盖层
src/llm/topics.yml                # 话题种子
ui/data/system_prompt.txt         # 人设
data/evolution_profile.json       # 人格画像（运行时生成）
src/mcp/mcp_config.json           # MCP 配置
plugins/enabled_plugins.json      # 插件启用清单
plugins/mindcraft/andy.json       # mindcraft 配置
lefthook.yml                      # git hook
pyproject.toml                    # 项目元数据
```

用户要改 1 件事，找 5 分钟不知道该改哪个文件。

### 4.2 收编方案

```
configs/                            # 唯一配置根目录
├── .env.example                    # 密钥 + 运行时调参（保留 dotenv）
├── profiles/                       # ← 新增：5.0 落地
│   ├── live/profile.yaml
│   ├── pet/profile.yaml
│   └── headless/profile.yaml
├── personas/                       # ← 收纳人格
│   ├── default/SKILL.md
│   └── feiniu/SKILL.md
├── plugins/                        # ← 收纳插件清单
│   ├── enabled.json                # （从 plugins/ 移过来）
│   └── disabled.json
├── mcp.json                        # ← 从 src/mcp/ 移过来
└── data/                           # 运行时数据（gitignore）
    ├── evolution_profile.json
    ├── session.jsonl               # ← 新增：会话日志
    ├── memory/                     # 记忆数据
    └── subtitles/                  # 字幕
```

**改动原则**：
- 用户手改的配置 → `configs/`
- 代码读的配置 → `configs/`（不放在 src/ 里）
- 运行时生成的数据 → `configs/data/`（gitignore）
- 包元数据 → `pyproject.toml`（保留）
- 第三方包内部配置 → 保留在第三方包内（如 mindcraft/andy.json）

### 4.3 迁移工具

写一个一次性脚本 `scripts/migrate_to_v5_layout.py`：
- 移动 `src/llm/topics.yml` → `configs/profiles/live/topics.yaml`
- 移动 `ui/data/system_prompt.txt` → `configs/personas/default/SKILL.md`
- 移动 `src/mcp/mcp_config.json` → `configs/mcp.json`
- 移动 `plugins/enabled_plugins.json` → `configs/plugins/enabled.json`
- 创建 `configs/data/` 占位 + `.gitkeep`
- 打印"找到 N 处 import 旧路径需要更新"

---

## 5. 死代码清理

### 5.1 必删

| 文件 | 为什么删 |
|---|---|
| `src/adapter/base.py` | BaseAdapter 0 处继承 |
| `src/adapter/llm.py` | BaseLLMAdapter 0 处继承，5.0 用 Protocol 重做 |
| `src/adapter/tts.py` | 同上 |
| `src/adapter/avatar.py` | 同上 |
| `src/adapter/input.py` | 同上 |
| `src/adapter/__init__.py` | 0 个真正 import |
| `src/llm/topics.yml` | 数据应该走 SKILL.md 或 configs/ |
| `src/llm/utils/` | 看内容，大概率是早期残留 |
| `src/llm/cleaners/` | 看内容，大概率未完成实验 |
| `src/llm/knowledge/` | 同上 |
| `src/llm/history/` | 同上 |
| `src/llm/tools/` | 同上 |
| `src/llm/client/` | 同上 |
| `src/llm/providers.py` | 看是否真用，没用就删 |

> **执行前先 grep**：每个文件 `grep -rln "from src.llm.knowledge" src/ plugins/ ui/`，如果 0 结果就删。

### 5.2 必搬

| 文件 | 搬到 |
|---|---|
| `src/llm/skills/` | `ev/domains/skills/` |
| `src/llm/memory/` | `ev/domains/memory/` |
| `src/llm/evolution/` | `ev/domains/evolution/` |
| `src/llm/proactive.py` | `ev/domains/proactive/engine.py` |
| `src/llm/llm_brain.py` | `ev/domains/chat/brain.py` |
| `src/llm/butler_agent.py` | `ev/domains/memory/extract.py` |
| `src/llm/stream.py` | `ev/domains/chat/stream.py` |
| `src/llm/sessiondb.py` | `ev/domains/chat/history.py` |
| `src/llm/auxiliary.py` | `ev/domains/chat/auxiliary.py` |
| `src/llm/tool_message_utils.py` | `ev/domains/chat/tool_utils.py` |
| `src/danmaku/*` | `ev/io/danmaku/*` |
| `src/tts/*` | `ev/io/tts/*` |
| `src/asr/*` | `ev/io/asr/*` |
| `src/vts/*` | `ev/render/vts/*` |
| `src/pet/*` | `ev/render/pet/*` |
| `src/emotion/*` | `ev/render/emotion/*` |
| `src/core/*`（部分） | `ev/kernel/*` |
| `src/agent/*` | `ev/agent/*`（不变） |
| `src/mcp/*` | `ev/mcp/*`（不变） |
| `src/utils/*` | `ev/utils/*` |

### 5.3 必拆

| 文件 | 拆成 |
|---|---|
| `src/utils/config.py`（1534 行） | 见 §3.1 |
| `src/core/runtime.py`（1270 行） | 见 §3.2 |
| `src/llm/llm_brain.py`（935 行） | 见 §3.3 |
| `src/llm/butler_agent.py`（1011 行） | 见 §3.4 |
| `src/llm/proactive.py`（1029 行） | 见 §3.5 |
| `src/tts/engine.py`（827 行） | 见 §3.6 |
| `src/pet/widget.py`（967 行） | 见 §3.7 |
| `src/emotion/corpus.py`（982 行） | 见 §3.8 |

---

## 6. `plugins/` 目录收编

### 6.1 现状

```
plugins/
├── __init__.py
├── base.py                    # Plugin 基类
├── context.py                 # PluginContext
├── manager.py                 # PluginManager
├── enabled_plugins.json       # 启用清单（空！）
├── tools/                     # ← 应该是 builtin/
│   ├── screen.py
│   ├── sfx.py
│   └── ...（11 个）
├── mindcraft/                 # ← 第三方插件
│   ├── asr.py
│   ├── dialogue_chat.py
│   └── ...（119 个文件）
└── neuro-sdk/                 # ← 第三方插件
```

### 6.2 拆分

```
plugins/
├── __init__.py
├── base.py                    # 沿用
├── context.py                 # 沿用
├── manager.py                 # 沿用
└── third_party/               # ← 第三方插件隔离
    ├── mindcraft/
    └── neuro-sdk/

# 官方内置插件单独一个目录
builtin/
├── tools/                     # 沿用 plugins/tools/ 内容
├── personas/                  # 默认人格
├── llm/                       # 内置 LLM 实现
│   ├── openai_compat.py
│   └── zhipu.py
├── tts/                       # 内置 TTS 实现
│   └── gsv.py
├── avatar/                    # 内置 Avatar
│   ├── vts.py
│   └── live2d.py
└── memory/
    └── memu.py
```

**好处**：
- 用户清楚哪些是"官方稳定"（builtin/），哪些是"第三方可能炸"（plugins/third_party/）
- 第三方插件升级不再污染 builtin/

---

## 7. 实施顺序（按 ROI 排）

### Day 1：死代码清理（4 小时）
- 删 `src/adapter/` 全部（4 个文件）
- 删 `src/llm/topics.yml`
- `grep -c` 检查 `src/llm/{cleaners,knowledge,history,tools,client,utils}/` 是否真用
- 运行测试，**保证 0 break**

**收益**：-6 个文件、-1500 行死代码

### Day 2：配置收编（4 小时）
- 建 `configs/profiles/` `configs/personas/` `configs/data/`
- 移 `topics.yml` `system_prompt.txt` `mcp_config.json` `enabled_plugins.json`
- 改 import 路径
- 跑测试

**收益**：配置从 5 个分散位置 → 1 个目录；用户找配置从 5 分钟 → 5 秒

### Day 3-4：拆 3 个最大文件（2 天）
- `config.py` 1534 → 3 文件
- `runtime.py` 1270 → 7 文件
- `llm_brain.py` 935 → 4 文件

**收益**：最大文件 1534 → ≤ 400 行；新功能更容易写

### Day 5：拆剩下 5 个大文件
- `butler_agent.py` `proactive.py` `engine.py` `widget.py` `corpus.py`

**收益**：所有文件 ≤ 500 行

### Day 6：目录搬迁
- `src/llm/` → `ev/domains/`
- `src/danmaku/` `src/tts/` `src/asr/` → `ev/io/`
- `src/vts/` `src/pet/` `src/emotion/` → `ev/render/`
- 改所有 import

**收益**：从 src/* 14 个子目录 → ev/ 8 个语义清晰的子目录

### Day 7：plugins 收编 + 测试
- `plugins/tools/` → `builtin/tools/`
- `plugins/mindcraft/` `plugins/neuro-sdk/` → `plugins/third_party/`
- 全量测试

**总计 1 人 × 1 周，788 → ~300 文件。**

---

## 8. 验收标准

| 指标 | 验收线 |
|---|---|
| 文件总数 | ≤ 300 |
| `src/llm/` 子目录 | 删除（拆到 ev/domains/） |
| 单文件最大行数 | ≤ 400 |
| 700+ 行文件 | 0 |
| 顶层目录 | ≤ 10 |
| `src/` 顶层子目录 | 0（全部移到 ev/） |
| 配置文件 | 全部在 `configs/` 根目录 |
| 死代码 | 0 个 0 引用文件 |
| 单元测试通过率 | 100% |
| 4.x 行为兼容性 | 100%（`python main.py` 跑出来一样） |

---

## 9. 风险与回滚

### 风险

| 风险 | 概率 | 应对 |
|---|---|---|
| 改 import 漏掉边角用例 | 中 | 每步跑 `grep -rln "from src.llm" .` 跟进 |
| 拆 runtime.py 拆坏主流程 | 高 | **Day 3-4 是单 PR**，跑完整直播流程 1 小时验证 |
| 配置迁移漏路径 | 低 | 写迁移脚本前先 `find . -name "*.json" -o -name "*.yaml" \| xargs ls` 全列出来 |
| 第三方插件（mindcraft）import 路径断了 | 高 | 先改 import 路径再移动文件；按 `grep -rln` 顺序迁移 |

### 回滚

每个 Sprint 1 天 = 1 个 commit。如果某天搞砸了：
```bash
git revert HEAD  # 回滚当天
```

不影响后续 Day 继续。

---

## 10. 与 5.0 重构的关系

| 阶段 | 文件瘦身（本方案） | 5.0 重构（前一份方案） |
|---|---|---|
| 时机 | 现在做（1 周） | 等瘦身完再开始（更省事） |
| 关系 | 准备动作 | 主菜 |
| 工作量 | 6 人日 | 12 人日（瘦身完做减半） |
| 兼容 | 100% 兼容 4.x | 兼容 4.x |
| 风险 | 低（机械搬迁） | 中（架构变更） |

**强烈建议**：先把这份瘦身做完，再做 5.0 重构。原因：
- 瘦身完，`src/llm/` 拆成 `ev/domains/{chat,memory,proactive,evolution,persona,skills}/`，5.0 的 Slot 抽象更好落
- 瘦身完，700+ 行大文件都没了，5.0 的 Kernel 才不会一上来就被 RuntimeContext 拖累
- 瘦身完，`src/adapter/` 死代码删了，5.0 才能用 Protocol 干净重写

---

## 11. 立刻能做的 3 件事（30 分钟见效）

如果你今天就想动手：

1. **删 `src/adapter/`**（5 分钟）
   ```bash
   # 先确认没人继承
   grep -rln "from src.adapter" src/ plugins/ ui/
   # 0 结果就删
   rm -rf src/adapter/
   ```

2. **查死代码**（10 分钟）
   ```bash
   # 找 src/llm 下没被引用的子目录
   for d in src/llm/*/; do
     count=$(grep -rln "from src.llm.${d%/}" src/ plugins/ ui/ 2>/dev/null | wc -l)
     echo "$d → $count references"
   done
   ```

3. **建 `configs/profiles/` `configs/personas/`**（15 分钟）
   ```bash
   mkdir -p configs/profiles configs/personas configs/data configs/plugins
   mv ui/data/system_prompt.txt configs/personas/default/SKILL.md
   mv src/llm/topics.yml configs/profiles/default/topics.yaml
   mv src/mcp/mcp_config.json configs/mcp.json
   mv plugins/enabled_plugins.json configs/plugins/enabled.json
   ```

**30 分钟能减 6 个文件 + 整 6 个分散配置**。

---

> **写在最后**：文件多不是病，"每个文件都能讲清楚干嘛"才是健康。E.V 现在的问题不是"文件多"，是"看完 `src/llm/` 11 个子目录不知道从哪看起"。**拆目录比加文档有用**——因为新人 onboarding 是先看目录再读文档的。
