"""播放音效工具（参考旧 src/llm/tools/sfx/ js 插件移植为 Python）。

音效库：plugins/builtin/tools/SFX/01.wav ~ 07.wav（讲解.txt 为音效含义说明）。
播放引擎：winsound（stdlib，仅 Windows），后台线程按序同步播放，不阻塞 asyncio 主循环，
          避免像 js 版那样每次调用都拉起一个 powershell 进程。
"""

from __future__ import annotations

import os
import random
import re
import threading
import time
import winsound

# 音效库：编号 → 含义（与 sfx/SFX/讲解.txt 保持一致）
_SFX_LIBRARY = {
    "01": "搞啥情况",
    "02": "突然一惊",
    "03": "巨大爆炸",
    "04": "钢管掉落",
    "05": "OMG 不可思议",
    "06": "震撼管弦乐",
    "07": "wow 效果音",
    "08": "警觉的意思",	
}

# 文本内音效标记：{{sfx:编号}} —— LLM 在叙述中插入，客户端拆段并在
# 该段音频开始播放时同步触发音效（标记本身不显示、不被 TTS 念出）。
_SFX_MARKER_RE = re.compile(r"\{\{sfx:(\d+)\}\}")

# 音效编号分隔符：同一归属段内多个标记（含句尾合并）按逗号串联，
# 播放时按序逐个触发（play_sfx_sequence 同样按逗号拆分）。
_SFX_SEP = ","

# 音效 wav 目录（SFX 资源与讲解.txt 同处）
_SFX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SFX")

# 多音效连续播放的间隔（秒），避免听感急促（与 js 版 250ms 一致）
_PLAY_INTERVAL = 0.25

# 播放次数上限（与 js 版 1-10 一致）
_MAX_REPEAT = 10

# 播放成功率：70% 成功、30% 失败（模拟真实播放偶发失败，返回失败提示
# 给 LLM，测试 AI 对失败工具调用的应对，失败后应正常回应而非反复重试）
_SUCCESS_RATE = 0.7


def _play_sequence(sfx_ids: list, repeat: int) -> None:
    """后台线程播放序列：按 repeat × ids 顺序逐个同步播放，音效之间留间隔。

    winsound 同时只支持一个异步播放，故用同步播放（不带 SND_ASYNC）在后台线程内串行播放。
    """
    for _ in range(repeat):
        for sfx_id in sfx_ids:
            wav_path = os.path.join(_SFX_DIR, f"{sfx_id}.wav")
            # 不带 SND_ASYNC 即同步播放（winsound 未导出值为 0 的 SND_SYNC）
            winsound.PlaySound(wav_path, winsound.SND_FILENAME)
            time.sleep(_PLAY_INTERVAL)


async def _play_sound_effect(sfx_id: str, repeat: int = 1) -> str:
    """播放指定音效（01-07），支持逗号分隔多个音效按序播放。

    有 _SUCCESS_RATE 概率成功；失败时返回失败提示，AI 应正常回应不重试。
    """
    ids = [item.strip() for item in sfx_id.split(",") if item.strip()]
    for item in ids:
        if item not in _SFX_LIBRARY:
            return f"无效的音效编号：{item}（可用编号见 list_sound_effects）"
    times = max(1, min(repeat or 1, _MAX_REPEAT))

    # 随机失败：掷骰未过成功率即模拟播放失败，不真正播放
    if random.random() >= _SUCCESS_RATE:
        return "不让你用气死你，嘻嘻"

    thread = threading.Thread(
        target=_play_sequence, args=(ids, times), daemon=True)
    thread.start()

    names = "、".join(f"{item}({_SFX_LIBRARY[item]})" for item in ids)
    if times > 1:
        return f"正在播放音效：{names}，重复 {times} 次"
    return f"正在播放音效：{names}"


async def _list_sound_effects() -> str:
    """列出音效库（编号 + 含义），供 LLM 选择播放。"""
    if not os.path.isdir(_SFX_DIR):
        return "音效库目录不存在，无法播放音效。"
    lines = [f"{item} = {meaning}" for item, meaning in _SFX_LIBRARY.items()]
    return "可用音效：" + "；".join(lines)


# ---------- 文本内音效标记（说话期间用音效，与 TTS 播放同步） ----------

def _merge_sfx(a: str, b: str) -> str:
    """串联两组音效编号（去空，逗号分隔），支持同一段内多标记累加。"""
    return _SFX_SEP.join(x for x in (a, b) if x)


def split_sfx_markers(text: str) -> list[tuple[str, str]]:
    """按音效标记拆分文本：返回 [(段文本, 该段播放时触发的音效编号或"")]。

    标记合并到**其后**的文本段（标记出现在句尾时合并到末段），该段音频
    开始播放时同步触发音效——"讲到那一刻，音效响起"。标记本身被移除。
    同一归属段内出现多个标记（连续标记或中段+句尾）时编号按逗号串联，
    播放时按序逐个触发。纯标记文本（无任何文字）返回 [("", 编号)]，
    由调用方直接触发。
    """
    if not text:
        return [("", "")]
    parts: list[tuple[str, str]] = []
    pending_sfx = ""
    last = 0
    for m in _SFX_MARKER_RE.finditer(text):
        seg = text[last:m.start()]
        if seg.strip():
            parts.append((seg, pending_sfx))
            pending_sfx = ""
        pending_sfx = _merge_sfx(pending_sfx, m.group(1))
        last = m.end()
    tail = text[last:]
    if tail.strip():
        parts.append((tail, pending_sfx))
    elif pending_sfx and parts:
        # 句尾标记：追加到末段已有音效后（不覆盖中段标记）
        parts[-1] = (parts[-1][0], _merge_sfx(parts[-1][1], pending_sfx))
    elif pending_sfx:
        parts.append(("", pending_sfx))  # 纯标记文本
    return parts


def strip_sfx_markers(text: str) -> str:
    """移除全部音效标记（历史保存/记忆提取用，避免标记污染上下文）。"""
    return _SFX_MARKER_RE.sub("", text)


def play_sfx_sequence(sfx_ids: list[str], repeat: int = 1) -> None:
    """稳定播放音效序列（不经过 30% 随机失败，供文本标记路径使用）。

    与 _play_sound_effect 的失败模拟不同：叙事增强音效应稳定触发，
    后台线程播放，不阻塞调用方。无效编号静默忽略。
    """
    ids = [
        i
        for item in sfx_ids
        for i in item.split(_SFX_SEP)
        if i in _SFX_LIBRARY
    ]
    if not ids:
        return
    thread = threading.Thread(
        target=_play_sequence, args=(ids, max(1, min(repeat or 1, _MAX_REPEAT))),
        daemon=True)
    thread.start()
