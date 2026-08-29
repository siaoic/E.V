"""用户对话处理器：可打断对话（原 Application._interruptible_converse）。"""

import asyncio

from ev.llm import stream
from ev.kernel.output_lock import (
    STATE_AI_SPEAKING, STATE_IDLE, get_output_lock, set_output_owner,
    set_global_state,
)
from ev.kernel.handlers.base import BaseHandler


class ChatHandler(BaseHandler):
    async def converse(self, text, on_llm_done=None,
                       profanity_filter=None,
                       profanity_filter_rate: float = 0.7) -> tuple:
        """用户对话期间同时监听键盘/语音输入：到达即打断当前输出。

        仅用户自己说话时运行：打断只能是用户打断自己；主动 / 弹幕无法并发
        抢占（锁保护）。
        """
        from ev.utils import console
        from ev.kernel.turn_lease import session_turn_gate
        runtime = self.runtime
        tts = runtime.tts
        face = runtime.face
        sub = runtime.sub
        stt = runtime.stt_engine
        loop = asyncio.get_running_loop()
        console.chat()  # 回复起始换行
        # 会话租约（3.10）：用户对话（键盘/语音同属本地会话）排队串行进入
        # brain；默认关闭时 gate 直接放行，行为与现状完全一致
        gate = session_turn_gate("local")
        gate_ok = await gate.__aenter__()
        if not gate_ok:
            return False, "", None
        output_lock = get_output_lock()
        async with output_lock:
            set_output_owner("user")
            set_global_state(STATE_AI_SPEAKING)
            input_fut = None  # 提前初始化：finally 兜底交还引用
            try:
                if tts is not None:
                    try:
                        tts.clear_interrupt()  # 新一轮输出：复位上一轮打断标志
                    except Exception:
                        pass
                conv = asyncio.create_task(
                    stream.converse(runtime.brain, text, tts=tts, face=face, sub=sub,
                                    profanity_filter=profanity_filter,
                                    profanity_filter_rate=profanity_filter_rate,
                                    on_llm_done=on_llm_done,
                                    emotion_actor=(
                                        runtime.emotion_actor
                                        if (getattr(runtime, "emotion_actor", None)
                                            is not None
                                            and runtime.cfg.EMOTION_ACTOR_ENABLED)
                                        else None)))
                # 优先复用 _wait_input 语音触发时挂起的键盘监听（input() 阻塞
                # 线程无法取消，任意时刻必须只有一个 input() 在等 stdin，否则
                # 更早挂起的那只抢占键盘输入）：复用后立即置 None
                if (runtime._pending_stdin_fut is not None
                        and not runtime._pending_stdin_fut.done()):
                    input_fut = runtime._pending_stdin_fut
                    runtime._pending_stdin_fut = None
                else:
                    input_fut = loop.run_in_executor(None, console.read_input)
                _stdin_eof = False
                stt_fut = stt.result_future() if stt is not None else None
                watch = {conv, input_fut}
                if stt_fut is not None:
                    watch.add(stt_fut)
                while True:
                    done, _ = await asyncio.wait(
                        watch, return_when=asyncio.FIRST_COMPLETED)
                    buzz = ""
                    got_input = False
                    if input_fut is not None and input_fut in done:
                        try:
                            buzz = input_fut.result()
                        except EOFError:
                            _stdin_eof = True
                            watch.discard(input_fut)
                            input_fut = None
                            continue
                        got_input = True
                        if not buzz.strip():
                            watch.discard(input_fut)
                            input_fut = loop.run_in_executor(None, console.read_input)
                            watch.add(input_fut)
                            continue
                    elif stt_fut is not None and stt_fut in done:
                        buzz, speech_seconds = stt_fut.result()
                        old = stt_fut
                        stt_fut = stt.result_future()
                        watch.discard(old)
                        watch.add(stt_fut)
                        if not buzz.strip():
                            continue
                        # 打断仅限说话时长超过阈值：过短语音（嗯/啊/咳嗽/
                        # 环境音）不打断当前播报，继续监听下一条
                        if speech_seconds <= runtime.cfg.STT_INTERRUPT_MIN_SECONDS:
                            console.dim(
                                f"[打断] 语音过短（{speech_seconds:.1f}s），"
                                "不打断当前播报")
                            continue
                        got_input = True
                    if got_input:
                        # 打断：立即闭嘴 → 取消 LLM 流
                        console.dim("[打断] 用户输入/语音打断当前播报")
                        # P1-1：通知子线程 drainer 断开 HTTP 流（协程取消
                        # 无法停掉线程池里的同步迭代，会空跑到 finish_reason）
                        try:
                            runtime.brain.cancel_llm_stream()
                        except Exception:
                            pass
                        if tts is not None:
                            try:
                                tts.interrupt()
                            except Exception:
                                pass
                        if face is not None:
                            try:
                                face.stop_speaking()
                            except Exception:
                                pass
                        if sub is not None:
                            try:
                                sub.push("clear", "")
                            except Exception:
                                pass
                        conv.cancel()
                        try:
                            await conv
                        except (asyncio.CancelledError, Exception):
                            pass
                        console.chat()  # 打断换行
                        buzz = buzz.strip()
                        # 5.6 负反馈信号：播报被用户输入/语音打断 → 记录事件
                        # （供复盘素材注入；失败静默，不影响打断链路）
                        try:
                            from ev.llm.evolution.feedback import record_feedback
                            record_feedback("interrupt", "interrupt", buzz)
                        except Exception:
                            pass
                        if stt_fut is not None and not stt_fut.done():
                            stt_fut.cancel()
                        if input_fut is not None and input_fut in done:
                            return True, buzz, None
                        console.info(f"[语音识别] {buzz}")
                        # 打断来源是语音：挂起的键盘监听（input() 阻塞线程无法
                        # 真正取消）必须交还主循环复用，否则残留的 input() 会
                        # 抢占后续键盘输入——表现为语音对话后键盘输入失灵
                        return True, buzz, input_fut
                    if conv in done:
                        try:
                            await conv
                        except Exception as e:
                            console.error(f"对话流程出错：{e}")
                        if stt_fut is not None and not stt_fut.done():
                            stt_fut.cancel()
                        console.chat()  # 回复结束换行
                        if input_fut is not None and not input_fut.done():
                            return False, "", input_fut
                        return False, "", None
            finally:
                # 异常/提前退出兜底：交还挂起的键盘监听，杜绝残留 input()
                # 抢占后续键盘输入（正常返回路径已通过返回值交还，这里只兜
                # 底异常场景——input() 阻塞线程无法取消，只能复用不能丢弃）
                if input_fut is not None and not input_fut.done():
                    runtime._pending_stdin_fut = input_fut
                set_output_owner(None)
                set_global_state(STATE_IDLE)
                if gate_ok:
                    await gate.__aexit__(None, None, None)
