"""AI 虚拟主播主程序封装：Application.run() 对应原 main() 的全部生命周期。

原 main.py 中与「启动程序」无关的所有业务逻辑都搬到这里，使根目录
main.py 仅保留最薄的入口层（编码设置、vendor 路径注入、调用 run）。

§3.7 拆分后 Application 仅剩编排职责：RuntimeContext 管组件、
Handler 管协作（输入/对话）、命令实现管控制台命令。外部接口不变。
"""

import asyncio
import json
import logging

from ev.utils import config, console
from ev.utils.constants import ROLE_AI_ALIAS
from ev.utils.perf_tracker import PerfTracker
from ev.kernel.runtime import RuntimeContext
from ev.kernel.handlers import ChatHandler, InputHandler
from ev.kernel.output_lock import STATE_IDLE, STATE_USER_TALKING, set_global_state
from ev.kernel.bus import EV_SESSION_END, EV_USER_INPUT, bus
from ev.kernel.error_handler import report_error
from ev.kernel.events.models import InputEvent, SessionEndEvent
from plugins import UserInputEvent

# 压制 httpx/openai 客户端的 HTTP 请求 INFO 日志（噪音）
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)


class Application:
    """编排层：RuntimeContext（组件）+ Handler（协作）+ 主循环。"""

    async def run(self) -> None:
        """原 main() 的完整生命周期：初始化 → 主循环 → 资源清理。"""
        cfg = config.cfg
        cfg.validate()
        # 数据根准备：集中创建可写子目录 + 首次运行补齐内置知识库（幂等，
        # 默认数据根下为空操作；失败静默不阻塞启动）
        try:
            from ev.kernel import paths
            paths.ensure_data_dirs()
            paths.sync_builtin_resources()
        except Exception:
            pass
        runtime = RuntimeContext(cfg)
        await runtime.setup()

        # 拟人化层（ev.social，EV-Anthropomorphic 方案）：加载状态 + 注册
        # 发言工具 + 接线主动引擎/学习判定器。完全事件驱动（无 tick_loop），
        # 失败彻底 bypass（不影响主程序）。
        try:
            from ev.social.bootstrap import bootstrap as social_bootstrap
            await social_bootstrap(runtime)
        except Exception as e:
            console.dim(f"[social] bootstrap 失败（忽略，行为回落原 E.V）：{e}")

        # TUI 模式：启动后立即推送模型信息到状态栏
        if console.IS_TUI:
            try:
                console.report_status(
                    model=cfg.LLM_MODEL or "",
                    working=False,
                    ctx_total=getattr(cfg, "CONTEXT_WINDOW", 0) or 0,
                )
            except Exception:
                pass

        # TUI 模式：推送元信息（顶栏/状态行徽标：模式、TTS、版本）
        if console.IS_TUI:
            try:
                from ev.utils.config import cfg as _cfg
                _ev_version = getattr(_cfg, "VERSION", "") or ""
            except Exception:
                _ev_version = ""
            try:
                console.report_meta(
                    mode="tui",
                    model=cfg.LLM_MODEL or "",
                    version=_ev_version,
                    tts=runtime.tts is not None,
                )
            except Exception:
                pass

        # 3.15 开播就绪检查（只读旁路）：聚合探测 TTS/ASR/VTS/弹幕/记忆/MCP，
        # 失败仅 WARN 不阻断启动（保持现状启动行为）
        try:
            from ev.kernel import readiness
            report = await readiness.check_readiness(runtime)
            readiness.warn_failures(report)
        except Exception as e:
            console.dim(f"[就绪检查] 探针异常（忽略）：{e}")

        # Sub-agent 桥（优化 7-A）：主对话委派长任务给后台 sub-agent
        # AGENT_ENABLED=false（默认）时 maybe_delegate 直接返回 None，
        # 主对话完全不进入委派分支，行为与现状一致
        from ev.agent.bridge import MainChatSubAgentBridge
        self._subagent_bridge = MainChatSubAgentBridge()

        input_handler = InputHandler(runtime)
        chat_handler = ChatHandler(runtime)

        try:
            await self._main_loop(runtime, input_handler, chat_handler)
            console.dim("再见～")
        finally:
            # 会话结束归档 + 蒸馏（蒸馏条目写回记忆库，重启后检索可带出）。
            # 归档含两轮 LLM 调用，给 20s 总额超时兜底：LLM 完全无响应时
            # 不能无限等待（否则控制中心 30 秒强杀导致 Crashed），超时跳过。
            if (runtime.cfg.MEMORY_ENABLED and runtime.butler is not None
                    and runtime.mm is not None):
                try:
                    await asyncio.wait_for(runtime.archive_session(), timeout=20)
                except Exception as e:
                    console.warn(f"会话摘要/蒸馏失败（不影响退出）：{e}")
            # 事件总线：会话结束（退出归档完成）
            await bus.emit(EV_SESSION_END, SessionEndEvent(
                turns=len(runtime.mm.recent_turns) if runtime.mm is not None else 0))
            # 组件清理：停插件 → 弹幕 → 字幕 → TTS → 面捕 → VTS
            await runtime.teardown()

    async def _main_loop(self, runtime, input_handler, chat_handler) -> None:
        """主循环：等待输入 → 对话（无命令交互，Ctrl+C / EOF 退出）。"""
        _quit = False  # /quit 命令置位（内层 break 只回外层等输入，需信号传外层）
        while not _quit:
            try:
                user_text = await input_handler.wait_input(show_prompt=True)
            except KeyboardInterrupt:
                print()
                break
            except EOFError:
                # stdin 断开（控制中心窗口关闭 / 终端结束 → QProcess 管道断裂）：
                # 主程序由控制中心 QProcess 启动时 stdin 是管道，父进程退出
                # 必然 EOF。此前直接 break 会连带终止直播/桌宠（"自动关闭"
                # bug）——改为进入守护模式：表情/弹幕/主动对话/后台委派任务
                # 继续运行，Ctrl+C 退出，或控制中心重新启动后发 /quit。
                print()
                console.dim("[守护模式] 输入通道已断开（控制中心/终端已关闭），"
                            "主程序继续运行：Ctrl+C 退出，或 /quit 命令退出")
                try:
                    await asyncio.Event().wait()  # 永久挂起；事件循环继续驱动其他任务
                except KeyboardInterrupt:
                    break
            # _pending_stdin_fut 的清理在 _wait_input 内部完成：
            # 键盘触发时监听已消费置 None；语音触发时保留挂起的监听
            # 供 _interruptible_converse 复用（此处无条件置 None 会泄漏
            # input() 阻塞线程，导致语音对话后键盘第一行输入被抢占吞掉）
            while user_text:
                user_text = user_text.strip()
                if not user_text:
                    break
                # /quit：优雅退出（归档记忆后走 finally 清理）。控制中心
                # 「停止」按钮向 stdin 写 /quit（此前无处理会被当聊天文本
                # 发给 AI，停止按钮形同虚设）；守护模式下这也是主要退出通道
                if user_text == "/quit":
                    _quit = True
                    break
                # 5.8 主播即时命令：!advice drop（话术即时负反馈），增量不影响普通对话
                if user_text.startswith("!advice"):
                    from ev.llm.evolution.advice import handle_advice_command
                    handled, result = handle_advice_command(user_text)
                    if handled:
                        console.ok(f"[命令] {result}")
                        break
                # 5.14 建议机制：!suggestions（查看/批准/否决）+ !blueprint（蓝图填槽），
                # consent-first，均只读挂起建议或显式转定时任务
                if user_text.startswith(("!suggestions", "!blueprint")):
                    from ev.agent.suggestions import handle_suggestions_command
                    handled, result = handle_suggestions_command(user_text)
                    if handled:
                        console.ok(f"[命令] {result}")
                        break
                # 5.13 学习可视化：!journey 输出 ASCII 学习星图（纯读侧，不写盘）
                if user_text.startswith("!journey"):
                    from ev.llm.evolution.graph import journey_timeline
                    console.ok(f"[命令]\n{journey_timeline()}")
                    break
                # 3.15 开播自检：!doctor 手动触发完整就绪检查（复用 readiness
                # 探针，只读旁路），输出 JSON 供排查依赖服务状态
                if user_text.startswith("!doctor"):
                    from ev.kernel import readiness
                    report = await readiness.check_readiness(runtime)
                    console.ok("[命令] 自检结果:\n" + json.dumps(
                        report, ensure_ascii=False, indent=2))
                    break
                # 3.16 辅助调用记账：!perf 展示各辅助任务 token 与耗时
                # （aux_usage.jsonl + evolution_usage.jsonl，均为旁路记账）
                if user_text.startswith("!perf"):
                    from ev.llm.auxiliary import get_aux_usage_summary
                    from ev.llm.evolution.usage import usage_summary
                    console.ok(f"[命令]\n{get_aux_usage_summary()}")
                    console.ok(f"[命令]\n{usage_summary()}")
                    break
                # 5.15 技能创作引导：!learn <主题> 后台走 Agent 链路创作技能
                # （created_by=user，curator 不自动策展）；后台执行不阻塞对话
                if user_text.startswith("!learn"):
                    topic = user_text[len("!learn"):].strip()
                    if not topic:
                        console.ok("[命令] 用法：!learn <主题>（如：!learn 怎么回应观众的夸夸）")
                        break
                    console.ok(f"[命令] 已启动技能创作（{topic}），后台运行中……")
                    asyncio.create_task(runtime._run_learn_task(topic))
                    break
                # 插件管理：!plugins list / sync / enable / disable / reload
                # （UI 控制中心启停插件时也发 !plugins sync 走同一路径）
                if user_text.startswith("!plugins"):
                    from plugins.manager import handle_plugins_command
                    handled, result = await handle_plugins_command(user_text)
                    if handled:
                        console.ok(f"[命令] {result}")
                        break
                # 委派任务状态查询（优化 7-C）：!delegation [limit]
                # 读 delegation.db 显示最近 N 条后台任务状态
                # （独立只读连接，WAL 允许与 worker 并发不冲突）
                if user_text.startswith("!delegation"):
                    parts = user_text.split()
                    limit = 50
                    if len(parts) > 1:
                        try:
                            limit = max(1, min(int(parts[1]), 200))
                        except ValueError:
                            pass
                    from tools.delegation_status import (
                        _format_table, list_jobs)
                    console.ok(f"[命令]\n{_format_table(list_jobs(limit))}")
                    break
                # 控制中心热更新命令（UI「更新配置」/工具屋/插件页发来）：
                # !config（统一配置热更新，可带组件名细粒度）/
                # !tools（工具总开关）/ !stt（语音识别）/
                # !tts_audio|!tts_text|!tts_audios（TTS 参考音频/文本）。
                # 此前这些命令没有分发——UI 发来的 !config 会穿透到对话链路
                # 被当作聊天文本发给 LLM，且热更新从不生效（典型症状：
                # 改了人设点「更新配置」，提示词没有变化）。
                if user_text == "!config":
                    await runtime.reload_all()
                    console.ok("[命令] 配置已全部热更新"
                               "（人设/LLM/主动对话/内容过滤/记忆/弹幕/桌宠/情绪）")
                    break
                if user_text.startswith("!config "):
                    component = user_text[len("!config "):].strip()
                    if await runtime.reload_component(component):
                        console.ok(f"[命令] {component} 配置已热更新")
                    break
                if user_text == "!tools":
                    await runtime.reload_tools()
                    console.ok("[命令] 工具配置已热更新")
                    break
                if user_text == "!stt":
                    await runtime.reload_stt()
                    break
                if user_text.startswith(("!tts_audio", "!tts_text", "!tts_audios")):
                    _tts_cmd, _, _tts_val = user_text.partition(" ")
                    await runtime.apply_tts_hot(_tts_cmd[1:], _tts_val.strip())
                    break
                # 启动页模型下拉热切换（桌宠模式）：直接换皮；
                # vtuber 模式无 pet_widget → 忽略（.env 2s 监控兜底也不适用）
                if user_text.startswith("!model "):
                    _model_path = user_text[len("!model "):].strip()
                    _pet = getattr(runtime, "pet_widget", None)
                    if _pet is not None and _pet.switch_model(_model_path):
                        console.ok(f"[命令] 桌宠模型已热切换：{_model_path}")
                    else:
                        console.dim(f"[命令] 忽略 !model（非桌宠模式或路径无效）："
                                    f"{_model_path}")
                    break
                # 表情动作页试播（桌宠/VTS）：/expr <表情名> / /motion <动作名>
                # （动作按文件名去扩展名播放，见 BaseEmotionActor.play_motion_by_name）
                if user_text.startswith("/expr "):
                    _name = user_text[len("/expr "):].strip()
                    _actor = runtime.emotion_actor
                    if _actor is not None and await _actor.play_expression(_name):
                        console.ok(f"[命令] 试播表情：{_name}")
                    else:
                        console.dim(f"[命令] 表情试播失败（未知名称或当前模式无表情）：{_name}")
                    break
                if user_text.startswith("/motion "):
                    _name = user_text[len("/motion "):].strip()
                    _actor = runtime.emotion_actor
                    if _actor is not None and await _actor.play_motion_by_name(_name):
                        console.ok(f"[命令] 试播动作：{_name}")
                    else:
                        console.dim(f"[命令] 动作试播失败（未知名称或当前模式无动作）：{_name}")
                    break
                # 插件钩子：onUserInput（可注入背景上下文 / 改写消息 / 拦截不发给 AI）
                if runtime.plugin_manager is not None:
                    event = UserInputEvent(user_text, "text")
                    await runtime.plugin_manager.run_user_input_hooks(event)
                    if event.prevented:
                        console.dim("[插件] 消息被插件拦截，未发送给 AI")
                        break
                    user_text = event.text
                    if event.contexts:
                        runtime.brain.push_turn_context(event.contexts)
                    if not user_text.strip():
                        break
                # 事件总线：用户输入进入内核（键盘 / 语音识别）
                await bus.emit(EV_USER_INPUT, InputEvent(
                    source=runtime._input_source or "text",
                    content=user_text, sender="user"))
                # 用户发言：重置主动引擎状态
                if runtime.proactive is not None:
                    runtime.proactive.on_user_message()
                # 全局状态：用户输入已到达，agent 触发被抑制（忙碌避让）
                set_global_state(STATE_USER_TALKING)

                # Mindcraft 双向桥：已连接时把用户输入转发给 MC bot，
                # bot 回复由桥回调朗读；本机不再走本地 LLM 对话（避免双重回答）
                if (runtime.mindcraft_bridge is not None
                        and runtime.mindcraft_bridge.connected):
                    try:
                        await runtime.mindcraft_bridge.send_message(user_text)
                    except Exception as e:
                        console.dim(f"[Mindcraft] 转发用户输入失败，回退本地对话：{e}")
                    else:
                        runtime.sub.push("user", user_text)
                        console.dim(f"[Mindcraft] 已转发给 MC 机器人：{user_text}")
                        break

                # 用户消息分类情绪 → 后台播放表情/动作（桌宠/VTS 模式，仅开关开启时）
                if (runtime.emotion_actor is not None
                        and config.cfg.EMOTION_ACTOR_ENABLED):
                    asyncio.create_task(runtime.emotion_actor.handle(user_text))

                # 每轮对话性能埋点
                turn_tracker = PerfTracker("本轮对话")
                turn_tracker.begin("端到端")

                # TUI 模式：对话开始 → working=True
                if console.IS_TUI:
                    try:
                        console.report_status(working=True)
                    except Exception:
                        pass

                # 用户输入推送到字幕网页
                runtime.sub.push("user", user_text)

                # Sub-agent 委派（优化 7-A）：命中委派关键词且 Agent 开关
                # 开启时，把长任务交给后台 sub-agent，主对话立即响应"正在后台
                # 执行"不进入 converse；未命中或异常返回 None 走原对话路径
                if await self._subagent_bridge.maybe_delegate(
                        user_text, runtime) is not None:
                    break

                # 注意：这里**不过滤用户输入**（按用户要求）。
                # 内容过滤只在三处生效：AI 回复句子、观众弹幕原文、主动对话播报。
                # 记忆也存原文（Butler 可以看到真实的骂人话帮 AI 决定应对策略）。

                _turn_user = user_text
                try:
                    async def _on_llm_done(reply_text: str) -> None:
                        if not (runtime.cfg.MEMORY_ENABLED and runtime.butler):
                            return
                        try:
                            runtime.mm.add_turn("user", _turn_user, source="user_input")
                            runtime.mm.add_turn(ROLE_AI_ALIAS, reply_text,
                                                source="main_llm_reply")
                            await runtime.butler.submit_extract_and_store(
                                [{"role": "user", "content": _turn_user},
                                 {"role": "assistant", "content": reply_text}],
                                runtime.mm.recent_turns[:-2],
                            )
                        except Exception as e:
                            console.dim(f"[ButlerAgent] 记忆提取出错（不影响对话）：{e}")
                        # 自我进化：复盘走管家模型（agent 配置），
                        # 后台执行不阻塞对话
                        if runtime.evolution is not None:
                            try:
                                asyncio.create_task(
                                    runtime.evolution.maybe_review(
                                        runtime.mm.recent_turns,
                                        proactive=runtime.proactive))
                            except Exception:
                                pass

                    interrupted, buzz, pending = await chat_handler.converse(
                        user_text,
                        on_llm_done=_on_llm_done if runtime.cfg.MEMORY_ENABLED else None,
                        profanity_filter=runtime.pf,
                        profanity_filter_rate=runtime.cfg.PROFANITY_FILTER_RATE,
                    )
                    runtime._pending_stdin_fut = pending
                    # AI 互动回复完成 → 回报契机引擎（刷新静默计时 + 氛围切换契机，
                    # 互动结束后的第一次心跳仍有一次开口机会）
                    if runtime.proactive is not None:
                        try:
                            runtime.proactive.on_ai_spoke()
                        except Exception:
                            pass
                except Exception as e:
                    console.error(f"对话流程出错：{e}")
                    await report_error(e, msg=f"对话流程出错：{e}")
                    interrupted, buzz = False, ""
                    # 挂起的键盘监听已由 _interruptible_converse 的 finally
                    # 交还到 _pending_stdin_fut，此处不要覆盖为 None
                    # 对话未正常结束：兜底复位状态，避免卡在忙碌态抑制 agent
                    set_global_state(STATE_IDLE)

                turn_tracker.end("端到端")
                turn_tracker.print_report()

                # TUI 模式：对话结束 → working=False
                if console.IS_TUI:
                    try:
                        console.report_status(working=False)
                    except Exception:
                        pass

                if interrupted:
                    user_text = buzz
                else:
                    break


async def run_with_cleanup() -> None:
    """运行 Application.run()；退出（含被取消）时关记忆连接 + 清理临时文件。"""
    try:
        await Application().run()
    finally:
        from tools.memory import memory
        try:
            await memory.aclose()
        except Exception:
            pass
        try:
            from ev.utils import cleaner
            cleaner.cleanup_temp_files(verbose=False)
        except Exception:
            pass
