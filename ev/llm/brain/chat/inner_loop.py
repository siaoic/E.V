"""_chat_stream_inner 主循环：system prompt 组装 + 多轮工具调用流式消费。

模块级函数 `_run_chat_stream_inner(self, ...)` 与原 `_ChatMixin._chat_stream_inner`
的方法体逐字一致，仅把 self 作为第一参数传入；最后把副作用收尾委托给
`inner_tail._chat_stream_tail`。
"""

import asyncio
import time
from typing import AsyncGenerator, List, Optional

from ev.utils import config, console
from tools.memory import memory
from plugins.builtin.tools.skills import get_skill_manager
from ev.llm.cleaners.api import _clean_messages_for_api
from ev.llm.cleaners.content import (
    _clean_sentence,
    _filter_thinking_content,
    _remove_tool_calls_from_content,
)
from ev.llm.cleaners.sentence import (
    _find_pause_end_from,
    _find_sentence_end_from,
    _split_sentences,
)
from ev.llm.client.factory import (
    build_thinking_extra_body,
)
from ev.llm.client.retry import _parse_retry_after
from ev.llm.utils.constants import (
    _MAX_429_WAIT,
    _SUMMARIZE_MIN_TURNS,
)
from ev.llm.tools.executor import _execute_tool_calls
from ev.llm.tools.formatter import _format_tool_calls
from ev.llm.tools.parser import _parse_qwen_tool_calls
from ev.llm.utils.content_check import has_content
from ev.utils.perf_tracker import PerfTracker

from .inner_tail import _chat_stream_tail

# 记忆召回硬超时（秒）：超时熔断跳过注入，保障首字延迟不被检索拖垮
_MEMORY_RECALL_TIMEOUT = 1.5

# 段落早产切分（压 TTS 首句延迟，让「从 LLM 第一个字开始合成」落地）：
# LLM 流式产字不等句末，尽早切出可合成段交 TTS（首块 ~200ms 即出声）——
# - 句末标点（。！？…）必切（原有行为，最自然边界）；
# - 停顿标点（逗号/顿号）且当前段 ≥ _PAUSE_SEGMENT_MIN_CHARS 字时切：
#   逗号是中文自然停顿点，切成独立段合成播放不显突兀；
# - 首段早产：首个可合成段攒够 _FIRST_SEGMENT_MIN_CHARS 字就硬切（LLM 还在
#   继续吐），不等待整句生成完；
# - 无标点超长段（≥ _MAX_SEGMENT_CHARS）强制切，防播放头被长句拖住。
_FIRST_SEGMENT_MIN_CHARS = 6    # 首段早产字数（太小 GSV 短文本合成不稳）
_PAUSE_SEGMENT_MIN_CHARS = 4    # 停顿标点切段的最短段长（防"啊，嗯，"被单切）
_MAX_SEGMENT_CHARS = 30         # 无标点强制切段上限


async def _run_chat_stream_inner(
    self,
    user_text: str,
    *,
    proactive: bool,
    history: Optional[list],
) -> AsyncGenerator[str, None]:
    """chat_stream 实际实现体（信号量外），保持原有逻辑不变。

    最后调用 `_chat_stream_tail(self, state)` 处理历史保存等副作用收尾。
    """
    tracker = PerfTracker("LLM")
    tracker.begin("首字延迟")     # 从调用到第一个 content chunk
    tracker.begin("总生成")       # 整个流程（含工具调用）耗时

    # 注入记忆上下文（严格参照 memU hosts/instruction.py 的 standing instruction）：
    # 1) 记忆使用说明常驻系统提示（segments/files 两层渐进 + fail-open）；
    # 2) 检索结果按 memU hosts/retrieval.py _shape_for_agent 的三层形状注入。
    #    Embedding 不可用/失败时自动回退 LLM 检索（仍输出同形状）。
    # 系统提示分层组装（对标 Hermes「prompt caching is sacred」）：
    # system 前缀保持字节稳定 → 服务端自动前缀缓存命中（降本/提速）。
    # stable 段（人设/工具说明/长期记忆/技能/策略，会话内稳定或低频变化）
    # 放前缀；volatile 段（知识/记忆检索/画像/插件本轮背景，每轮变化）放
    # 尾部。各段内容与升级前完全一致，仅调整段间相对顺序；
    # PROMPT_CACHE_MODE=0 时按原交错顺序拼装（行为与升级前一致）。
    sections: List[tuple] = []  # (is_stable: bool, content: str)

    # 1. 人设（stable）
    sections.append((True, self.cfg.SYSTEM_PROMPT))
    # 2. 工具能力引导 + 清单 + MCP 说明（stable，会话内稳定）
    tools = self._get_tools()
    if tools:
        tool_block = (
            """                
            \n\n### 工具使用\n
            你可以调用函数工具完成实际任务（联网搜索、抓取网页、查询时间天气、
            加载技能、读写记忆等）。当用户问需要实时/最新信息、新闻、资料、
            事实核查的问题时，必须先调用下方列出的搜索/抓取网页工具获取真实
            结果再回答，不要说自己无法联网搜索——工具列表已提供给你。"""
        )
        from plugins.builtin.tools import render_tool_guide
        tool_guide = render_tool_guide(tools)
        if tool_guide:
            tool_block += "\n\n" + tool_guide
        mcp_desc = self._describe_mcp_servers(self.mcp)
        if mcp_desc:
            tool_block += "\n\n### 可用的联网服务器\n" + mcp_desc
        sections.append((True, tool_block))
    # 3. 知识库（volatile）：信号闸门命中才追加权威设定段（防幻觉；
    #    闲聊/无关消息返回空串不注入，省 Token）。数据懒加载，进程内缓存一次。
    knowledge_section = self._knowledge_section(user_text)
    if knowledge_section:
        sections.append((False, knowledge_section))
    # 4. L2 内建长期记忆（MEMORY.md/USER.md 冻结快照，跨会话持久；stable）
    curated_section = self._curated_memory_section()
    if curated_section:
        sections.append((True, curated_section))
    # 5. 记忆召回（volatile）：记忆使用说明 + 本轮检索结果。写入完全交给
    #    管家模型（ButlerAgent 每轮从对话提取，参照 <memory> 标签）
    if self.cfg.MEMORY_ENABLED:
        mem_block = memory.STANDING_INSTRUCTION
        try:
            # 硬超时熔断：召回超过 1.5s 直接跳过注入，优先保障响应速度
            mem_ctx = await asyncio.wait_for(
                memory.retrieve(user_text), timeout=_MEMORY_RECALL_TIMEOUT)
        except asyncio.TimeoutError:
            console.warn("[记忆检索] 召回超时（>1.5s），熔断跳过本次注入")
            mem_ctx = ""
        if mem_ctx:
            mem_block += (
                "\n\n### 检索到的记忆（segments / files，按相关度排序）\n"
                + mem_ctx
            )
        sections.append((False, mem_block))
    # 6. 观众画像（volatile）：进化引擎复盘的长期事实，关键词召回补充向量记忆
    # 5.1：画像/技能索引/话术建议/GEPA 策略统称「进化注入段」——
    # EVOLUTION_INJECT_IN_USER=1 时迁移到 user 消息尾部（近因效应 + 保持
    # system 前缀字节稳定以命中提示缓存，对标 hermes「可变内容注入 user 消息」）；
    # =0 时回退旧行为（按原 stable/volatile 分组拼进 system）。
    user_tail: List[str] = []

    def _inject_evolution(block: str, stable: bool) -> None:
        if config.cfg.EVOLUTION_INJECT_IN_USER:
            user_tail.append(block)
        else:
            sections.append((stable, block))

    profile_section = self._profile_section(user_text)
    if profile_section:
        _inject_evolution(profile_section, False)
    # 7. 技能段（stable）：只列技能名+描述（轻量），完整指令由 load_skill
    #    工具按需加载（严格参照 Muika agent.py：系统提示 = 人设 + Available skills）
    skills_section = get_skill_manager().render_prompt_section()
    if skills_section:
        _inject_evolution(skills_section, True)
    # 8. 生效中的话术建议（stable，低频变化）：进化引擎沉淀，到期由复盘回评续期/移除
    advice_section = self._active_advice_section()
    if advice_section:
        _inject_evolution(advice_section, True)
    # 9. GEPA 进化策略段（stable，低频变化）：变异 → 评审择优落盘，
    #    与话术建议互补——策略是长期行为准则，建议是短期话术优化
    policy_section = self._policy_section()
    if policy_section:
        _inject_evolution(policy_section, True)
    # 10. 插件注入：on_user_input 本轮背景上下文（volatile，一次性）
    #     + add_system_prompt_patch 长期提示（stable）
    if self.plugin_manager is not None:
        turn_contexts = self._pop_turn_context()
        if turn_contexts:
            sections.append((
                False,
                "### 插件补充背景（仅本轮对话参考，不要向用户复述）\n"
                + "\n".join(f"- {t}" for t in turn_contexts),
            ))
        patch_section = self.plugin_manager.system_prompt_patch_section()
        if patch_section:
            sections.append((True, patch_section))

    if self.cfg.PROMPT_CACHE_MODE:
        # 缓存友好：stable 段作前缀（字节稳定可命中服务端前缀缓存），
        # volatile 段作尾部（每轮重建，只影响后缀）
        stable_block = "\n\n".join(s for stable, s in sections if stable)
        volatile_block = "\n\n".join(s for stable, s in sections if not stable)
        sys_content = stable_block
        if volatile_block:
            sys_content += "\n\n" + volatile_block
    else:
        # 回退模式：原交错顺序，与升级前字节一致
        sys_content = "\n\n".join(s for _, s in sections)
    messages: List[dict] = [{"role": "system", "content": sys_content}]
    # 注入早期对话摘要（历史裁剪时后台压缩生成，跨会话继承）
    summary = self._consume_summary()
    if summary:
        messages.append({
            "role": "system",
            "content": f"【早期对话摘要（以下为被压缩掉的更早对话内容）】{summary}",
        })
    # agent 主动发言：只带精简历史快照（最近 N 条），降低 token 消耗
    messages.extend(history if history is not None else self.history)
    messages.append({"role": "user", "content": user_text})
    # 5.1：进化注入段（画像/技能索引/话术建议/GEPA 策略）作为独立 system
    # 消息追加在 user 之后（与技能预判提示段同模式：近因效应，且不改动
    # system 前缀字节，最大化提示缓存命中；段内容与旧拼装完全一致）
    if user_tail:
        messages.append({
            "role": "system",
            "content": "### 技能与进化上下文（供本轮参考，不要向用户复述）\n\n"
                       + "\n\n".join(user_tail),
        })
    # 技能意图预判（本地 match_intent，零依赖）：命中时在 user 后追加
    # 提示段（近因效应），要求模型优先 load_skill——主动发言是 AI 内部
    # 指令非用户输入，不参与预判，避免误匹配。
    if not proactive:
        skill_hint = self._skill_intent_hint(user_text)
        if skill_hint:
            messages.append({"role": "system", "content": skill_hint})
    # Author's Note 尾部人设锚点（近因效应）：追加在 user 消息之后，
    # 比 system prompt 开头更有效锁定语气/格式；默认空 = 不注入。
    anchor = self._tail_anchor_section()
    if anchor:
        messages.append({
            "role": "system",
            "content": f"【人设锚点（务必遵守）】{anchor}",
        })

    # 插件钩子：on_llm_request（可在请求发出前修改 messages）
    if self.plugin_manager is not None:
        from plugins.base import LLMRequestEvent
        request = LLMRequestEvent(messages)
        await self.plugin_manager.run_llm_request_hooks(request)
        messages = request.messages

    # 模型路由进化：本轮按 UCB1 选择要用的 LLM 服务（未启用时均为
    # None → 沿用 self.client / LLM_MODEL，与原有行为完全一致）
    route_name = None
    route_client = None
    route_model = None
    if self.router is not None:
        route_name = self.router.select()
        if route_name is not None:
            route_client = self.router.client_for(route_name)
            service = self.router.service(route_name)
            route_model = (service or {}).get("model") or None

    loop = asyncio.get_running_loop()
    iteration = 0            # 工具调用轮数（含空响应催促轮）
    empty_count = 0          # 连续空响应计数
    tool_call_total = 0      # 工具调用总次数
    sentence_count = 0       # 已产出句数
    final_reply: Optional[str] = None
    sound_effect_used = False  # 本轮已播放音效：抑制音效后的文字/语音回复

    # ===== 多轮工具调用循环（对标 llm-handler.js 的 while (iteration < maxIterations)） =====
    max_tool_iterations = self._max_tool_iterations()
    while iteration < max_tool_iterations:
        q: asyncio.Queue = asyncio.Queue()
        tool_calls_acc: List[Optional[dict]] = []
        full_raw: List[str] = []
        reasoning_raw: List[str] = []   # 思考内容增量（DeepSeek 多轮需回传）
        round_content: List[str] = []
        _first_content = True

        # 子线程：同步迭代流式响应（详情见 stream_drainer._run_stream_drainer）。
        # ctx 中的可变容器（list/dict）子线程直接读写；路由回退会通过 ctx 写回。
        from .stream_drainer import _run_stream_drainer
        _first_content_ref = [_first_content]   # list 包装以便子线程修改首字标记
        ctx = dict(
            messages=messages, tools=tools, self=self,
            route_name=route_name, route_client=route_client,
            route_model=route_model,
            loop=loop, q=q, tool_calls_acc=tool_calls_acc, full_raw=full_raw,
            reasoning_raw=reasoning_raw,
            _first_content=_first_content_ref, tracker=tracker,
        )
        bg_task = loop.run_in_executor(None, _run_stream_drainer, ctx)

        # 首轮思考过程提示
        if iteration == 0 and self.cfg.LLM_THINKING:
            print(console.paint("💭 思考过程：", console.GRAY), end="", flush=True)

        # 主协程：消费 content 增量，按句切分 yield
        buffer = ""
        scanned = 0  # buffer[:scanned] 已确认无句末符号，增量扫描起点

        def _emit(sentence: str) -> str:
            """清洗、保存记忆、计数并返回清理后的文本。空文本返回 ''。"""
            nonlocal sentence_count
            cleaned = _clean_sentence(sentence)
            # 纯标点/符号/空白句（无实质文字）直接丢弃：
            # GPT-SoVITS 对这类文本会合成退化，输出拖长音怪叫
            if not cleaned.strip() or not has_content(cleaned):
                return ""
            round_content.append(cleaned)
            sentence_count += 1
            return cleaned

        while True:
            item = await q.get()
            if item is None:
                break
            if isinstance(item, str) and item.startswith("__ERROR__::"):
                console.error(f"LLM 调用失败：{item[len('__ERROR__::'):]}")
                await bg_task
                tracker.end("总生成", f"{sentence_count} 句（出错中断）")
                tracker.print_report()
                return
            if isinstance(item, str) and item.startswith("__RATELIMIT__::"):
                console.warn(item[len("__RATELIMIT__::"):])
                await bg_task
                tracker.end("总生成", f"{sentence_count} 句（限流）")
                tracker.print_report()
                return
            buffer += item

            # 增量切段（压 TTS 首句延迟）：每 chunk 只扫描新增区域
            # （scanned 之后），切出段后剩余 buffer 从头开始。
            # 边界优先级：句末标点 > 停顿标点（逗号/顿号） > 首段早产 > 超长兜底，
            # 保证 LLM 第一个字到达后尽早产出可合成段，TTS 首块即出声。
            while True:
                idx = _find_sentence_end_from(buffer, scanned)
                if idx < 0:
                    if len(buffer) >= _PAUSE_SEGMENT_MIN_CHARS:
                        # 从 0 全扫：句末标点才保证 scanned 前无边界，
                        # 逗号可能出现在旧 buffer（不足 4 字时未切）中
                        idx = _find_pause_end_from(buffer, 0)
                    if idx < 0 and not round_content \
                            and len(buffer) >= _FIRST_SEGMENT_MIN_CHARS:
                        # 首段早产：首个可合成段攒够字数即切（不等句末），
                        # 让 TTS 与 LLM 并行——边生成边合成，首声只等首块
                        idx = len(buffer) - 1
                    if idx < 0 and len(buffer) >= _MAX_SEGMENT_CHARS:
                        idx = len(buffer) - 1  # 无标点超长段兜底
                if idx < 0:
                    break
                sentence = buffer[: idx + 1]
                buffer = buffer[idx + 1:]
                scanned = 0
                cleaned = _emit(sentence)
                if cleaned and not sound_effect_used:
                    yield cleaned
            scanned = len(buffer)

        # 收尾：剩余内容作为最后一句
        cleaned = _emit(buffer)
        if cleaned and not sound_effect_used:
            yield cleaned

        await bg_task
        # 同步子线程可能写回的路由状态（服务不可用时被回退为 None/默认值），
        # 下一轮循环用最新状态（stream_drainer 已把成功记录记到 router 上）。
        route_name = ctx["route_name"]
        route_client = ctx["route_client"]
        route_model = ctx["route_model"]

        # ---- 流结束：判断本轮结果（对标 llm-client.js _handleStreamResponse）----
        raw_content = "".join(full_raw)
        # 先过滤思考内容，再解析工具调用（与 JS 顺序一致）
        clean_content = _filter_thinking_content(raw_content)
        tool_calls = [tc for tc in tool_calls_acc if tc and tc["function"]["name"]]

        # Qwen 文本格式工具调用解析（对标 llm-client.js _parseQwenToolCalls）
        if not tool_calls:
            parsed = _parse_qwen_tool_calls(clean_content)
            if parsed:
                tool_calls = parsed
                console.info(f"🔧 解析到 {len(parsed)} 个 Qwen 文本格式工具调用")
                # 从 content 中移除工具调用文本，只保留文本回复（对标 _removeToolCallsFromContent）
                clean_content = _remove_tool_calls_from_content(clean_content)

        if tool_calls:
            # ===== 执行工具并进入下一轮（对标 llm-handler.js） =====
            iteration += 1
            tool_call_total += len(tool_calls)
            console.accent(f"===== 🔧 第 {iteration} 轮工具调用 =====")
            console.accent(_format_tool_calls(tool_calls))

            # 本轮含音效播放工具：音效播放后不再语音/文字回复（直播场景
            # 只要音效效果，多余的总结会打断听感）。标记后后续轮次的
            # 文本只进历史上下文、不 yield 播出。
            if any(
                (tc.get("function") or {}).get("name") == "play_sound_effect"
                for tc in tool_calls
            ):
                sound_effect_used = True

            # 1) assistant 消息（含 tool_calls；content 为 null 时
            #    由 _clean_messages_for_api 兜底转为 ''，兼容严格模式 API）
            assistant_msg = {
                "role": "assistant",
                "content": clean_content.strip() or None,
                "tool_calls": tool_calls,
            }
            # 思考模式模型（DeepSeek 等）：reasoning_content 必须原样回传，
            # 缺失会触发 400（The reasoning_content ... must be passed back）。
            reasoning_text = "".join(reasoning_raw).strip()
            if reasoning_text:
                assistant_msg["reasoning_content"] = reasoning_text
            messages.append(assistant_msg)

            # 2) 执行工具 → tool 响应消息（工具链完整进入下一轮上下文）
            tool_messages = await _execute_tool_calls(self.mcp, tool_calls)
            messages.extend(tool_messages)
            continue

        # ===== 无工具调用 =====
        if not clean_content.strip():
            if sound_effect_used:
                # 音效播放后无需文字回复：空响应直接结束，不再催促
                break
            # 空响应处理（对标 llm-handler.js consecutiveEmptyResponses：
            # 第 1 次催模型回复，第 2 次仍空则放弃）
            empty_count += 1
            if empty_count == 1:
                console.warn("⚠️ 空响应，添加提示消息催促模型回复")
                messages.append({
                    "role": "user",
                    "content": "请根据工具执行结果，回复用户。",
                })
                iteration += 1
                continue
            console.error(f"❌ 连续 {empty_count} 次空响应，放弃等待")
            final_reply = "抱歉，我好像卡住了，请重新问我吧~"
            yield final_reply
            break

        # 正常产出文本，结束（本轮已流式 yield 的句子即最终回复）
        final_reply = "".join(round_content)
        break

    # ===== 达到最大工具轮数：非流式强制获取最终回复（对标 llm-handler.js L684-696）=====
    if final_reply is None and iteration >= max_tool_iterations:
        console.warn(f"⚠️ 已达到最大工具调用次数限制（{max_tool_iterations} 轮），"
                     "非流式获取最终回复")
        final_reply = await self._request_final_reply(messages)
        for seg in _split_sentences(final_reply):
            cleaned = _clean_sentence(seg)
            if cleaned.strip() and not sound_effect_used:
                yield cleaned

    # 思考过程换行
    if self.cfg.LLM_THINKING:
        print()

    meta = f"{sentence_count} 句"
    if tool_call_total:
        meta += f"，{tool_call_total} 次工具调用"
    tracker.end("总生成", meta)

    # ===== 副作用收尾：委托 inner_tail（无 yield） =====
    await _chat_stream_tail(self, {
        "messages": messages,
        "history": history,
        "user_text": user_text,
        "proactive": proactive,
        "final_reply": final_reply,
        "sound_effect_used": sound_effect_used,
        "tracker": tracker,
        "sentence_count": sentence_count,
        "tool_call_total": tool_call_total,
    })
