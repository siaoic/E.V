"""主动对话引擎 —— 由 LLM 自主决定「想说就说」，无时间门槛。

机制（自主开口，取代 Muika 的定时触发）：
  loop.collect_events 心跳          → heartbeat() 事件驱动检查（互动结束立即触发）
  state.MuikaState 状态累积/阈值     → 删除：不再由孤独/无聊/冷却/随机点决定是否开口
  loop.get_think_mode 触发判定       → _decide_and_generate()：主模型自主判断
                                       （想说就生成发言，不想说输出 <SILENT>）
  loop._run_topic_pipeline           → 灵感话题（可选）：主模型可顺着聊，也可自由发挥
  topic_manager.TopicManager         → _pick_topic()（权重 / 冷却 / 互动率评分）

开口机会（只决定「多久给一次机会」，是否说话完全由 LLM 自主）：
  - 互动结束（on_user_message）→ _wakeup 事件立即给一次机会
  - 静默期 → 从共用随机间隔范围（RESPONSE_INTERVAL_MIN~MAX）给一次机会

agent 分工（主动发言交给 agent，agent 催促主模型）：
  - ButlerAgent.build_proactive_prompt：组装主模型的自主开口请求——灵感话题、
    时段语气、记忆线索注入；prompt 明确允许选择沉默（输出 <SILENT>）
  - 主模型（brain.chat_stream）：决策 + 生成发言内容；`<SILENT>` → 沉默
  - 播报由 stream.speak_text 直接播放预生成文本（不再二次调用 LLM）
  - agent 负责发言后的记忆蒸馏（仅情感发言蒸馏为 self 记忆，话题闲聊不蒸馏）
  - 关闭记忆（无 agent）时回退内置简化 prompt

保留的非时间类保护（防冲突，不是说话时间限制）：
  - 忙碌抑制：主 LLM 推理 / 播报进行中不开口（避免抢话）
  - 弹幕回复已敲定：弹幕优先，主动不抢话
  - 话题活跃期：刚聊完一个话题不立刻又开口（防打断，超时自动结束）
  - 队列防堆积 / 内容去重 / 用户输入优先丢弃排队
"""

import asyncio
import os
import random
import sys
import time
from collections import deque
from datetime import datetime
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

import yaml

from src.utils import console
from src.llm import stream
from src.core.output_lock import (
    STATE_AGENT_THINKING, STATE_AI_SPEAKING, STATE_IDLE,
    get_output_lock, set_output_owner, set_global_state, is_idle,
    is_danmaku_pending,
)

# ======================================================================
# 话题权重 —— 严格对齐 Muika-After-Story
# （muika/core/topic_manager.py）
# ======================================================================

# —— 自主开口（LLM 自主决定，无时间门槛）——
# 主模型输出以下任一标记视为「此刻不想说」→ 保持沉默（幂等匹配）
_SILENT_MARKERS = (
    "<SILENT>", "SILENT", "沉默", "保持沉默", "不想说", "此刻不想说",
    "暂无", "无", "没有", "算了", "不了", "NONE", "NULL",
)

# —— 话题类别权重（对标 topic_manager.TOPIC_WEIGHTS）——
# 对齐 topics.yml 实际分类：neuro（日常脑洞/情绪）为主，neuro_fact（冷知识）、
# neuro_story（故事）次之，learned（自我进化沉淀的互动话题）低频兜底
_TOPIC_WEIGHTS: Dict[str, float] = {
    "neuro": 0.35,
    "neuro_fact": 0.25,
    "neuro_story": 0.25,
    "learned": 0.15,
}
# 近期已聊类别的权重惩罚（对标 _RECENT_TYPE_PENALTY，窗口 3，避免连续同主题）
_RECENT_TYPE_PENALTY: float = 0.25
_RECENT_TYPE_WINDOW: int = 3

# —— 互动率评分（对标 TopicManager._get_available_candidates）——
# 话题用过 ≥2 次后按互动率降权：<30% → 0.3，<50% → 0.6（聊过没人理的话题
# 会越来越少出现）。
_INTERACTION_LOW: float = 0.3
_INTERACTION_MID: float = 0.6

# —— 话题活跃期（对标 state.ActiveTopicState）——
# 话题发言后进入活跃期：期间不触发任何主动发言（防情绪管线打断话题）。
# 用户回应 → 结束话题并记互动；超过本超时（秒）无回应自动结束。
_ACTIVE_TOPIC_TIMEOUT: float = 120.0

# 话题种子源：完整话题表（157 条，含 id/category/concept/tags/cooldown_minutes），
# 内容取自 Muika-After-Story configs/topics.yml。原字段 cooldown_days（天），
# 直播场景按 Muika 分类原理缩放到分钟制：7天→1min、10天→2min、14天→3min、
# 21天→5min、30天→7min（trivia 可高频复用，story/meta 间隔更长）。
_TOPICS_PATH = (
    os.path.join(sys._MEIPASS, "src", "llm", "topics.yml")
    if getattr(sys, "frozen", False)
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), "topics.yml")
)


def _load_topic_seeds() -> List[dict]:
    """从 topics.yml 加载话题种子（对标 TopicStore._load）。

    每条含 id / category / concept / tags / cooldown_days；
    加载失败返回空列表（话题管线退化为仅孤独驱动，不阻断主流程）。
    """
    try:
        with open(_TOPICS_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, ValueError) as e:
        console.error(f"[主动] 加载话题种子失败：{e}（话题管线停用）")
        return []
    seeds = []
    for entry in data.get("topics", []):
        concept = entry.get("concept", entry.get("content", ""))
        if not concept:
            continue
        seeds.append({
            "id": entry["id"],
            "category": entry.get("category", "misc"),
            "concept": concept,
            "tags": entry.get("tags", []),
            # 冷却时长（分钟）：按 Muika 原始 cooldown_days 缩放（trivia 1min ~ meta 7min）
            "cooldown_minutes": int(entry.get("cooldown_minutes", 7)),
        })
    return seeds


class ProactiveEngine:
    """主动对话引擎：事件驱动心跳 → 灵感话题 → LLM 自主决策 → 后台播报。

    主循环 `_wait_input` 等待用户输入，互动/弹幕回复结束的唤醒事件到达时
    调用 `heartbeat()`；静默期靠 next_wake_in() 单次精确唤醒（到点才醒一次，
    无周期轮询）。触发后由 ButlerAgent（若启用）组装自主开口请求，主模型
    自主判断想说就说 / 不想说输出 <SILENT> 保持沉默；发言文本由
    stream.speak_text 直接播报，走与用户对话相同的 TTS/字幕/口型管线。
    """

    def __init__(self, brain, tts, face, sub, cfg,
                 butler=None, memory_manager=None,
                 profanity_filter=None, profanity_filter_rate: float = 0.7) -> None:
        self.cfg = cfg
        self.brain = brain
        self.tts = tts
        self.face = face
        self.sub = sub
        self.butler = butler          # agent：组装发言请求 + 发言后记忆蒸馏
        self.mm = memory_manager      # 可选：记忆上下文注入 + 记入会话轮次
        self.pf = profanity_filter    # 可选：仅过滤 AI 播报句子，不过滤用户/弹幕输入
        self.pf_rate = profanity_filter_rate
        # 全局输出互斥锁（模块单例，三方共用） + 说话者身份标记
        self._output_lock = get_output_lock()

        # 状态：距上次互动的时间戳（供自主决策 prompt 提供环境上下文）
        self.last_interaction = time.time()
        self.active_topic: Optional[dict] = None  # 活跃话题（对标 ActiveTopicState）
        # 事件驱动心跳：on_user_message（互动/弹幕回复结束）时 set，
        # 主循环立即醒来做一次心跳检查；静默期由 next_wake_in() 低频机会兜底。
        self._wakeup = asyncio.Event()
        self.speaking = False
        self._recent_categories: deque = deque(maxlen=_RECENT_TYPE_WINDOW)
        # 话题种子与使用记录（对标 TopicStore + TopicHistory：内存版无 DB）
        self._topic_seeds: List[dict] = _load_topic_seeds()
        self._topic_last_used: Dict[str, datetime] = {}
        # 话题互动统计：{id: {"use": 使用次数, "engaged": 用户互动次数}}
        self._topic_stats: Dict[str, dict] = {}

        # ===== 主动消息队列（限流/去重/丢弃策略，见 heartbeat/_enqueue/_worker）=====
        # 队列最大长度超出时丢弃最旧的主动消息，优先保留最新触发；
        # 用户输入不走此队列（直接进主循环），永远不丢。
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.PROACTIVE_QUEUE_MAX)
        # 后台消费 worker（异常捕获不退出；死亡后下次入队自动重建）
        self._worker_task: Optional[asyncio.Task] = None
        # 播报完成事件：worker 播完置位，_wait_input 据此精确唤醒重新等待
        # 输入（无周期轮询）；开始播报前 clear。
        self._speak_done = asyncio.Event()
        # 最近播报过的主动发言文本（去重比对基准，窗口 3）
        self._recent_prompts: deque = deque(maxlen=3)
        # 触发/播报/丢弃统计（供调试观察 agent 行为）
        self._stats: Dict[str, int] = {"trigger": 0, "speak": 0, "dropped": 0}

    # ---------- 对外接口 ----------

    def add_topic_seeds(self, new_seeds: List[dict]) -> None:
        """追加新话题种子（自我进化模块沉淀的话题），按 concept 去重合并。

        当前会话立即生效；进程重启后由 topics.yml 重新加载兜底。
        """
        seen = {t.get("concept") for t in self._topic_seeds}
        for seed in new_seeds:
            if not seed.get("concept") or seed["concept"] in seen:
                continue
            self._topic_seeds.append(seed)
            seen.add(seed["concept"])

    def on_user_message(self) -> None:
        """用户发言：刷新交互时间，结束活跃话题记互动，并立即给一次开口机会。

        有人说话即为互动：静默计时重新开始；主动发言是否开口由 LLM 自主
        判断（无时间门槛），互动结束立即触发一次心跳检查。
        """
        self.last_interaction = time.time()
        if self.active_topic is not None:
            stats = self._topic_stats.setdefault(
                self.active_topic["topic_id"], {"use": 0, "engaged": 0})
            stats["engaged"] += 1
            self.active_topic = None
        # 事件驱动心跳：互动结束立即唤醒主循环做一次心跳检查（LLM 自主决定）
        self._wakeup.set()

    async def heartbeat(self) -> bool:
        """自主开口检查：由主模型决定此刻想不想说话、想说什么。

        想说 → 生成发言文本并入队（后台 worker 播报）；不想说（<SILENT>）
        → 保持沉默。不再有任何时间门槛（孤独/无聊累积、冷却、随机唤醒点）。

        返回是否真的有主动消息入队。播报不再在此阻塞等待——由后台 worker
        消费队列，带忙碌抑制 / 去重，避免「事件疯狂触发 → 一次蹦出
        一大堆主动台词」（队列满丢弃最旧、保留最新触发）。
        """
        if not self.cfg.PROACTIVE_ENABLED:
            return False
        # 活跃话题超时自动结束（无回应兜底防永久沉默）
        if self.active_topic is not None:
            if time.time() - self.active_topic["started_at"].timestamp() > _ACTIVE_TOPIC_TIMEOUT:
                self.active_topic = None
        if self.speaking or not self._queue.empty():
            # 已有发言进行中或排队中：不重复触发（防堆积）
            return False
        # 忙碌抑制：主 LLM 推理 / 播报进行中 → 不开口（避免抢话）
        if self.cfg.AGENT_AVOID_MAIN_LLM and (
                not is_idle() or self._output_lock.locked()):
            return False
        # 弹幕回复已敲定：弹幕优先，主动不抢话（回复播报完成才恢复）
        if is_danmaku_pending():
            return False
        # 话题活跃期：不触发（防打断正在进行的话题）
        if self.active_topic is not None:
            return False
        self._log_heartbeat()

        # —— 灵感话题（可选）：LLM 可顺着聊，也可自由发挥 ——
        # _pick_topic 内部已兜底（全部冷却时放宽冷却强制选一个），不会返回 None
        topic = self._pick_topic()

        # —— LLM 自主决策：想说就说，不想说保持沉默 ——
        text = await self._decide_and_generate(topic)
        if not text:
            return False

        # —— 入队播报（后台 worker 异步消费）——
        return self._enqueue(text, topic)

    def discard_pending(self) -> int:
        """用户输入到来时清空排队中的主动消息（优先响应用户），返回丢弃条数。

        只丢弃**排队未播**的消息；正在播报的主动发言不打断（沿原有语义：
        主动说话期间拒收输入）。
        """
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        if dropped:
            self._stats["dropped"] += dropped
            console.dim(f"[主动] 用户输入优先，丢弃 {dropped} 条排队中的主动消息")
        return dropped

    # ---------- 主动消息队列（限流 / 去重 / 丢弃） ----------

    def _enqueue(self, text: str, topic: Optional[dict]) -> bool:
        """把一条主动发言放入队列；队列满丢弃最旧、与近期内容重复丢弃。

        消息统一打标签 "agent_proactive"（与 user_input / main_llm_reply 区分），
        供消费方按标签执行不同策略。
        """
        kind = "topic" if topic is not None else "emotional"
        # 去重：与最近会话 / 近期已播发言高度相似 → 直接丢弃（降重复）
        if self._is_duplicate(text, kind):
            self._stats["dropped"] += 1
            console.dim("[主动] 与近期内容高度相似，丢弃本条主动消息")
            return False
        # 队列满：丢弃最旧的主动消息，优先保留最新触发
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self._stats["dropped"] += 1
                console.dim("[主动] 队列已满，丢弃最旧的主动消息")
            except asyncio.QueueEmpty:
                pass
        item = {"tag": "agent_proactive", "kind": kind,
                "text": text, "topic": topic}
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self._stats["dropped"] += 1
            return False
        self._stats["trigger"] += 1
        self._ensure_worker()
        return True

    def _ensure_worker(self) -> None:
        """确保消费 worker 存活（异常退出后下次入队自动重建）。"""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(
            self._worker(), name="proactive_worker")

    async def _worker(self) -> None:
        """主动消息消费循环：忙碌过滤后播报，异常捕获不退出（容错）。"""
        while True:
            item = await self._queue.get()
            try:
                # —— 忙碌抑制：用户正在说话 / AI 正在播报 → 丢弃本条 ——
                if self.cfg.AGENT_AVOID_MAIN_LLM and not is_idle():
                    self._stats["dropped"] += 1
                    console.dim("[主动] 正在忙碌（用户/AI 处理中），丢弃本条主动消息")
                    continue
                # —— 弹幕回复已敲定：弹幕优先，主动避让（丢弃本条，不抢话） ——
                if is_danmaku_pending():
                    self._stats["dropped"] += 1
                    console.dim("[主动] 弹幕回复已敲定，主动避让，丢弃本条主动消息")
                    continue
                await self._speak_item(item)
                self._recent_prompts.append(item["text"])
                self._stats["speak"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # agent 线程异常：告警但不退出，继续消费后续消息
                import traceback as _tb
                console.error(
                    f"主动发言消费出错（线程不退出）：{e}\n"
                    f"{''.join(_tb.format_exception(type(e), e, e.__traceback__))}")
            finally:
                self._queue.task_done()

    def _is_duplicate(self, text: str, kind: str) -> bool:
        """与最近会话 / 近期已播主动发言做相似度比对，高度相似视为重复。

        - 最近会话（真实发言）两种发言都比对：防止「刚说过类似内容」；
        - 近期已播主动发言只比对 topic 发言：其文本携带真实话题内容；
          emotional 是「心里话」，与话题文本不混比，避免误判重复。
        """
        threshold = max(0.0, min(1.0, float(self.cfg.AGENT_DUP_THRESHOLD or 0.85)))
        if threshold <= 0:
            return False
        recent: List[str] = []
        if self.mm is not None:
            for turn in self.mm.recent_turns[-self.cfg.AGENT_HISTORY_SNAPSHOT:]:
                content = (turn.get("content") or "").strip()
                if content:
                    recent.append(content)
        if kind == "topic":
            recent.extend(self._recent_prompts)
        for recent_text in recent:
            if recent_text and SequenceMatcher(None, text, recent_text).ratio() >= threshold:
                return True
        return False

    async def _speak_item(self, item: dict) -> None:
        """播报一条主动发言（LLM 预生成文本）：输出锁互斥 + 拒收标记 + 播后状态回写。

        发言文本已由 heartbeat 的 LLM 决策生成，这里直接播报（stream.speak_text）；
        记忆提取在播报开始时以后台任务提交，与播报并行（原 on_llm_done 逻辑）。
        """
        kind = item["kind"]
        text = item["text"]
        topic = item.get("topic")
        reason = "心里话" if kind == "emotional" else "话题"
        topic_tag = (f"｜灵感话题 {topic['category']}/{topic['id']}"
                     if topic is not None else "")
        console.dim(f"[主动] 自主开口（{reason}）...{topic_tag}"
                    f"（{len(text)} 字）")

        self.speaking = True
        set_global_state(STATE_AGENT_THINKING)  # 正在处理主动发言
        try:
            import traceback as _tb

            # 主动发言记忆提取（原 on_llm_done 逻辑）：发言文本已生成，直接在
            # 播报开始时后台提交（submit_extract_and_store：串行 + 最新优先，
            # 防并发堆积），与 TTS 播报并行。仅 emotional（内心状态）蒸馏为
            # self 记忆；topic（话题闲聊）蒸馏价值低，跳过。两种都保留
            # add_turn 维持上下文连贯（source="proactive" 标记归属）。
            async def _store_memory() -> None:
                try:
                    if kind == "emotional" and self.butler is not None:
                        await self.butler.submit_extract_and_store(
                            [{"role": "assistant", "content": text}],
                            self.mm.recent_turns if self.mm is not None else None,
                        )
                    if self.mm is not None:
                        self.mm.add_turn("muika", text, source="proactive")
                except Exception:
                    pass

            if self.butler is not None or self.mm is not None:
                asyncio.create_task(_store_memory())

            # 全局输出互斥 + 拒收标记：
            # - 持有锁 → 弹幕/用户不能并发说话；
            # - owner="proactive" → 输入监听层丢弃期间到达的任何输入。
            console.dim(f"[主动] 等待输出锁…（当前锁状态："
                        f"locked={self._output_lock.locked()})")
            async with self._output_lock:
                self._speak_done.clear()  # 开始播报：置未完成，等待者重新挂起
                set_output_owner("proactive")
                set_global_state(STATE_AI_SPEAKING)  # 正在播报
                try:
                    console.dim(f"[主动] 已拿到输出锁，开始播报")
                    # 复位残留打断标志：用户对话中途被打断后 _interrupted 仍为
                    # True，若不复位，本条主动发言会被 speak()/_pump 全部丢弃
                    if self.tts is not None:
                        try:
                            self.tts.clear_interrupt()
                        except Exception:
                            pass
                    await stream.speak_text(
                        text, self.tts, self.face, self.sub,
                        proactive=True,
                        profanity_filter=self.pf,
                        profanity_filter_rate=self.pf_rate,
                    )
                    # 左栏对话换行：主动发言是单条文本（无换行），收尾补一个
                    console.chat()
                    console.dim(f"[主动] 播报完成")
                finally:
                    set_output_owner(None)
                    set_global_state(STATE_IDLE)
                    self._speak_done.set()  # 播报结束：精确唤醒等待中的主循环
        except Exception as e:
            console.error(
                f"主动发言失败：{e}\n"
                f"{''.join(_tb.format_exception(type(e), e, e.__traceback__))}")
        finally:
            self.speaking = False
            if topic is not None:
                # 话题已开口：进入话题活跃期（期间不再主动开口，防打断）
                self._begin_topic(topic)

    # ---------- 内部实现 ----------

    def _log_heartbeat(self) -> None:
        """心跳日志：显示距上次互动与当前开口机会状态。"""
        quiet = time.time() - self.last_interaction
        extra = ""
        if self.active_topic is not None:
            remain = _ACTIVE_TOPIC_TIMEOUT - (
                time.time() - self.active_topic["started_at"].timestamp())
            if remain > 0:
                extra = f" | 话题活跃中({remain:.0f}s后可触发)"
            else:
                extra = " | 话题活跃中(即将超时)"
        elif self.speaking:
            extra = " | 发言中"
        console.dim(f"[心跳] 静默 {quiet:.0f}s，由 LLM 自主决定是否开口{extra}")

    def next_wake_in(self) -> float:
        """距下一次「自主开口机会」的秒数（单次精确唤醒，无周期轮询）。

        主循环 sleep 到这个时刻才醒一次做心跳，而非固定间隔轮询：
        - 发言中 / 队列非空：等 _speak_done 事件精确唤醒（这里给兜底大值，
          不返回 0 造成心跳忙循环）
        - 活跃话题期间：只等话题超时那一刻（此后可再次开口）
        - 静默期：从共用随机间隔范围取一个值（与弹幕回复冷却同一范围，
          避免固定时间显得机械；互动结束由 on_user_message 的 _wakeup
          事件即时唤醒，不受此间隔限制）
        """
        now = time.time()
        if self.speaking or not self._queue.empty():
            # 播报由后台 worker 异步进行，结束会置位 _speak_done 精确唤醒
            return max(1.0, _ACTIVE_TOPIC_TIMEOUT)
        if self.active_topic is not None:
            remain = _ACTIVE_TOPIC_TIMEOUT - (
                now - self.active_topic["started_at"].timestamp())
            return max(0.0, remain)
        return random.uniform(
            self.cfg.RESPONSE_INTERVAL_MIN, self.cfg.RESPONSE_INTERVAL_MAX)

    async def _decide_and_generate(self, topic: Optional[dict]) -> Optional[str]:
        """LLM 自主决策：想说就生成发言文本，不想说返回 None（保持沉默）。

        一次调用完成「决定 + 内容」：prompt 明确允许选择沉默，模型输出
        <SILENT> 或空则沉默。决策过程不冒充用户发言（proactive=True 已剔除
        prompt）；沉默时的回复不保留在主历史，避免污染后续上下文。
        """
        prompt = await self._build_prompt(topic)
        if not prompt:
            return None
        history_len = len(self.brain.history)
        try:
            parts = []
            async for sentence in self.brain.chat_stream(prompt, proactive=True):
                if sentence:
                    parts.append(sentence)
        except Exception as e:
            console.error(f"[主动] LLM 自主决策生成失败：{e}")
            return None
        decided = self._parse_decision("".join(parts).strip())
        if decided is None and len(self.brain.history) > history_len:
            # 沉默：决策回复（<SILENT> 等）不保留在主历史
            del self.brain.history[history_len:]
        return decided

    @staticmethod
    def _parse_decision(text: str) -> Optional[str]:
        """解析自主开口结果：沉默 → None（保持沉默）；否则返回发言文本。

        模型可能输出 <SILENT>、沉默类短语或空；容错处理带引号/标点包裹。
        """
        if not text:
            return None
        t = text.strip()
        # 去掉可能的包裹字符（引号/括号/标记符号）
        for ch in "\"'“”‘’「」『』【】()（）<>＜＞":
            t = t.replace(ch, "")
        t = t.strip(" .。!！?？~～…")
        if not t:
            return None
        lower = t.lower().replace(" ", "")
        if lower in {m.lower().replace(" ", "") for m in _SILENT_MARKERS}:
            return None
        # 以「沉默」类表达开头且极短（如「此刻不想说」「没什么想说的」）→ 沉默
        for m in _SILENT_MARKERS:
            if t.startswith(m) and len(t) <= 12:
                return None
        return text

    def _begin_topic(self, topic: dict) -> None:
        """进入话题活跃期并记录使用次数（对标 ActiveTopicState + record_topic_used）。"""
        self.active_topic = {
            "topic_id": topic["id"],
            "started_at": datetime.now(),
            "user_engaged": False,
        }
        self._topic_stats.setdefault(topic["id"], {"use": 0, "engaged": 0})["use"] += 1

    def _pick_topic(self) -> Optional[dict]:
        """按权重机制挑选一个话题种子（对标 TopicManager.get_next_topic）。

        1. 类别权重：TOPIC_WEIGHTS 为基础；
        2. 近期已聊类别 ×0.25 惩罚（窗口 3）；
        3. 每个话题独立冷却 cooldown_minutes（内存记录上次使用时间）；
        4. 类别内按互动率加权（用过 ≥2 次、互动率 <30% → 0.3，<50% → 0.6）；
        5. 全部冷却中放宽冷却强制选一个（防所有话题都不可用导致无从开口）。
        """
        weights = dict(_TOPIC_WEIGHTS)
        now = datetime.now()
        available: Dict[str, List[dict]] = {}
        for topic in self._topic_seeds:
            last = self._topic_last_used.get(topic["id"])
            if last is not None:
                elapsed = (now - last).total_seconds()
                if elapsed < topic["cooldown_minutes"] * 60:
                    continue
            available.setdefault(topic["category"], []).append(topic)
        if not available:  # 全部冷却中：放弃会导致「无聊 100% 永远不开口」
            # 兜底：从「冷却最少（即最接近可用）的前 10 个话题」里随机挑一个，
            # 放宽冷却约束强制使用——避免已就绪但因所有话题冷却而永久沉默。
            def _remain(t):
                last = self._topic_last_used.get(t["id"])
                if last is None:
                    return 0.0
                elapsed = (now - last).total_seconds()
                return max(0.0, t["cooldown_minutes"] * 60 - elapsed)
            coolest = sorted(self._topic_seeds, key=_remain)[:10]
            fallback = random.choice(coolest)
            console.dim(
                f"[主动] 话题全部冷却中，放宽冷却强制选话题："
                f"{fallback['category']}/{fallback['id']}")
            return fallback
        # 类别加权（近期惩罚）
        filtered = {cat: weights.get(cat, 0.05) for cat in available}
        for recent in self._recent_categories:
            if recent in filtered:
                filtered[recent] *= _RECENT_TYPE_PENALTY
        total = sum(filtered.values())
        if total <= 0:
            return random.choice(self._topic_seeds)
        cats = list(filtered.keys())
        cat_weights = [filtered[c] / total for c in cats]
        chosen_cat = random.choices(cats, weights=cat_weights, k=1)[0]
        # 类别内按互动率加权（对标 TopicManager 的 individual_weight）
        pool = available[chosen_cat]
        pool_weights = []
        for t in pool:
            stats = self._topic_stats.get(t["id"])
            w = 1.0
            if stats is not None and stats["use"] >= 2:
                engagement = stats["engaged"] / stats["use"]
                if engagement < 0.3:
                    w = _INTERACTION_LOW
                elif engagement < 0.5:
                    w = _INTERACTION_MID
            pool_weights.append(w)
        chosen = random.choices(pool, weights=pool_weights, k=1)[0]
        self._recent_categories.append(chosen_cat)
        self._topic_last_used[chosen["id"]] = now
        return chosen

    async def _build_prompt(self, topic: Optional[dict]) -> str:
        """组装主模型的「自主开口决策」请求：agent（ButlerAgent）优先，无 agent 回退内置。

        agent 负责构造（时段语气 + 可选灵感话题 + 记忆线索注入），主模型
        自主判断「想说就说 / 不想说输出 <SILENT> 保持沉默」。
        """
        memory_context = ""
        if self.mm is not None:
            try:
                memory_context = await self.mm.get_memory_prompt(query="", top_k=4)
            except Exception:
                memory_context = ""
        hour = datetime.now().hour
        if self.butler is not None:
            try:
                return self.butler.build_proactive_prompt(
                    topic["concept"] if topic is not None else "",
                    memory_context, hour)
            except Exception as e:
                console.error(f"[主动] agent 构造发言请求失败，回退内置：{e}")
        # —— 回退：无 agent 或 agent 失败时的内置简化 prompt（自主决策格式）——
        topic_hint = (f"可以顺着这个灵感话题聊：{topic['concept']}"
                      if topic is not None else "也可以自己决定想聊什么")
        return (
            "现在是直播间的「静默时刻」。没有任何Cue，没有任何救场需求。\n"
            "你不是在答题，你是在决定「此刻这个灵魂」想不想暴露在麦克风前。\n\n"
            "请根据你当下的人设状态，从以下三种「心流路径」中选一条执行：\n"
            "1. 如果觉得无聊或疲惫 -> 输出 <SILENT>（保持沉默不丢人，避免水词）\n"
            "2. 如果心里有句憋不住的骚话/吐槽 -> 直接说（1-3句，带点冒犯或自嘲）\n"
            "3. 如果想找存在感 -> 用「反常规逻辑」解构那个灵感锚点（哪怕硬拐都行）\n\n"
            "--- 执行红线（根据历史经验修正） ---\n"
            "- 拒绝「你好吗」「今天天气」式的水词问候。\n"
            "- 拒绝教科书式的正面说教。如果话题涉及对立，用「诗意悖论」或「科学梗」消解，别站队。\n"
            "- 如果灵感锚点太无聊，允许你极端歪曲它，制造「地狱笑话」或「荒诞跳转」。\n"
            "- 玩梗可以挑衅，但收尾必须用夸张的「自我解构」把攻击性卸掉。\n\n"
            f"{topic_hint}\n"
            "规则：只要开口，必须是「非标品」的句子。无话可说时，请虔诚地输出 <SILENT>。"
        )
