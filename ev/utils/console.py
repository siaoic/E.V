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

RUN_MODE=tui 分支（旁路）：
- 由 TS/Node TUI（ui/tui）spawn 启动时，stdout/stdin 走 JSON-RPC。
- 本模块所有输出函数在 IS_TUI 时改发结构化事件（一行一个 JSON），
  stdin 读取改走 read_input() 解析 JSON-RPC 请求。
- 非 TUI 模式（vtuber/pet/cli 等）行为 100% 保持原状。
"""

import json
import os
import sys
import threading
import unicodedata

# ---- 输出互斥 ----
# 所有终端输出共用一把锁：对话流式打印（打字机 end=""）、后台线程日志
# （ButlerAgent / TTS pump / 记忆检索）、主循环提示符都可能并发写 stdout，
# 不加锁时一次输出内部的多次 write 会被其他线程插入，产生「我叫深[WARN]…」
# 这类撕裂行。裸 print 的模块（perf_tracker 等）用 output_lock() 共用此锁。
_PRINT_LOCK = threading.RLock()


def output_lock() -> threading.RLock:
    """返回全局输出锁：绕过 console 直接 print 的模块应 with 它包住打印。"""
    return _PRINT_LOCK

# ---- TUI 模式旁路 ----
# RUN_MODE=tui 时，stdout 发 JSON-RPC 事件、stdin 读 JSON-RPC 请求；
# 由 ui/tui 进程 spawn 本进程时设置。模块加载时算一次，避免热路径开销。
IS_TUI = (os.getenv("RUN_MODE") or "").strip().lower() == "tui"


def _tui_emit(event: dict) -> None:
    """TUI 模式：把事件序列化为一行 JSON 输出到 stdout（TUI 端按行解析）。

    非 TUI 模式不调用本函数。强制 flush，保证 TUI 实时收到事件。
    """
    try:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        # 兜底：JSON 序列化/输出异常不能阻断业务
        pass


def _tui_log(level: str, msg: str) -> None:
    """TUI 模式日志事件。level ∈ ok/info/warn/fail/dim。"""
    _tui_emit({"type": "log", "level": level, "text": msg})


def _tui_status(**kwargs) -> None:
    """TUI 模式状态事件：模型/TPS/缓存/tokens/ctx/working。

    仅在 IS_TUI 时发事件；非 TUI 模式是 no-op，不影响原行为。
    """
    if not IS_TUI:
        return
    _tui_emit({"type": "status", **kwargs})


def report_status(**kwargs) -> None:
    """TUI 模式状态事件（公开接口）。参数：model/tps/cache/tokens_in/tokens_out/ctx_used/ctx_total/working/thinking_ms。

    仅在 IS_TUI 时发事件；非 TUI 模式是 no-op。
    """
    if not IS_TUI:
        return
    _tui_emit({"type": "status", **kwargs})


def read_input(prompt: str = "") -> str:
    """统一输入入口：TUI 模式从 stdin 读 JSON-RPC 请求，否则走 input()。

    TUI 模式协议：TUI 端写一行 JSON 到本进程 stdin，
    格式 {"method":"send","text":"用户输入"} 或 {"method":"command","name":...,"args":[...]}。
    本函数解析后返回 text（命令请求由调用方另行处理，这里只返回文本）。

    非 TUI 模式：直接 input(prompt)，行为与原裸 input() 完全一致。
    """
    if IS_TUI:
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        line = line.strip()
        if not line:
            return ""
        try:
            req = json.loads(line)
            if isinstance(req, dict) and req.get("method") == "send":
                return str(req.get("text", ""))
        except Exception:
            # 非 JSON 或格式不符：当纯文本处理（兼容性兜底）
            return line
        return ""
    return input(prompt)


def prompt_user() -> None:
    """显示用户输入提示符。

    非 TUI 模式：print("你 > ", end="", flush=True)（原行为）。
    TUI 模式：noop（TUI 自己渲染输入框，不需要提示符）。
    """
    if not IS_TUI:
        with _PRINT_LOCK:
            print("你 > ", end="", flush=True)

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

# ---- 管道行帧模式 ----
# stdout 是管道（控制中心 QProcess / 测试 harness）而非真实终端时，chat
# 输出逐帧补换行（"TAG 内容 TAG\n"）：消费端按「完整行是否含标记」做
# 无状态路由，不再依赖跨块的标记交替状态——管道读取随时可能从一次
# write 的中间截断（LLM 流式 + TTS 合成抢 CPU 时频繁），交替状态一旦
# 错位，之后所有内容左右栏整体对调（实测回复被劈成两半分落两栏）。
# 真实终端保持原无换行输出，打字机效果不受影响。
_PIPELINE_STDOUT = False
try:
    if not IS_TUI and hasattr(sys.stdout, "isatty"):
        _PIPELINE_STDOUT = not sys.stdout.isatty()
except Exception:
    _PIPELINE_STDOUT = False
if _PIPELINE_STDOUT:
    # 管道模式禁用 write 侧 \n→\r\n 翻译（Windows TextIOWrapper 默认
    # newline=None 会翻译）：帧协议按 \n 切行，\r 会污染消费端路由
    # （旧版控制中心把 \r 当换行 → 每个流式块单独成行）。
    try:
        sys.stdout.reconfigure(newline="\n")
        sys.stderr.reconfigure(newline="\n")
    except Exception:
        pass


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

    管道模式（_PIPELINE_STDOUT，控制中心 / harness）：每帧补换行成完整
    行（"TAG 内容 TAG\\n"），消费端按行无状态路由；end 含换行时补发一个
    空帧作为「换行」信号（空帧由消费端翻译成对话面板换行，打字机分片
    不受影响）。真实终端保持原样（帧后不换行）。

    TUI 模式：非空 text 发 assistant_chunk 事件（TUI 端累积成流式回复），
    空 text（换行/起始信号）忽略——TUI 自己管布局，不需要换行事件。
    """
    if IS_TUI:
        if text:
            _tui_emit({"type": "assistant_chunk", "text": text})
        return
    with _PRINT_LOCK:
        if _PIPELINE_STDOUT:
            print(f"{CHAT_TAG}{text}{CHAT_TAG}\n", end="", flush=flush)
            if text and "\n" in end:
                print(f"{CHAT_TAG}{CHAT_TAG}\n", end="", flush=flush)
        else:
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
    if IS_TUI:
        # 映射到 TUI log level：[ OK ]→ok, [INFO]→info, [WARN]→warn, [FAIL]→fail
        level_map = {"[ OK ]": "ok", "[INFO]": "info",
                     "[WARN]": "warn", "[FAIL]": "fail"}
        _tui_log(level_map.get(label, "info"), msg)
        return
    with _PRINT_LOCK:
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
    if IS_TUI:
        _tui_log("dim", msg)
        return
    with _PRINT_LOCK:
        print(paint(msg, GRAY))


def accent(msg: str, end: str = "\n") -> None:
    """强调信息：亮青色（如指令触发日志）。"""
    if IS_TUI:
        _tui_log("info", msg)
        return
    with _PRINT_LOCK:
        print(paint(msg, BRIGHT_CYAN), end=end, flush=True)


def header(title: str, width: int = 56) -> None:
    """分区标题横幅：亮青色加粗 ━ 上下边框。

    按显示宽度居中：━ 在 Windows 终端 / Qt 等宽文本框里为 1 列宽，
    故 bar 宽度 = width（字符数）；标题的显示宽度按 east_asian_width
    正确计算（中文 2 列、英文 1 列）。只做左侧填充，不追加尾部空格。
    """
    if IS_TUI:
        _tui_log("info", title)
        return
    bar = "━" * width
    left = max(0, (width - _display_width(title)) // 2)
    with _PRINT_LOCK:
        print(f"\n{paint(bar, BRIGHT_CYAN)}")
        print(paint(" " * left + title, BOLD + BRIGHT_CYAN))
        print(paint(bar, BRIGHT_CYAN))


def kv(label: str, value: str, label_width: int = 12) -> None:
    """键值行：青色标签 + 值（左对齐缩进两格）。"""
    if IS_TUI:
        _tui_log("info", f"{label}: {value}")
        return
    with _PRINT_LOCK:
        print(f"  {paint(label.ljust(label_width), CYAN)}{value}")


def progress(text: str) -> None:
    """探测/扫描过程行：灰色打点前缀。"""
    if IS_TUI:
        _tui_log("dim", text)
        return
    with _PRINT_LOCK:
        print(f"  {paint('·', CYAN)} {text}")


# 兼容旧写法：from src.utils.console import console；某些上游通过 hasattr(module,'console') 校验。
# 这里让 console 属性等于本模块自身（单例化），所有顶层函数/常量仍可通过 `console.xxx()` 调用。
import sys as _sys
console = _sys.modules[__name__]

__all__ = [
    # ANSI 颜色常量
    "RESET", "BOLD", "DIM", "RED", "GREEN", "YELLOW", "CYAN", "GRAY",
    "BRIGHT_GREEN", "BRIGHT_YELLOW", "BRIGHT_MAGENTA", "BRIGHT_CYAN",
    "CHAT_TAG",
    # TUI 旁路
    "IS_TUI", "read_input", "prompt_user", "report_status",
    # 输出互斥
    "output_lock",
    # 函数
    "paint", "chat", "ok", "info", "warn", "error", "dim", "accent",
    "header", "kv", "progress",
    # 单例（self-alias，兼容 `from console import console` 老入口）
    "console",
]
