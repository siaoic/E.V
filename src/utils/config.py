"""配置加载：环境变量 → 结构化 Config 对象（§3.8 dataclass 化）。

原 50+ 个 cfg.XXX 平铺字段按语义分组为 17 个子 dataclass（LLMConfig /
ButlerConfig / VoiceConfig / ...），Config 聚合子配置，旧字段名通过
@property 转发保持完全兼容（`cfg.LLM_API_KEY` 等价 `cfg.llm.LLM_API_KEY`）。

- 字段加载表达式统一收敛到 _LOADERS 字段加载表（新增字段 = 子配置一行 +
  加载表一行），热更新 reload_config / reload_tool_runtime 按字段清单
  复用同一份 loader，消灭原来的逐字段重复赋值。
- 所有模块统一通过 `from src.utils import config` 读取 `config.cfg` 中的字段。
"""

import os
import sys
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

# 项目根目录（本文件位于 src/utils/ 下，上三级即根）：
# .env、data/、main.py 等都在根目录。
# PyInstaller 打包后（sys.frozen）：__file__ 指向临时解压目录（_MEIPASS），
# 必须用 exe 所在目录作为项目根——.env / live2d 模型 / data 都随 exe 放一起。
if getattr(sys, "frozen", False):
    _PROJECT_ROOT = os.path.dirname(sys.executable)
else:
    _PROJECT_ROOT = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))


# ===== System Prompt 加载（支持直接加载整个 skill 文件夹） =====
_SKILL_EXTENSIONS = (".md", ".txt")
# 控制中心 UI 人设编辑框（ed_prompt）的持久化文件：
# SYSTEM_PROMPT_FILE 未配置时，自动使用「ui 里的 System_prompt」（本文件内容）。
# 人设存这里而不是 .env——多行值写进 .env 有解析风险且会撑大 .env。
_UI_SYSTEM_PROMPT_FILE = os.path.join(
    _PROJECT_ROOT, "ui", "data", "system_prompt.txt")


def _strip_frontmatter(text: str) -> str:
    """剥离 YAML frontmatter（文件开头 --- ... --- 的元数据块）。"""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def _read_text_file(path: str) -> str:
    """读取文本文件，自动尝试常见编码（UTF-8 → GBK → 替换符兜底）。"""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="gbk", errors="replace") as f:
        return f.read()


def _collect_skill_files(base: str) -> list:
    """递归收集 skill 路径下的全部文本文件内容（相对路径作小节标题）。

    base 可以是单个文件或文件夹；路径不存在时返回空列表。
    """
    if not os.path.exists(base):
        return []
    if os.path.isfile(base):
        paths = [base]
    else:
        paths = []
        for root, _dirs, files in os.walk(base):
            for name in sorted(files):
                if name.lower().endswith(_SKILL_EXTENSIONS):
                    paths.append(os.path.join(root, name))
        paths.sort()
    parts = []
    for p in paths:
        try:
            text = _read_text_file(p)
        except OSError:
            continue
        text = _strip_frontmatter(text)
        if not text.strip():
            continue
        # 用相对路径作小节标题，便于模型区分来源
        rel = os.path.relpath(p, _PROJECT_ROOT)
        parts.append(f"<!-- source: {rel} -->\n{text}")
    return parts


def _safe_print(msg: str) -> None:
    """容错打印：打包后 windowed 模式 stdout 可能为 None、GBK 控制台可能
    编码失败（emoji 等），统一吞掉避免启动崩溃。"""
    try:
        print(msg)
    except Exception:
        pass


def _load_system_prompt() -> str:
    """加载人设 system prompt。

    1. SYSTEM_PROMPT_FILE 指向 skill 文件/文件夹 → 直接加载整个内容
       （人设不再硬编码在 .env，而是加载整个 skill 目录）
    2. 否则自动使用「控制中心 UI 里设置的 System_prompt」
       （ed_prompt 持久化在 ui/data/system_prompt.txt）
    3. 兼容旧配置：.env 的 SYSTEM_PROMPT 文本
    4. 兜底内置默认人设
    """
    # dotenv 不支持行内注释（`SYSTEM_PROMPT_FILE =   # 留空 = ...` 会把注释
    # 当值读入），截断 # 后内容，避免把注释文字误当路径触发警告。
    skill_file = (os.getenv("SYSTEM_PROMPT_FILE") or "").split("#", 1)[0].strip()
    if skill_file:
        base = os.path.join(_PROJECT_ROOT, skill_file)
        parts = _collect_skill_files(base)
        if parts:
            return "\n\n".join(parts)
        _safe_print(
            f"[config] [警告] SYSTEM_PROMPT_FILE 路径未找到任何文本文件，"
            f"回退到 UI 人设 / SYSTEM_PROMPT: {base}")
    try:
        if os.path.isfile(_UI_SYSTEM_PROMPT_FILE):
            # 与 skill 文件一致：剥离 YAML frontmatter（--- name/description ---
            # 是 skill 元数据，不是人设正文，直接发给 LLM 会污染 system prompt）
            text = _strip_frontmatter(
                _read_text_file(_UI_SYSTEM_PROMPT_FILE)).strip()
            if text:
                return text
    except OSError:
        pass
    prompt = os.getenv("SYSTEM_PROMPT") or ""
    if prompt.strip():
        return prompt.strip()
    return (
        "你是一名温柔可爱的虚拟主播，用自然亲切的口语与观众交流，回答简洁生动。"
    )


def _get_bool(key: str, default: bool) -> bool:
    """布尔配置：None 用默认值；"" / "false" / "0" 等返回 False。

    注意与 str/int 字段的 `or` 兜底不同：空串不回落默认值（历史行为）。
    """
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


def _get_optional_int(key: str) -> Optional[int]:
    """读整数配置；未设置或非数字返回 None（配合留空 = 服务端默认）。"""
    val = os.getenv(key)
    if not val or not val.strip():
        return None
    try:
        return int(val.strip())
    except ValueError:
        return None


def _get_room_ids() -> list:
    """直播间房间号列表：优先 BILI_ROOM_IDS（逗号分隔，多房间）；
    未配置时回退 BILI_ROOM_ID（单房间，与原行为一致）；都没配返回空列表。"""
    ids = []
    for part in (os.getenv("BILI_ROOM_IDS") or "").split(","):
        part = part.strip()
        if part.isdigit() and int(part) not in ids:
            ids.append(int(part))
    if ids:
        return ids
    rid = int(os.getenv("BILI_ROOM_ID") or "0")
    return [rid] if rid else []


# 桌宠模型默认值：只从 live2d 文件夹探测实际存在的 .model3.json（用户指定目录）。
# 返回相对项目根的路径（正斜杠）；找不到返回空串（由上层报错提示配置）。
def _default_pet_model() -> str:
    root = os.path.join(_PROJECT_ROOT, "live2d")
    if not os.path.isdir(root):
        return ""
    for base, _dirs, files in os.walk(root):
        for name in sorted(files):
            if name.lower().endswith(".model3.json"):
                return os.path.relpath(os.path.join(base, name),
                                       _PROJECT_ROOT).replace("\\", "/")
    return ""


def _data_root() -> str:
    """可写数据根：默认 <PROJECT_ROOT>/data，可用 E_V_DATA_DIR 重定向。

    与 _PATHS_LOADERS 的 DATA_ROOT 同一规则，供派生路径（TOKEN_FILE /
    EMOTION_MAP_FILE）复用，避免默认表达式重复。
    """
    return os.getenv("E_V_DATA_DIR") or os.path.join(_PROJECT_ROOT, "data")


def _default_emotion_map_file() -> str:
    """情绪 → 表情/动作映射文件默认路径：按运行模式分文件（vtuber/pet）。"""
    mode = (os.getenv("RUN_MODE") or "vtuber").strip().lower()
    return os.path.join(
        _data_root(),
        "emotion_map_vts.json" if mode == "vtuber" else "emotion_map.json")


# yaml 覆盖层不处理的字段（派生路径 / 配置文件自身路径）
_SKIP_YAML_FIELDS = {"PROJECT_ROOT", "TOKEN_FILE", "CONFIG_YAML_PATH"}


# =====================================================================
# 分组子配置（§3.8）：字段 = 类型占位，实际值由 _LOADERS 字段加载表填充
# =====================================================================
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
    """记忆系统（Mem0 判决链：ADD/UPDATE/DELETE/IGNORE）。"""
    MEMORY_ENABLED: bool = True
    MEMORY_LIFECYCLE_ENABLED: bool = False
    MEMORY_LIFECYCLE_MODEL: str = ""
    MEMORY_LIFECYCLE_THRESHOLD: float = 0.6


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
    PROACTIVE_QUEUE_MAX: int = 4
    AGENT_AVOID_MAIN_LLM: bool = True
    AGENT_HISTORY_SNAPSHOT: int = 6
    AGENT_DUP_THRESHOLD: float = 0.85


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
    TOOL_PLAY_SFX_ENABLED: bool = True
    MCP_ENABLED: bool = False
    MCP_CONFIG_PATH: str = ""
    SKILLS_DIR: str = "src/llm/skills"


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


@dataclass
class VoiceConfig:
    """语音链路：GPT-SoVITS TTS + STT（本地/云端）+ 口型参数。"""
    GPTSOVITS_REF_AUDIO: str = ""
    GPTSOVITS_REF_AUDIOS: str = ""
    GPTSOVITS_PROMPT_TEXT: str = ""
    GPTSOVITS_TIMEOUT: float = 120
    GPTSOVITS_MODELS_DIR: str = ""
    TTS_SERVER_URL: str = "http://127.0.0.1:8000"
    TTS_OUTPUT_DEVICE: str = ""
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


# =====================================================================
# 字段加载表：旧字段名 → 无参加载函数（逐条复刻原类体默认值表达式）
# 新增配置字段 = 子配置声明一行 + 本表一行，热更新自动生效
# =====================================================================
_LLM_LOADERS = {
    "LLM_API_KEY": lambda: os.getenv("LLM_API_KEY", "") or os.getenv("ZHIPU_API_KEY", ""),
    "LLM_BASE_URL": lambda: os.getenv("LLM_BASE_URL", ""),
    "LLM_MODEL": lambda: os.getenv("LLM_MODEL", "") or os.getenv("ZHIPU_MODEL", "glm-4.7-flash"),
    "LLM_THINKING": lambda: _get_bool("LLM_THINKING", _get_bool("THINKING_ENABLED", True)),
    "LLM_SERVERS": lambda: os.getenv("LLM_SERVERS") or "",
    "LLM_ROUTER_ENABLED": lambda: _get_bool("LLM_ROUTER_ENABLED", True),
    "LLM_ROUTER_EPSILON": lambda: float(os.getenv("LLM_ROUTER_EPSILON", "0.1")),
    "LLM_MAX_CONCURRENCY": lambda: int(os.getenv("LLM_MAX_CONCURRENCY") or "2"),
    "HISTORY_ROUNDS": lambda: int(os.getenv("HISTORY_ROUNDS", "10")),
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
    "PROACTIVE_QUEUE_MAX": lambda: int(os.getenv("PROACTIVE_QUEUE_MAX") or "4"),
    "AGENT_AVOID_MAIN_LLM": lambda: _get_bool("AGENT_AVOID_MAIN_LLM", True),
    "AGENT_HISTORY_SNAPSHOT": lambda: int(os.getenv("AGENT_HISTORY_SNAPSHOT") or "6"),
    "AGENT_DUP_THRESHOLD": lambda: float(os.getenv("AGENT_DUP_THRESHOLD") or "0.85"),
}

_PERSONA_LOADERS = {
    "SYSTEM_PROMPT_FILE": lambda: os.getenv("SYSTEM_PROMPT_FILE") or "",
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
    "TOOL_PLAY_SFX_ENABLED": lambda: _get_bool("TOOL_PLAY_SFX_ENABLED", True),
    "MCP_ENABLED": lambda: _get_bool("MCP_ENABLED", False),
    "MCP_CONFIG_PATH": lambda: os.getenv("MCP_CONFIG_PATH") or os.path.join(
        _PROJECT_ROOT, "src", "mcp", "mcp_config.json"),
    "SKILLS_DIR": lambda: os.getenv("SKILLS_DIR") or "src/llm/skills",
}

_EVOLUTION_LOADERS = {
    "EVOLUTION_ENABLED": lambda: _get_bool("EVOLUTION_ENABLED", True),
    "EVOLUTION_MIN_INTERVAL": lambda: int(os.getenv("EVOLUTION_MIN_INTERVAL", "600")),
    "EVOLUTION_MIN_TURNS": lambda: int(os.getenv("EVOLUTION_MIN_TURNS", "10")),
    "EVOLUTION_PERIODIC_INTERVAL": lambda: int(os.getenv("EVOLUTION_PERIODIC_INTERVAL", "1800")),
    "EVOLUTION_EVAL_ENABLED": lambda: _get_bool("EVOLUTION_EVAL_ENABLED", True),
    "EVOLUTION_EVAL_CASES": lambda: max(
        1, min(int(os.getenv("EVOLUTION_EVAL_CASES", "2")), 3)),
    "EVOLUTION_PROMPT_EVO_ENABLED": lambda: _get_bool("EVOLUTION_PROMPT_EVO_ENABLED", True),
    "EVOLUTION_PROMPT_EVO_INTERVAL": lambda: int(os.getenv("EVOLUTION_PROMPT_EVO_INTERVAL", "21600")),
}

_VOICE_LOADERS = {
    "GPTSOVITS_REF_AUDIO": lambda: os.getenv("GPTSOVITS_REF_AUDIO", ""),
    "GPTSOVITS_REF_AUDIOS": lambda: os.getenv("GPTSOVITS_REF_AUDIOS", ""),
    "GPTSOVITS_PROMPT_TEXT": lambda: os.getenv("GPTSOVITS_PROMPT_TEXT", ""),
    "GPTSOVITS_TIMEOUT": lambda: float(os.getenv("GPTSOVITS_TIMEOUT", "120")),
    "GPTSOVITS_MODELS_DIR": lambda: os.getenv("GPTSOVITS_MODELS_DIR", ""),
    "TTS_SERVER_URL": lambda: os.getenv("TTS_SERVER_URL", "http://127.0.0.1:8000"),
    "TTS_OUTPUT_DEVICE": lambda: os.getenv("TTS_OUTPUT_DEVICE", ""),
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
    "TOKEN_FILE": lambda: os.path.join(_data_root(), "vts_token.json"),
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
    "TOOL_PLAY_SFX_ENABLED",
    "EVOLUTION_ENABLED", "EVOLUTION_MIN_INTERVAL", "EVOLUTION_MIN_TURNS",
    "EVOLUTION_EVAL_ENABLED", "EVOLUTION_EVAL_CASES",
    "EVOLUTION_PROMPT_EVO_ENABLED", "EVOLUTION_PROMPT_EVO_INTERVAL",
    "LLM_SERVERS", "LLM_ROUTER_ENABLED", "LLM_ROUTER_EPSILON",
    "STT_ENABLED", "STT_MODEL",
    "STT_LOCAL_MODEL_PATH", "STT_LOCAL_MODEL_REVISION",
    "STT_SERVER_URL", "STT_API_KEY", "STT_BASE_URL",
)

# !config（统一配置热更新）：= !tools 字段 + 其余可热更新字段
_ALL_HOT_FIELDS = _TOOL_HOT_FIELDS + (
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_THINKING",
    "BUTLER_THINKING",
    "SYSTEM_PROMPT_FILE", "SYSTEM_PROMPT", "AUTHOR_NOTE",
    "KNOWLEDGE_ENABLED", "KNOWLEDGE_MAX_CHARS",
    "AGENT_ENABLED", "AGENT_MODEL", "AGENT_MAX_STEPS", "AGENT_MAX_TOKENS",
    "AGENT_WORKSPACE", "AGENT_ALLOW_SHELL",
    "PROACTIVE_ENABLED", "RESPONSE_INTERVAL_MIN", "RESPONSE_INTERVAL_MAX",
    "LLM_MAX_CONCURRENCY", "PROACTIVE_QUEUE_MAX",
    "AGENT_AVOID_MAIN_LLM", "AGENT_HISTORY_SNAPSHOT", "AGENT_DUP_THRESHOLD",
    "PROFANITY_FILTER_ENABLED", "PROFANITY_FILTER_RATE",
    "MEMORY_ENABLED", "MEMORY_LIFECYCLE_ENABLED",
    "MEMORY_LIFECYCLE_MODEL", "MEMORY_LIFECYCLE_THRESHOLD",
    "GPTSOVITS_REF_AUDIO", "GPTSOVITS_REF_AUDIOS", "GPTSOVITS_PROMPT_TEXT",
    "PET_ALWAYS_ON_TOP", "PET_WINDOW_SIZE", "PET_IDLE_MOTION",
    "PET_MODEL_PATH",
    "EMOTION_ACTOR_ENABLED",
    "BILI_ENABLED", "BILI_ROOM_ID", "BILI_ROOM_IDS",
    "BILI_SESSDATA", "BILI_SERVER_PORT",
)


@dataclass
class Config:
    """根配置：聚合所有分组子配置，旧字段名经 @property 转发保持兼容。

    字段加载发生在 __post_init__：从 _LOADERS 逐个读取 .env 填充子配置，
    再应用 config.yaml 覆盖层（环境变量优先）。reload 同样走 _load_selected，
    保证新增字段自动纳入热更新，无需再改 reload 函数。
    """
    llm: LLMConfig = field(default_factory=LLMConfig)
    butler: ButlerConfig = field(default_factory=ButlerConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)
    tool: ToolConfig = field(default_factory=ToolConfig)
    evolution: EvolutionConfig = field(default_factory=EvolutionConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    vts: VTSConfig = field(default_factory=VTSConfig)
    pet: PetConfig = field(default_factory=PetConfig)
    emotion: EmotionConfig = field(default_factory=EmotionConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    danmaku: DanmakuConfig = field(default_factory=DanmakuConfig)
    mindcraft: MindcraftConfig = field(default_factory=MindcraftConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)

    # ===== 字段加载 =====
    def _load_selected(self, fields: tuple) -> None:
        """按字段清单重新加载（loader 逐条读 .env 写入所在子配置）。"""
        for attr in fields:
            group = _FIELD_GROUP[attr]
            setattr(getattr(self, group), attr, _LOADERS[group][attr]())

    def _load_all(self) -> None:
        self._load_selected(tuple(_FIELD_GROUP))

    def __post_init__(self) -> None:
        """初始化完成后：从 .env 加载全部字段 + 应用 config.yaml 覆盖层。"""
        self._load_all()
        self._apply_yaml_overrides()

    # ===== yaml 覆盖层（环境变量 > config.yaml > 默认值） =====
    def _apply_yaml_overrides(self) -> None:
        """从 config.yaml 读取配置，覆盖「环境变量未设置」的字段。

        优先级：环境变量 > config.yaml > 默认值（与 load_dotenv 语义一致）。
        纯增量能力：文件不存在 / 解析失败时静默跳过，行为与原来完全一致。
        """
        yaml_path = self.paths.CONFIG_YAML_PATH
        if not os.path.isfile(yaml_path):
            return
        try:
            import yaml
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            try:
                from src.utils import console
                console.warn(f"[config] 读取 yaml 配置失败，忽略：{e}")
            except Exception:
                pass
            return
        for key, value in data.items():
            if value is None or key in _SKIP_YAML_FIELDS:
                continue
            if key not in _FIELD_GROUP:
                continue
            # 环境变量优先：已设置就不再被 yaml 覆盖
            if os.getenv(key) is not None:
                continue
            group_obj = getattr(self, _FIELD_GROUP[key])
            current = getattr(group_obj, key)
            if isinstance(current, bool):
                coerced = value if isinstance(value, bool) else (
                    str(value).strip().lower()
                    in ("1", "true", "yes", "y", "on"))
            elif isinstance(current, int):
                try:
                    coerced = int(value)
                except (TypeError, ValueError):
                    continue
            elif isinstance(current, float):
                try:
                    coerced = float(value)
                except (TypeError, ValueError):
                    continue
            else:
                coerced = value
            setattr(group_obj, key, coerced)

    def validate(self) -> None:
        """检查必填项；缺失时给出明确提示。"""
        if not self.LLM_API_KEY or self.LLM_API_KEY == "YOUR_API_KEY":
            raise RuntimeError(
                "未配置 LLM_API_KEY，请在 .env 中填入你的 LLM API Key\n"
                "  （旧配置 ZHIPU_API_KEY 仍兼容）。可在此获取：\n"
                "  智谱 https://open.bigmodel.cn/ | "
                "DeepSeek https://platform.deepseek.com/"
            )

    # ===== 向后兼容字段（旧 cfg.XXX 经 property 转发到子配置） =====
    @property
    def LLM_API_KEY(self) -> str:
        return self.llm.LLM_API_KEY

    @property
    def LLM_BASE_URL(self) -> str:
        return self.llm.LLM_BASE_URL

    @property
    def LLM_MODEL(self) -> str:
        return self.llm.LLM_MODEL

    @property
    def LLM_THINKING(self) -> bool:
        return self.llm.LLM_THINKING

    @property
    def LLM_SERVERS(self) -> str:
        return self.llm.LLM_SERVERS

    @property
    def LLM_ROUTER_ENABLED(self) -> bool:
        return self.llm.LLM_ROUTER_ENABLED

    @property
    def LLM_ROUTER_EPSILON(self) -> float:
        return self.llm.LLM_ROUTER_EPSILON

    @property
    def LLM_MAX_CONCURRENCY(self) -> int:
        return self.llm.LLM_MAX_CONCURRENCY

    @property
    def HISTORY_ROUNDS(self) -> int:
        return self.llm.HISTORY_ROUNDS

    @property
    def BUTLER_BASE_URL(self) -> str:
        return self.butler.BUTLER_BASE_URL

    @property
    def BUTLER_API_KEY(self) -> str:
        return self.butler.BUTLER_API_KEY

    @property
    def BUTLER_MODEL(self) -> str:
        return self.butler.BUTLER_MODEL

    @property
    def SESSION_SUMMARIZE_MODEL(self) -> str:
        return self.butler.SESSION_SUMMARIZE_MODEL

    @property
    def BUTLER_THINKING(self) -> bool:
        return self.butler.BUTLER_THINKING

    @property
    def MEMORY_ENABLED(self) -> bool:
        return self.memory.MEMORY_ENABLED

    @property
    def MEMORY_LIFECYCLE_ENABLED(self) -> bool:
        return self.memory.MEMORY_LIFECYCLE_ENABLED

    @property
    def MEMORY_LIFECYCLE_MODEL(self) -> str:
        return self.memory.MEMORY_LIFECYCLE_MODEL

    @property
    def MEMORY_LIFECYCLE_THRESHOLD(self) -> float:
        return self.memory.MEMORY_LIFECYCLE_THRESHOLD

    @property
    def KNOWLEDGE_ENABLED(self) -> bool:
        return self.knowledge.KNOWLEDGE_ENABLED

    @property
    def KNOWLEDGE_MAX_CHARS(self) -> int:
        return self.knowledge.KNOWLEDGE_MAX_CHARS

    @property
    def AGENT_ENABLED(self) -> bool:
        return self.agent.AGENT_ENABLED

    @property
    def AGENT_MODEL(self) -> str:
        return self.agent.AGENT_MODEL

    @property
    def AGENT_MAX_STEPS(self) -> int:
        return self.agent.AGENT_MAX_STEPS

    @property
    def AGENT_MAX_TOKENS(self) -> int:
        return self.agent.AGENT_MAX_TOKENS

    @property
    def AGENT_WORKSPACE(self) -> str:
        return self.agent.AGENT_WORKSPACE

    @property
    def AGENT_ALLOW_SHELL(self) -> bool:
        return self.agent.AGENT_ALLOW_SHELL

    @property
    def PROACTIVE_QUEUE_MAX(self) -> int:
        return self.agent.PROACTIVE_QUEUE_MAX

    @property
    def AGENT_AVOID_MAIN_LLM(self) -> bool:
        return self.agent.AGENT_AVOID_MAIN_LLM

    @property
    def AGENT_HISTORY_SNAPSHOT(self) -> int:
        return self.agent.AGENT_HISTORY_SNAPSHOT

    @property
    def AGENT_DUP_THRESHOLD(self) -> float:
        return self.agent.AGENT_DUP_THRESHOLD

    @property
    def SYSTEM_PROMPT_FILE(self) -> str:
        return self.persona.SYSTEM_PROMPT_FILE

    @property
    def SYSTEM_PROMPT(self) -> str:
        return self.persona.SYSTEM_PROMPT

    @property
    def AUTHOR_NOTE(self) -> str:
        return self.persona.AUTHOR_NOTE

    @property
    def PROFANITY_FILTER_ENABLED(self) -> bool:
        return self.filter.PROFANITY_FILTER_ENABLED

    @property
    def PROFANITY_FILTER_RATE(self) -> float:
        return self.filter.PROFANITY_FILTER_RATE

    @property
    def TOOLS_ENABLED(self) -> bool:
        return self.tool.TOOLS_ENABLED

    @property
    def OPENWEATHERMAP_API_KEY(self) -> str:
        return self.tool.OPENWEATHERMAP_API_KEY

    @property
    def TOOL_GET_CURRENT_TIME_ENABLED(self) -> bool:
        return self.tool.TOOL_GET_CURRENT_TIME_ENABLED

    @property
    def TOOL_GET_WEATHER_ENABLED(self) -> bool:
        return self.tool.TOOL_GET_WEATHER_ENABLED

    @property
    def TOOL_LOAD_SKILL_ENABLED(self) -> bool:
        return self.tool.TOOL_LOAD_SKILL_ENABLED

    @property
    def TOOL_LOOK_SCREEN_ENABLED(self) -> bool:
        return self.tool.TOOL_LOOK_SCREEN_ENABLED

    @property
    def TOOL_PLAY_SFX_ENABLED(self) -> bool:
        return self.tool.TOOL_PLAY_SFX_ENABLED

    @property
    def MCP_ENABLED(self) -> bool:
        return self.tool.MCP_ENABLED

    @property
    def MCP_CONFIG_PATH(self) -> str:
        return self.tool.MCP_CONFIG_PATH

    @property
    def SKILLS_DIR(self) -> str:
        return self.tool.SKILLS_DIR

    @property
    def EVOLUTION_ENABLED(self) -> bool:
        return self.evolution.EVOLUTION_ENABLED

    @property
    def EVOLUTION_MIN_INTERVAL(self) -> int:
        return self.evolution.EVOLUTION_MIN_INTERVAL

    @property
    def EVOLUTION_MIN_TURNS(self) -> int:
        return self.evolution.EVOLUTION_MIN_TURNS

    @property
    def EVOLUTION_PERIODIC_INTERVAL(self) -> int:
        return self.evolution.EVOLUTION_PERIODIC_INTERVAL

    @property
    def EVOLUTION_EVAL_ENABLED(self) -> bool:
        return self.evolution.EVOLUTION_EVAL_ENABLED

    @property
    def EVOLUTION_EVAL_CASES(self) -> int:
        return self.evolution.EVOLUTION_EVAL_CASES

    @property
    def EVOLUTION_PROMPT_EVO_ENABLED(self) -> bool:
        return self.evolution.EVOLUTION_PROMPT_EVO_ENABLED

    @property
    def EVOLUTION_PROMPT_EVO_INTERVAL(self) -> int:
        return self.evolution.EVOLUTION_PROMPT_EVO_INTERVAL

    @property
    def GPTSOVITS_REF_AUDIO(self) -> str:
        return self.voice.GPTSOVITS_REF_AUDIO

    @property
    def GPTSOVITS_REF_AUDIOS(self) -> str:
        return self.voice.GPTSOVITS_REF_AUDIOS

    @property
    def GPTSOVITS_PROMPT_TEXT(self) -> str:
        return self.voice.GPTSOVITS_PROMPT_TEXT

    @property
    def GPTSOVITS_TIMEOUT(self) -> float:
        return self.voice.GPTSOVITS_TIMEOUT

    @property
    def GPTSOVITS_MODELS_DIR(self) -> str:
        return self.voice.GPTSOVITS_MODELS_DIR

    @property
    def TTS_SERVER_URL(self) -> str:
        return self.voice.TTS_SERVER_URL

    @property
    def TTS_OUTPUT_DEVICE(self) -> str:
        return self.voice.TTS_OUTPUT_DEVICE

    @property
    def STT_ENABLED(self) -> bool:
        return self.voice.STT_ENABLED

    @property
    def STT_MODEL(self) -> str:
        return self.voice.STT_MODEL

    @property
    def STT_LOCAL_MODEL_PATH(self) -> str:
        return self.voice.STT_LOCAL_MODEL_PATH

    @property
    def STT_LOCAL_MODEL_REVISION(self) -> str:
        return self.voice.STT_LOCAL_MODEL_REVISION

    @property
    def STT_SERVER_URL(self) -> str:
        return self.voice.STT_SERVER_URL

    @property
    def STT_API_KEY(self) -> str:
        return self.voice.STT_API_KEY

    @property
    def STT_BASE_URL(self) -> str:
        return self.voice.STT_BASE_URL

    @property
    def STT_LEVEL_THRESHOLD(self) -> float:
        return self.voice.STT_LEVEL_THRESHOLD

    @property
    def STT_VAD_MODE(self) -> int:
        return self.voice.STT_VAD_MODE

    @property
    def STT_SILENCE_SECONDS(self) -> float:
        return self.voice.STT_SILENCE_SECONDS

    @property
    def STT_MAX_SECONDS(self) -> float:
        return self.voice.STT_MAX_SECONDS

    @property
    def STT_INTERRUPT_MIN_SECONDS(self) -> float:
        return self.voice.STT_INTERRUPT_MIN_SECONDS

    @property
    def MOUTH_PARAMETER(self) -> str:
        return self.voice.MOUTH_PARAMETER

    @MOUTH_PARAMETER.setter
    def MOUTH_PARAMETER(self, value: str) -> None:
        self.voice.MOUTH_PARAMETER = value

    @property
    def MOUTH_GAIN(self) -> float:
        return self.voice.MOUTH_GAIN

    @MOUTH_GAIN.setter
    def MOUTH_GAIN(self, value: float) -> None:
        self.voice.MOUTH_GAIN = value

    @property
    def VTS_PORT(self) -> int:
        return self.vts.VTS_PORT

    @property
    def VTS_PLUGIN_NAME(self) -> str:
        return self.vts.VTS_PLUGIN_NAME

    @property
    def VTS_PLUGIN_DEVELOPER(self) -> str:
        return self.vts.VTS_PLUGIN_DEVELOPER

    @property
    def MOTION_PATH(self) -> str:
        return self.vts.MOTION_PATH

    @property
    def VTS_ROOT(self) -> str:
        return self.vts.VTS_ROOT

    @property
    def VTS_IDLE_TAKEOVER(self) -> bool:
        return self.vts.VTS_IDLE_TAKEOVER

    @property
    def RUN_MODE(self) -> str:
        return self.pet.RUN_MODE

    @property
    def PET_MODEL_PATH(self) -> str:
        return self.pet.PET_MODEL_PATH

    @property
    def PET_WINDOW_SIZE(self) -> str:
        return self.pet.PET_WINDOW_SIZE

    @property
    def PET_ALWAYS_ON_TOP(self) -> bool:
        return self.pet.PET_ALWAYS_ON_TOP

    @property
    def PET_MOTION_PATH(self) -> str:
        return self.pet.PET_MOTION_PATH

    @property
    def PET_IDLE_MOTION(self) -> str:
        return self.pet.PET_IDLE_MOTION

    @property
    def EMOTION_ACTOR_ENABLED(self) -> bool:
        return self.emotion.EMOTION_ACTOR_ENABLED

    @property
    def SILICONFLOW_API_KEY(self) -> str:
        return self.emotion.SILICONFLOW_API_KEY

    @property
    def SILICONFLOW_MODEL(self) -> str:
        return self.emotion.SILICONFLOW_MODEL

    @property
    def SILICONFLOW_BASE_URL(self) -> str:
        return self.emotion.SILICONFLOW_BASE_URL

    @property
    def EMBEDDING_BASE_URL(self) -> str:
        return self.emotion.EMBEDDING_BASE_URL

    @property
    def EMBEDDING_API_KEY(self) -> str:
        return self.emotion.EMBEDDING_API_KEY

    @property
    def EMBEDDING_DIMENSIONS(self) -> Optional[int]:
        return self.emotion.EMBEDDING_DIMENSIONS

    @property
    def EMBEDDING_MODEL(self) -> str:
        return self.emotion.EMBEDDING_MODEL

    @property
    def EMOTION_MAP_FILE(self) -> str:
        return self.emotion.EMOTION_MAP_FILE

    @property
    def PROACTIVE_ENABLED(self) -> bool:
        return self.proactive.PROACTIVE_ENABLED

    @property
    def RESPONSE_INTERVAL_MIN(self) -> float:
        return self.proactive.RESPONSE_INTERVAL_MIN

    @property
    def RESPONSE_INTERVAL_MAX(self) -> float:
        return self.proactive.RESPONSE_INTERVAL_MAX

    @property
    def BILI_ENABLED(self) -> bool:
        return self.danmaku.BILI_ENABLED

    @property
    def BILI_ROOM_ID(self) -> int:
        return self.danmaku.BILI_ROOM_ID

    @property
    def BILI_ROOM_IDS(self) -> list:
        return self.danmaku.BILI_ROOM_IDS

    @property
    def BILI_SESSDATA(self) -> str:
        return self.danmaku.BILI_SESSDATA

    @property
    def BILI_SERVER_PORT(self) -> int:
        return self.danmaku.BILI_SERVER_PORT

    @property
    def MINDCRAFT_PATH(self) -> str:
        return self.mindcraft.MINDCRAFT_PATH

    @property
    def MINDCRAFT_LLM_BASE_URL(self) -> str:
        return self.mindcraft.MINDCRAFT_LLM_BASE_URL

    @property
    def MINDCRAFT_LLM_MODEL(self) -> str:
        return self.mindcraft.MINDCRAFT_LLM_MODEL

    @property
    def MINDCRAFT_BOT_NAME(self) -> str:
        return self.mindcraft.MINDCRAFT_BOT_NAME

    @property
    def MINDCRAFT_HOST(self) -> str:
        return self.mindcraft.MINDCRAFT_HOST

    @property
    def MINDCRAFT_PORT(self) -> int:
        return self.mindcraft.MINDCRAFT_PORT

    @property
    def MINDCRAFT_AUTH(self) -> str:
        return self.mindcraft.MINDCRAFT_AUTH

    @property
    def MINDCRAFT_MINDSERVER_PORT(self) -> int:
        return self.mindcraft.MINDCRAFT_MINDSERVER_PORT

    @property
    def MINDCRAFT_BRIDGE_ENABLED(self) -> bool:
        return self.mindcraft.MINDCRAFT_BRIDGE_ENABLED

    @property
    def MINDCRAFT_BOT_PERSONA(self) -> str:
        return self.mindcraft.MINDCRAFT_BOT_PERSONA

    @property
    def PROJECT_ROOT(self) -> str:
        return self.paths.PROJECT_ROOT

    @property
    def DATA_ROOT(self) -> str:
        return self.paths.DATA_ROOT

    @property
    def TOKEN_FILE(self) -> str:
        return self.paths.TOKEN_FILE

    @property
    def CONFIG_YAML_PATH(self) -> str:
        return self.paths.CONFIG_YAML_PATH


# === 单例 ===
cfg = Config()


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
