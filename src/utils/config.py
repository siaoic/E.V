"""配置加载：从 .env 读取，提供默认值。

所有模块统一通过 `from src.utils import config` 读取 `config.cfg` 中的字段。
"""

import os
import sys
from dataclasses import dataclass
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

# 加载 .env（若不存在则用环境变量 / 默认值）
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
    # 全部失败：用 gbk + 替换符兜底，保证不抛异常
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
    skill_file = (os.getenv("SYSTEM_PROMPT_FILE") or "").strip()
    if skill_file:
        base = os.path.join(_PROJECT_ROOT, skill_file)
        parts = _collect_skill_files(base)
        if parts:
            return "\n\n".join(parts)
        _safe_print(
            f"[config] [警告] SYSTEM_PROMPT_FILE 路径未找到任何文本文件，"
            f"回退到 UI 人设 / SYSTEM_PROMPT: {base}")
    # SYSTEM_PROMPT_FILE 未配置 → 自动使用控制中心 UI 人设
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
    # 兼容旧配置：.env SYSTEM_PROMPT
    prompt = os.getenv("SYSTEM_PROMPT") or ""
    if prompt.strip():
        return prompt.strip()
    return (
        "你是一名温柔可爱的虚拟主播，用自然亲切的口语与观众交流，回答简洁生动。"
    )


def _get_bool(key: str, default: bool) -> bool:
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


@dataclass
class Config:
    # ===== LLM（OpenAI 兼容接口） =====
    LLM_API_KEY: str = os.getenv(
        "LLM_API_KEY", "") or os.getenv("ZHIPU_API_KEY", "")
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "") or os.getenv(
        "ZHIPU_MODEL", "glm-4.7-flash")
    LLM_THINKING: bool = _get_bool(
        "LLM_THINKING", _get_bool("THINKING_ENABLED", True))

    # ===== VTubeStudio =====
    VTS_PORT: int = int(os.getenv("VTS_PORT", "8001"))
    VTS_PLUGIN_NAME: str = os.getenv("VTS_PLUGIN_NAME") or "ZhipuAI_VTuber"
    VTS_PLUGIN_DEVELOPER: str = os.getenv(
        "VTS_PLUGIN_DEVELOPER", "LocalUser")
    MOTION_PATH: str = os.getenv("MOTION_PATH", "")

    # ===== 口型同步 =====
    MOUTH_PARAMETER: str = os.getenv("MOUTH_PARAMETER", "")
    MOUTH_GAIN: float = float(os.getenv("MOUTH_GAIN", "0.4"))

    # ===== GPT-SoVITS TTS =====
    GPTSOVITS_URL: str = os.getenv(
        "GPTSOVITS_URL", "http://127.0.0.1:9880")
    GPTSOVITS_REF_AUDIO: str = os.getenv("GPTSOVITS_REF_AUDIO", "")
    GPTSOVITS_PROMPT_TEXT: str = os.getenv(
        "GPTSOVITS_PROMPT_TEXT", "")
    GPTSOVITS_TIMEOUT: float = float(os.getenv("GPTSOVITS_TIMEOUT", "120"))

    # ===== 内容过滤 =====
    PROFANITY_FILTER_ENABLED: bool = _get_bool(
        "PROFANITY_FILTER_ENABLED", True)
    PROFANITY_FILTER_RATE: float = float(
        os.getenv("PROFANITY_FILTER_RATE", "0.7"))

    # ===== 记忆系统（实验性） =====
    MEMORY_ENABLED: bool = _get_bool("MEMORY_ENABLED", True)

    # ===== ButlerAgent 管家模型（参照 Muika，OpenAI 兼容接口） =====
    # 严格参照 Muika 的 butler_model / session_summarize_model 语义：
    #   - BUTLER_BASE_URL / BUTLER_API_KEY 留空 → 与主对话共用服务
    #   - BUTLER_MODEL 留空 → 与主对话共享 LLM_MODEL（default 配置）
    #   - SESSION_SUMMARIZE_MODEL 留空 → 使用管家模型
    # 可把管家指向任意 OpenAI 协议服务（OpenAI 官方 / DeepSeek / 本地 vLLM 等）。
    BUTLER_BASE_URL: str = os.getenv("BUTLER_BASE_URL") or ""
    BUTLER_API_KEY: str = os.getenv("BUTLER_API_KEY") or ""
    BUTLER_MODEL: str = os.getenv("BUTLER_MODEL") or ""
    SESSION_SUMMARIZE_MODEL: str = os.getenv("SESSION_SUMMARIZE_MODEL") or ""
    # 管家模型是否开启思考模式（默认关：蒸馏/提取要的是结构化 JSON，
    # 思考模式会把回复全塞进 reasoning_content 导致 content 为空）
    BUTLER_THINKING: bool = _get_bool("BUTLER_THINKING", False)

    # ===== 人设 =====
    # SYSTEM_PROMPT_FILE 非空时，直接加载整个 skill 文件夹（递归收集 .md/.txt）
    # 作为人设；否则回退到 .env 的 SYSTEM_PROMPT 文本。
    SYSTEM_PROMPT_FILE: str = os.getenv("SYSTEM_PROMPT_FILE") or ""
    SYSTEM_PROMPT: str = _load_system_prompt()

    # ===== Function Calling 工具 =====
    # 搜索 / 天气 key 留空时对应工具自动隐藏（对标 live-2d(2) web-search 插件）
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY") or ""
    OPENWEATHERMAP_API_KEY: str = os.getenv("OPENWEATHERMAP_API_KEY") or ""
    # 工具总开关（设置页「启动工具」勾选框，.env TOOLS_ENABLED）：
    # 关闭后本地工具与 MCP 服务器全部停用（纯对话模式），再逐个开关才生效。
    TOOLS_ENABLED: bool = _get_bool("TOOLS_ENABLED", True)
    # 本地工具启用开关（控制中心「工具屋」勾选，写入 .env 后发 !tools 命令
    # 即可热生效，无需重启；默认全部启用）。web_search/get_weather 还需要
    # 对应 API Key 才能真正可用（没 key 时即使开关打开也会自动隐藏）。
    TOOL_WEB_SEARCH_ENABLED: bool = _get_bool("TOOL_WEB_SEARCH_ENABLED", True)
    TOOL_GET_CURRENT_TIME_ENABLED: bool = _get_bool(
        "TOOL_GET_CURRENT_TIME_ENABLED", True)
    TOOL_GET_WEATHER_ENABLED: bool = _get_bool(
        "TOOL_GET_WEATHER_ENABLED", True)
    TOOL_LOAD_SKILL_ENABLED: bool = _get_bool("TOOL_LOAD_SKILL_ENABLED", True)

    # ===== MCP（外部工具服务器） =====
    # 对标 live-2d(2)：MCP 服务器配置从外部 JSON 读取，tools 文件夹自动同步。
    # 配置统一放 configs/（mcp_config.json + configs/tools/ 自动同步工具）。
    MCP_ENABLED: bool = _get_bool("MCP_ENABLED", False)
    MCP_CONFIG_PATH: str = os.getenv("MCP_CONFIG_PATH") or os.path.join(
        _PROJECT_ROOT, "configs", "mcp_config.json"
    )

    # ===== 运行参数 =====
    HISTORY_ROUNDS: int = int(os.getenv("HISTORY_ROUNDS", "10"))

    # ===== 主动对话（参照 Muika-After-Story 的 Scheduler/loop 主动机制） =====
    # 事件驱动心跳：互动/弹幕回复结束时检查；孤独/无聊状态随空闲时间累积，
    # 突破阈值时 AI 主动开口。
    # 注意：.env 中字段被清空时 os.getenv 返回空串而非默认值，一律用 `or` 兜底。
    PROACTIVE_ENABLED: bool = _get_bool("PROACTIVE_ENABLED", True)
    # 用户发言后至少安静多久才考虑主动开口（秒）
    PROACTIVE_MIN_IDLE_SECONDS: float = float(os.getenv("PROACTIVE_MIN_IDLE_SECONDS") or "60")
    # 两次主动发言之间的最小间隔（秒，对标 PROACTIVE_COOLDOWN）
    PROACTIVE_COOLDOWN_SECONDS: float = float(os.getenv("PROACTIVE_COOLDOWN_SECONDS") or "600")
    # 孤独感 / 无聊感从 0 涨满所需小时数——决定主动开口的频率
    # （孤独阈值 0.8 / 无聊阈值 0.6 固定，对标 Muika constants）
    PROACTIVE_LONELINESS_HOURS: float = float(os.getenv("PROACTIVE_LONELINESS_HOURS") or "2")
    PROACTIVE_BOREDOM_HOURS: float = float(os.getenv("PROACTIVE_BOREDOM_HOURS") or "1")
    # ===== 主动对话：随机+事件混合触发（不用「定时回复」） =====
    # 事件驱动（互动结束立即心跳）+ 随机唤醒点：静默期按概率随机开口，
    # 说话时机不可预测；孤独/无聊阈值仍作为长时间不开口的兜底。
    # PROACTIVE_RANDOM_CHANCE：每个随机唤醒点开口的概率（0~1）
    # PROACTIVE_RANDOM_MAX_WAIT：随机唤醒点距现在的最大秒数（实际为 0~MAX 均匀随机）
    PROACTIVE_RANDOM_ENABLED: bool = _get_bool("PROACTIVE_RANDOM_ENABLED", True)
    PROACTIVE_RANDOM_CHANCE: float = float(os.getenv("PROACTIVE_RANDOM_CHANCE") or "0.25")
    PROACTIVE_RANDOM_MAX_WAIT: float = float(os.getenv("PROACTIVE_RANDOM_MAX_WAIT") or "180")

    # ===== 技能系统（严格参照 Muika plugin/skills.py） =====
    # 技能根目录（相对项目根，逗号分隔可配多个）。每个目录下按
    # <技能名>/SKILL.md 组织；frontmatter 的 name/description 注入系统提示，
    # 全文由 load_skill 工具按需加载；watchdog 监听目录支持热重载。
    SKILLS_DIR: str = os.getenv("SKILLS_DIR") or "src/llm/skills"

    # ===== 运行模式 =====
    # vtuber = VTubeStudio 虚拟主播（默认）；pet = 本地桌面宠物
    # （live2d-py + PySide6 透明窗口，无需打开 VTubeStudio）
    RUN_MODE: str = (os.getenv("RUN_MODE") or "vtuber").strip().lower()

    # ===== 桌宠模式（RUN_MODE=pet） =====
    # Live2D 模型路径（.model3.json，Cubism 3.0+；相对项目根或绝对路径）。
    # 默认从 live2d 文件夹自动探测实际存在的模型（取第一个）。
    PET_MODEL_PATH: str = os.getenv("PET_MODEL_PATH") or _default_pet_model()
    # 窗口尺寸（"宽x高"；留空 = 桌宠自动自适应主屏，不写死）
    PET_WINDOW_SIZE: str = os.getenv("PET_WINDOW_SIZE") or ""
    # 窗口始终置顶
    PET_ALWAYS_ON_TOP: bool = _get_bool("PET_ALWAYS_ON_TOP", True)
    # 基线动作文件（仅显式配置 PET_MOTION_PATH 时才加载；不回退 VTS 的
    # MOTION_PATH——其动效参数（Param243 等）与桌宠模型参数不匹配，
    # 循环驱动会让头部/呼吸/物理参数大幅振荡，表现为模型「抽搐」）
    PET_MOTION_PATH: str = os.getenv("PET_MOTION_PATH") or ""
    # 默认待机动作（控制中心「动作绑定区域」可配置）：模型无 Idle 动作组时
    # 循环播放该动作（文件名，去扩展名）；留空 = 智能匹配文件名含「待机」/idle/loop
    PET_IDLE_MOTION: str = os.getenv("PET_IDLE_MOTION") or ""

    # ===== 表情/动作（embedding 自动控制，仅桌宠模式生效） =====
    # 开启后：用户消息 → SiliconFlow Embedding 语义分类情绪 → 按映射表
    # （data/emotion_map.json，可在控制中心「表情与动作」页配置）播放表情/动作。
    EMOTION_ACTOR_ENABLED: bool = _get_bool("EMOTION_ACTOR_ENABLED", False)
    # SiliconFlow Embedding API（https://api.siliconflow.cn）
    SILICONFLOW_API_KEY: str = os.getenv("SILICONFLOW_API_KEY") or ""
    SILICONFLOW_MODEL: str = os.getenv("SILICONFLOW_MODEL") or "Qwen/Qwen3-Embedding-0.6B"
    SILICONFLOW_BASE_URL: str = os.getenv("SILICONFLOW_BASE_URL") or "https://api.siliconflow.cn/v1"
    # 嵌入模型（记忆检索 + 情绪语料分类）：默认走 SiliconFlow 云端；
    # 改本地 llama.cpp 时设 EMBEDDING_BASE_URL=http://127.0.0.1:8080/v1
    # （llama-server 需以 --embeddings 启动，本地嵌入无需 API Key）。
    EMBEDDING_BASE_URL: str = os.getenv("EMBEDDING_BASE_URL") or (
        os.getenv("SILICONFLOW_BASE_URL") or "https://api.siliconflow.cn/v1")
    EMBEDDING_API_KEY: str = (os.getenv("EMBEDDING_API_KEY")
                              or os.getenv("SILICONFLOW_API_KEY") or "")
    # 嵌入输出固定维度（如智谱 embedding-3 默认 2048，设 1024 与本地 Qwen3 库对齐；
    # 留空 = 服务端默认）。切换模型后若与库中向量维度不一致，检索会维度不匹配。
    EMBEDDING_DIMENSIONS: Optional[int] = _get_optional_int("EMBEDDING_DIMENSIONS")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL") or (
        os.getenv("SILICONFLOW_MODEL") or "Qwen/Qwen3-Embedding-0.6B")
    # 情绪 → 表情/动作映射文件（JSON，控制中心可配置；留空则只用默认）
    EMOTION_MAP_FILE: str = os.getenv("EMOTION_MAP_FILE") or os.path.join(
        _PROJECT_ROOT, "data", "emotion_map.json")

    # ===== 语音识别（STT，SiliconFlow 云端转写） =====
    # 开启后主程序监听麦克风：说话 → 能量 VAD 静音分割 → 上传
    # /v1/audio/transcriptions（SenseVoice）→ 识别文本作为用户输入
    # （与键盘输入并存，谁先到谁生效）。复用 SILICONFLOW_API_KEY。
    STT_ENABLED: bool = _get_bool("STT_ENABLED", False)
    STT_MODEL: str = os.getenv("STT_MODEL") or "FunAudioLLM/SenseVoiceSmall"
    # 语音识别独立 API Key（SiliconFlow）：留空回退复用 SILICONFLOW_API_KEY
    STT_API_KEY: str = os.getenv("STT_API_KEY") or ""
    # 语音识别服务地址：留空回退共用 SiliconFlow（云端 SenseVoice 转写）
    STT_BASE_URL: str = os.getenv("STT_BASE_URL") or (
        os.getenv("SILICONFLOW_BASE_URL") or "https://api.siliconflow.cn/v1")
    # 录音参数：STT_LEVEL_THRESHOLD 能量阈值（RMS）越高越不易被环境音误触发；
    # STT_SILENCE_SECONDS 静音持续多久切段上传；STT_MAX_SECONDS 单段最长
    # 录音时长（说话不停顿也强制切段）。
    STT_LEVEL_THRESHOLD: float = float(os.getenv("STT_LEVEL_THRESHOLD") or "500")
    STT_SILENCE_SECONDS: float = float(os.getenv("STT_SILENCE_SECONDS") or "1.0")
    STT_MAX_SECONDS: float = float(os.getenv("STT_MAX_SECONDS") or "10")

    # ===== B 站直播弹幕（blivedm → SSE 弹幕气泡网页） =====
    # BILI_ENABLED：弹幕服务总开关（false 时弹幕启动.bat 不再启动服务）
    # BILI_ROOM_ID：直播间房间号（必填；0 时服务启动但弹幕不连接）
    # BILI_SESSDATA：建议填上；不填也可连接，但收到弹幕的用户名会打码
    # BILI_SERVER_PORT：弹幕气泡网页端口（默认 8766，与字幕 8765 区分）
    BILI_ENABLED: bool = _get_bool("BILI_ENABLED", True)
    BILI_ROOM_ID: int = int(os.getenv("BILI_ROOM_ID") or "0")
    BILI_SESSDATA: str = os.getenv("BILI_SESSDATA") or ""
    BILI_SERVER_PORT: int = int(os.getenv("BILI_SERVER_PORT") or "8766")

    # 派生路径
    PROJECT_ROOT: str = _PROJECT_ROOT
    TOKEN_FILE: str = os.path.join(
        _PROJECT_ROOT, "data", "vts_token.json")

    def validate(self) -> None:
        """检查必填项；缺失时给出明确提示。"""
        if not self.LLM_API_KEY or self.LLM_API_KEY == "YOUR_API_KEY":
            raise RuntimeError(
                "未配置 LLM_API_KEY，请在 .env 中填入你的 LLM API Key\n"
                "  （旧配置 ZHIPU_API_KEY 仍兼容）。可在此获取：\n"
                "  智谱 https://open.bigmodel.cn/ | "
                "DeepSeek https://platform.deepseek.com/"
            )


cfg = Config()


def reload_tool_runtime() -> None:
    """重新读取 .env 中与工具启用相关的字段，刷新 cfg 单例。

    控制中心「工具屋」勾选工具后写 .env，再向主程序 stdin 发 `!tools`
    命令；主进程调用本函数即可让工具开关 / API Key / MCP_ENABLED 在
    下一轮对话生效（llm_brain._get_tools 每轮实时读取 cfg，无需重启）。
    """
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)
    cfg.TOOLS_ENABLED = _get_bool("TOOLS_ENABLED", True)
    cfg.TAVILY_API_KEY = os.getenv("TAVILY_API_KEY") or ""
    cfg.OPENWEATHERMAP_API_KEY = os.getenv("OPENWEATHERMAP_API_KEY") or ""
    cfg.MCP_ENABLED = _get_bool("MCP_ENABLED", False)
    cfg.TOOL_WEB_SEARCH_ENABLED = _get_bool("TOOL_WEB_SEARCH_ENABLED", True)
    cfg.TOOL_GET_CURRENT_TIME_ENABLED = _get_bool(
        "TOOL_GET_CURRENT_TIME_ENABLED", True)
    cfg.TOOL_GET_WEATHER_ENABLED = _get_bool("TOOL_GET_WEATHER_ENABLED", True)
    cfg.TOOL_LOAD_SKILL_ENABLED = _get_bool("TOOL_LOAD_SKILL_ENABLED", True)
    cfg.STT_ENABLED = _get_bool("STT_ENABLED", False)
    cfg.STT_MODEL = os.getenv("STT_MODEL") or "FunAudioLLM/SenseVoiceSmall"
    cfg.STT_API_KEY = os.getenv("STT_API_KEY") or ""
    cfg.STT_BASE_URL = os.getenv("STT_BASE_URL") or (
        os.getenv("SILICONFLOW_BASE_URL") or "https://api.siliconflow.cn/v1")


def reload_config() -> None:
    """重新读取 .env 全部可热更新字段，刷新 cfg 单例。

    控制中心「更新配置」保存后向主程序 stdin 发 `!config` 命令；主进程
    调用本函数即可让 LLM 配置 / 人设 / 主动对话 / 内容过滤 / 记忆 /
    桌宠窗口 / 待机动作等配置立即生效（无需重启主程序）。
    """
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)
    # 工具 / MCP / STT（复用工具屋热更新逻辑）
    reload_tool_runtime()
    # LLM
    cfg.LLM_API_KEY = os.getenv("LLM_API_KEY", "") or os.getenv(
        "ZHIPU_API_KEY", "")
    cfg.LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
    cfg.LLM_MODEL = os.getenv("LLM_MODEL", "") or os.getenv(
        "ZHIPU_MODEL", "glm-4.7-flash")
    cfg.LLM_THINKING = _get_bool(
        "LLM_THINKING", _get_bool("THINKING_ENABLED", True))
    # 管家模型思考开关（!config 热更新会重建 ButlerAgent，需同步刷新）
    cfg.BUTLER_THINKING = _get_bool("BUTLER_THINKING", False)
    # 人设（SYSTEM_PROMPT_FILE 优先，否则 UI 人设 / SYSTEM_PROMPT）
    cfg.SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE") or ""
    cfg.SYSTEM_PROMPT = _load_system_prompt()
    # 主动对话
    cfg.PROACTIVE_ENABLED = _get_bool("PROACTIVE_ENABLED", True)
    cfg.PROACTIVE_MIN_IDLE_SECONDS = float(
        os.getenv("PROACTIVE_MIN_IDLE_SECONDS") or "60")
    cfg.PROACTIVE_COOLDOWN_SECONDS = float(
        os.getenv("PROACTIVE_COOLDOWN_SECONDS") or "600")
    cfg.PROACTIVE_LONELINESS_HOURS = float(
        os.getenv("PROACTIVE_LONELINESS_HOURS") or "2")
    cfg.PROACTIVE_BOREDOM_HOURS = float(
        os.getenv("PROACTIVE_BOREDOM_HOURS") or "1")
    # 主动对话：随机+事件混合触发（随机唤醒点，随 !config 热更新生效）
    cfg.PROACTIVE_RANDOM_ENABLED = _get_bool("PROACTIVE_RANDOM_ENABLED", True)
    cfg.PROACTIVE_RANDOM_CHANCE = float(
        os.getenv("PROACTIVE_RANDOM_CHANCE") or "0.25")
    cfg.PROACTIVE_RANDOM_MAX_WAIT = float(
        os.getenv("PROACTIVE_RANDOM_MAX_WAIT") or "180")
    # 内容过滤
    cfg.PROFANITY_FILTER_ENABLED = _get_bool(
        "PROFANITY_FILTER_ENABLED", True)
    cfg.PROFANITY_FILTER_RATE = float(
        os.getenv("PROFANITY_FILTER_RATE", "0.7"))
    # 记忆
    cfg.MEMORY_ENABLED = _get_bool("MEMORY_ENABLED", True)
    # TTS 参考音频/文本
    cfg.GPTSOVITS_REF_AUDIO = os.getenv("GPTSOVITS_REF_AUDIO", "")
    cfg.GPTSOVITS_PROMPT_TEXT = os.getenv("GPTSOVITS_PROMPT_TEXT", "")
    # 桌宠窗口 / 待机动作
    cfg.PET_ALWAYS_ON_TOP = _get_bool("PET_ALWAYS_ON_TOP", True)
    cfg.PET_WINDOW_SIZE = os.getenv("PET_WINDOW_SIZE") or ""
    cfg.PET_IDLE_MOTION = os.getenv("PET_IDLE_MOTION") or ""
    cfg.PET_MODEL_PATH = os.getenv("PET_MODEL_PATH") or _default_pet_model()
    # 表情/动作（情绪控制）
    cfg.EMOTION_ACTOR_ENABLED = _get_bool("EMOTION_ACTOR_ENABLED", False)
    # 直播弹幕（BILI 字段供独立弹幕服务读取，重启弹幕启动.bat 生效）
    cfg.BILI_ENABLED = _get_bool("BILI_ENABLED", True)
    cfg.BILI_ROOM_ID = int(os.getenv("BILI_ROOM_ID") or "0")
    cfg.BILI_SESSDATA = os.getenv("BILI_SESSDATA") or ""
    cfg.BILI_SERVER_PORT = int(os.getenv("BILI_SERVER_PORT") or "8766")