"""对话输出流水线：LLM 流式产句 → 实时打印 → TTS 播放 → 字幕 → 口型。

被 main.py（用户对话）与 proactive.py（主动发言）共用，保证两条路径的
输出行为完全一致：有 TTS 时口型由音频播放回调（load_speech_curve）在
音频真正开始播的那一刻同步触发，不会提前张嘴；无 TTS 时用 start_speaking
驱动节拍口型。
"""

import asyncio
import random
from typing import Awaitable, Callable, Optional

from src.utils import console
from src.llm.llm_brain import LLMBrain
from src.utils.subtitle_server import SubtitleServer
from src.tts.engine import TTSEngine
from src.vts.face_driver import FaceDriver
from plugins.base import LLMResponseEvent
from plugins.manager import get_default_manager
from src.core.bus import EV_AI_REPLY, EV_SPEAKING_END, bus
from src.core.events.models import LLMResponse, SpeakingEvent
from src.core.output_lock import get_output_owner


async def _run_tts_hooks(pm, text: str) -> str:
    """运行 on_tts_text（改写语音文本）+ on_tts_start；无插件管理器返回原文。

    钩子异常由管理器内部逐个容错，这里无需再包 try/except。
    """
    if pm is None:
        return text
    text = await pm.run_tts_text_hooks(text)
    await pm.run_tts_start_hooks(text)
    return text


async def speak_text(text: str,
                     tts: Optional[TTSEngine] = None,
                     face: Optional[FaceDriver] = None,
                     sub: Optional[SubtitleServer] = None,
                     proactive: bool = False,
                     profanity_filter=None,
                     profanity_filter_rate: float = 0.7) -> None:
    """直接播报已有文本（无需 LLM 生成）：打印 + 网页字幕 + 口型 + TTS。

    供主动发言「LLM 自主决定想说就说」路径使用：发言内容已由主模型生成完毕，
    这里只做输出流水线（过滤 / 打印 / 字幕 / 口型 / TTS 排队播放），
    行为与 converse 的产句播报保持一致（脏话过滤、字幕时机、TTS 排空）。
    """
    sender = get_output_owner() or "user"
    spoken = text
    if profanity_filter is not None:
        masked, hit = profanity_filter.censor(spoken)
        if hit and random.random() < profanity_filter_rate:
            console.warn(f"[内容过滤] AI 回复命中敏感词，已替换")
            spoken = masked
    if proactive:
        console.chat("主动对话：", end="", flush=True)
    console.chat(spoken, end="", flush=True)
    # 事件总线：AI 回复（供订阅方实时消费）
    await bus.emit(EV_AI_REPLY, LLMResponse(text=spoken, sender=sender))
    if tts:
        # 插件钩子：on_tts_text（只影响语音）+ on_tts_start
        spoken = await _run_tts_hooks(get_default_manager(), spoken)
        # 有 TTS：口型由音频播放回调同步触发，字幕随播放推送
        await tts.speak(spoken)
    else:
        # 无 TTS：直接推送字幕，用节拍口型
        if sub:
            sub.push("text", spoken)
        if face:
            face.start_speaking(max(0.6, len(spoken) * 0.15))
    if tts:
        await tts.drain()
        # 插件钩子：on_tts_end（语音播放结束）
        pm = get_default_manager()
        if pm is not None:
            await pm.run_tts_end_hooks()
        # 播放完清理：服务端 TTS 合成输出（项目根 temp/tts_*.wav）不再需要
        try:
            from src.utils import cleaner
            cleaner.cleanup_tts_output(verbose=False)
        except Exception:
            pass
    if sub:
        sub.push("clear", "")
    # 事件总线：一次播报结束
    await bus.emit(EV_SPEAKING_END, SpeakingEvent(sender=sender))


async def converse(brain: LLMBrain,
                   text: str,
                   tts: Optional[TTSEngine] = None,
                   face: Optional[FaceDriver] = None,
                   sub: Optional[SubtitleServer] = None,
                   proactive: bool = False,
                   profanity_filter=None,
                   profanity_filter_rate: float = 0.7,
                   history: Optional[list] = None,
                   on_llm_done: Optional[Callable[[str], Awaitable[None]]] = None) -> None:
    """对话：LLM 流式产句 + 文本实时打印 + 网页字幕推送。

    当提供 tts 引擎时，每条句子立即送入合成队列并排队播放，
    不阻塞 LLM 流，实现「一边生成文本、一边合成音频、一边播放」。

    proactive=True 时以「内部自主行动指令」身份调用大脑：该 prompt
    不会写入历史（不冒充用户发言），只保留模型回复保持上下文连贯。

    history：可选历史快照（agent 主动发言只给最近 N 条精简上下文，
    降低 token）；None 时用完整历史（与原有行为一致）。

    profanity_filter：可选脏话过滤器（ProfanityFilter 实例），仅过滤
    AI 回复句子（TTS/字幕播出前），不过滤用户/弹幕输入。命中时按
    profanity_filter_rate 概率触发替换（默认 70%，与应用层原逻辑一致）。

    on_llm_done：可选异步回调（入参为 LLM 完整回复文本）。在 LLM 全文
    生成完毕（brain.history 已含该回复）时以后台任务调度，**与 TTS 播放
    并行执行**，不等 drain() 播完全部音频——用于记忆提取等不依赖音频
    播完的任务，避免端到端耗时被 TTS 播放拖长。回调内部需自行捕获异常。
    """
    sender = get_output_owner() or "user"
    try:
        full_reply_parts = []
        # 主动对话：回复流首个句子前加「主动对话：」前缀（只加一次，
        # 句子是连续流，后续句子不再重复前缀）
        turn_prefixed = False
        async for sentence in brain.chat_stream(text, proactive=proactive, history=history):
            # AI 回复脏话过滤：仅过滤要播给用户听 / 字幕展示的句子，
            # 不过滤 LLM 原文存记忆（记忆存原文，on_llm_done 存的是 LLM 历史）。
            spoken = sentence
            if profanity_filter is not None:
                masked, hit = profanity_filter.censor(spoken)
                if hit and random.random() < profanity_filter_rate:
                    console.warn(f"[内容过滤] AI 回复命中敏感词，已替换")
                    spoken = masked
            if proactive and not turn_prefixed:
                console.chat("主动对话：", end="", flush=True)
                turn_prefixed = True
            # AI 说的话走「对话」通道：控制中心左栏显示（读哪句显示哪句）
            console.chat(spoken, end="", flush=True)
            full_reply_parts.append(spoken)
            # 事件总线：AI 回复（流式逐句，供订阅方实时消费）
            await bus.emit(EV_AI_REPLY, LLMResponse(text=spoken, sender=sender))
            # 网页字幕：打字机效果（与语音播放基本同步）
            if tts:
                # 插件钩子：on_tts_text（只影响语音，字幕展示原文）+ on_tts_start
                spoken = await _run_tts_hooks(
                    getattr(brain, "plugin_manager", None), spoken)
                # 有 TTS：口型不在此处启动——TTS 合成+排队常需数秒，
                # 若在句子生成时就 start_speaking，音频未播嘴已先动。
                # 口型由音频播放回调（_on_tts_play → load_speech_curve）
                # 在音频真正开始播放的那一刻同步触发。
                await tts.speak(spoken)
                # 字幕：音频播放回调中推送（说哪句显示哪句）
            else:
                # 无 TTS：直接产句时推送（没有播放时机）
                if sub:
                    sub.push("text", spoken)
                if face:
                    dur = max(0.6, len(spoken) * 0.15)
                    face.start_speaking(dur)
            # 让出事件循环：否则 TTS 合成任务永远得不到调度，
            # 所有句子处理完后 TTS 才能开始合成，看起来像「LLM 发完才 TTS」
            await asyncio.sleep(0)
    except Exception as e:
        console.error(f"LLM 流式产出出错：{e}")
    finally:
        # LLM 全文已生成：立即以后台任务触发记忆提取等回调（与 TTS 播放
        # 并行），LLM 生成远比 TTS 播放快，不必等全部音频播完再存记忆。
        if on_llm_done is not None and brain.history:
            last_reply = brain.history[-1].get("content", "") or ""
            if last_reply:
                try:
                    asyncio.create_task(on_llm_done(last_reply))
                except Exception as e:
                    console.error(f"LLM 完成回调调度失败：{e}")
        # 插件钩子：on_llm_response（AI 回复之后、TTS 播放结束前；钩子内部逐个容错）
        pm = getattr(brain, "plugin_manager", None)
        if pm is not None and brain.history:
            last_reply = brain.history[-1].get("content", "") or ""
            if last_reply:
                await pm.run_llm_response_hooks(LLMResponseEvent(last_reply))
        # 等待所有音频播完（合成+播放队列排空），再清除字幕
        if tts:
            await tts.drain()
            # 插件钩子：on_tts_end（语音播放结束）
            if pm is not None:
                await pm.run_tts_end_hooks()
            # 播放完清理：服务端 TTS 合成输出（项目根 temp/tts_*.wav）在音频
            # 已完整拉取播放后不再需要，兜底清空（启动/退出另有 cleaner 清理）
            try:
                from src.utils import cleaner
                cleaner.cleanup_tts_output(verbose=False)
            except Exception:
                pass
        if sub:
            sub.push("clear", "")
        # 事件总线：一次播报结束（被用户打断 / 异常退出也会走到这里）
        await bus.emit(EV_SPEAKING_END, SpeakingEvent(sender=sender))
