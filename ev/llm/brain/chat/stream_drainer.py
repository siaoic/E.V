"""子线程同步流式响应迭代器：把 OpenAI SDK 的同步流迭代转成 asyncio.Queue。

对应原 `_chat_stream_inner` 中 while 循环内的嵌套函数 `_run`（含
`_create/_send/_push/_drain` 子闭包）。把这些闭包外置为顶层函数，通过
ctx 字典传入所需引用，避免闭包把 inner_loop.py 撑大。
"""

import asyncio
import copy
import json
import re
import time

from openai import RateLimitError

from ev.utils import config, console
from ev.llm.cleaners.api import _clean_messages_for_api
from ev.llm.client.factory import build_thinking_extra_body
from ev.llm.client.retry import _parse_retry_after
from ev.llm.utils.constants import _MAX_429_WAIT


class _DrainCancelled(Exception):
    """子线程流式产出被打断（cancel_event 置位）——仅作静默退出哨兵。"""


# 服务端 max_tokens 上限学习缓存（进程内，key = "base_url|model"）：
# 部分服务（智谱系错误码 1210）限制 max_tokens ∈ [1,1024]，硬编码 2048 会
# 直接 400 判死（用户端表现「大脑出了点小故障」）。首次 400 时从错误信息
# 解析上限并钳制重试，之后同服务请求直接带钳制值，省一个必败往返。
_MT_CAPS: dict = {}

# 错误信息里的上限写法：「限制数值范围[1,1024]」（智谱 1210）
_MT_CAP_RANGE_RE = re.compile(r"[\[(]\s*\d+\s*,\s*(\d+)\s*[\])]")
# 英文写法兜底：maximum 1024 / at most 1024 / <= 1024
_MT_CAP_MAX_RE = re.compile(r"(?:maximum|at most|less than or equal to|<=|≤)\s*(\d+)", re.I)


def _learn_max_tokens_cap(err_msg: str) -> int | None:
    """从 400 错误信息解析服务端 max_tokens 上限；与 max_tokens 无关返回 None。"""
    if "max_tokens" not in err_msg:
        return None
    m = _MT_CAP_RANGE_RE.search(err_msg) or _MT_CAP_MAX_RE.search(err_msg)
    if not m:
        return None
    try:
        cap = int(m.group(1))
    except ValueError:
        return None
    return cap if cap >= 1 else None


def _run_stream_drainer(ctx: dict) -> None:
    """子线程入口：同步迭代流式响应，推送 content 增量并累积 tool_calls。

    ctx 必须包含（所有 by-ref 的可变对象/容器，子线程直接读写）：
      - messages、tools（请求参数）
      - self（LLMBrain 实例，只读 client/cfg/router）
      - route_name、route_client、route_model（inout：路由回退会写回）
      - loop（asyncio 事件循环，用于 call_soon_threadsafe）
      - q（asyncio.Queue，主线程消费 content 增量）
      - tool_calls_acc（list，累积工具调用增量）
      - full_raw（list，累积原始 content 增量）
      - reasoning_raw（list，累积原始 reasoning_content 增量，多轮需回传）
      - _first_content（list[bool]，首字延迟标记，用 list 以便 nonlocal 修改）
      - tracker（PerfTracker，首字延迟打点）
    """
    messages = ctx["messages"]
    tools = ctx["tools"]
    self = ctx["self"]
    loop = ctx["loop"]
    q = ctx["q"]
    tool_calls_acc = ctx["tool_calls_acc"]
    full_raw = ctx["full_raw"]
    reasoning_raw = ctx["reasoning_raw"]
    _first_content_ref = ctx["_first_content"]   # list[bool]
    # P1-1：取消开关（打断时由 cancel_llm_stream 置位），可为 None
    cancel_event = ctx.get("cancel_event")

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    # 这些是 inout，需要从 ctx 取/回写
    extra_body = [None]   # list 包装以便内层函数修改（模拟 nonlocal）
    route_name = [ctx["route_name"]]
    route_client = [ctx["route_client"]]
    route_model = [ctx["route_model"]]

    def _first_content_mark_used() -> bool:
        """原子式消费「首字」标记。返回 True 表示本次是第一个 content chunk。"""
        if _first_content_ref[0]:
            _first_content_ref[0] = False
            return True
        return False

    def _create():
        """发起一次带 tools 的流式请求。thinking 字段不被支持时
        自动降级重试；服务端限制 max_tokens 时学习上限钳制重试；
        路由服务整体不可用时回退默认 LLM 服务重试一次。"""
        client = route_client[0] or self.client
        model = route_model[0] or self.cfg.LLM_MODEL

        def _send():
            # max_tokens 按该服务已学习的上限钳制（无记录时用默认 2048）
            cap_key = (f"{str(getattr(client, 'base_url', '')).rstrip('/')}"
                       f"|{model}")
            kwargs = dict(
                model=model,
                messages=_clean_messages_for_api(messages),
                stream=True,
                max_tokens=min(2048, _MT_CAPS.get(cap_key, 2048)),
                temperature=0.95,
            )
            if tools:
                kwargs["tools"] = tools
            if extra_body[0] is not None:
                return client.chat.completions.create(
                    **kwargs, extra_body=extra_body[0])
            return client.chat.completions.create(**kwargs)

        def _try_learn_cap(e: Exception) -> bool:
            """400 报 max_tokens 超限时学习上限；学到新上限返回 True（触发重试）。"""
            cap = _learn_max_tokens_cap(str(e))
            if cap is None:
                return False
            cap_key = (f"{str(getattr(client, 'base_url', '')).rstrip('/')}"
                       f"|{model}")
            old = _MT_CAPS.get(cap_key)
            if old is not None and old <= cap:
                return False  # 已按该上限发过仍报错：不是超限问题，勿死循环
            _MT_CAPS[cap_key] = cap
            console.warn(
                f"LLM 服务限制 max_tokens≤{cap}（{model}），已钳制重试")
            return True

        try:
            return _send()
        except Exception as e:
            # 429 是服务端限流不是 thinking 不支持，不能走降级分支
            # （否则会把 429 误判成「不支持 thinking」打误导警告）
            if (extra_body[0] is None
                    or isinstance(e, RateLimitError)
                    or "429" in str(e)):
                raise
            # max_tokens 超限（智谱系 400/1210）≠ thinking 不支持：
            # 学习上限钳制重试，避免误导性降级 + 必败重试
            if _try_learn_cap(e):
                return _send()
            console.warn("LLM 服务不支持 thinking 参数，降级为普通模式")
            extra_body[0] = None
            try:
                return _send()
            except Exception as e2:
                if _try_learn_cap(e2):
                    return _send()
                if isinstance(e2, RateLimitError) or "429" in str(e2):
                    raise
                # 路由服务整体不可用 → 记录失败并回退默认服务重试
                # （route_name 清空后成功路径不再误记到该服务头上）
                if route_name[0] is not None:
                    self.router.record(route_name[0], False)
                    console.warn(
                        f"[模型路由] 服务 {route_name[0]!r} 不可用，"
                        "回退默认 LLM 服务重试")
                    route_name[0] = None
                    route_client[0] = None
                    route_model[0] = None
                    client = self.client
                    model = self.cfg.LLM_MODEL
                    return _send()
                raise

    def _push(content: str) -> None:
        """把 content 增量推回主循环（记录首字延迟）。"""
        if not content:
            return
        if _first_content_mark_used():               # 首字到达
            loop.call_soon_threadsafe(ctx["tracker"].end, "首字延迟")
        loop.call_soon_threadsafe(q.put_nowait, content)

    try:
        # —— 组装请求（thinking 显式控制） ——
        # GLM-4.5/4.7 系列默认「强制思考」：即使不传 thinking 参数，
        # 模型也会输出思维链（造成"明明关了还在思考"）。
        # 所以关闭时必须显式传 {"type": "disabled"} 才能真正禁用；
        # 不支持 thinking 字段的服务（OpenAI 官方等）会 400，自动降级重试。
        def _drain(response) -> None:
            """迭代一个流式响应：推送 content 增量 + 累积工具调用。"""
            for chunk in response:
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None) or ""
                if reasoning:
                    # 思考过程：累积（DeepSeek 等思考模式要求多轮对话原样
                    # 回传 reasoning_content，否则 API 400）。
                    # 打印仅在 LLM_THINKING 显式开启时：部分模型（GLM-4.5+
                    # 等）关思考仍会流 reasoning_content，无脑打印会把内部
                    # 思维链刷进控制台对话区
                    reasoning_raw.append(reasoning)
                    if getattr(config.cfg, "LLM_THINKING", False):
                        print(console.paint(reasoning, console.GRAY), end="", flush=True)
                content = getattr(delta, "content", None) or ""
                if content:
                    full_raw.append(content)
                    _push(content)
                # —— 工具调用增量累积（对标 llm-client.js _handleStreamResponse）——
                tcs = getattr(delta, "tool_calls", None)
                if tcs:
                    for tc in tcs:
                        index = tc.index
                        while len(tool_calls_acc) <= index:
                            tool_calls_acc.append(None)
                        if tool_calls_acc[index] is None:
                            tool_calls_acc[index] = {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if getattr(tc, "id", None):
                            tool_calls_acc[index]["id"] = tc.id
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            if getattr(fn, "name", None):
                                tool_calls_acc[index]["function"]["name"] = fn.name
                            if getattr(fn, "arguments", None):
                                tool_calls_acc[index]["function"]["arguments"] += fn.arguments
                        # L2-A 提前启动：arguments 已累积成合法 JSON（id+name
                        # 齐全）即视为该工具调用完整，立即通知主循环启动执行，
                        # 工具耗时与 LLM 后续输出并行，不等流结束再启动。
                        entry = tool_calls_acc[index]
                        if (entry["id"] and entry["function"]["name"]
                                and entry["function"]["arguments"]):
                            try:
                                json.loads(entry["function"]["arguments"])
                                loop.call_soon_threadsafe(
                                    q.put_nowait,
                                    ("__TOOL_CALL_READY__", index,
                                     copy.deepcopy(entry)))
                            except (json.JSONDecodeError, TypeError):
                                pass

        # 429 自动等待重试：免费档服务端 1 并发限流（高峰期常触发）。
        # 按服务端 Retry-After / X-RateLimit-Reset 等待限流窗口结束后
        # 自动重试，而不是直接中断；总等待封顶 _MAX_429_WAIT 秒，
        # 超时仍 429 才放弃（走 __RATELIMIT__ 友好提示）。
        waited = 0.0
        while True:
            try:
                extra_body[0] = build_thinking_extra_body(self.cfg.LLM_THINKING)
                _drain_start = time.perf_counter()
                _drain(_create())
                # 路由服务调用成功 → 记录奖励与耗时（供 UCB1 择优）；
                # route_name 已被回退清空时不记录（成功属于默认服务）
                if route_name[0] is not None:
                    self.router.record(
                        route_name[0], True,
                        time.perf_counter() - _drain_start)
                break
            except Exception as e:
                if not (isinstance(e, RateLimitError) or "429" in str(e)):
                    raise
                wait = _parse_retry_after(e) or 15.0   # 无头信息用保守默认
                wait = min(wait, _MAX_429_WAIT - waited)
                if wait <= 0:
                    raise
                waited += wait
                print(console.paint(
                    f"⏳ LLM 限流(429)，等待 {wait:.0f}s 后自动重试…",
                    console.YELLOW), flush=True)
                # 分片等待：被打断立即退出，不等满整个限流窗口
                deadline = time.time() + wait
                while time.time() < deadline:
                    if _cancelled():
                        raise _DrainCancelled()
                    time.sleep(min(0.2, max(0.0, deadline - time.time())))
    except _DrainCancelled:
        # P1-1：被打断——静默退出（finally 仍推 None 哨兵，主协程已取消不消费）
        return
    except Exception as e:
        # 429 自动重试耗尽 → 中文友好提示；其他错误原样上报
        if isinstance(e, RateLimitError) or "429" in str(e):
            loop.call_soon_threadsafe(
                q.put_nowait,
                "__RATELIMIT__::你的 LLM 账户达到速率限制（429），"
                "自动等待重试后仍被限流。\n"
                "  免费模型限 1 并发，高峰期请稍等片刻再提问。",
            )
        else:
            loop.call_soon_threadsafe(q.put_nowait, f"__ERROR__::{e}")
    finally:
        # 把可能回退过的路由状态写回 ctx（调用方需要 route_name 判断是否记录）
        ctx["route_name"] = route_name[0]
        ctx["route_client"] = route_client[0]
        ctx["route_model"] = route_model[0]
        loop.call_soon_threadsafe(q.put_nowait, None)  # 结束哨兵
