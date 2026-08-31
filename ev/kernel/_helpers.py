"""RuntimeContext 的业务 helper 方法（从旧 runtime.py 后半整体剥离）。

此 mixin 保存原 RuntimeContext 除 __init__ / setup / teardown / _hydrate 外的
全部方法，使 ev/kernel/runtime.py 主骨架维持精简 ≤ 400 行。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from typing import Any, Optional

from ev.utils import config, console
from ev.utils.constants import (
    ROLE_AI_ALIAS, SOURCE_DANMAKU_INPUT, SOURCE_DANMAKU_REPLY,
)
from ev.llm import stream
from ev.llm.butler_agent import ButlerAgent
from ev.llm.proactive import ProactiveEngine
from ev.utils.content_filter import ProfanityFilter
from ev.kernel.output_lock import (
    STATE_AI_SPEAKING, STATE_IDLE, get_output_lock, get_output_owner,
    set_danmaku_pending, set_output_owner, set_global_state,
)
from plugins import UserInputEvent
from ev.kernel.bus import EV_USER_INPUT, bus
from ev.kernel.error_handler import report_error
from ev.kernel.events.models import InputEvent
from ev.utils.perf_tracker import PerfTracker
from ev.utils.safe_text import sanitize_external

_MEMORY_INTEGRATE_THRESHOLD = 60
_MEMORY_INTEGRATE_INTERVAL = 2 * 3600


class RuntimeHelpersMixin:
    """所有非生命周期方法（helper / 后台 loop / 热重载 等）。"""

    # ---------- 弹幕服务启停 ----------

    def stop_bili(self):
        cancel_coro = None
        if self.danmaku_picker is not None:
            try:
                self.danmaku_picker.stop()
                from ev.danmaku.bili_danmaku import set_danmaku_picker
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

    async def start_bili(self):
        cfg = self.cfg
        room_ids = cfg.BILI_ROOM_IDS or (
            [cfg.BILI_ROOM_ID] if cfg.BILI_ROOM_ID else [])
        if not (cfg.BILI_ENABLED and room_ids):
            if cfg.BILI_ENABLED and not room_ids:
                console.dim("[弹幕] BILI_ENABLED=true 但未配置房间号"
                            "（BILI_ROOM_ID/BILI_ROOM_IDS），不启用弹幕精选回复")
            return None, None, None
        try:
            from ev.danmaku.bili_danmaku import (
                BiliServiceManager, DanmakuPicker, set_danmaku_picker,
            )
            from ev.danmaku.client import bili_loop
        except Exception as e:
            console.warn(f"[弹幕] 模块导入失败：{e}")
            return None, None, None

        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        loop = asyncio.get_running_loop()
        html_path = os.path.join(cfg.PROJECT_ROOT, "ui", "弹幕卡片.html")
        mgr = BiliServiceManager(room_ids, cfg.BILI_SERVER_PORT, html_path)
        mgr.attach_client_starter(bili_loop)
        mgr.start()

        def _enqueue(items: list) -> None:
            try:
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

        # 弹幕观察器：全量观察（只看不回）+ 刷屏规律触发回复。
        # 触发回调与优选弹幕走同一条入队/回复管线（忙碌自动避让）。
        try:
            from ev.utils import config as _obs_cfg
            if getattr(_obs_cfg.cfg, "DANMAKU_OBSERVER_ENABLED", True):
                from ev.danmaku.observer import get_observer
                observer = get_observer()
                observer.reset()

                def _enqueue_pattern(hit) -> None:
                    # bili 线程调用：忙碌避让（不抢当前播报/回复），
                    # 合成一条「系统观察」弹幕走既有管线
                    from ev.kernel.output_lock import (
                        get_output_lock, is_danmaku_pending,
                    )
                    if get_output_lock().locked() or is_danmaku_pending():
                        console.dim(
                            f"[弹幕观察] 忙碌，跳过刷屏回复"
                            f"（「{hit.pattern}」×{hit.count}）")
                        return
                    item = (0, "弹幕流观察", hit.describe())
                    console.dim(
                        f"[弹幕观察] 刷屏触发回复：「{hit.pattern}」"
                        f"×{hit.count}（{hit.users} 人）")
                    _enqueue([item])

                observer.bind_pattern_sink(_enqueue_pattern)
        except Exception as e:
            console.dim(f"[弹幕观察] 初始化失败（忽略）：{e}")

        task = asyncio.create_task(
            self._danmaku_reply_loop(queue),
            name="danmaku_reply_loop",
        )
        return picker, mgr, task

    # ---------- 弹幕回复链路 ----------

    async def _danmaku_reply_loop(self, q) -> None:
        while True:
            try:
                items = await q.get()
                await self._chat_danmaku(items)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                console.error(f"[弹幕] 回复出错：{e}")
                await report_error(e, msg=f"[弹幕] 回复出错：{e}")

    async def _chat_danmaku(self, items) -> None:
        cfg = self.cfg
        items = [(uid, sanitize_external(nick) or "匿名", sanitize_external(t))
                 for uid, nick, t in items]
        uid, username, text = items[0]
        extra = len(items) - 1

        if get_output_lock().locked():
            console.dim(f"[弹幕] 正在播报，跳过本条（{username}：{text}）")
            if get_output_owner() != "danmaku":
                set_danmaku_pending(False)
            return

        try:
            from ev.llm.evolution.feedback import is_negative_text, record_feedback
            if is_negative_text(text):
                record_feedback("barrage", "negative", text)
        except Exception:
            pass
        # 观察弹幕流背景（只看不回）：让 AI 带全场语境回复优选，
        # 而不是只看到被挑中的那几条。排除本批回复目标避免重复。
        observe_block = ""
        try:
            from ev.utils import config as _obs_cfg
            if getattr(_obs_cfg.cfg, "DANMAKU_OBSERVER_ENABLED", True):
                from ev.danmaku.observer import get_observer
                _exclude = {t for _, _, t in items}
                _sec = float(getattr(_obs_cfg.cfg,
                                     "DANMAKU_OBSERVE_CONTEXT_SEC", 60))
                _max = int(getattr(_obs_cfg.cfg,
                                   "DANMAKU_OBSERVE_CONTEXT_MAX", 20))
                recent = get_observer().snapshot(
                    window_sec=_sec, limit=_max, exclude_texts=_exclude)
                if recent:
                    lines = "\n".join(f"- {n}：{t}" for n, t in recent)
                    observe_block = (
                        f"\n[你一直在看弹幕流（最近 {_sec:.0f} 秒，背景信息，"
                        f"用于理解直播间现在的氛围和梗，绝对不要逐条回应它们）]"
                        f"\n{lines}\n"
                        f"[如果大家最近在刷同一句话，可以顺势接一句热闹的。]"
                    )
        except Exception:
            observe_block = ""

        if extra:
            danmaku_lines = "\n".join(
                f"- 观众{nick}：{t}" for _, nick, t in items)
            wrapped = (
                f"[系统提示] 现在你在直播间，刚收到几条观众弹幕，请合并回应"
                f"（不要逐条念，抓住共同话题自然地回一句）。\n{danmaku_lines}"
                f"{observe_block}\n"
                f"请用 stream-chat 技能的直播闲聊风格回复（先回应情绪或接话，"
                f"一句话说完即可，不要连续追问）。"
            )
        else:
            wrapped = (
                f"[系统提示] 现在你在直播间，收到观众弹幕请自然地回一句。"
                f"观众昵称：{username}，弹幕内容：\n{text}"
                f"{observe_block}\n"
                f"请用 stream-chat 技能的直播闲聊风格回复（先回应情绪或接话，"
                f"一句话说完即可，不要连续追问）。"
            )

        if self.plugin_manager is not None:
            event = UserInputEvent(wrapped, "barrage")
            await self.plugin_manager.run_user_input_hooks(event)
            if event.prevented:
                console.dim("[插件] 弹幕回复被插件拦截")
                set_danmaku_pending(False)
                return
            wrapped = event.text
            if event.contexts:
                self.brain.push_turn_context(event.contexts)

        if self.proactive is not None:
            try:
                self.proactive.on_user_message()
            except Exception:
                pass

        turn_tracker = PerfTracker(f"弹幕回复@{username}")
        turn_tracker.begin("端到端")

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
            if self.evolution is not None:
                try:
                    asyncio.create_task(self.evolution.maybe_review(
                        self.mm.recent_turns, proactive=self.proactive))
                except Exception:
                    pass

        try:
            output_lock = get_output_lock()
            acquired = False
            async with output_lock:
                acquired = True
                set_output_owner("danmaku")
                set_global_state(STATE_AI_SPEAKING)
                if self.tts is not None:
                    try:
                        self.tts.clear_interrupt()
                    except Exception:
                        pass
                from ev.kernel.turn_lease import session_turn_gate
                async with session_turn_gate(str(uid) or username) as gate_ok:
                    if not gate_ok:
                        console.dim(f"[弹幕] 会话租约排队超时，丢弃（{username}）")
                        return
                    await stream.converse(
                        self.brain, wrapped, tts=self.tts, face=self.face, sub=self.sub,
                        profanity_filter=self.pf,
                        profanity_filter_rate=cfg.PROFANITY_FILTER_RATE,
                        on_llm_done=_on_llm_done if cfg.MEMORY_ENABLED else None,
                        emotion_actor=(
                            self.emotion_actor
                            if (getattr(self, "emotion_actor", None) is not None
                                and cfg.EMOTION_ACTOR_ENABLED)
                            else None),
                    )
                console.chat()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            console.error(f"弹幕回复出错：{e}")
        finally:
            if acquired:
                set_output_owner(None)
                set_global_state(STATE_IDLE)
            set_danmaku_pending(False)
            # 弹幕互动回复完成 → 回报契机引擎（刷新静默计时 + 氛围切换契机）
            if self.proactive is not None:
                try:
                    self.proactive.on_ai_spoke()
                except Exception:
                    pass

        turn_tracker.end("端到端")
        turn_tracker.print_report()

    # ---------- 记忆整合 / 进化后台 ----------

    async def _memory_integration_loop(self, interval: float = _MEMORY_INTEGRATE_INTERVAL) -> None:
        while True:
            try:
                await self._integrate_memories()
            except Exception as e:
                console.dim(f"[记忆整合] 检查出错（不影响运行）：{e}")
            await asyncio.sleep(interval)

    async def _integrate_memories(self) -> None:
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
        try:
            backup_path = os.path.join(
                self.cfg.DATA_ROOT,
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

    async def _evolution_periodic_loop(self) -> None:
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

    async def _agent_schedule_loop(self) -> None:
        from ev.agent.cron_harden import (
            ExecutionLedger, cron_harden_enabled, cross_process_tick_lock,
        )
        ledger = ExecutionLedger() if cron_harden_enabled() else None
        while True:
            await asyncio.sleep(30)
            try:
                if cron_harden_enabled():
                    with cross_process_tick_lock() as acquired:
                        if not acquired:
                            continue
                        due = self.agent_scheduler.due_items()
                        for item in due:
                            due_ts = float(item.get("last_run") or 0)
                            if ledger.already_executed(
                                    item.get("id"), due_ts):
                                continue
                            console.ok(
                                f"[Agent调度] 触发任务 #{item.get('id')}："
                                f"{item.get('task')}")
                            asyncio.create_task(
                                self._run_scheduled_task(item, ledger=ledger,
                                                         due_ts=due_ts))
                else:
                    due = self.agent_scheduler.due_items()
                    for item in due:
                        console.ok(
                            f"[Agent调度] 触发任务 #{item.get('id')}："
                            f"{item.get('task')}")
                        asyncio.create_task(self._run_scheduled_task(item))
            except Exception as e:
                console.dim(f"[Agent调度] 检查清单出错（不影响运行）：{e}")

    async def _run_scheduled_task(self, item: dict,
                                  ledger: Optional[Any] = None,
                                  due_ts: float = 0.0) -> None:
        from ev.agent import run_task
        task_text = item.get("task") or ""
        if ledger is not None:
            from ev.agent.cron_harden import scan_injection
            if scan_injection(task_text, strict=True):
                console.error(
                    f"[Agent调度] 任务 #{item.get('id')} 触发注入扫描拦截，已跳过")
                ledger.record(item.get("id"), due_ts, False, detail="注入扫描拦截")
                return
        t0 = time.time()
        try:
            if ledger is not None:
                from ev.utils.deadline import run_bounded_async
                bounded = await run_bounded_async(
                    run_task(task_text), 180.0, label=f"cron:{item.get('id')}")
                ok = not bounded.timed_out
                result = bounded.value if ok else "（3 分钟硬中断）"
            else:
                result = await run_task(task_text)
                ok = True
            console.ok(
                f"[Agent调度] 任务 #{item.get('id')} 完成：{str(result)[:200]}")
            if ledger is not None:
                ledger.record(item.get("id"), due_ts, ok,
                              detail=str(result)[:200],
                              duration=time.time() - t0)
        except Exception as e:
            console.error(
                f"[Agent调度] 任务 #{item.get('id')} 失败："
                f"{type(e).__name__}: {e}")
            if ledger is not None:
                ledger.record(item.get("id"), due_ts, False,
                              detail=f"{type(e).__name__}: {e}",
                              duration=time.time() - t0)

    async def _run_learn_task(self, topic: str) -> None:
        from ev.agent.learn_prompt import build_learn_task
        from ev.agent import run_task
        from plugins.builtin.tools.skills import get_skill_manager
        task = build_learn_task(topic)
        if not task:
            return
        manager = get_skill_manager()
        try:
            manager.reload()
            before = {s.name for s in manager.skills}
        except Exception:
            before = set()
        try:
            result = await run_task(task)
            console.ok(f"[learn] 创作完成：{str(result)[:200]}")
        except Exception as e:
            console.error(f"[learn] 创作失败：{type(e).__name__}: {e}")
            return
        try:
            manager.reload()
            after = {s.name for s in manager.skills}
            for name in after - before:
                manager.mark_created_by(name, "user")
                console.dim(f"[learn] 技能 {name} 已标记为用户创建（curator 不自动管理）")
        except Exception:
            pass

    # ---------- 会话归档 ----------

    async def archive_session(self) -> None:
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

    # ---------- TTS / 情绪 ----------

    async def init_tts_async(self) -> None:
        try:
            tts_ok = await self.tts.start()
        except Exception as e:
            console.warn(f"TTS 后台初始化异常：{e}")
            tts_ok = False
        if not tts_ok:
            self.tts = None
            return
        try:
            # GSV 库 INFO 日志已在引擎 _load_model 压 root logger 到 WARNING
            # （不刷屏）；这里只对外报一行预热耗时
            _t0 = time.perf_counter()
            _warm_ok = await self.tts.warmup()
            _warm_dt = time.perf_counter() - _t0
            if _warm_ok:
                console.dim(f"TTS 预热完成，耗时 {_warm_dt:.1f}s")
            else:
                console.warn(
                    f"TTS 预热未完成（已让路给真实合成，{_warm_dt:.1f}s）："
                    "首句首块可能略慢，不影响功能")
        except Exception:
            pass
        if self.face is not None:
            def _on_tts_play(audio, sr, text: str, dur_s: float) -> None:
                # P0-3 修复：回调现在携带真实音频（ndarray+采样率），
                # load_speech_curve 直接算 RMS 曲线；失败回退节拍口型
                if not self.face.load_speech_curve(audio, sr=sr):
                    self.face.start_speaking(dur_s)
            self.tts.set_on_play_callback(_on_tts_play)
        if self.emotion_actor is not None:
            self.tts.set_on_play_done_callback(self._restore_emotion_after_speech)
        if self.sub:
            self.tts.set_subtitle_callback(lambda t: self.sub.push("text", t))

    def _restore_emotion_after_speech(self) -> None:
        actor, loop = self.emotion_actor, self._loop
        if actor is None or loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(actor.restore(), loop)
        except Exception as e:
            console.dim(f"表情/动作复原调度失败：{e}")

    async def speak_memory_reply(self, text: str) -> None:
        if self.tts is None:
            if self.sub is not None:
                self.sub.push("text", text)
            console.ok(text)
            return
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
                if self.sub is not None:
                    self.sub.push("clear", "")

    # ---------- Mindcraft 双向桥 ----------

    async def _mindcraft_loop(self) -> None:
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
        await self.speak_bot_reply(message)

    async def speak_bot_reply(self, text: str) -> None:
        # P2-2 修复：直通路径统一清洗（MC 转述常含 markdown/emoji）
        text = _clean_sentence(text or "").strip()
        if not text or not has_content(text):
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
                    self.sub.push("text", text)
            finally:
                if bridge is not None and bridge.connected:
                    try:
                        await bridge.set_tts_playing(False)
                    except Exception:
                        pass
                set_output_owner(None)
                set_global_state(STATE_IDLE)
                if self.sub is not None:
                    self.sub.push("clear", "")

    # ---------- 命令分发 ----------

    def validate_cmd_path(self, raw: str) -> Optional[str]:
        if not raw or not raw.strip():
            return None
        try:
            base = os.path.abspath(self.cfg.PROJECT_ROOT)
            target = os.path.abspath(os.path.join(base, raw))
            if os.path.commonpath([base, target]) == base:
                return target
        except ValueError:
            pass
        return None

    # ---------- 配置热重载（细粒度） ----------

    HOT_COMPONENTS = ("llm", "proactive", "pf", "memory", "bili", "pet", "emotion")

    async def reload_all(self) -> None:
        config.reload_config()
        self.brain.reload_client()
        for name in self.HOT_COMPONENTS:
            await self._reloaders()[name]()

    async def reload_component(self, component: str) -> bool:
        reloaders = self._reloaders()
        if component not in reloaders:
            console.warn(
                f"未知的配置组件：{component}（可用：{'/'.join(self.HOT_COMPONENTS)}）")
            return False
        config.reload_config()
        try:
            await reloaders[component]()
        except Exception as e:
            console.error(f"「{component}」配置热更新失败（保留原配置）：{e}")
            return False
        return True

    async def reload_tools(self) -> None:
        """!tools（工具屋/设置页工具总开关）：重读 .env 工具字段 + 停启 MCP。

        本地/插件工具每轮实时读 cfg（get_merged_tools），刷新单例即生效；
        MCP 管理器对象在 MCP_ENABLED/TOOLS_ENABLED 切换时需要停启重建。
        """
        config.reload_tool_runtime()
        cfg = self.cfg
        want_mcp = bool(cfg.MCP_ENABLED and cfg.TOOLS_ENABLED)
        if want_mcp and self.mcp is None:
            try:
                from ev.mcp.manager import MCPManager
                self.mcp = MCPManager()
                await self.mcp.initialize()
                self.mcp.warmup()
                console.ok("MCP 管理器已热启用")
            except Exception as e:
                self.mcp = None
                console.warn(f"MCP 管理器热启用失败（本地工具不受影响）：{e}")
        elif not want_mcp and self.mcp is not None:
            try:
                await self.mcp.stop()
            except Exception:
                pass
            self.mcp = None
            console.ok("MCP 管理器已热关闭")

    async def reload_stt(self) -> None:
        """!stt（语音识别开关/Key/URL/模型变化）：停旧引擎，按新配置重建。"""
        config.reload_tool_runtime()
        cfg = self.cfg
        if self.stt_engine is not None:
            try:
                self.stt_engine.stop()
            except Exception:
                pass
            self.stt_engine = None
        if not cfg.STT_ENABLED:
            console.ok("语音识别已按配置关闭")
            return
        try:
            from ev.asr.stt import STTEngine
            self.stt_engine = STTEngine(cfg)
            self.stt_engine.start()
            if self.stt_engine.check_health():
                console.ok("语音识别引擎已按新配置重建")
            else:
                console.warn(
                    "语音识别引擎已重建，但本地 ASR 服务未响应"
                    "（请先启动 启动asr.bat 或配置 STT_BASE_URL 云端转写）")
        except Exception as e:
            self.stt_engine = None
            console.warn(f"语音识别重建失败（保持关闭）：{e}")

    async def apply_tts_hot(self, field: str, value: str) -> None:
        """!tts_audio / !tts_text / !tts_audios：TTS 参考音频/文本热更新。

        GPTSOVITS_* 字段只在全量热更新清单（不在 !tools 清单），先
        reload_config 刷新 cfg，再同步到运行中的 TTS 引擎（下一句生效）。
        """
        config.reload_config()
        cfg = self.cfg
        if self.tts is None:
            console.warn("TTS 引擎未运行，参考配置将在下次启动时生效")
            return
        if field == "tts_audio":
            self.tts.apply_ref(audio=value, text=cfg.GPTSOVITS_PROMPT_TEXT or "")
            console.ok(f"TTS 参考音频已热更新：{value or '（默认音色）'}")
        elif field == "tts_text":
            self.tts.apply_ref(audio=cfg.GPTSOVITS_REF_AUDIO or "", text=value)
            console.ok("TTS 参考文本已热更新")
        elif field == "tts_audios":
            self.tts.apply_ref_extras(value)
            console.ok("TTS 辅助参考已热更新")
        else:
            console.warn(f"未知的 TTS 热更新字段：{field}")

    def _reloaders(self) -> dict:
        if self._reloaders_map is None:
            self._reloaders_map = {
                "llm": self._reload_llm,
                "proactive": self._reload_proactive,
                "pf": self._reload_pf,
                "memory": self._reload_memory,
                "bili": self._reload_bili,
                "pet": self._reload_pet,
                "emotion": self._reload_emotion,
            }
        return self._reloaders_map

    async def _reload_llm(self) -> None:
        self.brain.reload_client()

    async def _reload_proactive(self) -> None:
        cfg = self.cfg
        if cfg.PROACTIVE_ENABLED and self.proactive is None:
            self.proactive = ProactiveEngine(
                brain=self.brain, tts=self.tts, face=self.face, sub=self.sub, cfg=cfg,
                butler=self.butler if cfg.MEMORY_ENABLED else None,
                memory_manager=self.mm if cfg.MEMORY_ENABLED else None,
                profanity_filter=self.pf,
                profanity_filter_rate=cfg.PROFANITY_FILTER_RATE,
                emotion_actor=(
                    self.emotion_actor
                    if (getattr(self, "emotion_actor", None) is not None
                        and cfg.EMOTION_ACTOR_ENABLED)
                    else None),
            )
            console.ok("主动对话已热启用")
        elif not cfg.PROACTIVE_ENABLED:
            self.proactive = None
            console.ok("主动对话已热关闭")
        elif self.proactive is not None:
            # 引擎已存在且保持启用：校准契机引擎阈值/开关（PROACTIVE_NUDGE_*）
            try:
                self.proactive.apply_nudge_cfg()
            except Exception as e:
                console.warn(f"契机引擎阈值校准失败（保留原阈值）：{e}")

    async def _reload_pf(self) -> None:
        cfg = self.cfg
        if cfg.PROFANITY_FILTER_ENABLED and self.pf is None:
            self.pf = ProfanityFilter()
            console.ok("内容过滤已热启用")
        elif not cfg.PROFANITY_FILTER_ENABLED:
            self.pf = None
            console.ok("内容过滤已热关闭")

    async def _reload_memory(self) -> None:
        cfg = self.cfg
        if cfg.MEMORY_ENABLED and self.butler is None:
            self.butler = ButlerAgent()
            console.ok("记忆系统已热启用")
        elif not cfg.MEMORY_ENABLED:
            self.butler = None
            console.ok("记忆系统已热关闭")
        from ev.llm.memory.curated import reset_curated_store
        reset_curated_store()
        from ev.llm.memory.session import reset_session_store
        reset_session_store()
        from ev.llm.memory.manager import reset_memory_manager
        reset_memory_manager()

    async def _reload_bili(self) -> None:
        cfg = self.cfg
        was_on = self.bili_svc is not None
        room_ids = cfg.BILI_ROOM_IDS or (
            [cfg.BILI_ROOM_ID] if cfg.BILI_ROOM_ID else [])
        want_on = bool(cfg.BILI_ENABLED and room_ids)
        if was_on:
            cancel = self.stop_bili()
            self.danmaku_picker, self.bili_svc, self.danmaku_reply_task = None, None, None
            if cancel is not None:
                try:
                    await cancel
                except (asyncio.CancelledError, Exception):
                    pass
            if not want_on:
                console.ok("B 站弹幕服务已热关闭")
        if want_on:
            self.danmaku_picker, self.bili_svc, self.danmaku_reply_task = await self.start_bili()
            if self.bili_svc is not None:
                console.ok(
                    f"B 站弹幕服务已热启用（房间 {'、'.join(str(r) for r in room_ids)}）")

    async def _reload_pet(self) -> None:
        if self.pet_widget is not None:
            self.pet_widget.apply_config(self.cfg)

    async def _reload_emotion(self) -> None:
        if self.emotion_actor is not None:
            self.emotion_actor.load_map()
