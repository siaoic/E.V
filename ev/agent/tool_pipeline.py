"""工具执行三段式管线：pre-execute / execute / post-execute（对齐 dsh tools.md）。

对照 deepseek-harness docs/subsystems/tools.md 的 waterfall 设计：
- tools/pre-execute waterfall：policy / approval / sandbox，可短路拒绝
    → E.V：插件 on_tool_call（deny / replace）+ L2-C schema 校验 + L2-B budget stub
- monotonic guard：最终拒绝位（deny-only，pre-execute 判定后不再进入 execute）
    → E.V：pre-execute 返回 dict 即短路，execute 只处理放行工具
- tools/execute waterfall：timeout / retry / metrics wrapper
    → E.V：wait_for 超时兜底 + 失败后台延迟重试（不阻塞本轮 gather）
- tools/post-execute waterfall：accept / block / replace / add context
    → E.V：结果控制台展示 + 单轮熔断计数更新 + tool/result 事件广播

设计约束：
- 对外返回格式与既有约定完全一致
  （{"role": "tool", "name", "tool_call_id", "content"}）；
- 不引入第三方库；plugins 相关导入惰性化，避免启动期循环导入。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from ev.utils import console
from ev.kernel.bus import bus, EV_TOOL_CALL, EV_TOOL_RESULT
from ev.llm.utils.constants import _MAX_ROUND_TOOL_CHARS
from ev.llm.tools.formatter import _format_search_result, _format_tool_result

# 单工具执行超时（秒）：MCP 远程调用/联网搜索卡住时不拖死整轮 gather
_TOOL_TIMEOUT = 10.0
# 失败后后台重试的延迟（秒）
_RETRY_DELAY = 1.0

# 工具-参数适配守卫（deny + 改道路由）：key=工具名，
# value=(允许的扩展名元组, 改道指引文本)。
# 背景：GLM 系模型实测会把「给我弹<乐谱图片>」误路由到 play_midi_file，
# 且 path 被工具描述里的示例目录带偏成 e:/sheet/...（连续 5 轮 ENOENT）。
# 在 pre-execute 确定性拦截，模型下一轮即按指引改用 read_sheet_music。
_TOOL_ARG_GUARDS = {
    "play_midi_file": (
        (".mid", ".midi"),
        "play_midi_file 仅支持 .mid/.midi 文件，传入的不是 MIDI 文件。"
        "若用户给的是乐谱图片（jpg/png 等），必须改用 read_sheet_music 工具："
        "path 原样传用户提供的路径/文件名（相对路径按工作目录解析，禁止自行补目录"
        "或改写），识谱成功后按其返回的 score_path 调用 play_score 弹奏。",
    ),
}


class ToolPipeline:
    """三段式工具执行管线（进程内单例 tool_pipeline）。"""

    async def execute(self, mcp, tc: dict, state: dict) -> dict:
        """三段式执行单个工具调用，返回 role=tool 消息。

        state 为共享熔断计数（{"round_chars", "truncated"}），与
        ev/llm/tools/executor.py 既有约定一致；L2-A 流式期间提前启动
        工具时逐工具调用本入口，行为与批模式完全一致。
        """
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"] or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        tool_call_id = tc.get("id") or f"call_{name}"

        # tool/call 事件：管线入口广播（durable 日志 / 外部订阅，对标 dsh tool/call）
        await bus.emit(EV_TOOL_CALL, {"id": tool_call_id, "name": name, "args": args})

        # -- pre-execute waterfall：可短路拒绝（monotonic guard：deny-only） --
        verdict = await self._pre_execute(name, args, tool_call_id, state)
        if verdict is not None:
            return verdict

        # -- execute waterfall：timeout + 失败后台重试 --
        result = await self._call_tool_with_retry(name, args, mcp)

        # -- post-execute waterfall：展示 + 熔断计数 + 事件 --
        return await self._post_execute(name, tool_call_id, result, state)

    # -- pre-execute ---------------------------------------------------------

    async def _pre_execute(
        self, name: str, args: dict, tool_call_id: str, state: dict,
    ) -> Optional[dict]:
        """pre-execute 策略：插件 on_tool_call → L2-C schema 校验 → L2-B 预算 stub。

        返回 None 表示放行进入 execute；返回 dict 表示短路（该 dict 即最终
        tool 消息，后续阶段不再执行——monotonic guard）。
        """
        from plugins.base import ToolCallEvent
        from plugins.manager import get_default_manager

        # 插件 on_tool_call 钩子：可拦截（deny）/ 替换结果
        pm = get_default_manager()
        if pm is not None:
            event = ToolCallEvent(name, args)
            await pm.run_tool_call_hooks(event)
            if event.denied:
                console.warn(f"  ↳ 「{name}」被插件拦截：{event.denied}")
                return {"role": "tool", "name": name,
                        "tool_call_id": tool_call_id,
                        "content": f"[工具 {name} 被拦截：{event.denied}]"}
            if event.replaced is not None:
                console.dim(f"  ↳ 「{name}」结果由插件替换")
                return {"role": "tool", "name": name,
                        "tool_call_id": tool_call_id,
                        "content": event.replaced}

        # 工具-参数适配守卫：扩展名不符直接改道指引（deny-only，不执行）
        guard = _TOOL_ARG_GUARDS.get(name)
        if guard is not None:
            _p = str((args or {}).get("path") or "").strip()
            if _p and not _p.lower().endswith(guard[0]):
                console.warn(
                    f"  ↳ 「{name}」参数非 {guard[0]} 文件（…{_p[-50:]}），拦截并改道")
                return {"role": "tool", "name": name,
                        "tool_call_id": tool_call_id,
                        "content": f"[{guard[1]}]"}

        # L2-B pre-execute budget：本轮预算已耗尽 → 不再真实执行，直接 stub
        # （模型依旧能看到「有工具被熔断」的提示，省去无谓的工具执行时间）
        if state["truncated"]:
            return {"role": "tool", "name": name,
                    "tool_call_id": tool_call_id,
                    "content": (f"[后续工具结果因本轮累计超过 "
                                f"{_MAX_ROUND_TOOL_CHARS // 1000}K 字符被截断]")}

        # L2-C schema 校验：参数不合法直接转 INVALID_ARGS，不启动真实工具
        # （注册表外的工具 / 无 parameters 的 schema 一律放行，不改变既有行为）
        from ev.agent.tool_registry import tool_registry
        arg_errors = tool_registry.validate_args(name, args)
        if arg_errors:
            console.warn(f"  ↳ 「{name}」参数校验失败：{'；'.join(arg_errors)}")
            return {"role": "tool", "name": name,
                    "tool_call_id": tool_call_id,
                    "content": json.dumps({
                        "error": "INVALID_ARGS",
                        "name": name,
                        "details": arg_errors,
                    }, ensure_ascii=False)}

        return None

    # -- execute -------------------------------------------------------------

    async def _call_tool_with_retry(self, name: str, args: dict, mcp) -> Any:
        """单工具执行：wait_for 超时兜底；失败不阻塞 gather，改后台延迟重试。

        返回成功结果；超时返回占位（不再让单个工具拖死整轮）；
        首次失败立刻返回占位并后台重试（重试结果仅打印控制台，
        不进本轮 LLM context——本轮已按占位继续，避免二次等待）。

        超时按工具放宽：注册超时（如 read_sheet_music 1500s）> 平铺
        _TOOL_TIMEOUT 时按注册值执行，长耗时工具不再被一刀切掐断。
        """
        from plugins.builtin.tools import call_tool
        from ev.agent.tool_registry import resolve_tool_timeout

        timeout = max(_TOOL_TIMEOUT, resolve_tool_timeout(name))
        try:
            return await asyncio.wait_for(
                call_tool(name, args, mcp), timeout=timeout)
        except asyncio.TimeoutError:
            console.warn(
                f"  ↳ 「{name}」执行超时（>{timeout:.0f}s），本轮返回占位")
            return f"[{name} 执行超时，本轮跳过]"
        except Exception as e:
            console.warn(f"  ↳ 「{name}」首次失败（{e}），后台重试...")
            asyncio.create_task(self._retry_later(name, args, mcp))
            return f"[{name} 执行失败，已转后台重试]"

    async def _retry_later(self, name: str, args: dict, mcp) -> None:
        """后台延迟重试一次：成功后仅控制台展示结果（本轮 LLM 已继续）。"""
        from plugins.builtin.tools import call_tool

        await asyncio.sleep(_RETRY_DELAY)
        try:
            result = await call_tool(name, args, mcp)
            console.accent(f"  ↳ 「{name}」后台重试成功")
            if isinstance(result, dict) and isinstance(result.get("results"), list):
                console.accent(_format_search_result(name, result))
            else:
                console.dim(_format_tool_result(name, result))
        except Exception as e:
            console.warn(f"  ↳ 「{name}」后台重试也失败：{e}")

    # -- post-execute --------------------------------------------------------

    async def _post_execute(
        self, name: str, tool_call_id: str, result: Any, state: dict,
    ) -> dict:
        """post-execute：结果控制台展示 + 单轮熔断计数更新 + tool/result 事件。"""
        if isinstance(result, dict) and isinstance(result.get("results"), list):
            # 搜索类结果：逐条醒目展示（标题/链接/摘要），便于直播时直接读取
            console.accent(_format_search_result(name, result))
        else:
            console.dim(_format_tool_result(name, result))
        # 单轮累计熔断：更新计数，超限标记供后续工具 pre-execute 截断
        if isinstance(result, str):
            state["round_chars"] += len(result)
        if state["round_chars"] > _MAX_ROUND_TOOL_CHARS:
            state["truncated"] = True
            result = (f"[后续工具结果因本轮累计超过 "
                      f"{_MAX_ROUND_TOOL_CHARS // 1000}K 字符被截断]")
        await bus.emit(EV_TOOL_RESULT, {"name": name, "content": result})
        return {"role": "tool", "name": name,
                "tool_call_id": tool_call_id,
                "content": result}


# 进程内单例：ev/llm/tools/executor.py 作为兼容入口转发到本管线
tool_pipeline = ToolPipeline()
