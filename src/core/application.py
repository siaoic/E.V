"""AI 虚拟主播主程序封装：Application.run() 对应原 main() 的全部生命周期。

原 main.py 中与「启动程序」无关的所有业务逻辑都搬到这里，使根目录
main.py 仅保留最薄的入口层（编码设置、vendor 路径注入、调用 run）。
"""

import asyncio
import json
import logging
import os
import random
from datetime import datetime
from typing import Optional

from src.utils import config, console
from src.utils.constants import (
    ROLE_AI_ALIAS, SOURCE_DANMAKU_INPUT, SOURCE_DANMAKU_REPLY,
)

# 压制 httpx/openai 客户端的 HTTP 请求 INFO 日志（噪音）
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

from tools.memory import memory
from src.llm import stream
from src.utils.content_filter import ProfanityFilter
from src.utils.safe_text import sanitize_external
from src.llm.agent import ButlerAgent
from src.llm.evolution import EvolutionEngine
from src.mcp.manager import MCPManager
from src.llm.proactive import ProactiveEngine
from src.vts.face_driver import FaceDriver
from src.llm.llm_brain import LLMBrain
from src.vts.model_scanner import scan_model
from src.tts.engine import TTSEngine
from src.vts.controller import VTSController
from src.mindcraft.bridge import MindcraftBridge
from src.utils.perf_tracker import PerfTracker
from src.utils.subtitle_server import SubtitleServer
from src.core.commands import Command, CommandRegistry
from src.core.output_lock import (
    STATE_AI_SPEAKING, STATE_IDLE, STATE_USER_TALKING,
    get_output_lock, get_output_owner, set_output_owner, set_global_state,
    is_rejecting_input, set_danmaku_pending,
)
from plugins import PluginManager, UserInputEvent
from src.core.bus import EV_ERROR, EV_SESSION_END, EV_USER_INPUT, bus
from src.core.events.models import ErrorEvent, InputEvent, SessionEndEvent
from src.core.exceptions import ErrorCode

# 记忆自动整合蒸馏：碎片条数 ≥ 阈值时触发 AI 蒸馏合并（成功后才删除旧碎片），
# 后台循环检查间隔（秒）。阈值 60 = 约 4 批 × 15 条，正常会话去重后很少触达。
_MEMORY_INTEGRATE_THRESHOLD = 60
_MEMORY_INTEGRATE_INTERVAL = 2 * 3600


class Application:
    """封装整份运行时状态：原 main() 内的局部变量全部变为 self 属性。"""

    # ---------- 自我进化：定期自我提示 ----------

    async def _evolution_periodic_loop(self) -> None:
        """后台循环：每 EVOLUTION_PERIODIC_INTERVAL 秒检查一次，空闲期主动补复盘。

        仅当距上次复盘已达标且存在未复盘的新对话轮次时才调用 LLM
        （见 EvolutionEngine.periodic_tick 的节流判定），不重复消费 token。
        """
        interval = max(60, int(self.cfg.EVOLUTION_PERIODIC_INTERVAL or 1800))
        while True:
            await asyncio.sleep(interval)
            if self.evolution is None:
                continue
            try:
                await self.evolution.periodic_tick(
                    self.mm.recent_turns if self.mm is not None else [],
                    proactive=self.proactive)
            except Exception as e:
                console.dim(f"[自我进化] 周期任务出错（不影响运行）：{e}")

    # ---------- 记忆自动整合蒸馏 ----------

    async def _memory_integration_loop(self, interval: float = _MEMORY_INTEGRATE_INTERVAL) -> None:
        """后台循环：记忆碎片超过阈值时由 AI 蒸馏合并，并删除已蒸馏的旧条目。"""
        while True:
            try:
                await self._integrate_memories()
            except Exception as e:
                console.dim(f"[记忆整合] 检查出错（不影响运行）：{e}")
            await asyncio.sleep(interval)

    async def _integrate_memories(self) -> None:
        """AI 自动蒸馏整库记忆：蒸馏成功才删除旧碎片，失败保留原记忆。"""
        if not (self.cfg.MEMORY_ENABLED and self.butler is not None
                and self.mm is not None):
            return
        try:
            files = self.mm.list_files()
        except Exception as e:
            console.dim(f"[记忆整合] 读取记忆库失败：{e}")
            return
        if len(files) < _MEMORY_INTEGRATE_THRESHOLD:
            return
        console.dim(f"[记忆整合] 记忆碎片达 {len(files)} 条，开始 AI 蒸馏整合...")
        entries = await self.butler.integrate_memories(files)
        if not entries:
            console.dim("[记忆整合] 无可用蒸馏结果，保留原记忆")
            return
        # 删除前备份：蒸馏合并为破坏性操作，留一份快照便于回滚
        try:
            backup_path = os.path.join(
                self.cfg.PROJECT_ROOT, "data",
                f"backup_memories_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(files, f, ensure_ascii=False, indent=1)
            console.dim(f"[记忆整合] 已备份 -> {backup_path}")
        except OSError as e:
            console.dim(f"[记忆整合] 备份失败（继续整合）：{e}")
        ids = [str(f.get("id")) for f in files if f.get("id")]
        deleted = self.mm.delete_memories(ids)
        result = await self.mm.commit_recall_files(entries)
        written = len(result.get("recall_files") or [])
        console.dim(f"[记忆整合] 完成：删除 {deleted} 条碎片，"
                    f"写入 {written} 条新记忆（当前共 {self.mm.count()} 条）")

    # ---------- 输入等待：事件驱动心跳 + 输入监听 ----------

    async def _wait_input(self, show_prompt: bool = True) -> str:
        """事件驱动输入等待：等待用户输入，互动/弹幕回复结束时做一次心跳检查。

        主动对话 / 弹幕回复在播报期间，本方法收到的任何键盘 / 语音识别输入
        会被**直接丢弃**（不返回给主循环、也不缓存）——确保「说话期间
        不接收任何信息」。
        """
        loop = asyncio.get_running_loop()
        if show_prompt:
            print("你 > ", end="", flush=True)
        if self._pending_stdin_fut is not None and not self._pending_stdin_fut.done():
            input_fut = self._pending_stdin_fut
        else:
            input_fut = loop.run_in_executor(None, lambda: input(""))
        if self.proactive is None and self.stt_engine is None:
            # 未启用主动对话/语音识别：纯阻塞等待（与原行为一致）
            return await input_fut
        stt_fut = self.stt_engine.result_future() if self.stt_engine is not None else None
        wake_task = None
        speak_task = None
        while True:
            pending = {input_fut}
            if stt_fut is not None:
                pending.add(stt_fut)
            if self.proactive is not None:
                # 事件驱动心跳 + 单次精确唤醒（无周期轮询）：平时等「互动结束
                # 事件」或「下一个有意义时刻」（无聊/孤独到阈值、话题超时）的
                # 精确超时，到点只醒一次做心跳，而不是固定间隔轮询。
                if wake_task is None or wake_task.done():
                    wake_task = asyncio.create_task(self.proactive._wakeup.wait())
                pending.add(wake_task)
                # 主动发言播报中：挂一个「播完事件」精确唤醒（无周期轮询），
                # 播完只需重新等待输入，不触发心跳（防刚说完立刻又接一条）
                if (not self.proactive._speak_done.is_set()
                        and (speak_task is None or speak_task.done())):
                    speak_task = asyncio.create_task(
                        self.proactive._speak_done.wait())
                    pending.add(speak_task)
            timeout = (self.proactive.next_wake_in()
                       if self.proactive is not None else None)
            done, _ = await asyncio.wait(
                pending, timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED)

            if wake_task is not None and wake_task in done:
                # 事件触发（互动结束）：重置事件并立即心跳
                self.proactive._wakeup.clear()
                print()
                try:
                    await self.proactive.heartbeat()
                except Exception as e:
                    import traceback as _tb
                    console.error(
                        f"主动对话心跳出错：{e}\n"
                        f"{''.join(_tb.format_exception(type(e), e, e.__traceback__))}")
                print("你 > ", end="", flush=True)
                continue

            if not done:
                # 静默期「开口机会」时刻到点（单次精确唤醒）：做一次心跳，
                # 由主模型自主决定此刻想不想说话（想说就开口，不想说保持
                # 沉默）。heartbeat 内部自带忙碌抑制/弹幕避让/话题保护，
                # 不会乱开口。
                print()
                try:
                    await self.proactive.heartbeat()
                except Exception as e:
                    import traceback as _tb
                    console.error(
                        f"主动对话心跳出错：{e}\n"
                        f"{''.join(_tb.format_exception(type(e), e, e.__traceback__))}")
                print("你 > ", end="", flush=True)
                continue

            if speak_task is not None and speak_task in done:
                # 主动播报结束：重新等待输入（speak_done 已置位，下次不会再挂）
                continue

            if input_fut in done:
                text = input_fut.result()
                # ---- 拒收：主动 / 弹幕正在播报 → 丢本条，换新 input_fut 继续等 ----
                if is_rejecting_input():
                    if text in ("/quit", "/exit", "/q"):
                        # 停止命令穿透拒收：播报中也能优雅退出。否则控制中心
                        # 点停止发的 /quit 被丢弃，30 秒窗口耗尽被强杀，记忆
                        # 归档全部丢失。
                        return text
                    if text.startswith("!"):
                        # 控制中心热更新命令（!config/!tts/!tools/!model 等）：
                        # 不是用户发言，即使正在播报也照常执行，避免命令被吞
                        try:
                            await self._dispatch(text)
                        except Exception as e:
                            console.error(f"[控制中心] 命令执行失败：{e}")
                    else:
                        console.dim("[输入丢弃] 正在回复弹幕 / 主动说话，忽略本次键盘输入")
                    input_fut = loop.run_in_executor(None, lambda: input(""))
                    continue
                if self.proactive is not None:
                    # 用户输入优先：丢弃排队中的主动消息，只保留正在播报的
                    self.proactive.discard_pending()
                self._input_source = "text"
                return text

            if stt_fut is not None and stt_fut in done:
                text, _ = stt_fut.result()
                stt_fut = self.stt_engine.result_future()  # 拿下一个识别结果
                if not text:
                    continue
                # ---- 拒收：主动 / 弹幕正在播报 → 丢本条，继续等 ----
                if is_rejecting_input():
                    console.dim("[输入丢弃] 正在回复弹幕 / 主动说话，忽略本次语音输入")
                    continue
                if self.proactive is not None:
                    # 用户输入优先：丢弃排队中的主动消息
                    self.proactive.discard_pending()
                print(f"[语音识别] {text}", flush=True)
                self._input_source = "voice"
                return text

            if self.proactive is None:
                continue
            # 单次精确唤醒到点：做一次心跳（可能触发主动发言），随后重新计算
            # 下一次唤醒时刻
            print()
            try:
                await self.proactive.heartbeat()
            except Exception as e:
                import traceback as _tb
                console.error(
                    f"主动对话心跳出错：{e}\n"
                    f"{''.join(_tb.format_exception(type(e), e, e.__traceback__))}")
            print("你 > ", end="", flush=True)

    # ---------- 弹幕回复：_chat_danmaku + 队列循环 + 启停辅助 ----------

    async def _chat_danmaku(self, items) -> None:
        """弹幕精选回复：走完整的 LLM→TTS→字幕→口型→记忆链路。

        items: [(uid, username, text), ...]；单条弹幕 = 1 个元素（与原来完全一致），
        多条 = 高密度批量聚合回复（"观众 A 说 X，观众 B 说 Y，你怎么看"），
        走一次完整对话，避免逐条回复刷屏。

        注意：到达这里的弹幕已经过 ProfanityFilter（data/profanity.txt）过滤，
        命中词库的弹幕在 _on_danmaku 入口就被丢弃（不显示、不回复）。
        所以这里直接用原文，不做任何替换/过滤。

        若当前已有播报在进行（主动对话 / 用户对话 / 弹幕），本条弹幕
        直接丢弃、不排队等锁——播放结束后才重新接受新的弹幕，避免
        「主动播完立刻又接弹幕」的连续开口。
        """
        cfg = self.cfg
        # 外部不可信文本（弹幕/昵称）先净化：防 prompt 注入标签与控制字符
        # 进入 LLM prompt / 记忆库 / 前端展示（见 src/utils/safe_text.py）
        items = [(uid, sanitize_external(nick) or "匿名", sanitize_external(t))
                 for uid, nick, t in items]
        # 主弹幕：picker 已按分数降序排列，items[0] 为最高分那条，用于展示/记忆归位
        uid, username, text = items[0]
        extra = len(items) - 1

        # 正在播报中（锁被占用）：丢弃本条，播放完才接受新弹幕。
        # 注意：_on_pick 已置位「弹幕回复待播报」标记，此处须按情况清除——
        # 锁被另一条弹幕占用：其播完的 finally 会清 pending，这里不能抢清
        # （否则正在播报的弹幕还没说完，主动对话就恢复触发）；
        # 锁被主动/用户占用：本条被跳过后无人再清 pending，必须立即清除，
        # 否则主动对话被永久静音。
        if get_output_lock().locked():
            console.dim(f"[弹幕] 正在播报，跳过本条（{username}：{text}）")
            if get_output_owner() != "danmaku":
                set_danmaku_pending(False)
            return

        # 通过跳过检查、真正开始回复：左栏「对话」显示本条弹幕（被跳过的
        # 弹幕不显示），同时推 SSE 让前端卡片展示
        # （被跳过/未真正回复的弹幕不上卡片，只留在左侧实时流）
        if extra:
            shown = f"{text}（等{extra}条弹幕）"
        else:
            shown = text
        console.chat(f"精彩弹幕：{shown}")
        # 事件总线：观众弹幕进入内核
        await bus.emit(EV_USER_INPUT, InputEvent(
            source="barrage", content=shown, sender=username,
            metadata={"uid": uid, "extra": extra}))
        if self.bili_svc is not None:
            try:
                self.bili_svc.broadcast({
                    "type": "reply",
                    "username": username,
                    "text": shown,
                    "uid": uid,
                })
            except Exception:
                pass

        if extra:
            # 批量回复：多条弹幕并进一个 prompt，AI 合并回应（不逐条念）
            danmaku_lines = "\n".join(
                f"- 观众{nick}：{t}" for _, nick, t in items)
            wrapped = (
                f"[系统提示] 现在你在直播间，刚收到几条观众弹幕，请合并回应"
                f"（不要逐条念，抓住共同话题自然地回一句）。\n{danmaku_lines}\n"
                f"请用 stream-chat 技能的直播闲聊风格回复（先回应情绪或接话，"
                f"一句话说完即可，不要连续追问）。"
            )
        else:
            wrapped = (
                f"[系统提示] 现在你在直播间，收到观众弹幕请自然地回一句。"
                f"观众昵称：{username}，弹幕内容：\n{text}\n"
                f"请用 stream-chat 技能的直播闲聊风格回复（先回应情绪或接话，"
                f"一句话说完即可，不要连续追问）。"
            )

        # 插件钩子：onUserInput（source="barrage"，可改写 prompt / 注入背景 / 拦截回复）
        if self.plugin_manager is not None:
            event = UserInputEvent(wrapped, "barrage")
            await self.plugin_manager.run_user_input_hooks(event)
            if event.prevented:
                console.dim("[插件] 弹幕回复被插件拦截")
                # 本条跳过且无人再清「弹幕回复待播报」标记，必须立即清除
                set_danmaku_pending(False)
                return
            wrapped = event.text
            if event.contexts:
                self.brain.push_turn_context(event.contexts)

        # 主动引擎：有观众说话也算"有人互动"——清 0 一下孤独/无聊
        if self.proactive is not None:
            try:
                self.proactive.on_user_message()
            except Exception:
                pass

        # —— 每轮对话性能埋点 ——
        turn_tracker = PerfTracker(f"弹幕回复@{username}")
        turn_tracker.begin("端到端")

        # 弹幕内容也推送到字幕网页（打字机气泡）：多条时合并一行
        try:
            self.sub.push("user", " ｜ ".join(
                f"@{nick}：{t}" for _, nick, t in items))
        except Exception:
            pass

        _turn_pairs = [(nick, t) for _, nick, t in items]

        async def _on_llm_done(reply_text: str) -> None:
            if not (cfg.MEMORY_ENABLED and self.butler and self.mm):
                return
            try:
                # 先取快照：避免把本次新增的弹幕轮次混进上下文
                prev_turns = self.mm.recent_turns[:]
                for nick, t in _turn_pairs:
                    self.mm.add_turn("user", f"[弹幕@{nick}] {t}",
                                     source=SOURCE_DANMAKU_INPUT)
                self.mm.add_turn(ROLE_AI_ALIAS, reply_text,
                                 source=SOURCE_DANMAKU_REPLY)
                user_msgs = [{"role": "user",
                              "content": f"[弹幕@{nick}] {t}"}
                             for nick, t in _turn_pairs]
                await self.butler.submit_extract_and_store(
                    user_msgs + [{"role": "assistant",
                                  "content": reply_text}],
                    prev_turns,
                )
            except Exception as e:
                console.dim(f"[ButlerAgent] 弹幕记忆提取出错：{e}")
            # 自我进化：复盘走管家模型（agent 配置），后台执行不阻塞弹幕播报
            if self.evolution is not None:
                try:
                    asyncio.create_task(self.evolution.maybe_review(
                        self.mm.recent_turns, proactive=self.proactive))
                except Exception:
                    pass

        try:
            # 全局输出互斥（owner="danmaku"）+ 不带打断监听的 stream.converse：
            # - 锁防止用户/主动并发抢占；
            # - owner 标记让 _wait_input 丢弃此间到达的输入；
            # - 无打断监听保证内部不会自己跳。
            output_lock = get_output_lock()
            acquired = False
            async with output_lock:
                acquired = True
                set_output_owner("danmaku")
                set_global_state(STATE_AI_SPEAKING)
                # 复位残留打断标志：用户对话中途被打断后 _interrupted 仍为 True，
                # 若不复位，本条弹幕回复的句子会被 speak()/_pump 全部丢弃，
                # 表现为「弹幕回复播放不完整（甚至完全无声）」。
                if self.tts is not None:
                    try:
                        self.tts.clear_interrupt()
                    except Exception:
                        pass
                await stream.converse(
                    self.brain, wrapped, tts=self.tts, face=self.face, sub=self.sub,
                    profanity_filter=self.pf,
                    profanity_filter_rate=cfg.PROFANITY_FILTER_RATE,
                    on_llm_done=_on_llm_done if cfg.MEMORY_ENABLED else None,
                )
                # 左栏对话换行：回复句子是连续流（无换行），收尾补一个，
                # 避免与下一条弹幕/发言粘连成一行
                console.chat()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            console.error(f"弹幕回复出错：{e}")
        finally:
            # 兜底恢复：真正拿到锁才还原 owner/状态（避免覆盖并发说话者）；
            # 「弹幕回复待播报」标记无论是否开场都要清除（含等锁期间取消），
            # 否则主动对话会被永久静音。
            if acquired:
                set_output_owner(None)
                set_global_state(STATE_IDLE)
            set_danmaku_pending(False)

        turn_tracker.end("端到端")
        turn_tracker.print_report()

    async def _danmaku_reply_loop(self, q) -> None:
        """后台协程：从弹幕队列取精选（单条或批量），走 _chat_danmaku 完整对话链路。"""
        while True:
            try:
                items = await q.get()
                await self._chat_danmaku(items)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                console.error(f"[弹幕] 回复出错：{e}")
                await bus.emit(EV_ERROR, ErrorEvent(
                    code=ErrorCode.INTERNAL_ERROR.value,
                    code_name="INTERNAL_ERROR", msg=f"[弹幕] 回复出错：{e}"))

    def _stop_bili(self):
        """关闭弹幕服务；返回 reply_task cancel 协程或 None。"""
        cancel_coro = None
        if self.danmaku_picker is not None:
            try:
                self.danmaku_picker.stop()
                from src.danmaku.bili_danmaku import set_danmaku_picker
                set_danmaku_picker(None)
            except Exception as e:
                console.dim(f"[弹幕] 挑选器停止失败（不影响整体退出）：{e}")
        if self.bili_svc is not None:
            try:
                self.bili_svc.stop()
            except Exception as e:
                console.dim(f"[弹幕] 服务停止失败（不影响整体退出）：{e}")
        if self.danmaku_reply_task is not None and not self.danmaku_reply_task.done():
            self.danmaku_reply_task.cancel()
            cancel_coro = self.danmaku_reply_task
        return cancel_coro

    async def _start_bili(self):
        """启动 B 站弹幕服务（多房间）+ DanmakuPicker + 回复协程。"""
        cfg = self.cfg
        room_ids = cfg.BILI_ROOM_IDS or (
            [cfg.BILI_ROOM_ID] if cfg.BILI_ROOM_ID else [])
        if not (cfg.BILI_ENABLED and room_ids):
            if cfg.BILI_ENABLED and not room_ids:
                console.dim("[弹幕] BILI_ENABLED=true 但未配置房间号"
                            "（BILI_ROOM_ID/BILI_ROOM_IDS），不启用弹幕精选回复")
            return None, None, None
        try:
            from src.danmaku.bili_danmaku import (
                BiliServiceManager, DanmakuPicker, set_danmaku_picker,
            )
            from src.danmaku.client import bili_loop
        except Exception as e:
            console.warn(f"[弹幕] 模块导入失败：{e}")
            return None, None, None

        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        loop = asyncio.get_running_loop()
        html_path = os.path.join(cfg.PROJECT_ROOT, "ui", "弹幕卡片.html")
        mgr = BiliServiceManager(room_ids, cfg.BILI_SERVER_PORT, html_path)
        # 注入 client.py 的线程启动函数：缺失会导致 BiliService.start()
        # 抛 "BiliService.set_client_starter 未调用"（多房间改造遗漏的注入点）
        mgr.attach_client_starter(bili_loop)
        mgr.start()

        def _enqueue(items: list) -> None:
            """把一条/批量精选弹幕送入回复队列。"""
            try:
                # 弹幕回复已敲定：置标记，主动对话在此期间避让不抢话
                set_danmaku_pending(True)
                loop.call_soon_threadsafe(queue.put_nowait, items)
            except Exception as e:
                console.error(f"[弹幕] 入队失败：{e}")

        def _on_pick(uid: int, username: str, text: str) -> None:
            _enqueue([(uid, username, text)])

        def _on_batch_pick(items: list) -> None:
            _enqueue(items)

        picker = DanmakuPicker(_on_pick, on_batch_reply_callback=_on_batch_pick)
        set_danmaku_picker(picker)
        task = asyncio.create_task(
            self._danmaku_reply_loop(queue),
            name="danmaku_reply_loop",
        )
        return picker, mgr, task

    # ---------- 可打断对话：用户自己说话 ----------

    async def _interruptible_converse(self, text, on_llm_done=None,
                                       profanity_filter=None,
                                       profanity_filter_rate: float = 0.7) -> tuple:
        """用户对话期间同时监听键盘/语音输入：到达即打断当前输出。

        仅用户自己说话时运行：打断只能是用户打断自己；主动 / 弹幕无法并发
        抢占（锁保护）。
        """
        tts = self.tts
        face = self.face
        sub = self.sub
        stt = self.stt_engine
        loop = asyncio.get_running_loop()
        console.chat()  # 回复起始换行
        output_lock = get_output_lock()
        async with output_lock:
            set_output_owner("user")
            set_global_state(STATE_AI_SPEAKING)
            try:
                if tts is not None:
                    try:
                        tts.clear_interrupt()  # 新一轮输出：复位上一轮打断标志
                    except Exception:
                        pass
                conv = asyncio.create_task(
                    stream.converse(self.brain, text, tts=tts, face=face, sub=sub,
                                    profanity_filter=profanity_filter,
                                    profanity_filter_rate=profanity_filter_rate,
                                    on_llm_done=on_llm_done))
                input_fut = loop.run_in_executor(None, lambda: input(""))
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
                            input_fut = loop.run_in_executor(None, lambda: input(""))
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
                        if speech_seconds <= self.cfg.STT_INTERRUPT_MIN_SECONDS:
                            console.dim(
                                f"[打断] 语音过短（{speech_seconds:.1f}s），"
                                "不打断当前播报")
                            continue
                        got_input = True
                    if got_input:
                        # 打断：立即闭嘴 → 取消 LLM 流
                        console.dim("[打断] 用户输入/语音打断当前播报")
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
                        if stt_fut is not None and not stt_fut.done():
                            stt_fut.cancel()
                        if input_fut is not None and input_fut in done:
                            return True, buzz, None
                        print(f"[语音识别] {buzz}", flush=True)
                        return True, buzz, None
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
                set_output_owner(None)
                set_global_state(STATE_IDLE)

    # ---------- 控制中心命令热更新：_dispatch ----------

    async def _speak_memory_reply(self, text: str) -> None:
        """记忆指令确认播报：控制台 + 字幕 + TTS（走全局输出锁，互斥不打断）。"""
        if self.tts is None:
            # 无 TTS：直接推整句字幕（没有播放时机，逐字推进无从谈起）
            if self.sub is not None:
                self.sub.push("text", text)
            console.ok(text)
            return
        # 有 TTS：字幕交给 TTS 引擎的字幕管线逐字推进，此处不预推整句，
        # 否则整句会先于语音出现、覆盖逐字浮现效果。
        output_lock = get_output_lock()
        async with output_lock:
            set_output_owner("command")
            set_global_state(STATE_AI_SPEAKING)
            try:
                self.tts.clear_interrupt()
                await self.tts.speak(text)
                await self.tts.drain()
            finally:
                set_output_owner(None)
                set_global_state(STATE_IDLE)
                # 说完后清字幕（前端据此 3 秒后淡出）
                if self.sub is not None:
                    self.sub.push("clear", "")

    # ---------- Mindcraft 双向桥（socket.io 连接 MindServer） ----------

    async def _mindcraft_loop(self) -> None:
        """后台循环：保持与 MindServer 的连接，失败自动重试（引擎可能后启动）。"""
        bridge = self.mindcraft_bridge
        if bridge is None:
            return
        while True:
            if not bridge.connected:
                try:
                    await bridge.connect()
                    console.ok(
                        f"[Mindcraft] 已连接 MindServer（{bridge.server_url}，"
                        f"bot={bridge.agent_name}）")
                except Exception as e:
                    console.dim(f"[Mindcraft] 连接 MindServer 失败：{e}（稍后重试）")
            await asyncio.sleep(5)

    async def _on_mindcraft_bot_output(self, message: str) -> None:
        """MC bot 回复到达：走全局输出锁朗读（不打断 AI 说话）。"""
        await self._speak_bot_reply(message)

    async def _speak_bot_reply(self, text: str) -> None:
        """朗读 MC bot 的回复：输出锁互斥，播报期间通知 bot 暂停自说自话。"""
        text = (text or "").strip()
        if not text:
            return
        bridge = self.mindcraft_bridge
        output_lock = get_output_lock()
        async with output_lock:
            set_output_owner("mindcraft")
            set_global_state(STATE_AI_SPEAKING)
            try:
                if bridge is not None and bridge.connected:
                    try:
                        await bridge.set_tts_playing(True)
                    except Exception:
                        pass
                console.ok(f"[MC机器人] {text}")
                if self.tts is not None:
                    self.tts.clear_interrupt()
                    await self.tts.speak(text)
                    await self.tts.drain()
                elif self.sub is not None:
                    # 无 TTS：直接推整句字幕
                    self.sub.push("text", text)
            finally:
                if bridge is not None and bridge.connected:
                    try:
                        await bridge.set_tts_playing(False)
                    except Exception:
                        pass
                set_output_owner(None)
                set_global_state(STATE_IDLE)
                # 说完后清字幕（前端据此 3 秒后淡出）
                if self.sub is not None:
                    self.sub.push("clear", "")

    async def _handle_memory_command(self, cmd: str) -> bool:
        """/memory 子命令：list 列出｜del <id>... 删除｜clear 清空｜decay 衰减。"""
        parts = cmd.split()
        sub = parts[1] if len(parts) > 1 else ""
        mm = self.mm
        if mm is None:
            console.dim("记忆系统不可用")
            return True
        if sub == "list":
            files = mm.list_files(limit=200)
            console.header("记忆列表")
            if not files:
                console.dim("暂无记忆（多和 E.V 聊聊天，会话结束会自动蒸馏）")
            for f in files:
                console.kv(str(f.get("id") or "-")[:16],
                           f"{f.get('name') or ''}｜{(f.get('content') or '')[:60]}")
            return True
        if sub == "del" and len(parts) >= 3:
            ids = [p for p in parts[2:] if p]
            deleted = await mm.delete_memories_async(ids)
            console.ok(f"已删除 {deleted} 条记忆")
            await self._speak_memory_reply(f"已经删除 {deleted} 条记忆")
            return True
        if sub == "clear":
            mm.clear_all()
            console.ok("已清空全部记忆")
            await self._speak_memory_reply("已经清空全部记忆")
            return True
        if sub == "decay":
            n = await asyncio.to_thread(memory.decay_stale_memories)
            console.ok(f"记忆衰减完成，清理 {n} 条")
            return True
        console.dim("用法：/memory list ｜ /memory del <id>... ｜ "
                    "/memory clear ｜ /memory decay")
        return True

    # ---------- _dispatch 命令注册表 handler ----------

    def _validate_cmd_path(self, raw: str) -> Optional[str]:
        """控制中心路径命令白名单：解析后必须位于项目根目录内，防目录穿越。

        返回规范化后的安全绝对路径；路径为空、越界（../ 逃逸、跨盘符）
        或非法时返回 None。项目内模型/TTS 参考音频均在项目目录下，
        正常用法不受影响。
        """
        if not raw or not raw.strip():
            return None
        try:
            base = os.path.abspath(self.cfg.PROJECT_ROOT)
            target = os.path.abspath(os.path.join(base, raw))
            if os.path.commonpath([base, target]) == base:
                return target
        except ValueError:
            # 跨盘符（如 base 在 C:、target 在 D:）commonpath 抛异常
            pass
        return None

    async def _cmd_model(self, cmd: str) -> bool:
        """!model <path> 桌宠模式热切换模型。"""
        new_path = cmd[len("!model "):].strip()
        if self._validate_cmd_path(new_path) is None:
            console.error(f"非法模型路径（仅允许项目目录内）：{new_path}")
            return True
        if self.pet_widget is not None:
            if self.pet_widget.switch_model(new_path):
                console.ok(f"已热切换桌宠模型：{new_path}")
            else:
                console.error(f"模型切换失败：{new_path}（文件不存在）")
        else:
            console.dim("已收到模型切换指令（当前非桌宠模式，忽略）")
        return True

    async def _cmd_clean(self, cmd: str) -> bool:
        """!clean 资源清理（运行时内存 + 临时文件）。"""
        from src.utils import cleaner
        cleaner.cleanup_runtime_memory(verbose=True)
        cleaner.cleanup_temp_files(verbose=True)
        return True

    async def _cmd_plugins(self, cmd: str) -> bool:
        """!plugins 插件管理：list / sync / reload <name> / enable|disable <相对路径>。"""
        mgr = self.plugin_manager
        if mgr is None:
            console.dim("插件系统未启用")
            return True
        parts = cmd.split()
        sub = parts[1] if len(parts) > 1 else "list"
        if sub == "list":
            entries = mgr.get_plugin_list()
            if not entries:
                console.dim("当前没有已加载的插件")
            else:
                console.header("插件列表")
                for it in entries:
                    console.kv(it["name"], f"{it['displayName']} v{it['version']}")
            return True
        if sub == "sync":
            await mgr.sync_enabled_plugins()
            console.ok("插件启用列表已同步（热加载/卸载完成）")
            return True
        if sub == "reload" and len(parts) >= 3:
            try:
                await mgr.reload(parts[2])
            except Exception as e:
                console.error(f"插件热重载失败（{parts[2]}）：{e}")
            return True
        if sub in ("enable", "disable") and len(parts) >= 3:
            try:
                result = await mgr.apply_enabled(parts[2], sub == "enable")
            except OSError as e:
                console.error(f"插件启停失败：{e}")
                return True
            console.ok(result)
            return True
        console.dim("用法：!plugins list ｜ !plugins sync ｜ !plugins reload <name> ｜ "
                    "!plugins enable|disable <相对路径>")
        return True

    async def _cmd_tools(self, cmd: str) -> bool:
        """!tools 工具 / MCP 配置热更新。"""
        from src.utils import config as cfg_mod
        from plugins.tools import get_merged_tools
        cfg_mod.reload_tool_runtime()
        if cfg_mod.cfg.MCP_ENABLED and cfg_mod.cfg.TOOLS_ENABLED:
            if self.mcp is not None:
                await self.mcp.stop()
                self.mcp.is_enabled = True
                self.mcp.load_mcp_config()
                await self.mcp.start_all_servers()
            else:
                self.mcp = MCPManager()
                await self.mcp.initialize()
                self.brain.mcp = self.mcp
        else:
            if self.mcp is not None:
                await self.mcp.stop()
                self.mcp = None
                self.brain.mcp = None
        merged = get_merged_tools(self.mcp)
        if merged:
            names = [t["function"]["name"] for t in merged]
            console.ok(
                f"工具配置已热更新（{len(names)} 个）：{'、'.join(names)}")
        else:
            console.warn("工具配置已热更新：当前无可用工具（纯对话模式）")
        return True

    async def _cmd_reload_config(self, cmd: str) -> bool:
        """!config 统一配置热更新（LLM / 主动对话 / 内容过滤 / 记忆 / 弹幕 / 桌宠 / 情绪）。"""
        from src.utils import config as cfg_mod
        cfg_mod.reload_config()
        # LLM 客户端重建
        self.brain.reload_client()
        # 主动对话热启停
        if cfg_mod.cfg.PROACTIVE_ENABLED and self.proactive is None:
            self.proactive = ProactiveEngine(
                brain=self.brain, tts=self.tts, face=self.face, sub=self.sub,
                cfg=cfg_mod.cfg,
                butler=self.butler if cfg_mod.cfg.MEMORY_ENABLED else None,
                memory_manager=self.mm if cfg_mod.cfg.MEMORY_ENABLED else None,
                profanity_filter=self.pf,
                profanity_filter_rate=cfg_mod.cfg.PROFANITY_FILTER_RATE,
            )
            console.ok("主动对话已热启用")
        elif not cfg_mod.cfg.PROACTIVE_ENABLED:
            self.proactive = None
            console.ok("主动对话已热关闭")
        # 内容过滤热重建
        if cfg_mod.cfg.PROFANITY_FILTER_ENABLED and self.pf is None:
            self.pf = ProfanityFilter()
            console.ok("内容过滤已热启用")
        elif not cfg_mod.cfg.PROFANITY_FILTER_ENABLED:
            self.pf = None
            console.ok("内容过滤已热关闭")
        # 记忆管家热重建
        if cfg_mod.cfg.MEMORY_ENABLED and self.butler is None:
            self.butler = ButlerAgent()
            console.ok("记忆系统已热启用")
        elif not cfg_mod.cfg.MEMORY_ENABLED:
            self.butler = None
            console.ok("记忆系统已热关闭")
        # B 站弹幕热重建
        was_on = self.bili_svc is not None
        room_ids = cfg_mod.cfg.BILI_ROOM_IDS or (
            [cfg_mod.cfg.BILI_ROOM_ID] if cfg_mod.cfg.BILI_ROOM_ID else [])
        want_on = bool(cfg_mod.cfg.BILI_ENABLED and room_ids)
        if was_on:
            cancel = self._stop_bili()
            self.danmaku_picker, self.bili_svc, self.danmaku_reply_task = None, None, None
            if cancel is not None:
                try:
                    await cancel
                except (asyncio.CancelledError, Exception):
                    pass
            if not want_on:
                console.ok("B 站弹幕服务已热关闭")
        if want_on:
            self.danmaku_picker, self.bili_svc, self.danmaku_reply_task = await self._start_bili()
            if self.bili_svc is not None:
                console.ok(
                    f"B 站弹幕服务已热启用（房间 {'、'.join(str(r) for r in room_ids)}）")
        # 桌宠窗口置顶/尺寸/待机动作热应用
        if self.pet_widget is not None:
            self.pet_widget.apply_config(cfg_mod.cfg)
        # 情绪映射热重载（仅桌宠模式生效）：actor 常驻（手动命令/试播始终可用），
        # 映射文件变化即时生效；自动分类播放由调用点按 EMOTION_ACTOR_ENABLED 控制
        if self.emotion_actor is not None:
            self.emotion_actor.load_map()
        console.ok("配置已全部热更新（立即生效，无需重启）")
        return True

    async def _cmd_stt(self, cmd: str) -> bool:
        """!stt 语音识别热更新。"""
        from src.utils import config as cfg_mod
        cfg_mod.reload_tool_runtime()
        if cfg_mod.cfg.STT_ENABLED:
            if self.stt_engine is not None:
                self.stt_engine.stop()
                self.stt_engine = None
            try:
                from src.asr.stt import STTEngine
                self.stt_engine = STTEngine(cfg_mod.cfg)
                self.stt_engine.start()
                console.ok(
                    "语音识别已开启：对着麦克风说话即可输入"
                    f"（{cfg_mod.cfg.STT_MODEL}）")
            except Exception as e:
                console.warn(f"语音识别启动失败：{e}")
        elif self.stt_engine is not None:
            self.stt_engine.stop()
            self.stt_engine = None
            console.ok("语音识别已关闭")
        return True

    async def _cmd_tts_audio(self, cmd: str) -> bool:
        """!tts_audio <path> 主参考音频热更新。"""
        if self.tts is None:
            return True
        new_audio = cmd[len("!tts_audio "):].strip()
        # 空串 = 清空参考音频（合法）；非空须在项目目录内
        if new_audio and self._validate_cmd_path(new_audio) is None:
            console.error(f"非法音频路径（仅允许项目目录内）：{new_audio}")
            return True
        self.tts.apply_ref(new_audio, self.tts.ref_text)
        if new_audio:
            console.ok(f"已热更新 TTS 参考音频：{new_audio}")
        else:
            console.warn("TTS 参考音频已清空，语音合成已关闭")
        return True

    async def _cmd_tts_text(self, cmd: str) -> bool:
        """!tts_text <text> 主参考文本热更新。"""
        if self.tts is None:
            return True
        new_text = cmd[len("!tts_text "):].strip()
        # 只更新文本：沿用主参考原始串（ref_audio 可能是主+辅助的合成 dict）
        self.tts.apply_ref(self.tts._ref_main, new_text)
        console.ok(f"已热更新 TTS 参考音频文本：{new_text}")
        return True

    async def _cmd_tts_audios(self, cmd: str) -> bool:
        """!tts_audios <path> 辅助参考音频热更新。"""
        if self.tts is None:
            return True
        new_extras = cmd[len("!tts_audios "):].strip()
        # 空串 = 清空辅助参考（合法）；非空时逐条校验须在项目目录内
        if new_extras and any(
                self._validate_cmd_path(p) is None
                for p in new_extras.split("|")):
            console.error("非法辅助音频路径（仅允许项目目录内），已忽略本次更新")
            return True
        self.tts.apply_ref_extras(new_extras)
        console.ok(f"已热更新 TTS 辅助参考音频：{new_extras}")
        return True

    def _build_command_registry(self) -> CommandRegistry:
        """构建本应用的命令注册表（在 __init__ 调一次缓存到 self）。"""
        registry = CommandRegistry()
        registry.register(
            Command("/memory", self._handle_memory_command, help="记忆管理：list/del/clear/decay"),
            Command("!model ", self._cmd_model, help="桌宠模式热切换模型"),
            Command("!clean", self._cmd_clean, exact=True, help="清理运行时内存和临时文件"),
            Command("!plugins", self._cmd_plugins, help="插件管理：list/sync/reload/enable/disable"),
            Command("!tools", self._cmd_tools, exact=True, help="工具 / MCP 配置热更新"),
            Command("!config", self._cmd_reload_config, exact=True, help="统一配置热更新"),
            Command("!stt", self._cmd_stt, exact=True, help="语音识别热启停"),
            Command("!tts_audio ", self._cmd_tts_audio, help="TTS 主参考音频热更新"),
            Command("!tts_text ", self._cmd_tts_text, help="TTS 主参考文本热更新"),
            Command("!tts_audios ", self._cmd_tts_audios, help="TTS 辅助参考音频热更新"),
        )
        return registry

    async def _dispatch(self, cmd: str) -> bool:
        """命令分发（/memory / !config / !tools / !stt / !tts_* / !model 等）。

        走 CommandRegistry 按 prefix 顺序匹配；未匹配时 emotion_actor
        的 / 开兜底。返回 True 表示已消费（不进入 LLM）。
        """
        result = await self._cmd_registry.dispatch(cmd)
        if result is not None:
            return result
        # emotion_actor 命令保留在原位置（依赖具体实例）
        if self.emotion_actor is not None and cmd.startswith("/"):
            await self.emotion_actor.handle(cmd)
            return True
        return False

    async def _init_tts_async(self) -> None:
        """后台初始化 TTS 引擎（本地模型 + 参考音频预编码约 20s），不阻塞启动流程。

        run() 中创建 TTS 引擎后立即后台启动加载（asyncio.create_task）；
        就绪前 speak/drain/stop 由引擎内部守卫静默处理，就绪后自动可用。
        """
        try:
            tts_ok = await self.tts.start()
        except Exception as e:
            console.warn(f"TTS 后台初始化异常：{e}")
            tts_ok = False
        if not tts_ok:
            self.tts = None
            return
        # 预热：合成一句短文本让服务端 CUDA graph / 缓存提前就绪，
        # 降低主播第一次真实说话时的首字延迟（失败不影响使用）
        try:
            await self.tts.warmup()
        except Exception:
            pass  # warmup 内部已捕获，这里仅兜底
        # 口型回调只在有脸部驱动器时注册：纯对话模式（无 VTS/桌宠）下
        # self.face 为 None，注册了会每块音频都报
        # 'NoneType' object has no attribute 'load_speech_curve'
        if self.face is not None:
            def _on_tts_play(wav: str, text: str, dur_s: float) -> None:
                if not self.face.load_speech_curve(wav):
                    self.face.start_speaking(dur_s)

            self.tts.set_on_play_callback(_on_tts_play)
        # 说话结束复原：表情/动作只在说话期间显示，播完（含打断）恢复默认
        if self.emotion_actor is not None:
            self.tts.set_on_play_done_callback(self._restore_emotion_after_speech)
        if self.sub:
            self.tts.set_subtitle_callback(lambda t: self.sub.push("text", t))

    def _restore_emotion_after_speech(self) -> None:
        """整段说话结束（播完或打断）→ 表情/动作复原。

        回调在播放线程触发，经主事件循环调度 actor.restore()（async 方法），
        保证与 TTS/情绪分类等主循环任务互斥。
        """
        actor, loop = self.emotion_actor, self._loop
        if actor is None or loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(actor.restore(), loop)
        except Exception as e:
            console.dim(f"表情/动作复原调度失败：{e}")

    async def _archive_session(self) -> None:
        """退出前会话归档：摘要写入档案 + 蒸馏记忆写回记忆库。

        含两轮 LLM 调用（会话摘要 + 蒸馏）。两轮互不依赖，并行执行——
        串行时停止最坏要等两轮 LLM；并行后耗时 ≈ 单轮，明显缩短停止等待。
        退出时由 _shutdown 包一层 20s 总额超时兜底，避免 LLM 完全无响应
        时退出无限等待。
        """
        turns = self.mm.recent_turns
        summary, entries = await asyncio.gather(
            self.butler.summarize_session(turns[-30:]),
            self.butler.distill_session(turns[-30:]),
        )
        if summary:
            await self.mm.update_archive_async(
                summary=summary,
                period_start=self.mm.started_at or turns[0].get("timestamp", ""),
                period_end=datetime.now().isoformat(),
            )
        if entries:
            await self.mm.commit_recall_files(entries)

    # ---------- 主运行入口 ----------

    async def run(self) -> None:
        """原 main() 的完整生命周期：初始化 → 主循环 → 资源清理。"""
        self.cfg = config.cfg
        self.cfg.validate()
        # 主事件循环引用：播放线程回调（说话结束复原）需经它调度回主循环
        self._loop = asyncio.get_running_loop()

        # 命令注册表：先建好，run 期间各 handler 内部用 self._cmd_registry.dispatch 派发
        self._cmd_registry = self._build_command_registry()

        # 启动清理上次残留临时文件
        try:
            from src.utils import cleaner
            cleaner.cleanup_temp_files(verbose=False)
        except Exception:
            pass

        # —— 渲染/驱动目标 ——
        self.pet_widget = None
        self.vts: "VTSController | None" = None
        vts_ok = False
        self.face: "FaceDriver | None" = None
        self.emotion_actor = None

        if self.cfg.RUN_MODE == "pet":
            from src.pet.widget import PetWidget, BubbleSub
            from src.pet.driver import PetFaceDriver

            console.kv("模型", f"{self.cfg.LLM_MODEL}（深度思考 {'开' if self.cfg.LLM_THINKING else '关'}）")
            console.kv("桌宠", os.path.basename(self.cfg.PET_MODEL_PATH))
            console.dim("拖动模型移动位置 | 点击模型播放动作 | 输入 /quit 退出")
            if self.cfg.EMOTION_ACTOR_ENABLED:
                console.dim("表情/动作：Embedding 情绪自动控制已启用（用户消息分类情绪播放；"
                            "也可用 /expr /motion /face list 手动控制）")
            else:
                console.dim("表情/动作：自动情绪控制未启用（/expr /motion /face list 手动控制仍可用）")

            self.pet_widget = PetWidget(self.cfg)
            self.pet_widget.show()
            # 表情/动作 actor 总是创建：手动命令与控制中心试播不依赖自动控制开关，
            # 自动分类播放由调用点按 EMOTION_ACTOR_ENABLED 开关控制
            from src.pet.emotion_actor import PetEmotionActor
            self.emotion_actor = PetEmotionActor(self.pet_widget, self.cfg)
            self.emotion_actor.scan()
            self.emotion_actor.load_map()
            self.pet_widget.on_model_loaded = lambda w: self.emotion_actor.scan()
            self.face = PetFaceDriver(self.pet_widget, self.cfg)
            self.pet_widget.attach_driver(self.face)
            self.face.start()
            if self.cfg.PET_MOTION_PATH:
                self.face.set_motion(self.cfg.PET_MOTION_PATH)
            self.sub = BubbleSub(self.pet_widget)

            # 桌宠模型热切换兜底监控
            async def _watch_pet_model_change() -> None:
                env_file = os.path.join(self.cfg.PROJECT_ROOT, ".env")
                active = str(self.cfg.PET_MODEL_PATH or "").strip()
                while True:
                    await asyncio.sleep(2.0)
                    try:
                        if not os.path.isfile(env_file):
                            continue
                        new_path = ""
                        with open(env_file, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line.startswith("PET_MODEL_PATH="):
                                    new_path = line.split("=", 1)[1].strip()
                                    new_path = new_path.strip('"').strip("'")
                                    break
                        if new_path and new_path != active:
                            active = new_path
                            if self.pet_widget.switch_model(new_path):
                                console.ok(
                                    f"检测到模型配置变更，已热切换桌宠：{new_path}")
                            else:
                                console.warn(f"模型配置变更但切换失败：{new_path}")
                    except Exception:
                        pass

            asyncio.create_task(_watch_pet_model_change())
        else:
            console.kv("模型", f"{self.cfg.LLM_MODEL}（深度思考 {'开' if self.cfg.LLM_THINKING else '关'}）")
            console.kv("VTS", f"端口 {self.cfg.VTS_PORT}")
            console.dim("AI 全权接管：自动眨眼 / 呼吸 / 身体摇摆 | 基线动画由 .motion3.json 驱动")
            console.dim("输入 /quit 退出")

            self.vts = VTSController()
            vts_ok = await self.vts.connect()
            if not vts_ok:
                console.warn("将以「纯对话」模式运行，无口型/动作/表情控制。")

            self.sub = SubtitleServer().start()
            console.dim(f"字幕网页：http://127.0.0.1:{self.sub.port}/（打字机效果，浏览器打开即可）")

        # ButlerAgent 记忆管家
        if self.cfg.MEMORY_ENABLED:
            self.butler = ButlerAgent()
        else:
            self.butler = None

        # 自我进化引擎（对话后后台复盘，随配置开关创建）
        self.evolution = EvolutionEngine() if self.cfg.EVOLUTION_ENABLED else None

        # MCP 管理器
        self.mcp = MCPManager() if (self.cfg.MCP_ENABLED and self.cfg.TOOLS_ENABLED) else None
        if self.mcp is not None:
            await self.mcp.initialize()
        from plugins.tools import get_merged_tools
        merged_tools = get_merged_tools(self.mcp)
        if merged_tools:
            names = [t["function"]["name"] for t in merged_tools]
            console.dim(f"工具已就绪（{len(names)} 个）：{'、'.join(names)}")
        else:
            console.dim("无可用工具：AI 将以纯对话模式运行（MCP 未启用且未配置搜索/天气 key）")

        # 技能系统
        from plugins.tools.skills import get_skill_manager
        skill_mgr = get_skill_manager()
        if skill_mgr.skills:
            names = [s.name for s in skill_mgr.skills]
            console.dim(f"技能已就绪（{len(names)} 个）：{'、'.join(names)}"
                        f"（load_skill 按需加载，改文件无需重启）")
        else:
            console.dim("技能目录为空：未发现技能（SKILLS_DIR/<技能名>/SKILL.md）")

        # 运行时 try/finally 保护
        self.mm: "memory.MemoryManager | None" = None
        self.stt_engine = None
        self.bili_svc = None
        self.danmaku_picker = None
        self.danmaku_reply_task = None
        self._pending_stdin_fut = None  # 交还复用的 stdin 监听
        self.plugin_manager = None      # 插件系统（服务就绪后初始化）
        try:
            # vtuber 模式：启动时模型扫描适配
            profile = None
            if vts_ok and self.vts is not None:
                profile = await scan_model(self.vts, self.cfg)
                if self.cfg.MOUTH_PARAMETER and profile.mouth_param:
                    profile.mouth_param = self.cfg.MOUTH_PARAMETER
                if profile.mouth_param:
                    self.cfg.MOUTH_PARAMETER = profile.mouth_param
                    self.cfg.MOUTH_GAIN = profile.mouth_gain

                self.face = FaceDriver(self.vts, profile)
                self.face.start()
                if self.cfg.MOTION_PATH:
                    self.face.set_motion(self.cfg.MOTION_PATH)
                elif self.cfg.VTS_IDLE_TAKEOVER and profile.idle_motion:
                    self.face.set_motion(profile.idle_motion)
                    console.ok(f"待机动画接管：{os.path.basename(profile.idle_motion)}"
                              f"（P2 覆盖 VTS 待机，循环点已平滑）")

                # 表情/动作演员（VTS 模式）：用户消息 → Embedding 情绪分类 → 播放表情/动作。
                # 总是创建：手动命令与控制中心试播不依赖自动控制开关，
                # 自动分类播放由主循环按 EMOTION_ACTOR_ENABLED 开关控制
                from src.vts.emotion_actor import VtsEmotionActor
                self.emotion_actor = VtsEmotionActor(self.vts, self.cfg, face=self.face)
                await self.emotion_actor.scan()
                self.emotion_actor.load_map()
                if self.cfg.EMOTION_ACTOR_ENABLED:
                    console.dim("表情/动作：Embedding 情绪自动控制已启用（用户消息分类情绪播放；"
                                "也可用 /expr /motion /face list 手动控制）")
                else:
                    console.dim("表情/动作：自动情绪控制未启用（/expr /motion /face list 手动控制仍可用）")

            # TTS 引擎：后台并行加载（本地模型 + 参考音频预编码约 20s），
            # 不阻塞 LLM / 记忆等后续初始化，启动即开始加载；
            # 未就绪时 speak/drain/stop 由引擎内部守卫静默处理，就绪后自动可用
            self.tts = None
            if self.cfg.GPTSOVITS_REF_AUDIO:
                self.tts = TTSEngine()
                asyncio.create_task(self._init_tts_async())

            # Mindcraft 双向桥（socket.io 连接 MindServer）：开关开启才创建，
            # 由后台循环负责连接/重连（引擎可能晚于主程序启动）。
            self.mindcraft_bridge = None
            if self.cfg.MINDCRAFT_BRIDGE_ENABLED:
                self.mindcraft_bridge = MindcraftBridge(
                    server_url=f"http://127.0.0.1:{self.cfg.MINDCRAFT_MINDSERVER_PORT}",
                    agent_name=self.cfg.MINDCRAFT_BOT_NAME,
                    on_bot_output=self._on_mindcraft_bot_output,
                )
                asyncio.create_task(self._mindcraft_loop())

            # 内容过滤（弹幕回复 / 主动对话 / 用户对话共用，需在引擎创建前就绪）
            self.pf = ProfanityFilter() if self.cfg.PROFANITY_FILTER_ENABLED else None
            if self.pf is not None:
                console.dim(f"内容过滤已启用：检测到骂人用语时 "
                            f"{self.cfg.PROFANITY_FILTER_RATE:.0%} 概率触发（替换为 Filter）")

            # 初始化记忆系统
            self.mm = memory.get_manager()
            self.mm.load()
            self.mm.new_session()
            if self.cfg.MEMORY_ENABLED:
                # 上次运行时 remember/forget 失败队列（drain 期间要等 memory service
                # 就绪，所以放后台任务里）
                async def _drain_retry_after_load() -> None:
                    from plugins.tools import memory_tools
                    # 等几帧让 mm.load() 完成 service 初始化
                    await asyncio.sleep(0.5)
                    try:
                        n = await asyncio.to_thread(memory_tools.drain_retry_queue)
                        if n:
                            console.dim(f"[记忆] 启动重放成功 {n} 条暂存记忆")
                    except Exception as e:
                        console.dim(f"[记忆] 重放失败队列失败：{e}")
                asyncio.create_task(_drain_retry_after_load())
                asyncio.create_task(memory.warmup())
                # 记忆时间衰减：后台定时清理长期未更新的非固定记忆
                asyncio.create_task(memory.decay_loop())
                # AI 自动整合蒸馏：碎片过多时后台蒸馏合并并删除旧条目
                asyncio.create_task(self._memory_integration_loop())

            self.brain = LLMBrain(mcp=self.mcp)

            # 连接预热：启动空闲期发一个最小请求，把 TLS 握手冷启动挪到后台，
            # 用户第一次提问即命中热连接（首轮 TTFT 实测 2565ms → 热连接 ~1s）
            asyncio.create_task(self.brain.warmup())

            if self.cfg.MEMORY_ENABLED:
                console.dim(f"记忆系统：已启用（{memory.count()} 个记忆文件）")

            # 主动对话引擎
            self.proactive = None
            if self.cfg.PROACTIVE_ENABLED:
                self.proactive = ProactiveEngine(
                    brain=self.brain, tts=self.tts, face=self.face, sub=self.sub, cfg=self.cfg,
                    butler=self.butler if self.cfg.MEMORY_ENABLED else None,
                    memory_manager=self.mm if self.cfg.MEMORY_ENABLED else None,
                    profanity_filter=self.pf,
                    profanity_filter_rate=self.cfg.PROFANITY_FILTER_RATE,
                )
                console.dim(
                    f"主动对话已启用：LLM 自主开口（互动/弹幕结束即给机会，"
                    f"静默期每 {self.cfg.RESPONSE_INTERVAL_MIN:.0f}~"
                    f"{self.cfg.RESPONSE_INTERVAL_MAX:.0f}s 随机给一次机会，"
                    f"是否开口由主模型自主决定）")
            else:
                console.dim("主动对话未启用（.env 设置 PROACTIVE_ENABLED=true 开启）")

            # 插件系统：加载 plugins/ 下启用的插件（Python 同进程 async 运行时，
            # 对标 live-2d 插件体系——钩子 / 工具 / 定时器，目录约定见 plugins/README.md）
            try:
                from plugins.manager import set_default_manager
                self.plugin_manager = PluginManager(self)
                await self.plugin_manager.load_all()
                await self.plugin_manager.start_all()
                # 供工具合并（get_merged_tools）与 speak_text 读取
                self.brain.plugin_manager = self.plugin_manager
                set_default_manager(self.plugin_manager)
            except Exception as e:
                self.plugin_manager = None
                console.warn(f"[插件] 插件系统初始化失败（不影响运行）：{e}")

            # 自我进化：定期自我提示（空闲期主动补复盘，后台循环）
            if self.evolution is not None:
                asyncio.create_task(self._evolution_periodic_loop())

            # 语音识别
            if self.cfg.STT_ENABLED:
                try:
                    from src.asr.stt import STTEngine
                    self.stt_engine = STTEngine(self.cfg)
                    self.stt_engine.start()
                    console.dim(
                        f"语音识别已启用：对着麦克风说话即可输入"
                        f"（{self.cfg.STT_MODEL}，静音 {self.cfg.STT_SILENCE_SECONDS:.0f}s 自动切段）")
                except Exception as e:
                    self.stt_engine = None
                    console.warn(f"语音识别启动失败（可忽略）：{e}")
            else:
                console.dim("语音识别未启用（.env 设置 STT_ENABLED=true 开启）")

            # B 站弹幕服务启动
            self.danmaku_picker, self.bili_svc, self.danmaku_reply_task = await self._start_bili()

            # VTS 模型切换事件订阅
            if vts_ok:
                _scanning = False

                async def _rescan_on_model_switch(msg: dict) -> None:
                    nonlocal _scanning
                    if not msg.get("data", {}).get("modelLoaded") or _scanning:
                        return
                    console.info("检测到模型切换，暂停伪面捕并重新扫描适配...")
                    _scanning = True
                    try:
                        await self.face.stop()
                        new_profile = await scan_model(self.vts, self.cfg)
                        if not new_profile.model_name:
                            console.warn("新模型未加载，跳过适配（保留当前状态）")
                            return
                        if self.cfg.MOUTH_PARAMETER and new_profile.mouth_param:
                            new_profile.mouth_param = self.cfg.MOUTH_PARAMETER
                        if new_profile.mouth_param:
                            self.cfg.MOUTH_PARAMETER = new_profile.mouth_param
                            self.cfg.MOUTH_GAIN = new_profile.mouth_gain
                        self.face.apply_profile(new_profile)
                        console.ok(f"已适配新模型「{new_profile.model_name}」")
                        if self.cfg.MOTION_PATH:
                            self.face.set_motion(self.cfg.MOTION_PATH)
                        elif self.cfg.VTS_IDLE_TAKEOVER:
                            if new_profile.idle_motion:
                                self.face.set_motion(new_profile.idle_motion)
                                console.ok(f"待机动画接管：{os.path.basename(new_profile.idle_motion)}")
                            else:
                                # 新模型无待机动画可接管：停止旧模型遗留的动作注入
                                self.face.stop_motion()
                        # 表情/动作演员重扫（新模型的表情/动画热键不同）
                        if self.emotion_actor is not None:
                            try:
                                await self.emotion_actor.scan()
                            except Exception as e:
                                console.dim(f"表情/动作重扫失败：{e}")
                    finally:
                        _scanning = False
                        self.face.start()

                self.vts.on_event("ModelLoadedEvent", _rescan_on_model_switch)
                if await self.vts.subscribe_event("ModelLoadedEvent"):
                    console.dim("已订阅模型切换事件：运行中切换模型将自动重新适配")

            # ===== 主循环：等待输入 → 命令或对话 =====
            quitting = False
            show_prompt = True
            while not quitting:
                try:
                    user_text = await self._wait_input(show_prompt=show_prompt)
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                self._pending_stdin_fut = None
                show_prompt = True
                while user_text:
                    user_text = user_text.strip()
                    if not user_text:
                        break
                    if user_text in ("/quit", "/exit", "/q"):
                        quitting = True
                        break
                    if await self._dispatch(user_text):
                        show_prompt = False
                        break
                    # 插件钩子：onUserInput（可注入背景上下文 / 改写消息 / 拦截不发给 AI）
                    if self.plugin_manager is not None:
                        event = UserInputEvent(user_text, "text")
                        await self.plugin_manager.run_user_input_hooks(event)
                        if event.prevented:
                            console.dim("[插件] 消息被插件拦截，未发送给 AI")
                            break
                        user_text = event.text
                        if event.contexts:
                            self.brain.push_turn_context(event.contexts)
                        if not user_text.strip():
                            break
                    # 事件总线：用户输入进入内核（键盘 / 语音识别）
                    await bus.emit(EV_USER_INPUT, InputEvent(
                        source=getattr(self, "_input_source", "text") or "text",
                        content=user_text, sender="user"))
                    # 用户发言：重置主动引擎状态
                    if self.proactive is not None:
                        self.proactive.on_user_message()
                    # 全局状态：用户输入已到达，agent 触发被抑制（忙碌避让）
                    set_global_state(STATE_USER_TALKING)

                    # Mindcraft 双向桥：已连接时把用户输入转发给 MC bot，
                    # bot 回复由桥回调朗读；本机不再走本地 LLM 对话（避免双重回答）
                    if (self.mindcraft_bridge is not None
                            and self.mindcraft_bridge.connected):
                        try:
                            await self.mindcraft_bridge.send_message(user_text)
                        except Exception as e:
                            console.dim(f"[Mindcraft] 转发用户输入失败，回退本地对话：{e}")
                        else:
                            self.sub.push("user", user_text)
                            console.dim(f"[Mindcraft] 已转发给 MC 机器人：{user_text}")
                            break

                    # 用户消息分类情绪 → 后台播放表情/动作（桌宠/VTS 模式，仅开关开启时）
                    if (self.emotion_actor is not None
                            and config.cfg.EMOTION_ACTOR_ENABLED):
                        asyncio.create_task(self.emotion_actor.handle(user_text))

                    # 每轮对话性能埋点
                    turn_tracker = PerfTracker("本轮对话")
                    turn_tracker.begin("端到端")

                    # 用户输入推送到字幕网页
                    self.sub.push("user", user_text)

                    # 注意：这里**不过滤用户输入**（按用户要求）。
                    # 内容过滤只在三处生效：AI 回复句子、观众弹幕原文、主动对话播报。
                    # 记忆也存原文（Butler 可以看到真实的骂人话帮 AI 决定应对策略）。

                    _turn_user = user_text
                    try:
                        async def _on_llm_done(reply_text: str) -> None:
                            if not (self.cfg.MEMORY_ENABLED and self.butler):
                                return
                            try:
                                self.mm.add_turn("user", _turn_user, source="user_input")
                                self.mm.add_turn(ROLE_AI_ALIAS, reply_text,
                                                 source="main_llm_reply")
                                await self.butler.submit_extract_and_store(
                                    [{"role": "user", "content": _turn_user},
                                     {"role": "assistant", "content": reply_text}],
                                    self.mm.recent_turns[:-2],
                                )
                            except Exception as e:
                                console.dim(f"[ButlerAgent] 记忆提取出错（不影响对话）：{e}")
                            # 自我进化：复盘走管家模型（agent 配置），
                            # 后台执行不阻塞对话
                            if self.evolution is not None:
                                try:
                                    asyncio.create_task(
                                        self.evolution.maybe_review(
                                            self.mm.recent_turns,
                                            proactive=self.proactive))
                                except Exception:
                                    pass

                        interrupted, buzz, pending = await self._interruptible_converse(
                            user_text,
                            on_llm_done=_on_llm_done if self.cfg.MEMORY_ENABLED else None,
                            profanity_filter=self.pf,
                            profanity_filter_rate=self.cfg.PROFANITY_FILTER_RATE,
                        )
                        self._pending_stdin_fut = pending
                    except Exception as e:
                        console.error(f"对话流程出错：{e}")
                        await bus.emit(EV_ERROR, ErrorEvent(
                            code=ErrorCode.INTERNAL_ERROR.value,
                            code_name="INTERNAL_ERROR",
                            msg=f"对话流程出错：{e}"))
                        interrupted, buzz, self._pending_stdin_fut = False, "", None
                        # 对话未正常结束：兜底复位状态，避免卡在忙碌态抑制 agent
                        set_global_state(STATE_IDLE)

                    turn_tracker.end("端到端")
                    turn_tracker.print_report()

                    if interrupted:
                        user_text = buzz
                    else:
                        break

            console.dim("再见～")
        finally:
            # 会话结束归档 + 蒸馏（蒸馏条目写回记忆库，重启后检索可带出）。
            # 归档含两轮 LLM 调用，给 20s 总额超时兜底：LLM 完全无响应时
            # 不能无限等待（否则控制中心 30 秒强杀导致 Crashed），超时跳过。
            if self.cfg.MEMORY_ENABLED and self.butler is not None and self.mm is not None:
                try:
                    await asyncio.wait_for(self._archive_session(), timeout=20)
                except Exception as e:
                    console.warn(f"会话摘要/蒸馏失败（不影响退出）：{e}")
            # 事件总线：会话结束（退出归档完成）
            await bus.emit(EV_SESSION_END, SessionEndEvent(
                turns=len(self.mm.recent_turns) if self.mm is not None else 0))

            # 清理：停插件 → 停弹幕 → 停字幕 → 排空 TTS → 关 TTS → 停面捕 → 关 VTS
            if self.plugin_manager is not None:
                try:
                    await self.plugin_manager.stop_all()
                except Exception:
                    pass
            cancel = self._stop_bili()
            self.danmaku_picker, self.bili_svc, self.danmaku_reply_task = None, None, None
            if cancel is not None:
                try:
                    await cancel
                except (asyncio.CancelledError, Exception):
                    pass
            self.sub.stop()
            if self.mindcraft_bridge is not None:
                try:
                    await asyncio.wait_for(
                        self.mindcraft_bridge.disconnect(), timeout=5)
                except Exception:
                    pass
            if self.stt_engine is not None:
                self.stt_engine.stop()
            if self.mcp is not None:
                try:
                    await asyncio.wait_for(self.mcp.stop(), timeout=15)
                except Exception:
                    pass
            if self.tts is not None:
                try:
                    # 停止时先打断播放：drain 立即返回。否则若队列里还有
                    # 音频，drain 会等它播完（最多 15s），停止明显变慢。
                    self.tts.interrupt()
                    await asyncio.wait_for(self.tts.drain(), timeout=5)
                    await asyncio.wait_for(self.tts.stop(), timeout=15)
                except Exception:
                    pass
            if self.face is not None:
                try:
                    await asyncio.wait_for(self.face.stop(), timeout=10)
                except Exception:
                    pass
            if self.vts is not None:
                try:
                    await asyncio.wait_for(self.vts.close(), timeout=10)
                except Exception:
                    pass


async def run_with_cleanup() -> None:
    """运行 Application.run()；退出（含被取消）时关记忆连接 + 清理临时文件。"""
    try:
        await Application().run()
    finally:
        try:
            await memory.aclose()
        except Exception:
            pass
        try:
            from src.utils import cleaner
            cleaner.cleanup_temp_files(verbose=False)
        except Exception:
            pass
