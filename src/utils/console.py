"""终端日志美化：Linux 终端风格（彩色分级输出，Windows 自动启用 ANSI 转义）。

设计：
- 状态标签使用系统启动风格的 [ OK ] / [INFO] / [WARN] / [FAIL]
- 分区标题用青色 ━ 横幅；键值行青色标签 + 右对齐
- 低优先级信息用灰色（dim）；指令触发用亮青色强调（accent）

用法：
    import console
    console.header("E.V")
    console.ok("已连接并认证成功")
    console.info("正在扫描当前模型，自动适配（约数秒）...")
    console.kv("模型", "glm-4.7-flash")
    console.warn("口型参数未配置，跳过口型注入")
    console.error("认证失败：xxx")
    console.accent("▶ [情绪:开心]")
    console.dim("提示 / 次要信息")
"""

import os
import unicodedata

# ---- ANSI 颜色 ----
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
GRAY = "\033[90m"
BRIGHT_GREEN = "\033[92m"
BRIGHT_YELLOW = "\033[93m"
BRIGHT_MAGENTA = "\033[95m"
BRIGHT_CYAN = "\033[96m"

# 控制中心左右分栏标记：对话类内容（弹幕 / LLM 发言 / Agent 回复 / 用户输入）
# 用零宽连接符包裹。普通终端里零宽字符不可见（输出效果等同 print）；
# 控制中心捕获 stdout 后凭此标记把内容路由到左侧「对话」面板并剥离。
CHAT_TAG = "\u2060"


def _display_width(text: str) -> int:
    """估算终端显示宽度：东亚宽字符（中文/全角符号）按 2 列，其余按 1 列。

    用于横幅标题居中计算——标题里的中文字符占 2 列，英文占 1 列。
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1
               for ch in text)


def paint(text: str, code: str) -> str:
    """给文本上色（含 ANSI 转义）。"""
    return f"{code}{text}{RESET}"


def chat(text: str = "", end: str = "\n", flush: bool = False) -> None:
    """输出「对话」类内容（弹幕 / LLM 发言 / Agent 回复 / 用户输入）。

    内容与 end 一起被零宽标记包裹：控制中心据此路由到左侧「对话」面板
    并剥离标记；直接在终端运行时零宽字符不可见，输出效果与 print 一致。
    """
    print(f"{CHAT_TAG}{text}{end}{CHAT_TAG}", end="", flush=flush)


# Windows 控制台 VT 转义相关常量
_STD_OUTPUT_HANDLE = -11
_ENABLE_VT_PROCESSING = 7  # ENABLE_PROCESSED_OUTPUT|WRAP_AT_EOL_OUTPUT|VT_PROCESSING


def _init_windows_ansi() -> None:
    """Windows 10+ 启用 VT 转义（cmd / PowerShell 均能显示彩色）。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(_STD_OUTPUT_HANDLE),
                                _ENABLE_VT_PROCESSING)
    except Exception:
        pass


_init_windows_ansi()


# ---- 分级日志（systemd 风格状态标签） ----
def _print_status(label: str, color: str, msg: str) -> None:
    """统一的状态行输出：[标签] 消息（标签上色）。"""
    print(f"{paint(label, color)} {msg}")


def ok(msg: str) -> None:
    """成功：[ OK ] 绿色。"""
    _print_status("[ OK ]", BRIGHT_GREEN, msg)


def info(msg: str) -> None:
    """进行中：[INFO] 青色。"""
    _print_status("[INFO]", CYAN, msg)


def warn(msg: str) -> None:
    """警告：[WARN] 黄色。"""
    _print_status("[WARN]", BRIGHT_YELLOW, msg)


def error(msg: str) -> None:
    """错误：[FAIL] 红色。"""
    _print_status("[FAIL]", RED, msg)


def dim(msg: str) -> None:
    """次要/提示信息：灰色。"""
    print(paint(msg, GRAY))


def accent(msg: str, end: str = "\n") -> None:
    """强调信息：亮青色（如指令触发日志）。"""
    print(paint(msg, BRIGHT_CYAN), end=end, flush=True)


def header(title: str, width: int = 56) -> None:
    """分区标题横幅：亮青色加粗 ━ 上下边框。

    按显示宽度居中：━ 在 Windows 终端 / Qt 等宽文本框里为 1 列宽，
    故 bar 宽度 = width（字符数）；标题的显示宽度按 east_asian_width
    正确计算（中文 2 列、英文 1 列）。只做左侧填充，不追加尾部空格。
    """
    bar = "━" * width
    left = max(0, (width - _display_width(title)) // 2)
    print(f"\n{paint(bar, BRIGHT_CYAN)}")
    print(paint(" " * left + title, BOLD + BRIGHT_CYAN))
    print(paint(bar, BRIGHT_CYAN))


def kv(label: str, value: str, label_width: int = 12) -> None:
    """键值行：青色标签 + 值（左对齐缩进两格）。"""
    print(f"  {paint(label.ljust(label_width), CYAN)}{value}")


def progress(text: str) -> None:
    """探测/扫描过程行：灰色打点前缀。"""
    print(f"  {paint('·', CYAN)} {text}")
