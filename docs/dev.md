# E.V 开发规范

> 面向二次开发与贡献者。接口细节见 `docs/api.md`，架构见 `docs/architecture.md`。

## 1. 硬性约束（不可破坏）

- **原有业务逻辑、输入输出、异常行为 100% 保持不变**；禁止私自修改功能、接口、入参、返回值；
- **不引入额外第三方库、不新增依赖**（`pyproject.toml` 已有依赖范围内实现）；
- 不写炫技黑魔法语法，保证普通开发者能看懂维护；
- 保留关键业务注释，删掉垃圾注释；公共函数签名与输出格式不变；
- `ctypes` 保持懒加载（仍在 `_init_windows_ansi` 内部 import）；
- 主动对话、回复弹幕、用户对话三方输出必须通过全局互斥锁（`_OUTPUT_LOCK`）控制；
- 主动对话或回复弹幕期间，丢弃所有键盘输入和语音识别结果，不缓存；
- `main.py` 仅负责程序启动，业务逻辑在 `Application` 类中。

## 2. 编码规范

### 命名

| 类别 | 规则 | 示例 |
|---|---|---|
| 文件 / 模块 | `snake_case` | `dialogue_manager.py` |
| 类 | `PascalCase` | `TTSEngine`、`DanmakuPicker` |
| 变量 / 函数 | `snake_case`，语义化，不用晦涩缩写 | `next_wake_in` |
| 常量 | `UPPER_SNAKE_CASE`，消除魔法数字 | `_BATCH_MAX`、`_OUTPUT_LOCK` |
| 私有成员 | 前缀单下划线 | `_ai_speaking` |

### 代码风格

- 提取公共逻辑、消除代码重复、合理拆分函数/方法；
- 提前 `return` 简化嵌套；删除冗余代码、重复逻辑、无用变量、无效判断、废弃注释；
- 异步接口统一 `async/await`，不混用同步异步；
- 弹幕服务启动/关闭逻辑统一走公共函数 `_start_bili()` / `_stop_bili()`。

### 注释

- 核心接口、基类、事件模型必须写 docstring：说明入参、返回、异常、调用时机；
- 关键业务逻辑保留注释（说明「为什么」，不写「是什么」）。

### 日志

- 禁止零散 `print()`，统一用 `src/utils/console.py` 分级输出：
  `console.ok / info / warn / error / dim / accent / header / kv / chat`；
- 对话类内容（弹幕 / LLM 发言 / 用户输入）用 `console.chat`（控制中心据此路由到左侧面板）。

### 异常

- 禁止直接 `raise Exception`；外部服务失败统一抛 `EVBaseException`（`src/core/exceptions.py`），
  消息文本与原异常一致，错误码走 `ErrorCode` 枚举；
- 上层捕获 `EVBaseException` 统一日志 / 统一错误事件推送。

## 3. 新增模块步骤

1. 在对应 `src/<domain>/` 下新建模块文件，只暴露本模块能力，不反向引用主循环；
2. 模块配置项加入 `src/utils/config.py` 的 `Config`（从 `.env` 读取 + 默认值），并在 `docs/config.md` 登记；
3. 在 `src/core/application.py` 的 `run()` 中初始化、`finally` 中清理；需要热更新的加入 `!config` / `!tools` 派发；
4. 外部服务失败抛 `EVBaseException`；日志用 `console`；
5. 公共接口在 `docs/api.md` 登记（入参 / 返回 / 调用时机）。

## 4. 新增适配器教程

> 统一适配器层 `src/adapter/` 已定义外部服务（LLM / TTS / 形象 / 输入源）的抽象契约，
> 现有实现 `LLMBrain` / `TTSEngine` / `VTSController` / `STTEngine` 均继承对应基类
> （抽象方法 = 既有公共方法，行为不变）。新增服务 = 新增实现类并继承基类，
> 保持上层（LLM 大脑 / 主循环 / stream）调用签名不变。

### 4.1 新增 LLM 服务

本项目 LLM 走 OpenAI 兼容协议（`LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`），
智谱 / DeepSeek / 本地 vLLM 均可直接切换。新增独立实现时：

1. 继承 `src.adapter.llm.BaseLLMAdapter`，实现 `chat_stream` / `push_turn_context` / `reload_client`：

```python
from src.adapter.llm import BaseLLMAdapter

class MyLLMAdapter(BaseLLMAdapter):
    name = "my_llm"

    def chat_stream(self, user_text: str, *, proactive: bool = False,
                    history=None):
        """流式对话：逐句产出回复文本。

        Args:
            user_text: 用户输入（或主动对话决策文本）
            proactive: 是否以「内部自主行动指令」身份调用（不写历史）
            history: 可选历史快照（None = 完整历史）
        Yields:
            str: 逐段回复文本
        """
        ...

    def push_turn_context(self, contexts: list) -> None: ...
    def reload_client(self) -> None: ...
```

2. 配置项（`xxx_BASE_URL` / `xxx_API_KEY` / `xxx_MODEL`）加入 `Config`；
3. 调用失败抛 `EVBaseException(ErrorCode.LLM_CONNECT_FAILED, ...)` / `LLM_QUOTA_EXHAUSTED`；
4. 上层仍通过 `LLMBrain` 调用，`LLMBrain` 内部按配置选择客户端。

### 4.2 新增 TTS 服务

1. 继承 `src.adapter.tts.BaseTTSAdapter`，实现抽象方法集（`start`/`speak`/`drain`/
   `interrupt`/`clear_interrupt`/`stop`/`set_on_play_callback`/`set_subtitle_callback`/
   `apply_ref`/`apply_ref_extras`）；
2. 播放 / 口型 / 字幕复用 `src/tts/player.py` 的 `TTSPlayer`（真实开播时刻锚定 + 词级时间戳字幕）；
3. 服务异常抛 `EVBaseException(ErrorCode.TTS_SERVICE_ERROR, ...)` / `TTS_TIMEOUT`；
4. 主循环中替换 `TTSEngine()` 实例即可，上层（`stream.converse` / `speak_text`）不用改。

### 4.3 新增输入源（弹幕 / ASR / 其他）

1. 输入统一转成与现有输入源一致的「文本消息」交给主循环（键盘 / STT / 弹幕三种已在 `_wait_input` 汇合）；
2. 弹幕类输入先 `sanitize_external()` 净化（防 prompt 注入），走「精选 → 回复队列 → 完整对话链路」；
3. 播报期间（`is_rejecting_input()`）到达的输入直接丢弃，不缓存。

## 5. 插件开发

- 目录：`plugins/<插件名>/`，入口文件 + `metadata.json`；启用列表 `enabled_plugins.json`；
- 继承 `plugins/base.py` 的 `Plugin`，只实现用到的钩子；
- 事件对象直接可读写（`UserInputEvent` 等），详见 `docs/api.md` 第 15 节；
- 插件目录约定与示例见 `plugins/README.md`。

## 6. 测试与验证

- 无单元测试框架强约束（不新增依赖），改动后至少做**冒烟验证**：
  导入目标模块 → 调用新函数/类（空参数或最小参数）→ 确认无语法/导入错误、行为符合预期；
- 涉及外部服务（LLM / TTS / VTS / 弹幕）的改动，验证「服务不可用时的降级路径」不被破坏
  （如 TTS 未启动时 `start()` 返回 False、纯字幕模式正常）；
- 并发相关改动重点回归：三方播报互斥、播报期间输入丢弃、`/quit` 与 `!` 命令穿透。

## 7. 验证命令参考

```powershell
# 语法 / 导入冒烟（示例）
python -c "from src.core.exceptions import ErrorCode, EVBaseException; print(list(ErrorCode))"
python -c "from src.mcp.manager import MCPManager; print('ok')"
python -c "import src.llm.stream, src.llm.llm_brain; print('ok')"
```

## 8. Git 提交规范

- `main`：稳定版本，禁止直接 push；功能分支 `feat/xxx`，修复分支 `fix/xxx`；
- Commit message：

```
feat: 新增 XXX 能力
fix: 修复 XXX 问题
refactor: 重构 XXX
docs: 更新接口文档
perf: 优化 XXX 性能
```

- 禁止提交密钥（.env 等）、模型权重、大资源文件（见 `.gitignore`）。
