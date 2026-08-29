"""18 个分组 dataclass（拆自 src/utils/config.py §3.8）。"""

from dataclasses import dataclass, field
from typing import Optional

from .env_loader import _default_pet_model, _data_root, _default_emotion_map_file


@dataclass
class LLMConfig:
    """主对话 LLM + 模型路由 + 并发上限 + 历史轮数。"""
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    LLM_MODEL: str = ""
    LLM_THINKING: bool = True
    LLM_SERVERS: str = ""
    LLM_ROUTER_ENABLED: bool = True
    LLM_ROUTER_EPSILON: float = 0.1
    LLM_MAX_CONCURRENCY: int = 2
    HISTORY_ROUNDS: int = 10
    # Prompt Cache（对标 Hermes「prompt caching is sacred」）：system 前缀
    # 保持字节稳定 → 服务端自动前缀缓存命中；0 回退原交错顺序拼装
    PROMPT_CACHE_MODE: bool = True
    # 辅助 LLM 记账（3.16）：1 = 辅助调用（butler/proactive 等）统一记账到
    # DATA_ROOT/aux_usage.jsonl（写失败静默）；0 = 关闭记账（行为不变）
    AUX_ACCOUNTING: bool = True
    # 辅助 LLM 按任务路由（3.16）：{"任务名": "模型名"} 覆盖默认主模型，
    # 如 AUX_MODELS={"review": "glm-4-flash"}；缺省任务走 LLM_MODEL
    AUX_MODELS: dict = field(default_factory=dict)


@dataclass
class ButlerConfig:
    """ButlerAgent 管家模型（参照 Muika，OpenAI 兼容接口；留空共用主服务）。"""
    BUTLER_BASE_URL: str = ""
    BUTLER_API_KEY: str = ""
    BUTLER_MODEL: str = ""
    SESSION_SUMMARIZE_MODEL: str = ""
    BUTLER_THINKING: bool = False


@dataclass
class MemoryConfig:
    """记忆系统（Mem0 判决链 + L2 纯文本长期记忆 + L3 会话历史）。"""
    MEMORY_ENABLED: bool = True
    MEMORY_LIFECYCLE_ENABLED: bool = False
    MEMORY_LIFECYCLE_MODEL: str = ""
    MEMORY_LIFECYCLE_THRESHOLD: float = 0.6
    # L2 内建长期记忆（Hermes 式纯文本 MEMORY.md/USER.md + 字符硬上限 + 冻结快照）
    MEMORY_CURATED_ENABLED: bool = True
    MEMORY_CURATED_MEMORY_LIMIT: int = 2200
    MEMORY_CURATED_USER_LIMIT: int = 1375
    # L3 会话历史（SQLite + FTS5 全文检索，Hermes state.db）
    MEMORY_HISTORY_ENABLED: bool = True
    HISTORY_DB_PATH: str = ""
    # L4 治理管道（Hermes「会后秘书」：后台复盘，把值得长期记住的事实写入 L2）
    MEMORY_CURATOR_ENABLED: bool = True
    MEMORY_CURATOR_INTERVAL: int = 10  # 每 N 轮对话触发一次复盘
    # L4 编排（MemoryManager）：每轮召回硬超时（秒，0 关闭），防止慢后端拖垮首字延迟
    MEMORY_GATE_TIMEOUT: float = 8.0


@dataclass
class KnowledgeConfig:
    """知识库（防幻觉，信号闸门命中才注入 system prompt）。"""
    KNOWLEDGE_ENABLED: bool = True
    KNOWLEDGE_MAX_CHARS: int = 1200


@dataclass
class AgentConfig:
    """任务执行 Agent（Firefly 风格 ReAct，仅 !agent 显式触发）。"""
    AGENT_ENABLED: bool = False
    AGENT_MODEL: str = ""
    AGENT_MAX_STEPS: int = 8
    AGENT_MAX_TOKENS: int = 50000
    AGENT_WORKSPACE: str = ""
    AGENT_ALLOW_SHELL: bool = False
    AGENT_ALLOWED_COMMANDS: str = ""  # 命令白名单（逗号分隔，空=放行所有）
    AGENT_AUDIT_LOG: str = ""         # 审计日志路径（空=派生工作空间）
    BUDGET_TOKEN_PER_MINUTE: int = 0       # 全局 token/分钟（0=禁用限流）
    BUDGET_REQUEST_PER_MINUTE: int = 0     # 全局请求/分钟（0=禁用限流）
    BUDGET_COST_PER_HOUR: float = 0.0      # 全局成本/小时 USD（0=禁用限流）
    AGENT_SKILL_CREATION: bool = True
    AGENT_MEMORY_SINK: bool = True
    PROACTIVE_QUEUE_MAX: int = 4
    AGENT_AVOID_MAIN_LLM: bool = True
    AGENT_HISTORY_SNAPSHOT: int = 6
    AGENT_DUP_THRESHOLD: float = 0.85
    # 工具集门控（3.3）：live/pet/minimal；空 = 全量（等价旧行为）
    AGENT_TOOLSET: str = ""
    # 迭代次数预算（3.1）：LLM 调用轮数上限；0 = 跟随 AGENT_MAX_STEPS（默认行为不变）
    AGENT_MAX_ITERATIONS: int = 0
    # 技能 Curator 增强（3.6）：1 = 技能归档/合并前打快照 + 审计日志 + 可回滚；
    # 0 = 保持现有归档流程不变（默认关闭，先跑通流程再启用）
    ENABLE_CURATOR: bool = False
    # Cron 作业化加固（3.9）：1 = 执行账本去重 + 跨进程文件锁 + 3 分钟硬中断
    # + 注入扫描；0 = 调度行为与现状完全一致（默认关闭）
    AGENT_CRON_HARDEN: bool = False
    # 后台委派队列（3.8）：1 = delegate 支持后台持久化执行（delegation.db 队列 +
    # 常驻 worker，重启不丢、自动重试）；0 = 仅同步并行委派（进程内 asyncio
    # 后台任务，重启即丢）。优化 7-D 默认开启以激活 sub-agent 委派能力。
    # 开启时启动 daemon worker 线程 + 创建 delegation.db（轻量副作用）；
    # AGENT_ENABLED=false（默认）时主对话 maybe_delegate 不入队，bridge
    # 不触发委派，worker 空转无副作用（delegate 工具本身也需 AGENT_ENABLED
    # 才能调用）
    AGENT_DELEGATE_BACKEND: bool = True
    # 会话租约（3.10）：1 = 进入 brain 前按 session_id 排队串行（防同会话重入）；
    # 0 = 直接放行，行为与现状完全一致（默认关闭）
    TURN_LEASE_ENABLED: bool = False
    # 全局急停（3.13）：1 = 存在 DATA_ROOT/ESTOP 哨兵时拒绝高危工具执行；
    # 0 = 关闭急停检查（行为不变）。默认开启但无哨兵文件时恒放行
    AGENT_ESTOP_ENABLED: bool = True
    # 复读防护（3.13）：1 = LLM 流式输出复读主导片段（≥400 字 + 60 字块重复
    # ≥5 次覆盖过半）时中断该句；0 = 关闭检测（正常文本永不触发）
    AGENT_REPETITION_GUARD: bool = True
    # 开播就绪检查（3.15）：1 = 启动时聚合探测 TTS/ASR/VTS/弹幕/记忆/MCP，
    # 失败仅 WARN 不阻断启动；0 = 关闭探针（行为不变）
    READINESS_CHECK: bool = True
    # TTS 回声防护（3.14）：1 = STT 识别结果与最近播报文本 difflib 相似度
    # ≥0.6 时判为扬声器漏音回声并丢弃；0 = 关闭检测（正常语音不会命中）
    AGENT_TTS_ECHO_GUARD: bool = True


@dataclass
class PersonaConfig:
    """人设（SYSTEM_PROMPT_FILE 优先，否则 UI 人设 / SYSTEM_PROMPT）。

    AUTHOR_NOTE：Author's Note 尾部人设锚点（近因效应）。默认空 = 不注入，
    行为与历史完全一致；配置后作为 system 尾注追加在 messages 末尾。
    """
    SYSTEM_PROMPT_FILE: str = ""
    SYSTEM_PROMPT: str = ""
    AUTHOR_NOTE: str = ""


@dataclass
class FilterConfig:
    """内容过滤（AI 回复 / 弹幕 / 主动对话播报）。"""
    PROFANITY_FILTER_ENABLED: bool = True
    PROFANITY_FILTER_RATE: float = 0.7


@dataclass
class ToolConfig:
    """Function Calling 工具 + MCP + 技能目录。"""
    TOOLS_ENABLED: bool = True
    OPENWEATHERMAP_API_KEY: str = ""
    TOOL_GET_CURRENT_TIME_ENABLED: bool = True
    TOOL_GET_WEATHER_ENABLED: bool = True
    TOOL_LOAD_SKILL_ENABLED: bool = True
    TOOL_LOOK_SCREEN_ENABLED: bool = True
    TOOL_READ_SHEET_ENABLED: bool = True
    TOOL_PLAY_SFX_ENABLED: bool = True
    TOOL_WRITE_DIARY_ENABLED: bool = True
    TOOL_EPIANO_ENABLED: bool = True
    # ToolRegistry（3.2）：1 走注册表统一门控/分发，0 回退旧直连路径（行为不变）
    TOOL_REGISTRY: bool = True
    MCP_ENABLED: bool = False
    MCP_CONFIG_PATH: str = ""
    SKILLS_DIR: str = "skills"
    # 会话搜索（3.7）：1 = 对话轮次落库 SessionDB 并暴露 session_search 工具；
    # 0 = 不落库不暴露（默认关闭，纯增量旁路）
    ENABLE_SESSION_SEARCH: bool = False


@dataclass
class EvolutionConfig:
    """自我进化（对话后后台复盘 + 定期自我提示 + GEPA 提示词进化）。"""
    EVOLUTION_ENABLED: bool = True
    EVOLUTION_MIN_INTERVAL: int = 600
    EVOLUTION_MIN_TURNS: int = 10
    EVOLUTION_PERIODIC_INTERVAL: int = 1800
    EVOLUTION_EVAL_ENABLED: bool = True
    EVOLUTION_EVAL_CASES: int = 2
    EVOLUTION_PROMPT_EVO_ENABLED: bool = True
    EVOLUTION_PROMPT_EVO_INTERVAL: int = 21600
    # 5.1：进化注入段（技能索引/话术建议/GEPA 策略/观众画像）是否注入到
    # user 消息尾部而非 system 前缀——保持 system 前缀字节稳定以命中提示缓存
    # （对标 hermes「可变内容注入 user 消息」）；0 = 回退旧行为（system 内拼装）
    EVOLUTION_INJECT_IN_USER: bool = True
    # 5.4：对话后路径（maybe_review）的技能库审阅要求"自上次活跃会话起空闲
    # 至少 EVOLUTION_CURATOR_IDLE_HOURS 小时"才执行，避免开播活跃期后台大动作；
    # 0 = 不限制（立即审阅）。定期 tick 路径（periodic_tick）不受此门控。
    EVOLUTION_CURATOR_IDLE_HOURS: float = 2.0
    # 5.6：观众负反馈信号采集（弹幕负向关键词 / 播报打断）总开关；
    # 关闭后不再写入 evolution_feedback.jsonl，复盘素材相应不注入负反馈块
    EVOLUTION_FEEDBACK_ENABLED: bool = True
    # 5.16：GEPA 策略注入 A/B 盲测开关——开启后按缓存周期奇偶各 50% 轮换
    # 注入上一版策略（previous），供线上效果对比；默认关闭，行为与现状一致
    EVOLUTION_POLICY_AB: bool = False


@dataclass
class VoiceConfig:
    """语音链路：GPT-SoVITS TTS + STT（本地/云端）+ 口型参数。"""
    GPTSOVITS_REF_AUDIO: str = ""
    GPTSOVITS_REF_AUDIOS: str = ""
    GPTSOVITS_PROMPT_TEXT: str = ""
    GPTSOVITS_TIMEOUT: float = 120
    GPTSOVITS_MODELS_DIR: str = ""
    # 角色专训权重（留空 = 官方底模 s1v3.ckpt + s2Gv2ProPlus.pth）；
    # 两项需成对填写（GPT ckpt 与 SoVITS pth），相对路径基于项目根解析
    GPTSOVITS_ROLE_GPT: str = ""
    GPTSOVITS_ROLE_SOVITS: str = ""
    TTS_OUTPUT_DEVICE: str = ""
    # 口型同步模式：builtin = 内置 RMS 口型注入（默认）；
    # vts_audio = TTS 音频经 TTS_OUTPUT_DEVICE（虚拟声卡）输出，
    # 嘴部交给 VTube Studio 自带音频口型同步，本程序停用嘴部注入
    LIPSYNC_MODE: str = "builtin"
    STT_ENABLED: bool = False
    STT_MODEL: str = "FunAudioLLM/SenseVoiceSmall"
    STT_LOCAL_MODEL_PATH: str = ""
    STT_LOCAL_MODEL_REVISION: str = "v2.0.4"
    STT_SERVER_URL: str = "http://127.0.0.1:8487"
    STT_API_KEY: str = ""
    # 转写 API 地址：填了 = 走该 API（整段上传转写）；留空 = 本地流式 ASR 服务
    STT_BASE_URL: str = ""
    STT_LEVEL_THRESHOLD: float = 500
    STT_VAD_MODE: int = 2
    STT_SILENCE_SECONDS: float = 0.6
    STT_MAX_SECONDS: float = 10
    STT_INTERRUPT_MIN_SECONDS: float = 1.0
    MOUTH_PARAMETER: str = ""
    MOUTH_GAIN: float = 0.4


@dataclass
class VTSConfig:
    """VTubeStudio 连接 + 待机动画接管。"""
    VTS_PORT: int = 8001
    VTS_PLUGIN_NAME: str = "ZhipuAI_VTuber"
    VTS_PLUGIN_DEVELOPER: str = "LocalUser"
    MOTION_PATH: str = ""
    VTS_ROOT: str = ""
    VTS_IDLE_TAKEOVER: bool = True


@dataclass
class PetConfig:
    """运行模式 + 桌宠模式（RUN_MODE=pet）。"""
    RUN_MODE: str = "vtuber"
    PET_MODEL_PATH: str = ""
    PET_WINDOW_SIZE: str = ""
    PET_ALWAYS_ON_TOP: bool = True
    PET_MOTION_PATH: str = ""
    PET_IDLE_MOTION: str = ""


@dataclass
class EmotionConfig:
    """表情/动作（Embedding 情绪分类）+ SiliconFlow/嵌入模型。"""
    EMOTION_ACTOR_ENABLED: bool = False
    SILICONFLOW_API_KEY: str = ""
    SILICONFLOW_MODEL: str = "Qwen/Qwen3-Embedding-0.6B"
    SILICONFLOW_BASE_URL: str = "https://api.siliconflow.cn/v1"
    EMBEDDING_BASE_URL: str = "https://api.siliconflow.cn/v1"
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_DIMENSIONS: Optional[int] = None
    EMBEDDING_MODEL: str = "Qwen/Qwen3-Embedding-0.6B"
    EMOTION_MAP_FILE: str = ""


@dataclass
class ProactiveConfig:
    """主动对话（LLM 自主决定，无时间门槛）+ 开口/弹幕回复共用随机间隔。"""
    PROACTIVE_ENABLED: bool = True
    RESPONSE_INTERVAL_MIN: float = 5
    RESPONSE_INTERVAL_MAX: float = 10


@dataclass
class DanmakuConfig:
    """B 站直播弹幕（blivedm → SSE 弹幕气泡网页）。"""
    BILI_ENABLED: bool = True
    BILI_ROOM_ID: int = 0
    BILI_ROOM_IDS: list = field(default_factory=list)
    BILI_SESSDATA: str = ""
    BILI_SERVER_PORT: int = 8766


@dataclass
class MindcraftConfig:
    """外部服务 mindcraft（LLM 驱动的 Minecraft bot）。"""
    MINDCRAFT_PATH: str = ""
    MINDCRAFT_LLM_BASE_URL: str = ""
    MINDCRAFT_LLM_MODEL: str = "glm-4-flash-250414"
    MINDCRAFT_BOT_NAME: str = "vtuber"
    MINDCRAFT_HOST: str = "127.0.0.1"
    MINDCRAFT_PORT: int = 55916
    MINDCRAFT_AUTH: str = "offline"
    MINDCRAFT_MINDSERVER_PORT: int = 8080
    MINDCRAFT_BRIDGE_ENABLED: bool = False
    MINDCRAFT_BOT_PERSONA: str = ""


@dataclass
class PathsConfig:
    """派生路径（yaml 覆盖层跳过）。"""
    PROJECT_ROOT: str = ""
    DATA_ROOT: str = ""
    TOKEN_FILE: str = ""
    CONFIG_YAML_PATH: str = ""


__all__ = [
    "LLMConfig", "ButlerConfig", "MemoryConfig", "KnowledgeConfig",
    "AgentConfig", "PersonaConfig", "FilterConfig", "ToolConfig",
    "EvolutionConfig", "VoiceConfig", "VTSConfig", "PetConfig",
    "EmotionConfig", "ProactiveConfig", "DanmakuConfig",
    "MindcraftConfig", "PathsConfig",
]
