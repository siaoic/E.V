"""环境加载 / helpers（拆自 src/utils/config.py §3.8）。"""

import json
import os
import sys
from typing import Optional

from dotenv import load_dotenv

# 项目根目录（本文件位于 ev/utils/config/ 下，上四级即根）：
# .env、data/、main.py 等都在根目录。
# PyInstaller 打包后（sys.frozen）：__file__ 指向临时解压目录（_MEIPASS），
# 必须用 exe 所在目录作为项目根——.env / live2d 模型 / data 都随 exe 放一起。
if getattr(sys, "frozen", False):
    _PROJECT_ROOT = os.path.dirname(sys.executable)
else:
    _PROJECT_ROOT = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
# 注意：python-dotenv 1.x 无 inline_comment_prefixes 参数（0.x/他库才有）；
# 1.x 默认剥离「非空值」后的行内注释（KEY = value  # 注释 → value），
# 但空值行（KEY =   # 注释）会把整段注释当值读入——.env 中须避免此写法
# （注释另起一行），否则会触发如 MOTION_PATH 的“动作文件不存在” WARN。


# ===== System Prompt 加载（支持直接加载整个 skill 文件夹） =====
_SKILL_EXTENSIONS = (".md", ".txt")
# 人设（统一目录 6.2）：优先 configs/personas/default/SKILL.md，
# 回退控制中心 UI 的 ui/data/system_prompt.txt（ed_prompt 持久化位置）。
_CONFIGS_PERSONA_SKILL = os.path.join(
    _PROJECT_ROOT, "configs", "personas", "default", "SKILL.md")
_UI_SYSTEM_PROMPT_FILE_LEGACY = os.path.join(
    _PROJECT_ROOT, "ui", "data", "system_prompt.txt")


def _resolve_ui_system_prompt_file() -> str:
    if os.path.isfile(_CONFIGS_PERSONA_SKILL):
        return _CONFIGS_PERSONA_SKILL
    return _UI_SYSTEM_PROMPT_FILE_LEGACY


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
        _ui_prompt_file = _resolve_ui_system_prompt_file()
        if os.path.isfile(_ui_prompt_file):
            # 与 skill 文件一致：剥离 YAML frontmatter（--- name/description ---
            # 是 skill 元数据，不是人设正文，直接发给 LLM 会污染 system prompt）
            text = _strip_frontmatter(
                _read_text_file(_ui_prompt_file)).strip()
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


def _parse_aux_models() -> dict:
    """辅助 LLM 任务→模型路由表：env AUX_MODELS 为 JSON 对象字符串。

    如 AUX_MODELS={"review": "glm-4-flash"}；解析失败/非对象回空表（默认
    全部走主模型，行为不变）。
    """
    raw = os.getenv("AUX_MODELS")
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


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
        _data_root(), "vts",
        "emotion_map_vts.json" if mode == "vtuber" else "emotion_map.json")


# yaml 覆盖层不处理的字段（派生路径 / 配置文件自身路径）
_SKIP_YAML_FIELDS = {"PROJECT_ROOT", "TOKEN_FILE", "CONFIG_YAML_PATH"}


__all__ = [
    "_PROJECT_ROOT",
    "_SKILL_EXTENSIONS",
    "_CONFIGS_PERSONA_SKILL",
    "_UI_SYSTEM_PROMPT_FILE_LEGACY",
    "_resolve_ui_system_prompt_file",
    "_strip_frontmatter",
    "_read_text_file",
    "_collect_skill_files",
    "_safe_print",
    "_load_system_prompt",
    "_get_bool",
    "_get_optional_int",
    "_parse_aux_models",
    "_get_room_ids",
    "_default_pet_model",
    "_data_root",
    "_default_emotion_map_file",
    "_SKIP_YAML_FIELDS",
]
