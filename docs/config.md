# E.V 配置说明

> 配置来源：**环境变量（.env）> configs/config.yaml > 默认值**，详见文末「配置分层（yaml 覆盖层）」。
> 布尔值识别：`1 / true / yes / y / on` 为真。
> `!config` / `!tools` 命令可热更新（见文末）。
> 多行内容（人设）不写进 `.env`，存放在 `ui/data/system_prompt.txt` 或技能文件。

## 1. LLM（OpenAI 兼容接口）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_API_KEY` | `ZHIPU_API_KEY` 兼容 | 必填。智谱 / DeepSeek / 任意 OpenAI 兼容服务 |
| `LLM_BASE_URL` | 空（SDK 默认） | 服务地址 |
| `LLM_MODEL` | `ZHIPU_MODEL` 兼容，默认 `glm-4.7-flash` | 主对话模型 |
| `LLM_THINKING`（旧名 `THINKING_ENABLED`） | `true` | 深度思考模式 |

## 2. VTubeStudio

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VTS_PORT` | `8001` | VTS WebSocket 端口 |
| `VTS_PLUGIN_NAME` / `VTS_PLUGIN_DEVELOPER` | `ZhipuAI_VTuber` / `LocalUser` | 插件认证身份 |
| `MOTION_PATH` | 空 | 指定待机动作文件（绝对或相对项目根） |
| `VTS_ROOT` | 空 | VTS 安装根目录（待机动画接管定位模型用；留空自动从 Steam 注册表定位） |
| `VTS_IDLE_TAKEOVER` | `true` | 待机动画接管：把模型待机动画交给插件注入路径播放（消除首尾帧硬跳） |

## 3. 口型同步

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MOUTH_PARAMETER` | 空 | 口型参数名（留空自动扫描适配） |
| `MOUTH_GAIN` | `0.4` | 口型振幅 |

## 4. GPT-SoVITS TTS

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TTS_SERVER_URL` | `http://127.0.0.1:8000` | **外部 TTS 合成服务**（`tts.bat` 启动的 fastapi_server_example.py） |
| `TTS_OUTPUT_DEVICE` | 空 | 低延迟输出设备名子串（如 `Voicemeeter In`）：走 WASAPI 独占直达混音台，首句出声 ~120ms→几 ms；留空用系统默认输出（缓冲 50ms） |
| `GPTSOVITS_REF_AUDIO` | 空 | 主参考音频（音色 + 发音参考）；**为空则语音合成关闭** |
| `GPTSOVITS_REF_AUDIOS` | 空 | 辅助参考音频，多条 `\|` 分隔（服务端仅支持单说话人，当前忽略） |
| `GPTSOVITS_PROMPT_TEXT` | 空 | 参考音频对应文本 |
| `GPTSOVITS_TIMEOUT` | `120` | 合成超时（秒） |
| `GPTSOVITS_MODELS_DIR` | `tools/gsv_tts/API/models` | 本地模型目录（GSV-TTS-Lite） |

## 5. 内容过滤

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PROFANITY_FILTER_ENABLED` | `true` | 脏话过滤开关（弹幕原文 / AI 回复 / 主动播报） |
| `PROFANITY_FILTER_RATE` | `0.7` | 命中敏感词时触发替换的概率 |

## 6. 记忆系统

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MEMORY_ENABLED` | `true` | 记忆总开关（会话蒸馏 + 检索注入 + 显式记忆工具） |

## 7. ButlerAgent 管家模型

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BUTLER_BASE_URL` / `BUTLER_API_KEY` / `BUTLER_MODEL` | 留空 | 留空则与主对话共用服务/模型 |
| `SESSION_SUMMARIZE_MODEL` | 留空 | 会话摘要专用模型，留空用管家模型 |
| `BUTLER_THINKING` | `false` | 管家思考模式（默认关：蒸馏要结构化 JSON，思考模式会污染 content） |

## 8. 人设

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SYSTEM_PROMPT_FILE` | 空 | 指向 skill 文件/文件夹时加载整个内容作为人设（优先） |
| `SYSTEM_PROMPT` | 内置默认 | 兜底人设文本（UI 人设优先于它） |

加载顺序：`SYSTEM_PROMPT_FILE`（skill 目录，剥离 frontmatter）→ `ui/data/system_prompt.txt`（控制中心人设编辑框）→ `.env SYSTEM_PROMPT` → 内置默认。

## 9. Function Calling 工具

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENWEATHERMAP_API_KEY` | 空 | 天气工具 key |
| `TOOLS_ENABLED` | `true` | 工具总开关（关闭 = 纯对话模式） |
| `TOOL_GET_CURRENT_TIME_ENABLED` / `TOOL_GET_WEATHER_ENABLED` / `TOOL_LOAD_SKILL_ENABLED` / `TOOL_LOOK_SCREEN_ENABLED` / `TOOL_PLAY_SFX_ENABLED` | `true` | 各工具开关（控制中心「工具屋」勾选，`!tools` 热生效） |

## 10. 自我进化（对话后后台复盘）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `EVOLUTION_ENABLED` | `true` | 自我进化总开关 |
| `EVOLUTION_MIN_INTERVAL` | `600` | 复盘最小间隔（秒） |
| `EVOLUTION_MIN_TURNS` | `10` | 复盘最小新增对话轮次 |
| `EVOLUTION_PERIODIC_INTERVAL` | `1800` | 定期自我提示检查间隔（秒），空闲期主动补复盘 |
| `EVOLUTION_EVAL_ENABLED` | `true` | 技能评估闭环（fail-open，失败不阻塞落盘） |
| `EVOLUTION_EVAL_CASES` | `2` | 每次评估测试用例数（钳制 1~3） |
| `EVOLUTION_PROMPT_EVO_ENABLED` | `true` | GEPA 系统提示词进化 |
| `EVOLUTION_PROMPT_EVO_INTERVAL` | `21600` | GEPA 独立节流间隔（秒，默认 6 小时） |

## 11. 模型路由进化（多臂老虎机 UCB1）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_SERVERS` | 空 | 逗号分隔多服务：`名称;base_url;api_key;model`；**配 ≥2 个才启用路由** |
| `LLM_ROUTER_ENABLED` | `true` | 路由开关 |
| `LLM_ROUTER_EPSILON` | `0.1` | 探索率（0~1），小概率尝试非最优服务 |

## 12. MCP（外部工具服务器）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MCP_ENABLED` | `false` | MCP 总开关（联网搜索走 bing-cn-mcp，无需 key） |
| `MCP_CONFIG_PATH` | `src/mcp/mcp_config.json` | 服务器配置 JSON 路径 |

## 13. 外部服务（Mindcraft）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MINDCRAFT_PATH` | `plugins/mindcraft` | bot 项目路径 |
| `MINDCRAFT_LLM_BASE_URL` / `MINDCRAFT_LLM_MODEL` | 复用主 LLM | bot 大脑模型 |
| `MINDCRAFT_BOT_NAME` / `MINDCRAFT_HOST` / `MINDCRAFT_PORT` / `MINDCRAFT_AUTH` | `vtuber` / `127.0.0.1` / `55916` / `offline` | bot 连接参数 |
| `MINDCRAFT_MINDSERVER_PORT` | `8080` | MindServer（socket.io）端口 |
| `MINDCRAFT_BRIDGE_ENABLED` | `false` | 双向桥：true 时用户输入转发给 MC bot、bot 回复由主播 TTS 朗读 |
| `MINDCRAFT_BOT_PERSONA` | 空 | bot 人设（andy.json 的 conversing 提示词） |

## 14. 运行参数与并发控制

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HISTORY_ROUNDS` | `10` | 会话历史保留轮数 |
| `LLM_MAX_CONCURRENCY` | `2` | 同时最多 LLM 推理数（用户/主动/弹幕共用信号量） |
| `PROACTIVE_QUEUE_MAX` | `4` | 主动消息队列最大长度（超出丢最旧） |
| `AGENT_AVOID_MAIN_LLM` | `true` | 主 LLM 推理/播报期间抑制主动触发 |
| `AGENT_HISTORY_SNAPSHOT` | `6` | 主动发言读取主会话历史条数 |
| `AGENT_DUP_THRESHOLD` | `0.85` | 主动发言与最近对话相似度去重阈值 |

## 15. 主动对话

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PROACTIVE_ENABLED` | `true` | 主动对话总开关（LLM 自主决定开口，无时间门槛） |
| `RESPONSE_INTERVAL_MIN` / `RESPONSE_INTERVAL_MAX` | `5` / `10` | 主动开口 / 弹幕回复共用的随机间隔范围（秒） |

## 16. 技能系统

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SKILLS_DIR` | `src/llm/skills` | 技能根目录（相对项目根，逗号分隔可配多个）；按 `<技能名>/SKILL.md` 组织，watchdog 热重载 |

## 17. 运行模式

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RUN_MODE` | `vtuber` | `vtuber` = VTubeStudio 虚拟主播；`pet` = 本地桌面宠物（live2d-py + PySide6，无需 VTS） |

## 18. 桌宠模式（RUN_MODE=pet）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PET_MODEL_PATH` | 自动探测 live2d/ 下第一个 `.model3.json` | 模型路径（相对项目根或绝对） |
| `PET_WINDOW_SIZE` | 空（自适应主屏） | 窗口尺寸 `"宽x高"` |
| `PET_ALWAYS_ON_TOP` | `true` | 窗口置顶 |
| `PET_MOTION_PATH` | 空 | 基线动作文件（仅显式配置才加载，不回退 VTS 的 MOTION_PATH——参数不匹配会模型「抽搐」） |
| `PET_IDLE_MOTION` | 空 | 默认待机动作（无 Idle 组时循环播放；留空智能匹配含「待机」/idle/loop 的文件名） |

## 19. 表情/动作（Embedding 情绪控制，桌宠 / VTS 双模式）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `EMOTION_ACTOR_ENABLED` | `false` | 用户消息 → Embedding 情绪分类 → 自动播放表情/动作 |
| `SILICONFLOW_API_KEY` | 空 | SiliconFlow key（情绪嵌入 + STT 复用） |
| `SILICONFLOW_MODEL` | `Qwen/Qwen3-Embedding-0.6B` | SiliconFlow 嵌入模型 |
| `SILICONFLOW_BASE_URL` | `https://api.siliconflow.cn/v1` | SiliconFlow 服务地址 |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` | 复用 SiliconFlow | 嵌入服务（可改本地 llama.cpp：`http://127.0.0.1:8080/v1`，无需 key） |
| `EMBEDDING_DIMENSIONS` | 留空（服务端默认） | 嵌入固定维度（切换模型需与库中向量维度一致） |
| `EMOTION_MAP_FILE` | `data/emotion_map_vts.json`（vtuber）/ `data/emotion_map.json`（pet） | 情绪 → 表情/动作映射文件 |

## 20. 语音识别（STT）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `STT_ENABLED` | `false` | 麦克风语音输入开关 |
| `STT_MODEL` | `FunAudioLLM/SenseVoiceSmall` | 转写模型 |
| `STT_API_KEY` / `STT_BASE_URL` | 复用 SiliconFlow | 独立转写服务（可换云端/本地） |
| `STT_LEVEL_THRESHOLD` | `500` | 能量阈值（RMS），越高越不易误触发 |
| `STT_SILENCE_SECONDS` | `1.0` | 静音多久切段上传 |
| `STT_MAX_SECONDS` | `10` | 单段最长录音时长 |
| `STT_INTERRUPT_MIN_SECONDS` | `3` | 语音打断阈值：播报期间识别语音「说话时长」超过才打断（过短不打断，防误触） |

## 21. B 站直播弹幕

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BILI_ENABLED` | `true` | 弹幕服务总开关 |
| `BILI_ROOM_ID` | `0` | 直播间房间号（0 = 不连接） |
| `BILI_ROOM_IDS` | 空 | 多直播间（逗号分隔如 `123,456`），配置后优先于 `BILI_ROOM_ID`，每房间独立连接 |
| `BILI_SESSDATA` | 空 | 建议填：不填可连接但用户名打码 |
| `BILI_SERVER_PORT` | `8766` | 弹幕气泡网页端口（与字幕 8765 错开） |

## 22. 派生路径（非配置）

| 属性 | 说明 |
|---|---|
| `PROJECT_ROOT` | 项目根目录（打包后为 exe 所在目录） |
| `TOKEN_FILE` | VTS 认证 token 文件 `data/vts_token.json` |

---

## 配置分层（yaml 覆盖层）

可选配置文件 `configs/config.yaml`（路径可用 `CONFIG_YAML_PATH` 覆盖）提供分层配置：

- **优先级**：环境变量 > config.yaml > 默认值。环境变量已设置的字段，yaml **不会**覆盖；
- **纯增量**：`configs/config.yaml` 不存在时行为与原来完全一致（模板见 `configs/config.example.yaml`）；
- yaml 键名与 .env 变量名一致；布尔/int/float/list 自动按字段类型转换，未知键忽略；
- `!config` / `!tools` 热更新时会重新应用 yaml 覆盖层（环境变量未设置的字段回落 yaml）。

```yaml
# configs/config.yaml 示例（完整模板见 configs/config.example.yaml）
HISTORY_ROUNDS: 20
LLM_THINKING: false
BILI_ROOM_IDS: [123, 456]
```

> 密钥（`LLM_API_KEY` 等）建议留在 .env；yaml 适合「非密钥、按环境差异化」的配置
> （直播间 / 运行参数 / 模型选择），避免改配置时反复动 .env。

## 热更新命令（主程序 stdin）

| 命令 | 作用 | 调用 |
|---|---|---|
| `!config` | 统一热更新：LLM / 人设 / 主动对话 / 内容过滤 / 记忆 / 弹幕 / 桌宠 / 情绪 | `config.reload_config()` |
| `!tools` | 工具 / MCP 配置热更新 | `config.reload_tool_runtime()` |
| `!stt` | 语音识别热启停 | `config.reload_tool_runtime()` |
| `!tts_audio <path>` | TTS 主参考音频热更新（空串清空） | `tts.apply_ref()` |
| `!tts_text <text>` | TTS 主参考文本热更新 | `tts.apply_ref()` |
| `!tts_audios <path>` | TTS 辅助参考音频热更新（`\|` 分隔） | `tts.apply_ref_extras()` |
| `!model <path>` | 桌宠模式热切换模型 | `pet_widget.switch_model()` |
| `!plugins` | 插件管理：list / sync / reload / enable / disable | `PluginManager` |
| `!clean` | 清理运行时内存 + 临时文件 | `cleaner` |
| `/memory` | 记忆管理：list / del / clear / decay | `MemoryManager` |

> 路径类参数仅允许项目根目录内（防目录穿越）。`/quit`、`/exit`、`/q` 退出；播报期间 `!` 命令照常执行，普通输入被丢弃。
