"""资源清理：进程内运行时内存 + 运行残留的临时文件。

用法：
    from ev.utils import cleaner
    cleaner.cleanup_temp_files()       # 启动时清理上次崩溃残留（静默）
    cleaner.cleanup_runtime_memory()   # 主动回收进程内存（!clean 命令）

原则：
- 只清「可再生的缓存 / 确认无用的残留文件」，绝不碰功能数据
  （对话历史、记忆库、.env、配置等）。
- 每个清理点单独 try，单个失败不影响其余，任何情况都不抛异常。
"""

import gc
import glob
import os
import tempfile
from typing import Dict, Tuple

from ev.utils import config, console

# STT 临时音频前缀/后缀（src/asr/stt.py 用 tempfile.mkstemp 创建）
_STT_TMP_PREFIX = "vtuber_stt_"
_STT_TMP_SUFFIX = ".wav"

# 旧 GSV-TTS-Lite 服务端合成输出目录（项目根 temp/，服务端已随 gsv_tts 移除，
# 此处仅兜底清理历史残留 tts_*.wav，不碰 temp/ 下其他任何文件）。
# 目录在函数内求值（依赖 config 已加载）。
_TTS_OUT_PREFIX = "tts_"
_TTS_OUT_SUFFIX = ".wav"


def _tts_out_dir() -> str:
    return os.path.join(config.cfg.PROJECT_ROOT, "temp")


def _remove_glob(directory: str, prefix: str, suffix: str) -> Tuple[int, int]:
    """删除目录下匹配 {prefix}*{suffix} 的文件，返回 (删除数, 释放字节)。

    任意失败均静默跳过（文件被占用 / 目录不存在等），不抛异常。
    """
    removed_count = 0
    freed_bytes = 0
    pattern = os.path.join(directory, prefix + "*" + suffix)
    for path in glob.glob(pattern):
        try:
            freed_bytes += os.path.getsize(path)
            os.remove(path)
            removed_count += 1
        except OSError:
            pass
    return removed_count, freed_bytes


def _cleanup_tts_tmp() -> Tuple[int, int]:
    """清空旧服务端 TTS 合成输出目录（项目根 temp/，仅 tts_*.wav 历史残留）。"""
    try:
        return _remove_glob(_tts_out_dir(), _TTS_OUT_PREFIX, _TTS_OUT_SUFFIX)
    except Exception:
        return 0, 0


def cleanup_tts_output(verbose: bool = True) -> dict:
    """清理旧服务端 TTS 合成输出残留（播放完/兜底调用），返回 {files, bytes}。"""
    files, freed_bytes = _cleanup_tts_tmp()
    stats = {"files": files, "bytes": freed_bytes}
    if verbose and files:
        console.ok(f"TTS 临时音频清理完成：删除 {files} 个文件（释放 {freed_bytes / 1024.0:.1f} KB）")
    return stats


def cleanup_runtime_memory(verbose: bool = True) -> dict:
    """回收进程内垃圾 + 清空各模块缓存，返回统计。

    - gc.collect()：回收循环引用 / 已 del 但未释放的 Python 对象
    - TTS wav 缓存：output 目录音频播完即删，缓存里的文件名映射
      已失效，清空避免无意义驻留（下次播放同句会重新合成）
    会话历史 / 记忆数据属于功能数据，一律不动。
    """
    stats: Dict[str, int] = {"gc_objects": 0, "tts_wav_cache": 0}

    stats["gc_objects"] = gc.collect()
    try:
        from ev.tts.engine import _wav_cache
        stats["tts_wav_cache"] = len(_wav_cache)
        _wav_cache.clear()
    except Exception:
        pass

    if verbose:
        console.ok(
            f"内存清理完成：gc 回收 {stats['gc_objects']} 个对象，"
            f"清空 TTS 缓存 {stats['tts_wav_cache']} 条")
    return stats


def cleanup_temp_files(verbose: bool = True) -> dict:
    """清理运行残留的临时文件，返回统计 {files, bytes}。

    - output/：TTS 临时音频（复用 engine._cleanup_output 的安全删除）
    - 系统临时目录：STT 崩溃 / 进程被杀残留的 vtuber_stt_*.wav
    - 项目根 temp/：旧服务端 TTS 合成输出残留（tts_*.wav）
    删除失败（文件被占用）静默跳过。
    """
    stats: Dict[str, int] = {"files": 0, "bytes": 0}

    # 1. TTS output 目录（engine._cleanup_output 已处理 winsound 句柄释放，
    #    现返回 (删除数, 释放字节) 供统计）
    try:
        from ev.tts.engine import _cleanup_output
        files, freed_bytes = _cleanup_output()
        stats["files"] += files
        stats["bytes"] += freed_bytes
    except Exception:
        pass

    # 2. STT 残留临时音频（转写 worker 正常会删除，仅清理异常残留）
    try:
        files, freed_bytes = _remove_glob(
            tempfile.gettempdir(), _STT_TMP_PREFIX, _STT_TMP_SUFFIX)
        stats["files"] += files
        stats["bytes"] += freed_bytes
    except Exception:
        pass

    # 3. 旧服务端 TTS 合成输出残留（项目根 temp/tts_*.wav，兜底清理）
    try:
        files, freed_bytes = _cleanup_tts_tmp()
        stats["files"] += files
        stats["bytes"] += freed_bytes
    except Exception:
        pass

    if verbose:
        console.ok(
            f"临时文件清理完成：删除 {stats['files']} 个文件"
            f"（释放 {stats['bytes'] / 1024.0:.1f} KB）")
    return stats
