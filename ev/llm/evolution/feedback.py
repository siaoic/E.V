"""观众负反馈信号采集与复盘素材注入（5.6）。

利用直播特有信号（Hermes 没有的差异化优势）：
- 弹幕负向关键词（吐槽 / 差评 / 点名否定）→ source=barrage, kind=negative
- 播报被用户输入/语音打断 → source=interrupt, kind=interrupt
- 主播显式否定命令（!no / !advice drop）→ source=command, kind=reject

事件落盘 DATA_ROOT/evolution_feedback.jsonl（JSON Lines，追加写，超上限截断最旧）：
{ts, source, kind, text, meta}

复盘时由 engine 追加 [AUDIENCE FEEDBACK] 块，负反馈优先触发 skill_patch 修正
或 advice_status 移除失效话术；skill_eval 用例生成与 GEPA 评审也注入真实反馈素材。

设计约束：
- 全程 fail-open：开关关闭 / 文件读写失败仅告警或静默，绝不阻塞主链路
- 弹幕只对「真正要回复的弹幕」采集（与 Embedding 口径一致，避免噪音）
- 采集为同步小文件追加（一次 <1ms），不引入额外异步任务
"""

from __future__ import annotations

import json
import os
import time

from ev.utils import config, console

# 负向信号关键词（吐槽 / 否定 / 不满意，保守小集合避免误伤正常弹幕）
_NEGATIVE_KEYWORDS = (
    "吐槽", "差评", "好难听", "难听", "不好听", "真难听", "没意思", "无聊",
    "不行", "不像", "不对", "错了", "搞砸", "翻车", "离谱", "拉胯", "拉垮",
    "烂", "垃圾", "废物", "讨厌", "烦死了", "别说了", "别唱", "闭嘴",
    "换一个", "换首歌", "不要这个", "别这样",
)

# 反馈事件上限：超过时截断最旧（防文件无限膨胀）
_FEEDBACK_MAX_LINES = 500


def is_negative_text(text: str) -> bool:
    """弹幕文本是否命中负向信号关键词（弹幕负反馈采集入口）。"""
    return any(k in text for k in _NEGATIVE_KEYWORDS)


def _feedback_path() -> str:
    """负反馈事件文件路径（可写数据根，与其它 evolution 落盘口径一致）。"""
    return os.path.join(config.cfg.DATA_ROOT, "evolution_feedback.jsonl")


def _feedback_enabled() -> bool:
    """负反馈采集总开关（EVOLUTION_FEEDBACK_ENABLED，缺省开启）。"""
    return getattr(config.cfg, "EVOLUTION_FEEDBACK_ENABLED", True)


def record_feedback(source: str, kind: str, text: str,
                    *, meta: dict | None = None) -> None:
    """记录一条负反馈事件（JSONL 追加写，失败静默不影响主链路）。

    source：barrage（弹幕）/ interrupt（播报被打断）/ command（显式否定命令）；
    kind：negative / interrupt / reject。
    写入后若文本命中生效话术建议，顺手标记 negative_hits+1（5.8 复盘优先评估）。
    """
    if not _feedback_enabled():
        return
    text = (text or "").strip()
    if not text:
        return
    path = _feedback_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "source": source,
                "kind": kind,
                "text": text[:200],
                "meta": meta or {},
            }, ensure_ascii=False) + "\n")
        _trim_feedback(path)
    except OSError as e:
        console.warn(f"[进化] 负反馈事件写入失败：{e}")
        return
    # 5.8.2：负反馈命中生效话术建议 → 标记 negative_hits（供复盘优先评估）
    try:
        from .advice import bump_advice_negative_hits
        bump_advice_negative_hits(text)
    except Exception:
        pass


def _trim_feedback(path: str) -> None:
    """超过上限截断最旧（读全部行、保留尾部、写回）；失败静默。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= _FEEDBACK_MAX_LINES:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-_FEEDBACK_MAX_LINES:])
    except OSError:
        pass


def recent_feedback(max_n: int = 20) -> list[dict]:
    """读取最近 N 条负反馈事件（最新在前；文件缺失/损坏返回空列表）。"""
    if not _feedback_enabled():
        return []
    path = _feedback_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    events: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
        if len(events) >= max_n:
            break
    return events


def feedback_section(max_events: int = 10) -> str:
    """复盘素材 [AUDIENCE FEEDBACK] 块（无数据返回空串，不拼无意义块）。

    最近 N 条负反馈按时间倒序；提示 LLM 这些是真实反馈，应优先触发
    skill_patch 修正对应技能或 advice_status 移除失效话术。
    """
    events = recent_feedback(max_events)
    if not events:
        return ""
    lines = []
    for ev in events:
        ts = time.strftime("%m-%d %H:%M", time.localtime(ev.get("ts") or 0))
        src = ev.get("source") or "?"
        kind = ev.get("kind") or "?"
        text = (ev.get("text") or "").strip()
        if text:
            lines.append(f"- [{ts}] {src}/{kind}：{text}")
    if not lines:
        return ""
    return (
        "\n\n[AUDIENCE FEEDBACK]\n"
        "以下为最近采集到的观众/主播负反馈信号（吐槽、打断、否定），是真实反馈。"
        "若其中指向某技能或话术失效，请优先输出 skill_patch 修正对应技能，"
        "或通过 advice_status 移除失效话术建议。\n"
        + "\n".join(lines)
    )
