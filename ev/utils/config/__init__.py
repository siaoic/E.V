"""Config package - 拆自 src/utils/config.py（§3.8）。"""

# 1) helpers
from .env_loader import (
    _PROJECT_ROOT, _SKILL_EXTENSIONS, _CONFIGS_PERSONA_SKILL,
    _UI_SYSTEM_PROMPT_FILE_LEGACY, _resolve_ui_system_prompt_file,
    _strip_frontmatter, _read_text_file, _collect_skill_files,
    _safe_print, _load_system_prompt, _get_bool, _get_optional_int,
    _parse_aux_models, _get_room_ids, _default_pet_model,
    _data_root, _default_emotion_map_file, _SKIP_YAML_FIELDS,
)

# 2) 18 dataclasses
from .dataclasses import (
    LLMConfig, ButlerConfig, MemoryConfig, KnowledgeConfig, AgentConfig,
    PersonaConfig, FilterConfig, ToolConfig, EvolutionConfig, VoiceConfig,
    VTSConfig, PetConfig, EmotionConfig, ProactiveConfig, DanmakuConfig,
    MindcraftConfig, PathsConfig,
)

# 3) 字段加载表 + 热更新字段清单
from .loaders import (
    _LLM_LOADERS, _BUTLER_LOADERS, _MEMORY_LOADERS, _KNOWLEDGE_LOADERS,
    _AGENT_LOADERS, _PERSONA_LOADERS, _FILTER_LOADERS, _TOOL_LOADERS,
    _EVOLUTION_LOADERS, _VOICE_LOADERS, _VTS_LOADERS, _PET_LOADERS,
    _EMOTION_LOADERS, _PROACTIVE_LOADERS, _DANMAKU_LOADERS,
    _MINDCRAFT_LOADERS, _PATHS_LOADERS, _LOADERS, _FIELD_GROUP,
    _TOOL_HOT_FIELDS, _ALL_HOT_FIELDS,
)

# 4) Config 根类
from .root import Config

# 5) cfg 单例
cfg = Config()  # noqa: F401（from src.utils import config.cfg 用）

# 6) reload（在 cfg 实例化后）
import os
from dotenv import load_dotenv


def reload_tool_runtime() -> None:
    """重新读取 .env 中与工具启用相关的字段，刷新 cfg 单例。

    控制中心「工具屋」勾选工具后写 .env，再向主程序 stdin 发 `!tools`
    命令；主进程调用本函数即可让工具开关 / API Key / MCP_ENABLED 在
    下一轮对话生效（llm_brain._get_tools 每轮实时读取 cfg，无需重启）。
    """
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)
    cfg._load_selected(_TOOL_HOT_FIELDS)
    # yaml 覆盖层（环境变量未设置的字段回落到 config.yaml，保持一致）
    cfg._apply_yaml_overrides()


def reload_config() -> None:
    """重新读取 .env 全部可热更新字段，刷新 cfg 单例。

    控制中心「更新配置」保存后向主程序 stdin 发 `!config` 命令；主进程
    调用本函数即可让 LLM 配置 / 人设 / 主动对话 / 内容过滤 / 记忆 /
    桌宠窗口 / 待机动作等配置立即生效（无需重启主程序）。
    """
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)
    # 全量可热更新字段（含 !tools 字段，复用同一份 loader）
    cfg._load_selected(_ALL_HOT_FIELDS)
    # yaml 覆盖层（环境变量未设置的字段回落到 config.yaml，保持一致）
    cfg._apply_yaml_overrides()


# 7) 兼容旧路径残留（有些模块直接用 config 的 dataclass）
__all__ = [
    "cfg", "Config",
    # dataclasses
    "LLMConfig", "ButlerConfig", "MemoryConfig", "KnowledgeConfig",
    "AgentConfig", "PersonaConfig", "FilterConfig", "ToolConfig",
    "EvolutionConfig", "VoiceConfig", "VTSConfig", "PetConfig",
    "EmotionConfig", "ProactiveConfig", "DanmakuConfig",
    "MindcraftConfig", "PathsConfig",
    # reload
    "reload_tool_runtime", "reload_config",
]
