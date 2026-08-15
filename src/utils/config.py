"""配置加载：从 .env 读取，提供默认值。

所有模块统一通过 `from src.utils import config` 读取 `config.cfg` 中的字段。
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
    skill_file = (os.getenv("SYSTEM_PROMPT_FILE") or "").strip()
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


# yaml 覆盖层不处理的字段（派生路径 / 配置文件自身路径）
_SKIP_YAML_FIELDS = {"PROJECT_ROOT", "TOKEN_FILE", "CONFIG_YAML_PATH"}


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
    # VTube Studio 安装根目录（待机动画接管定位模型文件用；
    # 留空自动从 Steam 注册表定位，找不到则跳过接管）
    VTS_ROOT: str = os.getenv("VTS_ROOT", "").strip()
    # 待机动画接管（RUN_MODE=vtuber 且 MOTION_PATH 为空时生效）：
    # 把模型在 VTS 里配置的待机动画交给插件注入路径播放（P2 优先级高于
    # VTS 内置 P1 待机动画），由 MotionPlayer 平滑混合循环点，
    # 消除待机动画尾帧→首帧硬跳（官方 API 无淡入淡出接口可修）。
    VTS_IDLE_TAKEOVER: bool = _get_bool("VTS_IDLE_TAKEOVER", True)

    # ===== 口型同步 =====
    MOUTH_PARAMETER: str = os.getenv("MOUTH_PARAMETER", "")
    MOUTH_GAIN: float = float(os.getenv("MOUTH_GAIN", "0.4"))

    # ===== GPT-SoVITS TTS =====
    GPTSOVITS_URL: str = os.getenv(
        "GPTSOVITS_URL", "http://127.0.0.1:9880")
    GPTSOVITS_REF_AUDIO: str = os.getenv("GPTSOVITS_REF_AUDIO", "")
    # 辅助参考音频（多条以 | 分隔，与主参考混合出说话人音色；可空）
    GPTSOVITS_REF_AUDIOS: str = os.getenv("GPTSOVITS_REF_AUDIOS", "")
    GPTSOVITS_PROMPT_TEXT: str = os.getenv(
        "GPTSOVITS_PROMPT_TEXT", "")
    GPTSOVITS_TIMEOUT: float = float(os.getenv("GPTSOVITS_TIMEOUT", "120"))
    # 本地 TTS 模型目录（GSV-TTS-Lite 的 models_dir；留空 = tools/gsv_tts/API/models）
    GPTSOVITS_MODELS_DIR: str = os.getenv("GPTSOVITS_MODELS_DIR", "")
    # 外部 TTS 合成服务地址（tts.bat 启动的 fastapi_server_example.py，
    # 监听 0.0.0.0:8000；留空 = http://127.0.0.1:8000）
    TTS_SERVER_URL: str = os.getenv(
        "TTS_SERVER_URL", "http://127.0.0.1:8000")

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
    # SYSTEM_PROMPT_FILE 非空时加载整个 skill 文件夹作为人设；
    # 否则回退 UI 人设 / .env SYSTEM_PROMPT（详见 _load_system_prompt）。
    SYSTEM_PROMPT_FILE: str = os.getenv("SYSTEM_PROMPT_FILE") or ""
    SYSTEM_PROMPT: str = _load_system_prompt()

    # ===== Function Calling 工具 =====
    # 联网搜索走 bing-cn-mcp（MCP 服务器，见 src/mcp/mcp_config.json），无需 key
    OPENWEATHERMAP_API_KEY: str = os.getenv("OPENWEATHERMAP_API_KEY") or ""
    # 工具总开关：关闭后本地工具与 MCP 全部停用（纯对话模式）
    TOOLS_ENABLED: bool = _get_bool("TOOLS_ENABLED", True)
    # 各工具开关供控制中心「工具屋」勾选（写 .env 后发 !tools 热生效）；
    # get_weather 还需 API Key，没 key 时开关打开也自动隐藏。
    TOOL_GET_CURRENT_TIME_ENABLED: bool = _get_bool(
        "TOOL_GET_CURRENT_TIME_ENABLED", True)
    TOOL_GET_WEATHER_ENABLED: bool = _get_bool(
        "TOOL_GET_WEATHER_ENABLED", True)
    TOOL_LOAD_SKILL_ENABLED: bool = _get_bool("TOOL_LOAD_SKILL_ENABLED", True)
    TOOL_LOOK_SCREEN_ENABLED: bool = _get_bool("TOOL_LOOK_SCREEN_ENABLED", True)
    TOOL_PLAY_SFX_ENABLED: bool = _get_bool("TOOL_PLAY_SFX_ENABLED", True)

    # ===== 自我进化（对话后后台复盘：技能沉淀/话题进化/行为反思/话术建议） =====
    EVOLUTION_ENABLED: bool = _get_bool("EVOLUTION_ENABLED", True)
    # 复盘最小间隔（秒）与最小新增对话轮次：达标才调用 LLM 复盘，控制成本。
    # 轮数阈值对标 hermes memory.nudge_interval（默认每 10 轮触发一次记忆复盘）
    EVOLUTION_MIN_INTERVAL: int = int(os.getenv("EVOLUTION_MIN_INTERVAL", "600"))
    EVOLUTION_MIN_TURNS: int = int(os.getenv("EVOLUTION_MIN_TURNS", "10"))
    # 定期自我提示：后台每 EVOLUTION_PERIODIC_INTERVAL 秒检查一次，空闲期
    # 主动补复盘（对标 hermes 定期自我评估）。仅当距上次复盘已达标且存在
    # 未复盘的新对话轮次时才调用 LLM，不重复消费 token。
    EVOLUTION_PERIODIC_INTERVAL: int = int(os.getenv("EVOLUTION_PERIODIC_INTERVAL", "1800"))
    # 技能评估闭环：技能沉淀/修补时生成测试集执行打分（fail-open，
    # 评估失败不阻塞落盘；修补时新版更差自动回滚）。EVOLUTION_EVAL_CASES
    # 为每次评估的测试用例数（钳制 1~3，越多越准但越耗 token）。
    EVOLUTION_EVAL_ENABLED: bool = _get_bool("EVOLUTION_EVAL_ENABLED", True)
    EVOLUTION_EVAL_CASES: int = max(
        1, min(int(os.getenv("EVOLUTION_EVAL_CASES", "2")), 3))
    # GEPA 系统提示词进化（对标 hermes 的 GEPA：变异 → 评审择优 → 注入）：
    # 分析对话失败点变异候选行为策略段，与当前策略同批评审，择优落盘
    # evolution_policy.json 由 llm_brain 注入系统提示。独立节流（默认 6 小时
    # 一次），不消耗复盘节奏。
    EVOLUTION_PROMPT_EVO_ENABLED: bool = _get_bool(
        "EVOLUTION_PROMPT_EVO_ENABLED", True)
    EVOLUTION_PROMPT_EVO_INTERVAL: int = int(
        os.getenv("EVOLUTION_PROMPT_EVO_INTERVAL", "21600"))

    # ===== 模型路由进化（多臂老虎机 UCB1） =====
    # LLM_SERVERS：逗号分隔的多服务列表，每项「名称;base_url;api_key;model」。
    # 配置 2 个及以上才启用路由（按历史成功率自动择优，越用越准）；
    # 未配置时主对话完全走原有单一 LLM 服务逻辑，行为不变。
    # LLM_ROUTER_EPSILON：探索率（0~1），小概率随机尝试非最优服务防局部最优。
    LLM_SERVERS: str = os.getenv("LLM_SERVERS") or ""
    LLM_ROUTER_ENABLED: bool = _get_bool("LLM_ROUTER_ENABLED", True)
    LLM_ROUTER_EPSILON: float = float(os.getenv("LLM_ROUTER_EPSILON", "0.1"))

    # ===== 屏幕视觉（look_at_screen 工具用） =====
    # 截图交给多模态模型描述画面。优先用主模型（LLM_*）；主模型不支持图片
    # 输入时回退 BUTLER_MODEL（需多模态 VLM，默认 glm-4v-flash）。

    # ===== MCP（外部工具服务器） =====
    # 对标 live-2d(2)：MCP 服务器配置从外部 JSON 读取，tools 文件夹自动同步。
    # 配置随 mcp 模块（src/mcp/mcp_config.json），可在 .env 用 MCP_CONFIG_PATH 覆盖。
    MCP_ENABLED: bool = _get_bool("MCP_ENABLED", False)
    MCP_CONFIG_PATH: str = os.getenv("MCP_CONFIG_PATH") or os.path.join(
        _PROJECT_ROOT, "src", "mcp", "mcp_config.json"
    )

    # ===== 外部服务（插件页进程托管：mindcraft） =====
    # mindcraft = LLM 驱动的 Minecraft bot（plugins/mindcraft，Node + Mineflayer）。
    # 复用本项目 LLM（LLM_BASE_URL / LLM_MODEL / LLM_API_KEY）作为 bot 大脑，
    # 插件页负责进程启停；未 clone / 未 npm install 时卡片提示未安装。
    MINDCRAFT_PATH: str = os.getenv("MINDCRAFT_PATH") or os.path.join(
        _PROJECT_ROOT, "plugins", "mindcraft")
    MINDCRAFT_LLM_BASE_URL: str = os.getenv(
        "MINDCRAFT_LLM_BASE_URL") or os.getenv("LLM_BASE_URL", "")
    MINDCRAFT_LLM_MODEL: str = os.getenv(
        "MINDCRAFT_LLM_MODEL") or os.getenv(
        "LLM_MODEL", "") or "glm-4-flash-250414"
    MINDCRAFT_BOT_NAME: str = os.getenv("MINDCRAFT_BOT_NAME") or "vtuber"
    MINDCRAFT_HOST: str = os.getenv("MINDCRAFT_HOST") or "127.0.0.1"
    MINDCRAFT_PORT: int = int(os.getenv("MINDCRAFT_PORT") or "55916")
    MINDCRAFT_AUTH: str = os.getenv("MINDCRAFT_AUTH") or "offline"
    # MindServer（socket.io）端口：Node 引擎与控制中心/Python 桥共用。
    MINDCRAFT_MINDSERVER_PORT: int = int(
        os.getenv("MINDCRAFT_MINDSERVER_PORT") or "8080")
    # 双向桥开关：true 时主播程序作为 socket.io 客户端连入 MindServer，
    # 用户输入转发给 MC bot、bot 回复由主播 TTS 朗读。
    MINDCRAFT_BRIDGE_ENABLED: bool = _get_bool(
        "MINDCRAFT_BRIDGE_ENABLED", False)
    # bot 人设（andy.json 的 conversing 提示词）；留空用内置默认。
    MINDCRAFT_BOT_PERSONA: str = os.getenv("MINDCRAFT_BOT_PERSONA") or ""

    # ===== 运行参数 =====
    HISTORY_ROUNDS: int = int(os.getenv("HISTORY_ROUNDS", "10"))

    # ===== 并发控制（LLM / 主动消息队列 / agent 避让） =====
    # 同时进行的最多 LLM 推理数（用户对话 + agent 主动 + 弹幕回复共用信号量）；
    # 本地大模型防显存打满、远程 API 防限流，超出排队等待而非无限并发。
    LLM_MAX_CONCURRENCY: int = int(os.getenv("LLM_MAX_CONCURRENCY") or "2")
    # agent 主动消息队列最大长度：超出丢弃最旧的主动消息，优先保留最新触发
    # （用户输入 / 弹幕回复不走此队列，永远不丢）。
    PROACTIVE_QUEUE_MAX: int = int(os.getenv("PROACTIVE_QUEUE_MAX") or "4")
    # 主 LLM 推理/播报期间是否抑制 agent 主动触发（体验更顺滑，代价是少部分
    # 主动搭话被推迟）
    AGENT_AVOID_MAIN_LLM: bool = _get_bool("AGENT_AVOID_MAIN_LLM", True)
    # agent 主动发言读取主会话历史的最大条数（精简上下文，降 token）
    AGENT_HISTORY_SNAPSHOT: int = int(
        os.getenv("AGENT_HISTORY_SNAPSHOT") or "6")
    # agent 输出与最近对话的相似度去重阈值（0~1，达到即丢弃不送入队列）
    AGENT_DUP_THRESHOLD: float = float(
        os.getenv("AGENT_DUP_THRESHOLD") or "0.85")

    # ===== 主动对话（LLM 自主决定，无时间门槛） =====
    # 主动发言不再由孤独/无聊累积、冷却、随机唤醒点等时间参数控制：
    # 互动结束后立即给一次机会、静默期按随机间隔给一次机会，由主模型
    # 自主判断「此刻想不想说话、想说什么」——想说就生成发言，不想说就
    # 保持沉默。
    # 注意：.env 中字段被清空时 os.getenv 返回空串而非默认值，一律用 `or` 兜底。
    PROACTIVE_ENABLED: bool = _get_bool("PROACTIVE_ENABLED", True)

    # ===== 主动开口 / 弹幕回复共用的随机间隔范围（秒） =====
    # 主动对话的开口机会间隔、弹幕回复的冷却间隔都从本范围随机取值，
    # 避免固定时间（如 60s 整点开口）显得机械规律。互动结束后会额外
    # 立即给一次主动开口机会。
    RESPONSE_INTERVAL_MIN: float = float(
        os.getenv("RESPONSE_INTERVAL_MIN") or "5")
    RESPONSE_INTERVAL_MAX: float = float(
        os.getenv("RESPONSE_INTERVAL_MAX") or "10")

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

    # ===== 表情/动作（embedding 自动控制，桌宠 / vtuber 双模式） =====
    # 开启后：用户消息 → SiliconFlow Embedding 语义分类情绪 → 按映射表
    # （按模式存 data/emotion_map.json / emotion_map_vts.json，可在控制中心
    # 「表情与动作」页配置）播放表情/动作。
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
    # 情绪 → 表情/动作映射文件（JSON，控制中心可配置；留空则用默认）。
    # 按运行模式分文件：桌宠与 VTS 模型的表情/动作名互不相同，各自维护
    # 绑定互不干扰（桌宠 emotion_map.json，vtuber emotion_map_vts.json）
    EMOTION_MAP_FILE: str = os.getenv("EMOTION_MAP_FILE") or os.path.join(
        _PROJECT_ROOT, "data",
        "emotion_map_vts.json" if RUN_MODE == "vtuber" else "emotion_map.json")

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
    # STT_INTERRUPT_MIN_SECONDS 语音打断阈值（秒）：回复播报期间识别到的
    # 语音段「说话时长」超过该值才打断当前播报；过短语音（嗯/啊/咳嗽/
    # 环境音）不打断，避免误触发。
    STT_INTERRUPT_MIN_SECONDS: float = float(
        os.getenv("STT_INTERRUPT_MIN_SECONDS") or "3")

    # ===== B 站直播弹幕（blivedm → SSE 弹幕气泡网页） =====
    # BILI_ENABLED：弹幕服务总开关（false 时弹幕启动.bat 不再启动服务）
    # BILI_ROOM_ID：直播间房间号（必填；0 时服务启动但弹幕不连接）
    # BILI_ROOM_IDS：多直播间房间号（逗号分隔，如 "123,456"）；
    #   配置后优先于 BILI_ROOM_ID，每房间独立 blivedm 连接
    # BILI_SESSDATA：建议填上；不填也可连接，但收到弹幕的用户名会打码
    # BILI_SERVER_PORT：弹幕气泡网页端口（默认 8766，与字幕 8765 区分）
    BILI_ENABLED: bool = _get_bool("BILI_ENABLED", True)
    BILI_ROOM_ID: int = int(os.getenv("BILI_ROOM_ID") or "0")
    BILI_ROOM_IDS: list = field(default_factory=_get_room_ids)
    BILI_SESSDATA: str = os.getenv("BILI_SESSDATA") or ""
    BILI_SERVER_PORT: int = int(os.getenv("BILI_SERVER_PORT") or "8766")

    # 派生路径
    PROJECT_ROOT: str = _PROJECT_ROOT
    TOKEN_FILE: str = os.path.join(
        _PROJECT_ROOT, "data", "vts_token.json")

    # ===== 配置中心（可选 yaml 覆盖层） =====
    # configs/config.yaml（或 CONFIG_YAML_PATH 指定）提供分层配置：
    # 优先级 环境变量 > config.yaml > 默认值；文件不存在时不影响现有行为。
    CONFIG_YAML_PATH: str = os.getenv("CONFIG_YAML_PATH") or os.path.join(
        _PROJECT_ROOT, "configs", "config.yaml")

    def __post_init__(self) -> None:
        """初始化完成后应用 config.yaml 覆盖层（环境变量优先）。"""
        self._apply_yaml_overrides()

    def _apply_yaml_overrides(self) -> None:
        """从 config.yaml 读取配置，覆盖「环境变量未设置」的字段。

        优先级：环境变量 > config.yaml > 默认值（与 load_dotenv 语义一致）。
        纯增量能力：文件不存在 / 解析失败时静默跳过，行为与原来完全一致。
        """
        yaml_path = self.CONFIG_YAML_PATH
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
            if key not in self.__dataclass_fields__:
                continue
            # 环境变量优先：已设置就不再被 yaml 覆盖
            if os.getenv(key) is not None:
                continue
            current = getattr(self, key)
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
            setattr(self, key, coerced)

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
    cfg.OPENWEATHERMAP_API_KEY = os.getenv("OPENWEATHERMAP_API_KEY") or ""
    cfg.MCP_ENABLED = _get_bool("MCP_ENABLED", False)
    cfg.MINDCRAFT_PATH = os.getenv("MINDCRAFT_PATH") or os.path.join(
        _PROJECT_ROOT, "plugins", "mindcraft")
    cfg.MINDCRAFT_LLM_BASE_URL = os.getenv(
        "MINDCRAFT_LLM_BASE_URL") or os.getenv("LLM_BASE_URL", "")
    cfg.MINDCRAFT_LLM_MODEL = os.getenv(
        "MINDCRAFT_LLM_MODEL") or os.getenv(
        "LLM_MODEL", "") or "glm-4-flash-250414"
    cfg.MINDCRAFT_BOT_NAME = os.getenv("MINDCRAFT_BOT_NAME") or "vtuber"
    cfg.MINDCRAFT_HOST = os.getenv("MINDCRAFT_HOST") or "127.0.0.1"
    cfg.MINDCRAFT_PORT = int(os.getenv("MINDCRAFT_PORT") or "55916")
    cfg.MINDCRAFT_AUTH = os.getenv("MINDCRAFT_AUTH") or "offline"
    cfg.MINDCRAFT_MINDSERVER_PORT = int(
        os.getenv("MINDCRAFT_MINDSERVER_PORT") or "8080")
    cfg.MINDCRAFT_BRIDGE_ENABLED = _get_bool(
        "MINDCRAFT_BRIDGE_ENABLED", False)
    cfg.MINDCRAFT_BOT_PERSONA = os.getenv("MINDCRAFT_BOT_PERSONA") or ""
    cfg.TOOL_GET_CURRENT_TIME_ENABLED = _get_bool(
        "TOOL_GET_CURRENT_TIME_ENABLED", True)
    cfg.TOOL_GET_WEATHER_ENABLED = _get_bool("TOOL_GET_WEATHER_ENABLED", True)
    cfg.TOOL_LOAD_SKILL_ENABLED = _get_bool("TOOL_LOAD_SKILL_ENABLED", True)
    cfg.TOOL_LOOK_SCREEN_ENABLED = _get_bool("TOOL_LOOK_SCREEN_ENABLED", True)
    cfg.TOOL_PLAY_SFX_ENABLED = _get_bool("TOOL_PLAY_SFX_ENABLED", True)
    cfg.EVOLUTION_ENABLED = _get_bool("EVOLUTION_ENABLED", True)
    cfg.EVOLUTION_MIN_INTERVAL = int(os.getenv("EVOLUTION_MIN_INTERVAL", "600"))
    cfg.EVOLUTION_MIN_TURNS = int(os.getenv("EVOLUTION_MIN_TURNS", "10"))
    cfg.EVOLUTION_EVAL_ENABLED = _get_bool("EVOLUTION_EVAL_ENABLED", True)
    cfg.EVOLUTION_EVAL_CASES = max(
        1, min(int(os.getenv("EVOLUTION_EVAL_CASES", "2")), 3))
    cfg.EVOLUTION_PROMPT_EVO_ENABLED = _get_bool(
        "EVOLUTION_PROMPT_EVO_ENABLED", True)
    cfg.EVOLUTION_PROMPT_EVO_INTERVAL = int(
        os.getenv("EVOLUTION_PROMPT_EVO_INTERVAL", "21600"))
    cfg.LLM_SERVERS = os.getenv("LLM_SERVERS") or ""
    cfg.LLM_ROUTER_ENABLED = _get_bool("LLM_ROUTER_ENABLED", True)
    cfg.LLM_ROUTER_EPSILON = float(os.getenv("LLM_ROUTER_EPSILON", "0.1"))
    cfg.STT_ENABLED = _get_bool("STT_ENABLED", False)
    cfg.STT_MODEL = os.getenv("STT_MODEL") or "FunAudioLLM/SenseVoiceSmall"
    cfg.STT_API_KEY = os.getenv("STT_API_KEY") or ""
    cfg.STT_BASE_URL = os.getenv("STT_BASE_URL") or (
        os.getenv("SILICONFLOW_BASE_URL") or "https://api.siliconflow.cn/v1")
    # yaml 覆盖层（环境变量未设置的字段回落到 config.yaml，保持一致）
    cfg._apply_yaml_overrides()


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
    # 管家模型思考开关（热更新会重建 ButlerAgent，需同步刷新）
    cfg.BUTLER_THINKING = _get_bool("BUTLER_THINKING", False)
    # 人设（SYSTEM_PROMPT_FILE 优先，否则 UI 人设 / SYSTEM_PROMPT）
    cfg.SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE") or ""
    cfg.SYSTEM_PROMPT = _load_system_prompt()
    # 主动对话
    cfg.PROACTIVE_ENABLED = _get_bool("PROACTIVE_ENABLED", True)
    # 主动开口 / 弹幕回复共用随机间隔范围
    cfg.RESPONSE_INTERVAL_MIN = float(
        os.getenv("RESPONSE_INTERVAL_MIN") or "5")
    cfg.RESPONSE_INTERVAL_MAX = float(
        os.getenv("RESPONSE_INTERVAL_MAX") or "10")
    # 并发控制
    cfg.LLM_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY") or "2")
    cfg.PROACTIVE_QUEUE_MAX = int(os.getenv("PROACTIVE_QUEUE_MAX") or "4")
    cfg.AGENT_AVOID_MAIN_LLM = _get_bool("AGENT_AVOID_MAIN_LLM", True)
    cfg.AGENT_HISTORY_SNAPSHOT = int(
        os.getenv("AGENT_HISTORY_SNAPSHOT") or "6")
    cfg.AGENT_DUP_THRESHOLD = float(
        os.getenv("AGENT_DUP_THRESHOLD") or "0.85")
    # 内容过滤
    cfg.PROFANITY_FILTER_ENABLED = _get_bool(
        "PROFANITY_FILTER_ENABLED", True)
    cfg.PROFANITY_FILTER_RATE = float(
        os.getenv("PROFANITY_FILTER_RATE", "0.7"))
    # 记忆
    cfg.MEMORY_ENABLED = _get_bool("MEMORY_ENABLED", True)
    # TTS 参考音频/辅助参考/文本
    cfg.GPTSOVITS_REF_AUDIO = os.getenv("GPTSOVITS_REF_AUDIO", "")
    cfg.GPTSOVITS_REF_AUDIOS = os.getenv("GPTSOVITS_REF_AUDIOS", "")
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
    cfg.BILI_ROOM_IDS = _get_room_ids()
    cfg.BILI_SESSDATA = os.getenv("BILI_SESSDATA") or ""
    cfg.BILI_SERVER_PORT = int(os.getenv("BILI_SERVER_PORT") or "8766")
    # yaml 覆盖层（环境变量未设置的字段回落到 config.yaml，保持一致）
    cfg._apply_yaml_overrides()