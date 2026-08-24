"""读写 .env 的工具函数：_update_env / _remove_env_key / _env_defaults。

从 control_center.py 与 launcher.py 抽出合并，两处共用：
launcher 启动时写 RUN_MODE / PET_MODEL_PATH，控制中心保存配置写全部字段。
"""

import os
import re
from typing import Dict

from ev.utils import config


def _env_key_re(key: str) -> re.Pattern:
    """匹配 .env 中 KEY 的配置行（行首、允许 = 前有对齐空格）。

    主块配置是「KEY    = value」对齐格式，旧实现用 startswith(key + "=")
    只认紧凑的「KEY=」，主块永远匹配不上 → 每次保存都追加一份重复块到
    文件末尾（历史重复块的根因）。锚定行首避免误匹配前缀相似的 key
    （如 LLM 不会命中 LLM_THINKING）。
    """
    return re.compile(rf"^\s*{re.escape(key)}\s*=")


def _format_env_value(value: str) -> str:
    """把值序列化为 .env 行内可解析的形式。

    含换行/引号/反斜杠的值必须用双引号包裹并转义，否则 python-dotenv
    会把后续每行都当成非法语句（曾因 SYSTEM_PROMPT 写入整段人设导致
    .env 被撑成 5000+ 行、启动刷几百条 could not parse statement）。
    """
    if "\n" in value or '"' in value or "\\" in value:
        escaped = (value.replace("\\", "\\\\")
                   .replace('"', '\\"')
                   .replace("\n", "\\n"))
        return f'"{escaped}"'
    return value


def _update_env(key: str, value: str, root: str = "") -> None:
    """把 .env 中 key 的值改为 value（保留注释；不存在则追加在末尾）。

    同名 key 出现多处时全部更新——主程序热更新（reload_config）按
    last-wins 读 .env，若只改第一个匹配，残留的重复 key 会让热更新
    读到旧值（曾因 .env 末尾历史重复块导致切开关不生效）。
    无生效行但存在被注释的同 key 行（`# KEY = ...`）时，原地启用该行
    （替换为生效行），避免追加重复行让用户误以为配置「没变化」。

    root 指定 .env 所在目录；留空用 config.cfg.PROJECT_ROOT。
    打包后的 UI（sys.frozen）PROJECT_ROOT 是 exe 目录，而主程序读的是
    项目根的 .env——启动主程序时必须显式传项目根，见 ProcessHandler._start。
    """
    path = os.path.join(root or config.cfg.PROJECT_ROOT, ".env")
    value = _format_env_value(value)
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines(keepends=True)
    except OSError:
        lines = []
    found = False
    key_re = _env_key_re(key)
    commented_re = re.compile(rf"^\s*#\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if key_re.match(line):
            lines[i] = f"{key}={value}\n"
            found = True
        elif not found and commented_re.match(line):
            # 被注释的同 key 行 → 原地启用（替换为生效行）
            lines[i] = f"{key}={value}\n"
            found = True
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"{key}={value}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _env_defaults() -> Dict[str, str]:
    """直播弹幕字段的代码默认值（与 src/utils/config.py 保持一致）。

    保存配置时值等于默认 → 不写入 .env（.env 只保留自定义配置，避免冗余行）。
    """
    cfg = config.cfg
    return {
        "BILI_ENABLED": "true",
        "BILI_ROOM_ID": "0",
        "BILI_SESSDATA": "",
        "BILI_SERVER_PORT": "8766",
        "STT_BASE_URL": "",  # 空 = 本地流式 ASR 服务（与 config.py 默认一致）
        "STT_MODEL": "FunAudioLLM/SenseVoiceSmall",
    }


def _remove_env_key(key: str) -> None:
    """从 .env 移除某 key 的配置行（值等于默认时清理冗余行，保留其它注释）。"""
    path = os.path.join(config.cfg.PROJECT_ROOT, ".env")
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines(keepends=True)
    except OSError:
        return
    key_re = _env_key_re(key)
    kept = [line for line in lines if not key_re.match(line)]
    if len(kept) == len(lines):
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(kept)
    except OSError:
        pass


def _update_env_skip_default(key: str, value: str, default: str) -> None:
    """写 .env 时跳过默认值：值为空或与代码默认相同 → 不写入（移除冗余行）。

    留空字段 = 使用代码回退默认（如 STT 回退共用 SiliconFlow），
    写空行既无效果又违背「.env 只保留自定义配置」。
    """
    v = (value or "").strip()
    if not v or v == (default or "").strip():
        _remove_env_key(key)
    else:
        _update_env(key, value)
