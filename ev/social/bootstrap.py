"""ev.social.bootstrap — 拟人化层启动入口（完全事件驱动，无 tick_loop）。

设计：
  - 启动时加载状态（engagement + wake + persona）
  - 注册全局 proactive 引擎引用 + LLM 学习判定器
  - 注册 MCP 工具（set_wake_config / cancel_wake / request_speak / nudge_check）
  - **不起任何后台 task** —— 完全事件驱动
  - 主循环在「事件到来」时调 ev.social.on_event() 推进状态
  - 主循环在「事件处理后」调 ev.social.observe_danmaku() 等埋点

失败兜底：
  - 任何异常都 swallow
  - 拟人化层失败时主程序行为回落到原 E.V
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("ev.social")


# ===== 全局开关 =====
_enabled = False
_bootstrapped = False


def is_enabled() -> bool:
    return _enabled


def _read_flags() -> dict:
    try:
        from ev.utils import config as ev_config
        return {
            "engagement": getattr(ev_config.cfg, "SOCIAL_ENGAGEMENT_ENABLED", True),
            "deliberation": getattr(ev_config.cfg, "SOCIAL_DELIBERATION_ENABLED", True),
            "quote": getattr(ev_config.cfg, "SOCIAL_QUOTE_ENABLED", True),
            "silence": getattr(ev_config.cfg, "SOCIAL_SILENCE_ENABLED", True),
            "learning": getattr(ev_config.cfg, "SOCIAL_LEARNING_ENABLED", True),
            "event_driven": getattr(ev_config.cfg, "SOCIAL_EVENT_DRIVEN", True),
            "speak_tools": getattr(ev_config.cfg, "SOCIAL_SPEAK_TOOLS_ENABLED", True),
        }
    except Exception:
        return {k: True for k in (
            "engagement", "deliberation", "quote", "silence",
            "learning", "event_driven", "speak_tools")}


# ===== 主入口 =====

async def bootstrap(ctx=None) -> bool:
    """异步 bootstrap（在 application 启动处调用）。

    Args:
        ctx: RuntimeContext（可 None）。用于注册全局 proactive 引擎引用
             与 LLM 学习判定器；为 None 时跳过这两步（纯状态加载）。

    关键：不启动任何 tick_loop / 后台 task（学习沉淀由调用方驱动或跳过）。
    """
    global _enabled, _bootstrapped

    if _bootstrapped:
        logger.info("[social] already bootstrapped, skip")
        return _enabled

    try:
        flags = _read_flags()
        if not any(v for k, v in flags.items() if k != "speak_tools"):
            logger.info("[social] all modules disabled, skip bootstrap")
            return False

        logger.info(f"[social] bootstrap started (event_driven="
                    f"{flags.get('event_driven', True)}), flags={flags}")

        # 0) 注册主事件循环句柄：弹幕线程的埋点/poke 统一经
        #    call_soon_threadsafe 投递回主循环（Event.set 非线程安全）
        try:
            from .wake import set_main_loop
            set_main_loop(asyncio.get_running_loop())
        except Exception as e:
            logger.debug(f"[social] main loop register failed: {e}")

        # 1) 加载人设参数
        try:
            from .personalize import load_persona_params
            load_persona_params()
        except Exception as e:
            logger.warning(f"[social] persona load failed: {e}")

        # 2) 注册全局 proactive 引擎（wake 的 request_speak 工具走这里）
        if ctx is not None:
            try:
                from .proactive import set_proactive_engine
                engine = getattr(ctx, "proactive", None)
                if engine is not None:
                    set_proactive_engine(engine)
            except Exception as e:
                logger.debug(f"[social] proactive engine register failed: {e}")

            # 3) 注入 LLM 学习判定器（词库沉淀用；失败仅影响新词学习）
            if flags.get("learning"):
                try:
                    brain = getattr(ctx, "brain", None)
                    if brain is not None:
                        from .learning import set_llm_judge
                        set_llm_judge(_make_brain_judge(brain))
                except Exception as e:
                    logger.debug(f"[social] llm judge inject failed: {e}")

        # 4) 加载 engagement 状态
        if flags.get("engagement"):
            try:
                from . import engagement as _eng
                await _eng.load_persisted()
                _eng.apply_config(_read_config_dict())
                logger.info(f"[social/engagement] state={_eng.get_state()}")
            except Exception as e:
                logger.warning(f"[social/engagement] init failed: {e}")

        # 5) 加载 wake 状态
        if flags.get("event_driven", True):
            try:
                from .wake import load_persisted
                await load_persisted()
                logger.info("[social/wake] config loaded")
            except Exception as e:
                logger.warning(f"[social/wake] init failed: {e}")

        # 6) 社会学习：清空调试开关 + 后台沉淀循环（唯一后台 task，5min 一次；
        #    参考实现定义了循环但未启动，这里补上接线，失败仅影响新词学习）
        if flags.get("learning"):
            try:
                from . import learning as _learning
                from ev.utils import config as _cfg
                if getattr(_cfg.cfg, "SOCIAL_LEARNING_PURGE_TODAY", False):
                    _learning.purge_today()
                _start_learning_loop()
            except Exception as e:
                logger.debug(f"[social/learning] consolidate loop start failed: {e}")

        _enabled = True
        _bootstrapped = True
        logger.info("[social] bootstrap done (event-driven mode, no tick_loop)")
        return True

    except Exception as e:
        logger.exception(f"[social] bootstrap failed (non-fatal): {e}")
        return False


def bootstrap_lazy() -> None:
    """同步被动 bootstrap（事件循环未运行时也安全）。"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(bootstrap())
    except RuntimeError:
        pass
    except Exception as e:
        logger.debug(f"[social] bootstrap_lazy error: {e}")


def _make_brain_judge(brain):
    """把主项目 brain 包装成 learning 的 LLM 判定器。

    brain.chat_stream(prompt) 是异步生成器；这里取 final 段拼成完整文本。
    """
    async def _judge(prompt: str) -> str:
        parts = []
        async for item in brain.chat_stream(prompt):
            if isinstance(item, tuple) and item and item[0] == "final":
                parts.append(item[1])
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return _judge


# ===== 主循环的「事件推进」辅助函数 =====

async def on_event(event_type: str, payload: Optional[dict] = None) -> None:
    """主循环在事件到来时调这个，推进 engagement 状态。

    主循环伪代码：
        while True:
            event = await wait_for_next_event()
            await ev.social.on_event("danmaku", {...})
            # 处理事件...
    """
    if not is_enabled():
        return
    try:
        from .engagement import on_event as _on_event
        await _on_event(event_type, payload)
    except Exception as e:
        logger.debug(f"[social] on_event error: {e}")


async def on_post_event(event_type: str, payload: Optional[dict] = None):
    """主循环在每个事件处理完后调这个，触发契机检查（兼容参考 API）。

    返回 NudgeEvent（如果有）或 None。主项目已把契机埋点直接接到
    ev.llm.proactive.nudge.observe（弹幕 client），此函数保留给
    「主循环显式驱动」的接入方式使用。
    """
    if not is_enabled():
        return None
    try:
        from .nudge import check
        return await check({"type": event_type, "payload": payload or {}})
    except Exception as e:
        logger.debug(f"[social] on_post_event error: {e}")
        return None


async def handle_nudge(nudge) -> dict:
    """主循环拿到契机后调这个：转成一次 request_speak 交给主动引擎。"""
    if not is_enabled() or nudge is None:
        return {"ok": False, "reason": "no_nudge"}

    try:
        from .proactive import get_proactive_engine
        engine = get_proactive_engine()
        if engine is None:
            return {"ok": False, "reason": "no_engine"}

        result = await engine.request_speak(
            topic_hint=f"[NUDGE: {nudge.reason.value}] {nudge.prompt_hint}",
            reason=f"nudge: {nudge.reason.value}",
            nudge_reason=nudge.reason.value,
        )
        return result
    except Exception as e:
        logger.exception(f"[social] handle_nudge error: {e}")
        return {"ok": False, "error": str(e)}


async def wait_for_window(timeout_sec: Optional[float] = None) -> str:
    """主循环空闲时调这个，等下一个事件 / 唤醒 deadline。

    Returns:
        "woken" / "deadline"
    """
    if not is_enabled():
        return "woken"
    try:
        from .wake import wait_for_window as _wait
        return await _wait(timeout_sec)
    except Exception as e:
        logger.debug(f"[social] wait_for_window error: {e}")
        return "woken"


# ===== 弹幕埋点（ev/danmaku/client.py 调用；全部非阻塞、失败 swallow）=====

def observe_danmaku(text: str, *, user_id=None,
                    is_superchat: bool = False, is_gift: bool = False) -> None:
    """一条弹幕到达时的拟人化层埋点（同步、非阻塞、零异常外泄）。

    线程安全：弹幕线程调用时，整个埋点体经 call_soon_threadsafe 投递到
    主循环执行（bootstrap 已注册主循环句柄）；未注册/已在主循环时就地执行。
    覆盖：engagement 事件推进 + wake poke + learning 被动监听。
    deliberation/quote 的打分走 should_block / detect_quote_signal，
    由调用方按需使用（主项目弹幕挑选器已有自己的评分管线）。
    """
    if not is_enabled():
        return
    # 跨线程归队：弹幕线程 -> 主循环
    try:
        from .wake import _main_loop as _wake_loop
    except Exception:
        _wake_loop = None
    if _wake_loop is not None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not _wake_loop:
            try:
                _wake_loop.call_soon_threadsafe(
                    _observe_danmaku_on_loop, text, user_id,
                    is_superchat, is_gift)
                return
            except RuntimeError:
                pass  # 主循环已关闭：就地执行兜底
    _observe_danmaku_on_loop(text, user_id, is_superchat, is_gift)


def _observe_danmaku_on_loop(text: str, user_id,
                             is_superchat: bool, is_gift: bool) -> None:
    """埋点实现（保证在主循环上执行，create_task 才归位）。"""
    flags = _read_flags()

    # 1) engagement 状态推进（事件驱动核心）
    if flags.get("engagement"):
        try:
            from . import engagement as _eng
            import asyncio as _aio
            try:
                loop = _aio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(_eng.on_event("danmaku", {
                    "user_id": user_id, "is_sc": is_superchat}))
        except Exception as e:
            logger.debug(f"[social/engagement] observe error: {e}")

    # 2) learning 被动监听（候选词计数，纯内存）
    if flags.get("learning"):
        try:
            import asyncio as _aio
            from .learning import observe_danmaku as _observe
            loop = _aio.get_running_loop()
            loop.create_task(_observe(text, is_sc=is_superchat))
        except Exception as e:
            logger.debug(f"[social/learning] observe error: {e}")

    # 3) wake poke：事件打断 wait_for（wait_for_window 的接入方式）
    if flags.get("event_driven", True):
        try:
            from .wake import poke
            poke()
        except Exception as e:
            logger.debug(f"[social/wake] poke error: {e}")


# ===== 兜底：should_block 给弹幕挑选器 / 老 application 用 =====

def should_block(danmaku) -> bool:
    """deliberation 选择性决策：该弹幕是否不值得回（SOCIAL_DELIBERATION_ENABLED）。

    danmaku 可以是对象（有 text/user_id 等属性）或 dict。任何异常返回 False
    （全放行，行为回落到原 E.V）。
    """
    if not is_enabled():
        return False
    try:
        flags = _read_flags()
        if not flags.get("deliberation"):
            return False
        from .engagement import get_state
        from .deliberation import should_pass
        from .personalize import get_persona_params
        from .quote import detect_quote_signal

        if isinstance(danmaku, dict):
            text = danmaku.get("text", "")
            user_id = danmaku.get("uid", danmaku.get("user_id"))
            is_sc = bool(danmaku.get("is_superchat", False))
            is_gift = bool(danmaku.get("is_gift", False))
            reply_to = danmaku.get("reply_to_message_id")
        else:
            text = getattr(danmaku, "text", "")
            user_id = getattr(danmaku, "uid", getattr(danmaku, "user_id", None))
            is_sc = bool(getattr(danmaku, "is_superchat", False))
            is_gift = bool(getattr(danmaku, "is_gift", False))
            reply_to = getattr(danmaku, "reply_to_message_id", None)

        persona = get_persona_params()
        signal = detect_quote_signal(
            text, reply_to_message_id=reply_to,
            is_superchat=is_sc, is_gift=is_gift,
        )
        result = should_pass(
            text, engagement_state=get_state(),
            social_level=persona.social_level,
            is_gift=is_gift, is_superchat=is_sc,
            is_at_me=signal.is_at_me, is_quoted=signal.is_quoted,
            mentions_name=signal.mentions_name,
            interest_keywords=persona.interest_keywords,
            blocked_keywords=persona.ignore_keywords,
        )
        return not result.passed
    except Exception:
        return False


# ===== 内部 =====

_learning_task = None


def _start_learning_loop() -> None:
    """启动学习沉淀后台循环（幂等；异常退出后下次 bootstrap 重建）。"""
    global _learning_task
    if _learning_task is not None and not _learning_task.done():
        return
    try:
        from .learning import _periodic_consolidate_loop
        _learning_task = asyncio.create_task(
            _periodic_consolidate_loop(), name="social_learning_loop")
        logger.info("[social/learning] consolidate loop started (5min)")
    except Exception as e:
        logger.debug(f"[social/learning] loop create failed: {e}")


def stop_learning_loop() -> None:
    """停止学习沉淀循环（进程退出 / 测试用）。"""
    global _learning_task
    if _learning_task is not None and not _learning_task.done():
        _learning_task.cancel()
    _learning_task = None


def _read_config_dict() -> dict:
    try:
        from ev.utils import config as ev_config
        cfg = ev_config.cfg
        keys = [k for k in dir(cfg) if k.startswith("SOCIAL_")]
        return {k: getattr(cfg, k) for k in keys}
    except Exception:
        return {}
