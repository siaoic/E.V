"""字段加载表 + 热更新字段清单（拆自 src/utils/config.py §3.8）。"""

import os

from .env_loader import (
    _get_bool,
    _get_optional_int,
    _parse_aux_models,
    _get_room_ids,
    _default_pet_model,
    _data_root,
    _default_emotion_map_file,
    _load_system_prompt,
    _PROJECT_ROOT,
)


# =====================================================================
# 字段加载表：旧字段名 → 无参加载函数（逐条复刻原类体默认值表达式）
# 新增配置字段 = 子配置声明一行 + 本表一行，热更新自动生效
# =====================================================================
_LLM_LOADERS = {
    "LLM_API_KEY": lambda: os.getenv("LLM_API_KEY", "") or os.getenv("ZHIPU_API_KEY", ""),
    "LLM_BASE_URL": lambda: os.getenv("LLM_BASE_URL", ""),
    "LLM_MODEL": lambda: os.getenv("LLM_MODEL", "") or os.getenv("ZHIPU_MODEL", "glm-4.7-flash"),
    "LLM_THINKING": lambda: _get_bool("LLM_THINKING", _get_bool("THINKING_ENABLED", True)),
    "LLM_REASONING_EFFORT": lambda: os.getenv("LLM_REASONING_EFFORT") or "",
    "LLM_SERVERS": lambda: os.getenv("LLM_SERVERS") or "",
    "LLM_ROUTER_ENABLED": lambda: _get_bool("LLM_ROUTER_ENABLED", True),
    "LLM_ROUTER_EPSILON": lambda: float(os.getenv("LLM_ROUTER_EPSILON", "0.1")),
    "LLM_MAX_CONCURRENCY": lambda: int(os.getenv("LLM_MAX_CONCURRENCY") or "2"),
    "HISTORY_ROUNDS": lambda: int(os.getenv("HISTORY_ROUNDS", "10")),
    "PROMPT_CACHE_MODE": lambda: _get_bool("PROMPT_CACHE_MODE", True),
    "AUX_ACCOUNTING": lambda: _get_bool("AUX_ACCOUNTING", True),
    # 辅助 LLM 按任务路由表：env 为 JSON 对象字符串（容错解析，失败回空表）
    "AUX_MODELS": lambda: _parse_aux_models(),
}

_BUTLER_LOADERS = {
    "BUTLER_BASE_URL": lambda: os.getenv("BUTLER_BASE_URL") or "",
    "BUTLER_API_KEY": lambda: os.getenv("BUTLER_API_KEY") or "",
    "BUTLER_MODEL": lambda: os.getenv("BUTLER_MODEL") or "",
    "SESSION_SUMMARIZE_MODEL": lambda: os.getenv("SESSION_SUMMARIZE_MODEL") or "",
    "BUTLER_THINKING": lambda: _get_bool("BUTLER_THINKING", False),
}

_MEMORY_LOADERS = {
    "MEMORY_ENABLED": lambda: _get_bool("MEMORY_ENABLED", True),
    "MEMORY_LIFECYCLE_ENABLED": lambda: _get_bool("MEMORY_LIFECYCLE_ENABLED", False),
    "MEMORY_LIFECYCLE_MODEL": lambda: os.getenv("MEMORY_LIFECYCLE_MODEL") or "",
    "MEMORY_LIFECYCLE_THRESHOLD": lambda: float(os.getenv("MEMORY_LIFECYCLE_THRESHOLD", "0.6")),
    "MEMORY_CURATED_ENABLED": lambda: _get_bool("MEMORY_CURATED_ENABLED", True),
    "MEMORY_CURATED_MEMORY_LIMIT": lambda: int(os.getenv("MEMORY_CURATED_MEMORY_LIMIT") or "2200"),
    "MEMORY_CURATED_USER_LIMIT": lambda: int(os.getenv("MEMORY_CURATED_USER_LIMIT") or "1375"),
    "MEMORY_HISTORY_ENABLED": lambda: _get_bool("MEMORY_HISTORY_ENABLED", True),
    # 会话历史库派生路径走 _data_root()（可写数据根单源），避免默认表达式重复
    "HISTORY_DB_PATH": lambda: (
        os.getenv("HISTORY_DB_PATH")
        or os.path.join(_data_root(), "memory", "history.db")),
    "MEMORY_CURATOR_ENABLED": lambda: _get_bool("MEMORY_CURATOR_ENABLED", True),
    "MEMORY_CURATOR_INTERVAL": lambda: int(os.getenv("MEMORY_CURATOR_INTERVAL") or "10"),
    "MEMORY_GATE_TIMEOUT": lambda: float(os.getenv("MEMORY_GATE_TIMEOUT") or "8.0"),
    # 每轮召回注入硬超时（inner_loop 快路径）。兼容历史 .env 里的旧名
    # MEMORY_RETRIEVE_TIMEOUT（此前从未被读取，纯死变量；同名语义一致）
    "MEMORY_RECALL_TIMEOUT": lambda: float(
        os.getenv("MEMORY_RECALL_TIMEOUT")
        or os.getenv("MEMORY_RETRIEVE_TIMEOUT")
        or "0.8"),
}

_KNOWLEDGE_LOADERS = {
    "KNOWLEDGE_ENABLED": lambda: _get_bool("KNOWLEDGE_ENABLED", True),
    "KNOWLEDGE_MAX_CHARS": lambda: int(os.getenv("KNOWLEDGE_MAX_CHARS") or "1200"),
}

_AGENT_LOADERS = {
    "AGENT_ENABLED": lambda: _get_bool("AGENT_ENABLED", False),
    "AGENT_MODEL": lambda: os.getenv("AGENT_MODEL") or "",
    "AGENT_MAX_STEPS": lambda: int(os.getenv("AGENT_MAX_STEPS") or "8"),
    "AGENT_MAX_TOKENS": lambda: int(os.getenv("AGENT_MAX_TOKENS") or "50000"),
    "AGENT_WORKSPACE": lambda: os.getenv("AGENT_WORKSPACE") or "",
    "AGENT_ALLOW_SHELL": lambda: _get_bool("AGENT_ALLOW_SHELL", False),
    "AGENT_ALLOWED_COMMANDS": lambda: os.getenv("AGENT_ALLOWED_COMMANDS") or "",
    "AGENT_AUDIT_LOG": lambda: os.getenv("AGENT_AUDIT_LOG") or "",
    "BUDGET_TOKEN_PER_MINUTE": lambda: int(os.getenv("BUDGET_TOKEN_PER_MINUTE") or "0"),
    "BUDGET_REQUEST_PER_MINUTE": lambda: int(os.getenv("BUDGET_REQUEST_PER_MINUTE") or "0"),
    "BUDGET_COST_PER_HOUR": lambda: float(os.getenv("BUDGET_COST_PER_HOUR") or "0"),
    "AGENT_SKILL_CREATION": lambda: _get_bool("AGENT_SKILL_CREATION", True),
    "AGENT_MEMORY_SINK": lambda: _get_bool("AGENT_MEMORY_SINK", True),
    "PROACTIVE_QUEUE_MAX": lambda: int(os.getenv("PROACTIVE_QUEUE_MAX") or "4"),
    "AGENT_AVOID_MAIN_LLM": lambda: _get_bool("AGENT_AVOID_MAIN_LLM", True),
    "AGENT_HISTORY_SNAPSHOT": lambda: int(os.getenv("AGENT_HISTORY_SNAPSHOT") or "6"),
    "AGENT_DUP_THRESHOLD": lambda: float(os.getenv("AGENT_DUP_THRESHOLD") or "0.85"),
    "AGENT_TOOLSET": lambda: (os.getenv("AGENT_TOOLSET") or "").strip().lower(),
    "AGENT_MAX_ITERATIONS": lambda: int(os.getenv("AGENT_MAX_ITERATIONS") or "0"),
    "ENABLE_CURATOR": lambda: _get_bool("ENABLE_CURATOR", False),
    "AGENT_CRON_HARDEN": lambda: _get_bool("AGENT_CRON_HARDEN", False),
    "AGENT_DELEGATE_BACKEND": lambda: _get_bool("AGENT_DELEGATE_BACKEND", True),
    "TURN_LEASE_ENABLED": lambda: _get_bool("TURN_LEASE_ENABLED", False),
    "AGENT_ESTOP_ENABLED": lambda: _get_bool("AGENT_ESTOP_ENABLED", True),
    "AGENT_REPETITION_GUARD": lambda: _get_bool("AGENT_REPETITION_GUARD", True),
    "READINESS_CHECK": lambda: _get_bool("READINESS_CHECK", True),
    "AGENT_TTS_ECHO_GUARD": lambda: _get_bool("AGENT_TTS_ECHO_GUARD", True),
}

_PERSONA_LOADERS = {
    "SYSTEM_PROMPT_FILE": lambda: (
        os.getenv("SYSTEM_PROMPT_FILE") or "").split("#", 1)[0].strip(),
    "SYSTEM_PROMPT": lambda: _load_system_prompt(),
    # Author's Note 尾部人设锚点（近因效应，默认空 = 不注入，行为不变）
    "AUTHOR_NOTE": lambda: os.getenv("AUTHOR_NOTE") or "",
}

_FILTER_LOADERS = {
    "PROFANITY_FILTER_ENABLED": lambda: _get_bool("PROFANITY_FILTER_ENABLED", True),
    "PROFANITY_FILTER_RATE": lambda: float(os.getenv("PROFANITY_FILTER_RATE", "0.7")),
}

_TOOL_LOADERS = {
    "TOOLS_ENABLED": lambda: _get_bool("TOOLS_ENABLED", True),
    "OPENWEATHERMAP_API_KEY": lambda: os.getenv("OPENWEATHERMAP_API_KEY") or "",
    "TOOL_GET_CURRENT_TIME_ENABLED": lambda: _get_bool("TOOL_GET_CURRENT_TIME_ENABLED", True),
    "TOOL_GET_WEATHER_ENABLED": lambda: _get_bool("TOOL_GET_WEATHER_ENABLED", True),
    "TOOL_LOAD_SKILL_ENABLED": lambda: _get_bool("TOOL_LOAD_SKILL_ENABLED", True),
    "TOOL_LOOK_SCREEN_ENABLED": lambda: _get_bool("TOOL_LOOK_SCREEN_ENABLED", True),
    "TOOL_READ_SHEET_ENABLED": lambda: _get_bool("TOOL_READ_SHEET_ENABLED", True),
    "TOOL_PLAY_SFX_ENABLED": lambda: _get_bool("TOOL_PLAY_SFX_ENABLED", True),
    "TOOL_WRITE_DIARY_ENABLED": lambda: _get_bool("TOOL_WRITE_DIARY_ENABLED", True),
    "TOOL_EPIANO_ENABLED": lambda: _get_bool("TOOL_EPIANO_ENABLED", True),
    "TOOL_REGISTRY": lambda: _get_bool("TOOL_REGISTRY", True),
    "MCP_ENABLED": lambda: _get_bool("MCP_ENABLED", False),
    "MCP_CONFIG_PATH": lambda: os.getenv("MCP_CONFIG_PATH") or (
        os.path.join(_PROJECT_ROOT, "configs", "mcp.json")
        if os.path.isfile(os.path.join(_PROJECT_ROOT, "configs", "mcp.json"))
        else os.path.join(_PROJECT_ROOT, "src", "mcp", "mcp_config.json")),
    "SKILLS_DIR": lambda: os.getenv("SKILLS_DIR") or "skills",
    "ENABLE_SESSION_SEARCH": lambda: _get_bool("ENABLE_SESSION_SEARCH", False),
}

_EVOLUTION_LOADERS = {
    "EVOLUTION_ENABLED": lambda: _get_bool("EVOLUTION_ENABLED", True),
    "EVOLUTION_MIN_INTERVAL": lambda: int(os.getenv("EVOLUTION_MIN_INTERVAL", "600")),
    "EVOLUTION_MIN_TURNS": lambda: int(os.getenv("EVOLUTION_MIN_TURNS", "10")),
    "EVOLUTION_MIN_ACTIVE_GAP": lambda: int(
        os.getenv("EVOLUTION_MIN_ACTIVE_GAP", "300")),
    "EVOLUTION_PERIODIC_INTERVAL": lambda: int(os.getenv("EVOLUTION_PERIODIC_INTERVAL", "1800")),
    "EVOLUTION_EVAL_ENABLED": lambda: _get_bool("EVOLUTION_EVAL_ENABLED", True),
    "EVOLUTION_EVAL_CASES": lambda: max(
        1, min(int(os.getenv("EVOLUTION_EVAL_CASES", "2")), 3)),
    "EVOLUTION_PROMPT_EVO_ENABLED": lambda: _get_bool("EVOLUTION_PROMPT_EVO_ENABLED", True),
    "EVOLUTION_PROMPT_EVO_INTERVAL": lambda: int(os.getenv("EVOLUTION_PROMPT_EVO_INTERVAL", "21600")),
    "EVOLUTION_INJECT_IN_USER": lambda: _get_bool("EVOLUTION_INJECT_IN_USER", True),
    "EVOLUTION_CURATOR_IDLE_HOURS": lambda: float(
        os.getenv("EVOLUTION_CURATOR_IDLE_HOURS", "2")),
    "EVOLUTION_FEEDBACK_ENABLED": lambda: _get_bool(
        "EVOLUTION_FEEDBACK_ENABLED", True),
    "EVOLUTION_POLICY_AB": lambda: _get_bool("EVOLUTION_POLICY_AB", False),
}

_VOICE_LOADERS = {
    "GPTSOVITS_REF_AUDIO": lambda: os.getenv("GPTSOVITS_REF_AUDIO", ""),
    "GPTSOVITS_REF_AUDIOS": lambda: os.getenv("GPTSOVITS_REF_AUDIOS", ""),
    "GPTSOVITS_PROMPT_TEXT": lambda: os.getenv("GPTSOVITS_PROMPT_TEXT", ""),
    "GPTSOVITS_TIMEOUT": lambda: float(os.getenv("GPTSOVITS_TIMEOUT", "120")),
    "GPTSOVITS_MODELS_DIR": lambda: os.getenv("GPTSOVITS_MODELS_DIR", ""),
    "GPTSOVITS_ROLE_GPT": lambda: os.getenv("GPTSOVITS_ROLE_GPT", ""),
    "GPTSOVITS_ROLE_SOVITS": lambda: os.getenv("GPTSOVITS_ROLE_SOVITS", ""),
    "TTS_OUTPUT_DEVICE": lambda: os.getenv("TTS_OUTPUT_DEVICE", ""),
    "LIPSYNC_MODE": lambda: (os.getenv("LIPSYNC_MODE") or "builtin").strip(),
    "STT_ENABLED": lambda: _get_bool("STT_ENABLED", False),
    "STT_MODEL": lambda: os.getenv("STT_MODEL") or "FunAudioLLM/SenseVoiceSmall",
    "STT_LOCAL_MODEL_PATH": lambda: os.getenv("STT_LOCAL_MODEL_PATH") or "",
    "STT_LOCAL_MODEL_REVISION": lambda: os.getenv("STT_LOCAL_MODEL_REVISION") or "v2.0.4",
    "STT_SERVER_URL": lambda: os.getenv("STT_SERVER_URL") or "http://127.0.0.1:8487",
    "STT_API_KEY": lambda: os.getenv("STT_API_KEY") or "",
    "STT_BASE_URL": lambda: os.getenv("STT_BASE_URL") or "",
    "STT_LEVEL_THRESHOLD": lambda: float(os.getenv("STT_LEVEL_THRESHOLD") or "500"),
    "STT_VAD_MODE": lambda: int(os.getenv("STT_VAD_MODE") or "2"),
    "STT_SILENCE_SECONDS": lambda: float(os.getenv("STT_SILENCE_SECONDS") or "0.6"),
    "STT_MAX_SECONDS": lambda: float(os.getenv("STT_MAX_SECONDS") or "10"),
    "STT_INTERRUPT_MIN_SECONDS": lambda: float(os.getenv("STT_INTERRUPT_MIN_SECONDS") or "1.0"),
    "MOUTH_PARAMETER": lambda: os.getenv("MOUTH_PARAMETER", ""),
    "MOUTH_GAIN": lambda: float(os.getenv("MOUTH_GAIN", "0.4")),
}

_VTS_LOADERS = {
    "VTS_PORT": lambda: int(os.getenv("VTS_PORT", "8001")),
    "VTS_PLUGIN_NAME": lambda: os.getenv("VTS_PLUGIN_NAME") or "ZhipuAI_VTuber",
    "VTS_PLUGIN_DEVELOPER": lambda: os.getenv("VTS_PLUGIN_DEVELOPER", "LocalUser"),
    "MOTION_PATH": lambda: os.getenv("MOTION_PATH", ""),
    "VTS_ROOT": lambda: os.getenv("VTS_ROOT", "").strip(),
    "VTS_IDLE_TAKEOVER": lambda: _get_bool("VTS_IDLE_TAKEOVER", True),
}

_PET_LOADERS = {
    "RUN_MODE": lambda: (os.getenv("RUN_MODE") or "vtuber").strip().lower(),
    "PET_MODEL_PATH": lambda: os.getenv("PET_MODEL_PATH") or _default_pet_model(),
    "PET_WINDOW_SIZE": lambda: os.getenv("PET_WINDOW_SIZE") or "",
    "PET_ALWAYS_ON_TOP": lambda: _get_bool("PET_ALWAYS_ON_TOP", True),
    "PET_MOTION_PATH": lambda: os.getenv("PET_MOTION_PATH") or "",
    "PET_IDLE_MOTION": lambda: os.getenv("PET_IDLE_MOTION") or "",
}

_EMOTION_LOADERS = {
    "EMOTION_ACTOR_ENABLED": lambda: _get_bool("EMOTION_ACTOR_ENABLED", False),
    "SILICONFLOW_API_KEY": lambda: os.getenv("SILICONFLOW_API_KEY") or "",
    "SILICONFLOW_MODEL": lambda: os.getenv("SILICONFLOW_MODEL") or "Qwen/Qwen3-Embedding-0.6B",
    "SILICONFLOW_BASE_URL": lambda: os.getenv("SILICONFLOW_BASE_URL") or "https://api.siliconflow.cn/v1",
    "EMBEDDING_BASE_URL": lambda: os.getenv("EMBEDDING_BASE_URL") or (
        os.getenv("SILICONFLOW_BASE_URL") or "https://api.siliconflow.cn/v1"),
    "EMBEDDING_API_KEY": lambda: (os.getenv("EMBEDDING_API_KEY")
                                  or os.getenv("SILICONFLOW_API_KEY") or ""),
    "EMBEDDING_DIMENSIONS": lambda: _get_optional_int("EMBEDDING_DIMENSIONS"),
    "EMBEDDING_MODEL": lambda: os.getenv("EMBEDDING_MODEL") or (
        os.getenv("SILICONFLOW_MODEL") or "Qwen/Qwen3-Embedding-0.6B"),
    "EMOTION_MAP_FILE": lambda: os.getenv("EMOTION_MAP_FILE") or _default_emotion_map_file(),
}

_PROACTIVE_LOADERS = {
    "PROACTIVE_ENABLED": lambda: _get_bool("PROACTIVE_ENABLED", True),
    "RESPONSE_INTERVAL_MIN": lambda: float(os.getenv("RESPONSE_INTERVAL_MIN") or "5"),
    "RESPONSE_INTERVAL_MAX": lambda: float(os.getenv("RESPONSE_INTERVAL_MAX") or "10"),
    # —— Nudge 契机引擎（Neuro 风格事件驱动）——
    "PROACTIVE_NUDGE_ENABLED": lambda: _get_bool("PROACTIVE_NUDGE_ENABLED", True),
    "PROACTIVE_NUDGE_LONG_SILENCE_SEC": lambda: float(
        os.getenv("PROACTIVE_NUDGE_LONG_SILENCE_SEC") or "30"),
    "PROACTIVE_NUDGE_SILENT_TOO_LONG_SEC": lambda: float(
        os.getenv("PROACTIVE_NUDGE_SILENT_TOO_LONG_SEC") or "300"),
    "PROACTIVE_NUDGE_MANY_UNREAD": lambda: int(
        os.getenv("PROACTIVE_NUDGE_MANY_UNREAD") or "5"),
    "PROACTIVE_NUDGE_BURST_THRESHOLD": lambda: int(
        os.getenv("PROACTIVE_NUDGE_BURST_THRESHOLD") or "10"),
    "PROACTIVE_NUDGE_BURST_WINDOW_SEC": lambda: float(
        os.getenv("PROACTIVE_NUDGE_BURST_WINDOW_SEC") or "30"),
    "PROACTIVE_NUDGE_COOLDOWN_SEC": lambda: float(
        os.getenv("PROACTIVE_NUDGE_COOLDOWN_SEC") or "30"),
    "PROACTIVE_NUDGE_REPEAT_GAP_SEC": lambda: float(
        os.getenv("PROACTIVE_NUDGE_REPEAT_GAP_SEC") or "60"),
    "PROACTIVE_FORCE_SPEAK": lambda: _get_bool("PROACTIVE_FORCE_SPEAK", True),
}

_SOCIAL_LOADERS = {
    # —— 拟人化层（ev.social，EV-Anthropomorphic 方案）——
    "SOCIAL_EVENT_DRIVEN": lambda: _get_bool("SOCIAL_EVENT_DRIVEN", True),
    "SOCIAL_ENGAGEMENT_ENABLED": lambda: _get_bool("SOCIAL_ENGAGEMENT_ENABLED", True),
    "SOCIAL_ENGAGEMENT_DEFAULT_STATE": lambda: (
        os.getenv("SOCIAL_ENGAGEMENT_DEFAULT_STATE") or "observe"),
    "SOCIAL_ENGAGEMENT_ACTIVE_DENSITY": lambda: int(
        os.getenv("SOCIAL_ENGAGEMENT_ACTIVE_DENSITY") or "5"),
    "SOCIAL_ENGAGEMENT_EXIT_AFTER": lambda: int(
        os.getenv("SOCIAL_ENGAGEMENT_EXIT_AFTER") or "180"),
    "SOCIAL_ENGAGEMENT_SLEEP_HOUR": lambda: int(
        os.getenv("SOCIAL_ENGAGEMENT_SLEEP_HOUR") or "24"),
    "SOCIAL_DELIBERATION_ENABLED": lambda: _get_bool("SOCIAL_DELIBERATION_ENABLED", False),
    "SOCIAL_QUOTE_ENABLED": lambda: _get_bool("SOCIAL_QUOTE_ENABLED", True),
    "SOCIAL_QUOTE_EXTRA_NAMES": lambda: os.getenv("SOCIAL_QUOTE_EXTRA_NAMES") or "",
    "SOCIAL_QUOTE_INTEREST_KEYWORDS": lambda: os.getenv("SOCIAL_QUOTE_INTEREST_KEYWORDS") or "",
    "SOCIAL_QUOTE_BLOCKED_KEYWORDS": lambda: os.getenv("SOCIAL_QUOTE_BLOCKED_KEYWORDS") or "",
    "SOCIAL_QUOTE_RECENT_WINDOW_SEC": lambda: float(
        os.getenv("SOCIAL_QUOTE_RECENT_WINDOW_SEC") or "300"),
    "SOCIAL_SILENCE_ENABLED": lambda: _get_bool("SOCIAL_SILENCE_ENABLED", True),
    "SOCIAL_SILENCE_RATE_TARGET": lambda: float(
        os.getenv("SOCIAL_SILENCE_RATE_TARGET") or "0.2"),
    "SOCIAL_LEARNING_ENABLED": lambda: _get_bool("SOCIAL_LEARNING_ENABLED", True),
    "SOCIAL_LEARNING_SKIP_SC": lambda: _get_bool("SOCIAL_LEARNING_SKIP_SC", True),
    "SOCIAL_LEARNING_MIN_FREQ": lambda: int(os.getenv("SOCIAL_LEARNING_MIN_FREQ") or "3"),
    "SOCIAL_LEARNING_MAX_LEXICON_SIZE": lambda: int(
        os.getenv("SOCIAL_LEARNING_MAX_LEXICON_SIZE") or "200"),
    "SOCIAL_LEARNING_TTL_DAYS": lambda: int(os.getenv("SOCIAL_LEARNING_TTL_DAYS") or "30"),
    "SOCIAL_LEARNING_PURGE_TODAY": lambda: _get_bool("SOCIAL_LEARNING_PURGE_TODAY", False),
    "TOOL_SOCIAL_SPEAK_ENABLED": lambda: _get_bool("TOOL_SOCIAL_SPEAK_ENABLED", True),
}

_DANMAKU_LOADERS = {
    "BILI_ENABLED": lambda: _get_bool("BILI_ENABLED", True),
    "BILI_ROOM_ID": lambda: int(os.getenv("BILI_ROOM_ID") or "0"),
    "BILI_ROOM_IDS": lambda: _get_room_ids(),
    "BILI_SESSDATA": lambda: os.getenv("BILI_SESSDATA") or "",
    "BILI_SERVER_PORT": lambda: int(os.getenv("BILI_SERVER_PORT") or "8766"),
}

_MINDCRAFT_LOADERS = {
    "MINDCRAFT_PATH": lambda: os.getenv("MINDCRAFT_PATH") or os.path.join(
        _PROJECT_ROOT, "plugins", "mindcraft"),
    "MINDCRAFT_LLM_BASE_URL": lambda: os.getenv(
        "MINDCRAFT_LLM_BASE_URL") or os.getenv("LLM_BASE_URL", ""),
    "MINDCRAFT_LLM_MODEL": lambda: os.getenv(
        "MINDCRAFT_LLM_MODEL") or os.getenv(
        "LLM_MODEL", "") or "glm-4-flash-250414",
    "MINDCRAFT_BOT_NAME": lambda: os.getenv("MINDCRAFT_BOT_NAME") or "vtuber",
    "MINDCRAFT_HOST": lambda: os.getenv("MINDCRAFT_HOST") or "127.0.0.1",
    "MINDCRAFT_PORT": lambda: int(os.getenv("MINDCRAFT_PORT") or "55916"),
    "MINDCRAFT_AUTH": lambda: os.getenv("MINDCRAFT_AUTH") or "offline",
    "MINDCRAFT_MINDSERVER_PORT": lambda: int(
        os.getenv("MINDCRAFT_MINDSERVER_PORT") or "8080"),
    "MINDCRAFT_BRIDGE_ENABLED": lambda: _get_bool("MINDCRAFT_BRIDGE_ENABLED", False),
    "MINDCRAFT_BOT_PERSONA": lambda: os.getenv("MINDCRAFT_BOT_PERSONA") or "",
}

_PATHS_LOADERS = {
    "PROJECT_ROOT": lambda: _PROJECT_ROOT,
    # 可写数据根：默认 <PROJECT_ROOT>/data，可用 E_V_DATA_DIR 重定向到
    # 独立目录（便携版把用户数据放 U 盘等）；内置资源仍走 PROJECT_ROOT
    "DATA_ROOT": _data_root,
    "TOKEN_FILE": lambda: os.path.join(_data_root(), "vts", "vts_token.json"),
    "CONFIG_YAML_PATH": lambda: os.getenv("CONFIG_YAML_PATH") or os.path.join(
        _PROJECT_ROOT, "configs", "config.yaml"),
}

# 组名 → 字段加载表（Config 子配置属性名与组名一致）
_LOADERS = {
    "llm": _LLM_LOADERS,
    "butler": _BUTLER_LOADERS,
    "memory": _MEMORY_LOADERS,
    "knowledge": _KNOWLEDGE_LOADERS,
    "agent": _AGENT_LOADERS,
    "persona": _PERSONA_LOADERS,
    "filter": _FILTER_LOADERS,
    "tool": _TOOL_LOADERS,
    "evolution": _EVOLUTION_LOADERS,
    "voice": _VOICE_LOADERS,
    "vts": _VTS_LOADERS,
    "pet": _PET_LOADERS,
    "emotion": _EMOTION_LOADERS,
    "proactive": _PROACTIVE_LOADERS,
    "social": _SOCIAL_LOADERS,
    "danmaku": _DANMAKU_LOADERS,
    "mindcraft": _MINDCRAFT_LOADERS,
    "paths": _PATHS_LOADERS,
}

# 字段名 → 所在组（yaml 覆盖层 / 按字段热更新用）
_FIELD_GROUP = {
    attr: group
    for group, fields in _LOADERS.items()
    for attr in fields
}

# ===== 热更新字段清单（精确复刻原 reload_tool_runtime / reload_config） =====
# !tools（工具屋）：工具 / MCP / Mindcraft / 进化 / 模型路由 / STT 开关
_TOOL_HOT_FIELDS = (
    "TOOLS_ENABLED", "OPENWEATHERMAP_API_KEY", "MCP_ENABLED",
    "MINDCRAFT_PATH", "MINDCRAFT_LLM_BASE_URL", "MINDCRAFT_LLM_MODEL",
    "MINDCRAFT_BOT_NAME", "MINDCRAFT_HOST", "MINDCRAFT_PORT",
    "MINDCRAFT_AUTH", "MINDCRAFT_MINDSERVER_PORT",
    "MINDCRAFT_BRIDGE_ENABLED", "MINDCRAFT_BOT_PERSONA",
    "TOOL_GET_CURRENT_TIME_ENABLED", "TOOL_GET_WEATHER_ENABLED",
    "TOOL_LOAD_SKILL_ENABLED", "TOOL_LOOK_SCREEN_ENABLED",
    "TOOL_READ_SHEET_ENABLED",
    "TOOL_PLAY_SFX_ENABLED",
    "TOOL_EPIANO_ENABLED",
    "EVOLUTION_ENABLED", "EVOLUTION_MIN_INTERVAL", "EVOLUTION_MIN_TURNS",
    "EVOLUTION_EVAL_ENABLED", "EVOLUTION_EVAL_CASES",
    "EVOLUTION_PROMPT_EVO_ENABLED", "EVOLUTION_PROMPT_EVO_INTERVAL",
    "EVOLUTION_INJECT_IN_USER", "EVOLUTION_CURATOR_IDLE_HOURS",
    "EVOLUTION_FEEDBACK_ENABLED", "EVOLUTION_POLICY_AB",
    "LLM_SERVERS", "LLM_ROUTER_ENABLED", "LLM_ROUTER_EPSILON",
    "STT_ENABLED", "STT_MODEL",
    "STT_LOCAL_MODEL_PATH", "STT_LOCAL_MODEL_REVISION",
    "STT_SERVER_URL", "STT_API_KEY", "STT_BASE_URL",
)

# !config（统一配置热更新）：= !tools 字段 + 其余可热更新字段
_ALL_HOT_FIELDS = _TOOL_HOT_FIELDS + (
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_THINKING",
    "LLM_REASONING_EFFORT",
    "BUTLER_THINKING",
    "SYSTEM_PROMPT_FILE", "SYSTEM_PROMPT", "AUTHOR_NOTE",
    "KNOWLEDGE_ENABLED", "KNOWLEDGE_MAX_CHARS",
    "AGENT_ENABLED", "AGENT_MODEL", "AGENT_MAX_STEPS", "AGENT_MAX_TOKENS",
    "AGENT_WORKSPACE", "AGENT_ALLOW_SHELL", "AGENT_ALLOWED_COMMANDS",
    "AGENT_AUDIT_LOG", "BUDGET_TOKEN_PER_MINUTE", "BUDGET_REQUEST_PER_MINUTE",
    "BUDGET_COST_PER_HOUR", "AGENT_SKILL_CREATION",
    "AGENT_MEMORY_SINK",
    "PROACTIVE_ENABLED", "RESPONSE_INTERVAL_MIN", "RESPONSE_INTERVAL_MAX",
    "PROACTIVE_NUDGE_ENABLED", "PROACTIVE_NUDGE_LONG_SILENCE_SEC",
    "PROACTIVE_NUDGE_SILENT_TOO_LONG_SEC", "PROACTIVE_NUDGE_MANY_UNREAD",
    "PROACTIVE_NUDGE_BURST_THRESHOLD", "PROACTIVE_NUDGE_BURST_WINDOW_SEC",
    "PROACTIVE_NUDGE_COOLDOWN_SEC", "PROACTIVE_NUDGE_REPEAT_GAP_SEC",
    "PROACTIVE_FORCE_SPEAK",
    "SOCIAL_EVENT_DRIVEN", "SOCIAL_ENGAGEMENT_ENABLED",
    "SOCIAL_ENGAGEMENT_DEFAULT_STATE", "SOCIAL_ENGAGEMENT_ACTIVE_DENSITY",
    "SOCIAL_ENGAGEMENT_EXIT_AFTER", "SOCIAL_ENGAGEMENT_SLEEP_HOUR",
    "SOCIAL_DELIBERATION_ENABLED", "SOCIAL_QUOTE_ENABLED",
    "SOCIAL_QUOTE_EXTRA_NAMES", "SOCIAL_QUOTE_INTEREST_KEYWORDS",
    "SOCIAL_QUOTE_BLOCKED_KEYWORDS", "SOCIAL_QUOTE_RECENT_WINDOW_SEC",
    "SOCIAL_SILENCE_ENABLED", "SOCIAL_SILENCE_RATE_TARGET",
    "SOCIAL_LEARNING_ENABLED", "SOCIAL_LEARNING_SKIP_SC",
    "SOCIAL_LEARNING_MIN_FREQ", "SOCIAL_LEARNING_MAX_LEXICON_SIZE",
    "SOCIAL_LEARNING_TTL_DAYS", "SOCIAL_LEARNING_PURGE_TODAY",
    "TOOL_SOCIAL_SPEAK_ENABLED",
    "LLM_MAX_CONCURRENCY", "PROACTIVE_QUEUE_MAX",
    "AGENT_AVOID_MAIN_LLM", "AGENT_HISTORY_SNAPSHOT", "AGENT_DUP_THRESHOLD",
    "PROFANITY_FILTER_ENABLED", "PROFANITY_FILTER_RATE",
    "MEMORY_ENABLED", "MEMORY_LIFECYCLE_ENABLED",
    "MEMORY_LIFECYCLE_MODEL", "MEMORY_LIFECYCLE_THRESHOLD",
    "MEMORY_CURATED_ENABLED", "MEMORY_HISTORY_ENABLED", "HISTORY_DB_PATH",
    "MEMORY_CURATOR_ENABLED", "MEMORY_CURATOR_INTERVAL",
    "GPTSOVITS_REF_AUDIO", "GPTSOVITS_REF_AUDIOS", "GPTSOVITS_PROMPT_TEXT",
    "PET_ALWAYS_ON_TOP", "PET_WINDOW_SIZE", "PET_IDLE_MOTION",
    "PET_MODEL_PATH",
    "EMOTION_ACTOR_ENABLED",
    "BILI_ENABLED", "BILI_ROOM_ID", "BILI_ROOM_IDS",
    "BILI_SESSDATA", "BILI_SERVER_PORT",
)

__all__ = [
    "_LLM_LOADERS", "_BUTLER_LOADERS", "_MEMORY_LOADERS", "_KNOWLEDGE_LOADERS",
    "_AGENT_LOADERS", "_PERSONA_LOADERS", "_FILTER_LOADERS", "_TOOL_LOADERS",
    "_EVOLUTION_LOADERS", "_VOICE_LOADERS", "_VTS_LOADERS", "_PET_LOADERS",
    "_EMOTION_LOADERS", "_PROACTIVE_LOADERS", "_SOCIAL_LOADERS",
    "_DANMAKU_LOADERS",
    "_MINDCRAFT_LOADERS", "_PATHS_LOADERS", "_LOADERS", "_FIELD_GROUP",
    "_TOOL_HOT_FIELDS", "_ALL_HOT_FIELDS",
]
