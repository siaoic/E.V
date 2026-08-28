# E.V 内部接口文档

> 本文档说明各模块对外提供的公共函数 / 类的入参、返回与调用时机。
> 适用于二次开发与维护；代码以 `src/` 下实现为准，文档随接口变更同步更新。
> 全局异常见文末「统一错误码」；所有模块间异步接口统一 `async/await`。

## 目录

- [1. 全局状态与互斥（src/core/output_lock.py）](#1-全局状态与互斥)
- [2. 统一异常（src/core/exceptions.py）](#2-统一异常)
- [3. 应用入口（src/core/application.py）](#3-应用入口)
- [4. LLM 大脑（src/llm/llm_brain.py）](#4-llm-大脑)
- [5. 对话输出流水线（src/llm/stream.py）](#5-对话输出流水线)
- [6. 记忆管家（src/llm/agent.py ButlerAgent）](#6-记忆管家)
- [7. 主动对话（src/llm/proactive.py）](#7-主动对话)
- [8. TTS 引擎（src/tts/engine.py）](#8-tts-引擎)
- [9. VTS 控制器（src/vts/controller.py）](#9-vts-控制器)
- [10. 口型驱动（src/vts/face_driver.py）](#10-口型驱动)
- [11. 字幕服务器（src/utils/subtitle_server.py）](#11-字幕服务器)
- [12. B 站弹幕（src/danmaku/bili_danmaku.py）](#12-b-站弹幕)
- [13. MCP 管理器（src/mcp/manager.py）](#13-mcp-管理器)
- [14. 插件系统（plugins/manager.py / base.py）](#14-插件系统)
- [15. 语音识别（src/asr/stt.py）](#15-语音识别)
- [16. 记忆系统（tools/memory/memory.py）](#16-记忆系统)
- [17. 配置接口（src/utils/config.py）](#17-配置接口)
- [18. 事件总线（src/core/bus.py）](#18-事件总线)
- [19. 消息结构体 Schema（src/core/events/models.py）](#19-消息结构体-schema)
- [20. 适配器抽象层（src/adapter/）](#20-适配器抽象层)

---

## 1. 全局状态与互斥

模块：`src/core/output_lock.py`

三方说话者（用户对话 / 主动对话 / 弹幕回复）通过全局 `asyncio.Lock` 互斥，
同时维护一个全局状态机（单线程读写，跨线程读取加锁）。

| 函数 | 签名 | 说明 |
|---|---|---|
| get_output_lock | `() -> asyncio.Lock` | 获取全局输出互斥锁。播报前 `async with` 获取，说话期间 `set_output_owner` 标记身份 |
| get_output_owner | `() -> Optional[str]` | 当前持有锁正在播报的一方：`"user"` / `"proactive"` / `"danmaku"` / `"command"` / `"mindcraft"` / `None` |
| set_output_owner | `(owner: Optional[str]) -> None` | 标记当前播报身份（进入播报置位，`finally` 还原为 `None`） |
| is_rejecting_input | `() -> bool` | 是否处于「主动对话 / 弹幕回复」播报（此时应拒收键盘/语音输入） |
| set_global_state | `(state: str) -> None` | 设置全局状态；非法值忽略。合法值：`idle` / `user_talking` / `ai_speaking` / `agent_thinking` |
| get_global_state | `() -> str` | 读取全局状态 |
| is_idle / is_busy | `() -> bool` | 空闲 / 忙碌判断（忙碌抑制 agent 触发） |
| set_danmaku_pending | `(pending: bool) -> None` | 置位/清除「弹幕回复待播报」标记（主动对话据此避让） |
| is_danmaku_pending | `() -> bool` | 是否有弹幕回复已敲定但未播报完成 |

**使用规范**：所有播报入口必须 `async with get_output_lock()` + `set_output_owner(...)` 成对使用，
并在 `finally` 中还原 owner / 状态，防止状态卡死。

## 2. 统一异常

模块：`src/core/exceptions.py`

| 符号 | 说明 |
|---|---|
| `ErrorCode`（IntEnum） | 统一错误码枚举，见下表 |
| `EVBaseException` | 项目基础异常：`__init__(code: ErrorCode, msg: str)`，`msg` 与原异常文本一致（不加前缀），`.code` / `.msg` 属性供上层统一处理 |

错误码分段：`0` 成功；`1xxx` 外部服务连接/调用异常；`4xxx` 参数/输入错误；`5xxx` 未分类内部错误。

| 错误码 | 值 | 含义 |
|---|---|---|
| SUCCESS | 0 | 成功 |
| LLM_CONNECT_FAILED | 1001 | LLM 连接失败 |
| LLM_QUOTA_EXHAUSTED | 1002 | LLM 配额 / 限流 |
| TTS_SERVICE_ERROR | 1003 | TTS 服务异常 |
| TTS_TIMEOUT | 1004 | TTS 合成超时 |
| AVATAR_CONNECTION_LOST | 1005 | VTS / 桌宠连接断开 |
| MCP_SERVER_FAILED | 1006 | MCP 服务器启动 / 连接失败 |
| MCP_TOOL_FAILED | 1007 | MCP 工具调用失败 |
| INVALID_EVENT_DATA | 4001 | 事件 / 消息数据不合法 |
| TOOL_NOT_FOUND | 4002 | 工具不存在 |
| INTERNAL_ERROR | 5000 | 未分类内部错误 |

**规范**：模块内禁止直接 `raise Exception`；外部服务失败统一抛 `EVBaseException(code=..., msg=原始消息)`。

## 3. 应用入口

模块：`src/core/application.py`；入口：`main.py`

| 符号 | 签名 | 说明 |
|---|---|---|
| `run_with_cleanup` | `async () -> None` | 运行 `Application().run()`；退出时（含被取消）关闭记忆连接 + 清理临时文件 |
| `Application.run` | `async () -> None` | 完整生命周期：校验配置 → 初始化渲染目标（桌宠/VTS）→ TTS → 记忆 → LLMBrain → 主动对话 → 插件 → STT → 弹幕 → 主循环等待输入 → 会话归档 → 清理 |

主循环输入流：键盘 `input()` / 语音识别（STT）/ 弹幕精选，均汇入 `_wait_input`；
播报期间键盘/语音输入被丢弃（`is_rejecting_input`）。无命令交互，Ctrl+C / EOF 退出。

## 4. LLM 大脑

模块：`src/llm/llm_brain.py`，类 `LLMBrain`

| 方法 | 签名 | 说明 |
|---|---|---|
| `__init__` | `(mcp=None)` | mcp 为 `MCPManager` 或 None |
| `chat_stream` | `async (user_text: str, *, proactive=False, history=None) -> AsyncIterator[Tuple[str, str]]` | 流式对话：按 `(mode, text)` 协议产出。`mode="delta"` 是打字机流式实时显示（累加 buffer），`mode="final"` 是完整可播分段（触发 TTS/字幕/复读检测/事件）。`proactive=True` 时不写入历史（只保留模型回复）；`history` 为可选历史快照（None 用完整历史） |
| `push_turn_context` | `(contexts: List[str]) -> None` | 注入本轮系统提示背景信息（插件注入用） |
| `reload_client` | `() -> None` | 重建 LLM 客户端（配置热更新后调用） |

内部流程：工具调用（本地插件工具 + MCP）→ 深度思考模式 → 最终回复；
系统提示由「人设 + 技能注入 + 观众画像 + 策略 + 记忆召回（2-gram 片段交集 + 向量记忆）」组装。

## 5. 对话输出流水线

模块：`src/llm/stream.py`

| 函数 | 签名 | 说明 |
|---|---|---|
| `converse` | `async (brain, text, *, tts=None, face=None, sub=None, proactive=False, profanity_filter=None, profanity_filter_rate=0.7, history=None, on_llm_done=None) -> None` | LLM 流式产句 → 打印 → 字幕 → 口型 → TTS 排队播放。`on_llm_done: Callable[[str], Awaitable[None]]` 在全文生成后后台并行调度（不等 TTS 播完），用于记忆提取 |
| `speak_text` | `async (text, *, tts=None, face=None, sub=None, proactive=False, profanity_filter=None, profanity_filter_rate=0.7) -> None` | 直接播报已有文本（无需 LLM）：过滤 → 打印 → 字幕 → 口型 → TTS。供主动发言使用 |

口型同步：有 TTS 时由音频播放回调 `load_speech_curve` 在音频真正播放瞬间触发；无 TTS 时 `start_speaking(duration)` 节拍口型。

## 7. 记忆管家

模块：`src/llm/agent.py`，类 `ButlerAgent`（OpenAI 兼容接口，默认与主对话共用服务或独立 `BUTLER_*` 配置）

| 方法 | 签名 | 说明 |
|---|---|---|
| `submit_extract_and_store` | `async (messages: list[dict], prev_turns: list[dict]) -> None` | 后台队列提交「对话 → 记忆提取 → 落库」（消息在 worker 串行处理，防并发乱序） |
| `summarize_session` | `async (turns: list[dict]) -> str` | 会话摘要（退出归档用） |
| `distill_session` | `async (turns: list[dict]) -> list[dict]` | 会话蒸馏为可落库记忆条目 |
| `integrate_memories` | `async (files: list[dict]) -> list[dict]` | 碎片记忆蒸馏整合（碎片数 ≥ 阈值时由后台循环触发） |
| `describe_image` | `async (image_b64: str, prompt="") -> str` | 多模态看图（look_at_screen 工具用） |
| `build_proactive_prompt` | `(context) -> str` | 构建主动开口决策 prompt（静态方法，供 ProactiveEngine 用） |

## 7. 主动对话

模块：`src/llm/proactive.py`，类 `ProactiveEngine`

| 方法 | 签名 | 说明 |
|---|---|---|
| `__init__` | `(brain, tts, face, sub, cfg, *, butler=None, memory_manager=None, profanity_filter=None, profanity_filter_rate=0.7)` | 注意：键参只能传关键字 |
| `on_user_message` | `() -> None` | 有用户输入 / 弹幕时调用：清孤独感、清冷却状态 |
| `heartbeat` | `async () -> bool` | 主动开口机会：互动结束 / 静默期到点调用；内部含忙碌抑制、弹幕避让、话题保护，由主模型自主决定是否开口；返回是否说了话 |
| `discard_pending` | `() -> int` | 丢弃排队中的主动消息（用户输入优先），返回丢弃条数 |
| `next_wake_in` | `() -> float` | 距离下一次唤醒的秒数（供主循环精确超时用） |
| `add_topic_seeds` | `(new_seeds: List[dict]) -> None` | 追加话题种子（进化引擎产出） |

## 9. TTS 引擎

模块：`src/tts/engine.py`，类 `TTSEngine`（GPT-SoVITS HTTP 服务端客户端）

| 方法 | 签名 | 说明 |
|---|---|---|
| `start` | `async () -> bool` | 探测服务端 `/` 可用性；未配置参考音频或服务未启动返回 False（语音降级为纯字幕） |
| `speak` | `async (text: str) -> None` | 入队合成并播放（串行泵批量调用 `/tts/batch`，逐句 `/audio/{filename}` 拉流） |
| `drain` | `async () -> None` | 等待队列播完 |
| `interrupt` / `clear_interrupt` | `() -> None` | 打断当前播放 / 复位打断标志（新一轮输出前调用） |
| `stop` | `async () -> None` | 关闭客户端 |
| `apply_ref` | `(ref_audio, ref_text) -> None` | 热更新主参考音频/文本 |
| `apply_ref_extras` | `(ref_audios: str) -> None` | 热更新辅助参考音频（`|` 分隔） |
| `set_on_play_callback` | `(cb: Callable[[str, str, float], None]) -> None` | 音频播放回调：`(wav_path, text, duration_s)`，用于口型同步 |
| `set_subtitle_callback` | `(cb: Callable[[str], None]) -> None` | 字幕推送回调 |

## 9. VTS 控制器

模块：`src/vts/controller.py`，类 `VTSController`（VTube Studio WebSocket 客户端）

| 方法 | 签名 | 说明 |
|---|---|---|
| `connect` | `async () -> bool` | 连接 + 认证（token 存 `data/vts_token.json`）；失败返回 False |
| `ensure_connected` | `async () -> bool` | 断线自动重连 |
| `on_event` | `(event_name: str, handler: Callable) -> None` | 订阅 VTS 事件（如 `ModelLoadedEvent`），handler 收消息 dict |
| `subscribe_event` | `async (event_name: str) -> bool` | 向 VTS 订阅事件，成功返回 True |
| `inject_mouth` | `async (value: float) -> None` | 注入口型参数（自动适配模型口型参数名） |
| `inject_parameters` | `async (params: Dict[str, float]) -> None` | 批量注入参数 |
| `trigger_hotkey` / `trigger_hotkey_by_name` | `async (...) -> bool` | 触发热键（表情/动作） |
| `trigger_motion` | `async (motion_file: str) -> bool` | 播放动作文件 |
| `activate_expression` | `async (expr_file: str, active=True) -> bool` | 切换表情 |
| `get_current_model` / `get_available_models` / `get_hotkeys` / `get_expressions` / `get_folder_info` / `get_input_parameters` / `get_output_parameters` | `async -> dict/list/tuple` | 模型扫描 / 热键 / 表情枚举用 |
| `load_model` | `async (model_id: str) -> bool` | 加载模型 |
| `close` | `async () -> None` | 断开连接 |

## 11. 口型驱动

模块：`src/vts/face_driver.py`，类 `FaceDriver`

| 方法 | 签名 | 说明 |
|---|---|---|
| `start` / `stop` | `() -> None` / `async -> None` | 启动/停止伪面捕循环（眨眼/呼吸/摇摆/待机动作） |
| `start_speaking` | `(duration: float) -> None` | 无 TTS 时的节拍口型（按时长张开） |
| `load_speech_curve` | `(wav_path: str) -> bool` | 从 wav 提取能量曲线驱动口型；成功返回 True |
| `stop_speaking` | `() -> None` | 停止口型 |
| `set_motion` / `stop_motion` | `(path: str) -> None` | 注入待机/循环动作 / 停止 |
| `apply_profile` | `(profile: ModelProfile) -> None` | 应用模型适配档案（`scan_model` 产出） |

## 12. 字幕服务器

模块：`src/utils/subtitle_server.py`，类 `SubtitleServer`（HTTP + SSE 网页字幕）

| 方法 | 签名 | 说明 |
|---|---|---|
| `start` | `() -> SubtitleServer` | 启动 HTTP 服务器（端口 8765 起 +1 递增，最多试 16 次）；返回 self，`port` 属性可用 |
| `push` | `(kind: str, text: str = "", speed_ms: int = 0) -> None` | `"text"` 累积打字机字幕；`"user"` 用户/观众发言独立行自动淡出；`"clear"` 清除并淡出 |
| `stop` | `() -> None` | 停止服务器 |

浏览器访问 `http://127.0.0.1:{port}/`，SSE 事件源 `GET /events`。

## 13. B 站弹幕

模块：`src/danmaku/bili_danmaku.py`

| 符号 | 签名 | 说明 |
|---|---|---|
| `BiliServiceManager` | `__init__(room_ids: List[int], port: int, html_path: str)` | 多房间管理：一个 `ThreadingHTTPServer` 共享 + 每房间独立 `BiliService` 与 blivedm 线程 |
| `start` / `stop` | `() -> None` | 启动（HTTP `/` 弹幕卡片页、`/events` SSE、`/avatar` 头像代理）/ 停止 |
| `broadcast` | `(msg: dict) -> None` | 向所有房间广播消息；`/events?room_id=X` 按房间过滤，主房间用 `broadcaster` 兜底 |
| `DanmakuPicker` | `__init__(on_reply_callback, *, on_batch_reply_callback=None, pool_size=50, window_s=..., max_gap_s=..., batch_score_delta=15, batch_max_items=3, cooldown_s=...)` | 弹幕精选池：满 50 踢「最低分」；高分顶替同用户旧弹幕 |
| `submit` | `(uid: int, username: str, text: str) -> None` | 弹幕入池（blivedm handler 线程同步调用，内部双堆 heapq） |
| `stop` | `() -> None` | 停止挑选器 |

弹幕到回复链路：blivedm handler → 清洗净化（`sanitize_external`）→ 脏话过滤 → `DanmakuPicker` 精选（单条或批量聚合 ≤3 条）→ 回复队列 → `_chat_danmaku` 完整对话链路（LLM→TTS→字幕→记忆）。

## 13. MCP 管理器

模块：`src/mcp/manager.py`，类 `MCPManager`

| 方法 | 签名 | 说明 |
|---|---|---|
| `initialize` | `async () -> bool` | 加载配置 + 自动同步 tools 文件夹 + 启动全部服务器 |
| `load_mcp_config` | `() -> None` | 重新读取外部 JSON 配置（`MCP_CONFIG_PATH`），`_disabled` 后缀服务器跳过 |
| `start_all_servers` / `start_server` | `async (name, server_config) -> None` | 启动服务器（stdio / http 两种传输） |
| `call_mcp_tool` | `async (tool_name: str, args: dict) -> str` | 调用工具，返回字符串结果 |
| `get_tools_for_llm` | `() -> List[dict]` | OpenAI Function Calling 格式工具列表 |
| `handle_tool_calls` | `async (tool_calls: list) -> Optional[list]` | 批量执行 LLM 下发的 tool_calls |
| `execute_function` | `async (tool_name: str, parameters: dict) -> str` | 工具执行统一入口（MCP 或本地插件工具） |
| `get_stats` | `() -> dict` | 统计（工具数 / 类型分布 / 名称） |
| `stop` | `async () -> None` | 停止全部服务器 |

配置：`src/mcp/mcp_config.json`（支持 `${ENV}` 占位符展开），`MCP_ENABLED` 总开关（默认关）。

## 15. 插件系统

模块：`plugins/manager.py`（PluginManager）、`plugins/base.py`（Plugin / 事件对象）

**PluginManager 主要方法**：

| 方法 | 签名 | 说明 |
|---|---|---|
| `load_all` / `start_all` / `stop_all` | `async -> None` | 加载启用列表插件 / 启动 / 停止 |
| `run_user_input_hooks` | `async (event) -> None` | 用户消息发给 AI 前（可改写/拦截） |
| `run_llm_request_hooks` / `run_llm_response_hooks` | `async (request / response) -> None` | LLM 请求前 / 回复后 |
| `run_tts_text_hooks` | `async (text: str) -> str` | TTS 文本改写（只影响语音） |
| `run_tts_start_hooks` / `run_tts_end_hooks` | `async (...) -> None` | 语音开始 / 结束 |
| `get_all_tools` | `() -> list` | 汇总插件工具定义 |
| `execute_tool` | `async (name: str, params: dict) -> str | None` | 执行插件工具 |
| `reload` / `unload` / `sync_enabled_plugins` / `apply_enabled` | `async -> ...` | 热重载 / 卸载 / 同步启用列表 / 启停 |
| `emit` | `async (event: str, data=None) -> None` | 事件总线广播（`on` / `off` 订阅） |

**事件对象**（base.py）：

| 类 | 字段 / 方法 | 说明 |
|---|---|---|
| `UserInputEvent` | `text`（可改写）、`source`（`text`/`voice`/`barrage`）、`contexts`、`add_context()`、`set_text()`、`prevent_default()`、`stop_propagation()` | onUserInput 钩子事件 |
| `LLMRequestEvent` | `messages: list`（可原地修改） | onLLMRequest 钩子事件 |
| `LLMResponseEvent` | `text: str` | onLLMResponse 钩子事件 |

插件目录约定：`plugins/`（built-in 与社区）→ 目录含入口文件 + `metadata.json`；详见 `plugins/README.md`。

## 15. 语音识别

模块：`src/asr/stt.py`，类 `STTEngine`（SiliconFlow 云端转写，SenseVoice）

| 方法 | 签名 | 说明 |
|---|---|---|
| `start` / `stop` | `() -> None` | 启动（能量 VAD 静音分割录音）/ 停止 |
| `result_future` | `() -> asyncio.Future` | 返回下一个识别结果的 future（完成时 `result()` 返回 `(text, speech_seconds)` 二元组） |
| `transcribe` | `(wav_path: str) -> str` | 同步转写单个 wav 文件 |

主循环中通过 `asyncio.wait` 监听 `result_future` 接入；播报期间结果被丢弃。

## 17. 记忆系统

模块：`tools/memory/memory.py`

模块级函数：

| 函数 | 签名 | 说明 |
|---|---|---|
| `get_manager` | `() -> MemoryManager` | 获取全局单例 |
| `count` | `() -> int` | 记忆文件数 |
| `warmup` | `() -> None` | 后台预热（构建检索索引） |
| `decay_loop` | `() -> None` | 后台时间衰减循环 |
| `decay_stale_memories` | `() -> int` | 衰减长期未更新非固定记忆，返回清理条数 |
| `remember_explicit` / `forget_phrase` | `(key, value) / (keyword)` | 显式记忆 / 遗忘 |
| `export_graph_data` | `() -> str | None` | 导出记忆图谱 JSON |
| `format_turns_text` | `(turns: list[dict]) -> str` | 轮次列表 → LLM 文本 |

`MemoryManager` 主要方法：

| 方法 | 签名 | 说明 |
|---|---|---|
| `load` / `new_session` | `() -> None` | 加载记忆库 / 开启新会话 |
| `recent_turns` | `property -> list[dict]` | 最近对话轮次 |
| `add_turn` | `(role: str, content: str, user=None, source=None) -> None` | 追加轮次 |
| `list_files` | `(limit=200, max_total=1000) -> list[dict]` | 记忆文件列表 |
| `delete_memories` / `delete_memories_async` | `(ids) -> int` | 删除记忆 |
| `clear_all` | `() -> None` | 清空 |
| `commit_recall_files` | `(entries) -> dict` | 蒸馏结果落库 |
| `update_archive_async` | `(summary, period_start, period_end)` | 更新会话档案 |
| `graph_data` | `(limit=200) -> tuple[list[dict], None]` | 图谱数据 |

检索：Embedding（默认 SiliconFlow 云端 / 可换本地 llama.cpp）+ BM25，Rerank 独立超时 2.0s 超时回退向量排序，`_EmbedBatcher` 批量并发嵌入。

## 18. 配置接口

模块：`src/utils/config.py`

| 符号 | 说明 |
|---|---|
| `cfg` | `Config` 单例，全部配置项为属性（`.env` 读取 + 默认值），详见 `docs/config.md` |
| `Config.validate()` | 校验必填项（`LLM_API_KEY`），缺失抛 `RuntimeError` |
| `reload_tool_runtime()` | 热更新工具/MCP/STT 相关字段 |
| `reload_config()` | 热更新全部可热更新字段 |
| `_PROJECT_ROOT` | 项目根路径（PyInstaller 打包后为 exe 目录） |

---

## 19. 事件总线

模块：`src/core/bus.py`；全局单例 `bus`（发布 / 订阅，顺序执行订阅者，异常隔离）。

事件名常量（订阅 / 发布统一引用，避免手写字符串拼错）：

| 常量 | 事件 | 载荷（models） | 发布点 |
|---|---|---|---|
| `EV_USER_INPUT` | 用户 / 观众输入进入内核 | `InputEvent` | 主循环键盘/语音（source=text/voice）、`_chat_danmaku` 精选弹幕（source=barrage） |
| `EV_AI_REPLY` | AI 产出回复（流式逐句） | `LLMResponse` | `stream.converse` / `speak_text` 每句 |
| `EV_SPEAKING_START` | 一次播报开始 | `SpeakingEvent` | （预留） |
| `EV_SPEAKING_END` | 一次播报结束（含被中断/异常） | `SpeakingEvent` | `stream.converse` / `speak_text` 末尾 |
| `EV_ERROR` | 统一错误事件 | `ErrorEvent` | 主循环对话出错、弹幕回复出错 |
| `EV_STATE_CHANGE` | 全局状态机变化 | `StateChangeEvent` | （预留） |
| `EV_SESSION_END` | 会话结束（退出归档完成） | `SessionEndEvent` | `Application.run` finally |

用法：

```python
from src.core.bus import bus, EV_AI_REPLY

async def on_reply(payload):      # payload: LLMResponse
    print(payload.text, payload.sender)

bus.subscribe(EV_AI_REPLY, on_reply)
await bus.emit(EV_AI_REPLY, LLMResponse(text="hi", sender="user"))
bus.unsubscribe(EV_AI_REPLY, on_reply)
```

**UI 解耦说明**：控制中心已是独立进程，通过 QProcess + stdin/stdout 协议与内核通信
（`console.chat` 的 CHAT_TAG 通道路由到左栏对话面板）。事件总线是**内核内部**的
标准化事件契约——新增功能（字幕 / 情绪 / 监控 / 未来 WebUI）只需 `subscribe`，
无需改动生产方代码。

## 19. 消息结构体 Schema

模块：`src/core/events/models.py`（Pydantic v2，环境已随 mcp SDK 依赖安装，不新增依赖）。

| 模型 | 字段 | 说明 |
|---|---|---|
| `InputEvent` | `source`（text/voice/barrage/command）、`content`、`sender`（默认 user）、`timestamp`、`metadata` | 输入消息 |
| `LLMResponse` | `text`、`done`（默认 False）、`sender`（user/danmaku/proactive/...） | AI 流式回复 |
| `SpeakingEvent` | `sender`、`kind`（speech/command/mindcraft） | 播报开始/结束 |
| `ErrorEvent` | `code`、`code_name`、`msg`、`timestamp` | 统一错误（对齐 ErrorCode） |
| `StateChangeEvent` | `state`、`previous` | 状态机变化 |
| `SessionEndEvent` | `turns`、`summary` | 会话结束 |

所有模型统一预留 `api_version` 字段（当前 = 1）：结构不兼容变更时 +1，消费方据此做兼容分支。

设计约束：**仅作用于事件总线新增消息**，不改动既有 dict 接口，行为 100% 不变。
字段缺失时 Pydantic 自动抛 `ValidationError`。

## 20. 适配器抽象层

模块：`src/adapter/`（`base.py` 公共基类 + `llm.py` / `tts.py` / `avatar.py` / `input.py`）。

统一外部服务契约：**上层只依赖抽象基类，切换具体实现只需新增实现类**，业务代码不改。

| 基类 | 抽象方法 | 项目实现（真实继承，行为不变） |
|---|---|---|
| `BaseLLMAdapter` | `chat_stream`（流式逐句）、`push_turn_context`、`reload_client` | `LLMBrain`（src/llm/llm_brain.py） |
| `BaseTTSAdapter` | `start`/`speak`/`drain`/`interrupt`/`clear_interrupt`/`stop`/`set_on_play_callback`/`set_subtitle_callback`/`apply_ref`/`apply_ref_extras` | `TTSEngine`（src/tts/engine.py） |
| `BaseAvatarAdapter` | `connect`/`ensure_connected`/`close`/`on_event`/`subscribe_event`/`inject_parameters`/`trigger_motion`/`trigger_hotkey`/`activate_expression` | `VTSController`（src/vts/controller.py） |
| `BaseInputAdapter` | `start`/`stop`/`result_future` | `STTEngine`（src/asr/stt.py） |

新增实现示例见 `docs/dev.md`「新增适配器教程」；抽象方法 = 既有实现的公共方法
（签名一致），继承不改动任何行为。`BaseAdapter` 提供 `name` 与 `display_name()` 供辨识。

---

## 统一错误码接入说明

改造目标：**异常消息文本 100% 不变**。接入新模块只需改两处：

```python
from src.core.exceptions import EVBaseException, ErrorCode

# 改前：raise RuntimeError(f"调用失败: {e}")
# 改后：
raise EVBaseException(ErrorCode.LLM_CONNECT_FAILED, f"调用失败: {e}")
```

上层捕获统一处理（日志 / 错误事件推送）：

```python
try:
    ...
except EVBaseException as e:
    console.error(f"[{e.code.name}] {e.msg}")   # 例如 [LLM_CONNECT_FAILED] 调用失败: xxx
```
