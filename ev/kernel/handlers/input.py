"""输入处理器：等待用户键盘/语音输入，事件驱动心跳（原 Application._wait_input）。"""

from ev.kernel.output_lock import is_rejecting_input
from ev.kernel.handlers.base import BaseHandler


class InputHandler(BaseHandler):
    async def wait_input(self, show_prompt: bool = True) -> str:
        """事件驱动输入等待：等待用户输入，互动/弹幕回复结束时做一次心跳检查。

        主动对话 / 弹幕回复在播报期间，本方法收到的任何键盘 / 语音识别输入
        会被**直接丢弃**（不返回给主循环、也不缓存）——确保「说话期间
        不接收任何信息」。
        """
        import asyncio
        from ev.utils import console
        loop = asyncio.get_running_loop()
        if show_prompt:
            console.prompt_user()
        runtime = self.runtime
        if runtime._pending_stdin_fut is not None and not runtime._pending_stdin_fut.done():
            input_fut = runtime._pending_stdin_fut
        else:
            input_fut = loop.run_in_executor(None, console.read_input)
        if runtime.proactive is None and runtime.stt_engine is None:
            # 未启用主动对话/语音识别：纯阻塞等待（与原行为一致）
            return await input_fut
        stt_fut = runtime.stt_engine.result_future() if runtime.stt_engine is not None else None
        wake_task = None
        speak_task = None
        while True:
            pending = {input_fut}
            if stt_fut is not None:
                pending.add(stt_fut)
            if runtime.proactive is not None:
                # 事件驱动心跳 + 单次精确唤醒（无周期轮询）：平时等「互动结束
                # 事件」或「下一个有意义时刻」（无聊/孤独到阈值、话题超时）的
                # 精确超时，到点只醒一次做心跳，而不是固定间隔轮询。
                if wake_task is None or wake_task.done():
                    wake_task = asyncio.create_task(runtime.proactive._wakeup.wait())
                pending.add(wake_task)
                # 主动发言播报中：挂一个「播完事件」精确唤醒（无周期轮询），
                # 播完只需重新等待输入，不触发心跳（防刚说完立刻又接一条）
                if (not runtime.proactive._speak_done.is_set()
                        and (speak_task is None or speak_task.done())):
                    speak_task = asyncio.create_task(
                        runtime.proactive._speak_done.wait())
                    pending.add(speak_task)
            timeout = (runtime.proactive.next_wake_in()
                       if runtime.proactive is not None else None)
            done, _ = await asyncio.wait(
                pending, timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED)

            if wake_task is not None and wake_task in done:
                # 事件触发（互动结束）：重置事件并立即心跳
                runtime.proactive._wakeup.clear()
                print()
                try:
                    await runtime.proactive.heartbeat()
                except Exception as e:
                    import traceback as _tb
                    console.error(
                        f"主动对话心跳出错：{e}\n"
                        f"{''.join(_tb.format_exception(type(e), e, e.__traceback__))}")
                console.prompt_user()
                continue

            if not done:
                # 静默期「开口机会」时刻到点（单次精确唤醒）：做一次心跳，
                # 由主模型自主决定此刻想不想说话（想说就开口，不想说保持
                # 沉默）。heartbeat 内部自带忙碌抑制/弹幕避让/话题保护，
                # 不会乱开口。
                print()
                try:
                    await runtime.proactive.heartbeat()
                except Exception as e:
                    import traceback as _tb
                    console.error(
                        f"主动对话心跳出错：{e}\n"
                        f"{''.join(_tb.format_exception(type(e), e, e.__traceback__))}")
                console.prompt_user()
                continue

            if speak_task is not None and speak_task in done:
                # 主动播报结束：重新等待输入（speak_done 已置位，下次不会再挂）
                continue

            if input_fut in done:
                text = input_fut.result()
                # ---- 拒收：主动 / 弹幕正在播报 → 丢本条，换新 input_fut 继续等 ----
                if is_rejecting_input():
                    console.dim("[输入丢弃] 正在回复弹幕 / 主动说话，忽略本次键盘输入")
                    input_fut = loop.run_in_executor(None, console.read_input)
                    continue
                if runtime.proactive is not None:
                    # 用户输入优先：丢弃排队中的主动消息，只保留正在播报的
                    runtime.proactive.discard_pending()
                runtime._input_source = "text"
                # 键盘监听已消费（input_fut done），清理复用引用；
                # 语音触发路径存的挂起监听在下方保留，供对话复用
                runtime._pending_stdin_fut = None
                return text

            if stt_fut is not None and stt_fut in done:
                text, _ = stt_fut.result()
                stt_fut = runtime.stt_engine.result_future()  # 拿下一个识别结果
                if not text:
                    continue
                # ---- 拒收：主动 / 弹幕正在播报 → 丢本条，继续等 ----
                if is_rejecting_input():
                    console.dim("[输入丢弃] 正在回复弹幕 / 主动说话，忽略本次语音输入")
                    continue
                if runtime.proactive is not None:
                    # 用户输入优先：丢弃排队中的主动消息
                    runtime.proactive.discard_pending()
                # 语音先触发：挂起的键盘监听（input() 阻塞线程无法取消）交还
                # 下轮复用，避免残留 input() 抢占后续键盘输入
                if input_fut is not None and not input_fut.done():
                    runtime._pending_stdin_fut = input_fut
                print(f"[语音识别] {text}", flush=True)
                runtime._input_source = "voice"
                return text

            if runtime.proactive is None:
                continue
            # 单次精确唤醒到点：做一次心跳（可能触发主动发言），随后重新计算
            # 下一次唤醒时刻
            print()
            try:
                await runtime.proactive.heartbeat()
            except Exception as e:
                import traceback as _tb
                console.error(
                    f"主动对话心跳出错：{e}\n"
                    f"{''.join(_tb.format_exception(type(e), e, e.__traceback__))}")
            console.prompt_user()
