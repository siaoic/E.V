"""主动对话引擎 —— 严格参照 Muika-After-Story 1.4.1 的主动机制。

机制映射（Muika → 本项目）：
  loop.collect_events 的 5s 心跳 tick   → heartbeat() 事件驱动检查（互动结束时触发）
  state.MuikaState 状态累积              → loneliness / boredom / curiosity / mood
  loop.get_think_mode 触发判定           → _should_trigger()（emotional / topic / None）
  loop._run_topic_pipeline               → 话题管线（TopicManager 选择 + expand_topic 扩写）
  loop._run_brain_pipeline               → 情绪管线（主 Brain 生成，走完整输出管线）
  topic_manager.TopicManager             → _pick_topic()（权重 / 冷却 / 互动率评分）
  constants.PROACTIVE_COOLDOWN 等        → 对应 .env 配置（速率保留可调）

agent 分工（用户确认的架构：主动发言交给 agent，agent 催促主模型）：
  - ButlerAgent.build_proactive_prompt：负责组装主模型的发言请求——话题、
    时段语气、类别结尾策略、记忆线索注入（对标 Muika brain.expand_topic）
  - 主模型（stream.converse）：负责开口生成内容 + TTS/字幕/口型
  - agent 还负责发言后的记忆蒸馏（on_llm_done 回调）：仅情绪发言
    （孤独倾诉，反映内心状态）蒸馏为 self 记忆；话题发言（闲聊）不蒸馏
  - 关闭记忆（无 agent）时回退内置简化 prompt

与 Muika 的差异（适配本项目的控制台 REPL 场景）：
  - 不引入完整事件系统，用事件驱动心跳 + 单次精确唤醒（互动结束事件
    立即检查；静默期算好「下一个有意义时刻」到点只醒一次，无周期轮询）
  - 话题冷却 / 互动率用内存记录（Muika 用 SQLite TopicHistory）
  - 话题发言后进入 active_topic：期间心跳不再触发任何发言（防打断），
    用户回应则话题结束并记录互动（engaged），10 分钟无回应自动结束
  - 冷却（PROACTIVE_COOLDOWN_SECONDS）仅作用于情绪发言（对齐 Muika：
    PROACTIVE_COOLDOWN 是「两次主动情绪发言的最小间隔」），话题发言不受限
"""

import asyncio
import os
import random
import sys
import time
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import yaml

from src.utils import console
from src.llm import stream
from src.core.output_lock import (
    get_output_lock, set_output_owner,
)

# ======================================================================
# 触发阈值 / 话题权重 —— 严格对齐 Muika-After-Story
# （muika/core/constants.py + muika/core/topic_manager.py）
# ======================================================================

# —— 触发阈值（对标 muika/core/constants.py）——
_LONELINESS_THRESHOLD = 1.0    # 孤独感满格(100%)才触发情绪管线
_BOREDOM_THRESHOLD = 1.0       # 无聊感满格(100%)才触发话题管线
_CURIOSITY_DRIVE_THRESHOLD = 0.6  # 探索冲动超过此值且 30% 概率 → 话题管线
_LONELINESS_PROACTIVE_RELIEF = 0.35  # 主动发言后孤独感降低幅度
# 对标 LONELINESS_PROACTIVE_RELIEF：表达出来会有所缓解，但不等于彻底不孤独。
# 两次主动情绪发言的最小间隔（秒）：Muika 默认 1 小时；本项目由
# .env PROACTIVE_COOLDOWN_SECONDS 配置（见 _should_trigger）。

# —— 话题类别权重（对标 topic_manager.TOPIC_WEIGHTS）——
_TOPIC_WEIGHTS: Dict[str, float] = {
    "relationship": 0.35,
    "philosophy": 0.25,
    "trivia": 0.20,
    "story": 0.10,
    "meta": 0.05,
}
# 无聊感 > 好奇心时的权重调整（对标 get_next_topic：更想听点轻松的）
_TOPIC_BOREDOM_BOOST: Dict[str, float] = {
    "trivia": 0.10, "story": 0.05,
    "philosophy": -0.10, "meta": -0.05,
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
# 默认跟随发言冷却，使快节奏配置下话题活跃期不会过长卡死后续触发。
_ACTIVE_TOPIC_TIMEOUT: float = 120.0

# 探索冲动涨满所需小时数（直播场景比无聊更慢，作为低频惊喜触发）
_CURIOSITY_DRIVE_HOURS: float = 4.0

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
    """主动对话引擎：事件驱动心跳 → 状态累积 → 触发判定 → agent 准备发言 → 主模型开口。

    主循环 `_wait_input` 等待用户输入，互动/弹幕回复结束的唤醒事件到达时
    调用 `heartbeat()`；静默期靠 next_wake_in() 单次精确唤醒（到点才醒一次，
    无周期轮询）。触发后由 ButlerAgent（若启用）组装发言请求，主模型
    （stream.converse）生成并播报，走与用户对话相同的 TTS/字幕/口型管线。
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

        # 状态（对标 MuikaState）：随空闲时间线性累积，用户发言清孤独
        self.loneliness = 0.0
        self.boredom = 0.0
        self.curiosity = 0.5  # 探索欲：初始 0.5，空闲每 tick ×0.99 缓慢衰减（对标 MuikaState）
        self.curiosity_drive = 0.0  # 探索冲动：独立累积，> 阈值且 30% 概率触发话题（对标 get_think_mode）
        self.mood = "calm"
        self.last_interaction = time.time()
        self.last_proactive_at: Optional[datetime] = None  # 仅情绪发言记录（冷却）
        self.active_topic: Optional[dict] = None  # 活跃话题（对标 ActiveTopicState）
        self._last_tick = time.time()
        # 事件驱动心跳（对标 NagaAgent Heartbeat v3）：on_user_message
        # （互动/弹幕回复结束）时 set，主循环立即醒来做一次心跳检查；
        # 静默期由 next_wake_in() 单次精确唤醒兜底（无周期轮询）。
        self._wakeup = asyncio.Event()
        # 随机唤醒点（随机+事件混合触发，不用「定时回复」）：静默期下一个
        # 「可能随机开口」的时刻，到点掷骰子决定是否开口；未命中则重采样
        # 下一个随机点——说话时机不可预测，不依赖孤独/无聊定时累积到阈值。
        self._random_enabled = bool(cfg.PROACTIVE_RANDOM_ENABLED)
        self._random_chance = max(
            0.0, min(1.0, float(cfg.PROACTIVE_RANDOM_CHANCE or 0.25)))
        self._random_max_wait = max(1e-6, float(cfg.PROACTIVE_RANDOM_MAX_WAIT or 180))
        self._next_random_at = time.time() + random.uniform(0, self._random_max_wait)
        self.speaking = False
        self._recent_categories: deque = deque(maxlen=_RECENT_TYPE_WINDOW)
        # 话题种子与使用记录（对标 TopicStore + TopicHistory：内存版无 DB）
        self._topic_seeds: List[dict] = _load_topic_seeds()
        self._topic_last_used: Dict[str, datetime] = {}
        # 话题互动统计：{id: {"use": 使用次数, "engaged": 用户互动次数}}
        self._topic_stats: Dict[str, dict] = {}

    # ---------- 对外接口 ----------

    def on_user_message(self) -> None:
        """用户发言：孤独、无聊直接清零；刷新交互时间，结束活跃话题记互动。

        规则：有人说话就清零，没人说话的沉默时间才累积孤独/无聊。
        """
        self.loneliness = 0.0
        self.boredom = 0.0
        self.last_interaction = time.time()
        if self.active_topic is not None:
            stats = self._topic_stats.setdefault(
                self.active_topic["topic_id"], {"use": 0, "engaged": 0})
            stats["engaged"] += 1
            self.active_topic = None
        # 事件驱动心跳：互动结束立即唤醒主循环做一次心跳检查
        self._wakeup.set()
        # 随机唤醒点重新计时：互动后进入新的随机窗口（随机+事件混合）
        self._resample_random_at()

    async def heartbeat(self) -> bool:
        """事件驱动心跳检查：更新状态，若判定触发则生成并播报一次主动发言。

        返回是否真的开口说了话。
        """
        if not self.cfg.PROACTIVE_ENABLED:
            return False
        self._tick_state()
        self._log_heartbeat()
        kind = self._should_trigger()
        if kind is None:
            return False

        # —— 话题管线：先选话题，进入活跃期（对标 _run_topic_pipeline）——
        topic = None
        if kind == "topic":
            topic = self._pick_topic()
            # _pick_topic 内部已兜底（全部冷却时放宽冷却强制选一个），不会返回 None
            self._begin_topic(topic)

        # —— agent 准备发言请求（agent 催促主模型开口）——
        prompt = await self._build_prompt(kind, topic)
        if not prompt:
            # 构造失败：回退触发状态（防单次精确唤醒空转——若阈值已满又
            # 不重置，next_wake_in 会一直返回 0 造成忙循环），等下次累积
            # 到阈值再重试。
            if topic is not None:
                self.active_topic = None
                self.boredom = 0.0
            else:
                self.loneliness = 0.0
            console.dim("[主动] 构造发言 prompt 失败（空），本次跳过")
            return False

        self.speaking = True
        try:
            import traceback as _tb
            reason = "孤独" if kind == "emotional" else "无聊"
            topic_tag = (f"｜话题 {topic['category']}/{topic['id']}"
                         if topic is not None else "")
            console.dim(
                f"[主动] {reason}感驱动，开口搭话...{topic_tag}"
                f"（prompt={len(prompt)} 字）")
            console.dim(f"[主动] 等待输出锁…（当前锁状态："
                        f"locked={self._output_lock.locked()})")

            # 主动发言记忆提取回调：LLM 回复生成完毕立即提交后台蒸馏
            # （submit_extract_and_store：串行 + 最新优先，防并发堆积）。
            # 仅 emotional（孤独倾诉，反映 AI 内心状态）蒸馏为 self 记忆；
            # topic（话题展开）是 trivia/知识类闲聊，蒸馏价值低，跳过
            # 可省 BUTLER token 并避免堆积低价值记忆。两种发言都保留
            # add_turn，维持上下文连贯（供后续蒸馏/检索参考）。
            async def _on_llm_done(reply_text: str) -> None:
                if not reply_text:
                    return
                try:
                    if kind == "emotional" and self.butler is not None:
                        await self.butler.submit_extract_and_store(
                            [{"role": "assistant", "content": reply_text}],
                            self.mm.recent_turns if self.mm is not None else None,
                        )
                    if self.mm is not None:
                        self.mm.add_turn("muika", reply_text)
                except Exception:
                    pass

            # 全局输出互斥 + 拒收标记：
            # - 持有锁 → 弹幕/用户不能并发说话；
            # - owner="proactive" → 输入监听层丢弃期间到达的任何输入。
            async def _speak_with_lock():
                console.dim(f"[主动] 已拿到输出锁，开始生成并播报")
                set_output_owner("proactive")
                try:
                    await stream.converse(
                        self.brain, prompt,
                        self.tts, self.face, self.sub,
                        proactive=True,
                        profanity_filter=self.pf,
                        profanity_filter_rate=self.pf_rate,
                        on_llm_done=_on_llm_done
                        if (self.butler is not None or self.mm is not None) else None,
                    )
                    console.dim(f"[主动] 播报完成")
                finally:
                    set_output_owner(None)
            async with self._output_lock:
                await _speak_with_lock()
        except Exception as e:
            console.error(
                f"主动发言失败：{e}\n"
                f"{''.join(_tb.format_exception(type(e), e, e.__traceback__))}")
        finally:
            self.speaking = False
            if kind == "emotional":
                # 说出来会有所缓解，但不等于彻底不孤独（对标 LONELINESS_PROACTIVE_RELIEF）；
                # 冷却（last_proactive_at）仅情绪发言记录（对标 PROACTIVE_COOLDOWN）
                self.loneliness = max(0.0, self.loneliness - _LONELINESS_PROACTIVE_RELIEF)
                self.last_proactive_at = datetime.now()
            else:
                self.boredom = 0.0
        return True

    # ---------- 内部实现 ----------

    def _rates(self) -> Tuple[float, float]:
        """计算孤独/无聊每 tick 的增长率（每秒比例），避免多处重复计算。"""
        lon = 1.0 / (max(1e-6, self.cfg.PROACTIVE_LONELINESS_HOURS) * 3600)
        bor = 1.0 / (max(1e-6, self.cfg.PROACTIVE_BOREDOM_HOURS) * 3600)
        return lon, bor

    def _log_heartbeat(self) -> None:
        """心跳日志：控制台显示当前状态与距离触发的进度。

        显示规则：未到阈值时显示真实百分比 + 倒计时；达到阈值显示 100% + 已就绪，
        避免「已就绪但只有 60%」的视觉矛盾（阈值 0.6=60%，之前会 60%+已就绪同时显示）。
        """
        lon_rate, bor_rate = self._rates()
        lon_ready = self.loneliness >= _LONELINESS_THRESHOLD
        bor_ready = self.boredom >= _BOREDOM_THRESHOLD
        if lon_ready:
            lon_str = "孤独 100%(已就绪)"
        else:
            remain = (_LONELINESS_THRESHOLD - self.loneliness) / lon_rate / 60
            lon_str = f"孤独 {self.loneliness * 100:.0f}%({remain:.0f}min后触发)"
        if bor_ready:
            bor_str = "无聊 100%(已就绪)"
        else:
            remain = (_BOREDOM_THRESHOLD - self.boredom) / bor_rate / 60
            bor_str = f"无聊 {self.boredom * 100:.0f}%({remain:.0f}min后触发)"
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
        console.dim(f"[心跳] {lon_str} | {bor_str}{extra}")

    def _tick_state(self) -> None:
        now = time.time()
        dt = now - self._last_tick
        self._last_tick = now
        if dt <= 0:
            return
        lon_rate, bor_rate = self._rates()
        self.loneliness = min(1.0, self.loneliness + lon_rate * dt)
        self.boredom = min(1.0, self.boredom + bor_rate * dt)
        # 探索欲缓慢衰减（对标 MuikaState.tick_state: curiosity *= 0.99）
        self.curiosity = max(0.0, self.curiosity * 0.99)
        # 探索冲动独立累积（对标 get_think_mode 的 curiosity_drive）
        self.curiosity_drive = min(
            1.0, self.curiosity_drive + dt / (max(1e-6, _CURIOSITY_DRIVE_HOURS) * 3600))
        # mood 规则（对标 MuikaState.tick_state）：孤独/无聊接近满格才切换情绪态
        if self.loneliness > 0.95:
            self.mood = "lonely"
        elif self.boredom > 0.9:
            self.mood = "bored"
        else:
            self.mood = "calm"
        # 活跃话题超时自动结束（REPL 无 session 结束信号，超时兜底防永久沉默）
        if self.active_topic is not None:
            if now - self.active_topic["started_at"].timestamp() > _ACTIVE_TOPIC_TIMEOUT:
                self.active_topic = None

    def _resample_random_at(self) -> None:
        """重置下一个随机唤醒点（当前时刻 + 0~MAX_WAIT 均匀随机延迟）。"""
        self._next_random_at = time.time() + random.uniform(0, self._random_max_wait)

    def _random_probe_wait(self, now: float) -> float:
        """随机唤醒点距现在的秒数（随机+事件混合）。

        随机点已到但静默期未过 → 重采样下一个随机点，避免 0 超时造成心跳忙循环。
        """
        idle_ok = (now - self.last_interaction) >= self.cfg.PROACTIVE_MIN_IDLE_SECONDS
        if now >= self._next_random_at and not idle_ok:
            self._resample_random_at()
        return max(0.0, self._next_random_at - now)

    def next_wake_in(self) -> float:
        """距下一次有意义心跳的秒数（单次精确唤醒，无周期轮询）。

        主循环 sleep 到这个时刻才醒一次做心跳，而非固定间隔轮询：
        - 活跃话题期间：只等话题超时那一刻（此后可再次开口）
        - 平时：等无聊/孤独到阈值、随机唤醒点、或探索冲动到阈值（最近的一次）
        - 已全部就绪（等待列表为空）→ 返回 0 立即检查
        """
        now = time.time()
        if self.active_topic is not None:
            remain = _ACTIVE_TOPIC_TIMEOUT - (
                now - self.active_topic["started_at"].timestamp())
            return max(0.0, remain)
        lon_rate, bor_rate = self._rates()
        waits = []
        if self.loneliness < _LONELINESS_THRESHOLD:
            waits.append((_LONELINESS_THRESHOLD - self.loneliness) / lon_rate)
        if self.boredom < _BOREDOM_THRESHOLD:
            waits.append((_BOREDOM_THRESHOLD - self.boredom) / bor_rate)
        if self.curiosity_drive < _CURIOSITY_DRIVE_THRESHOLD:
            waits.append(
                (_CURIOSITY_DRIVE_THRESHOLD - self.curiosity_drive)
                * _CURIOSITY_DRIVE_HOURS * 3600)
        # 随机唤醒点：静默期到点掷骰子决定是否随机开口（随机+事件混合）
        if self._random_enabled:
            waits.append(self._random_probe_wait(now))
        if not waits:
            return 0.0
        return max(0.0, min(waits))

    def _should_trigger(self) -> Optional[str]:
        """判定本心跳是否触发主动发言，返回 "emotional" / "topic" / None。

        优先级（对标 Muika loop.get_think_mode）：
          - 正在发言 → 不触发（防嵌套）
          - 用户最近才发言（安静期不足）→ 不触发
          - 话题活跃期 → 不触发（防情绪管线打断正在进行的话题）
          - 随机唤醒点（随机+事件混合）：到点按概率随机开口（情绪/话题），
            未命中则重采样下一个随机点——不依赖定时累积
          - 孤独 > 0.8 且情绪冷却已过 → emotional（冷却仅情绪发言）
          - 无聊 > 0.6 → topic
          - 探索冲动 > 0.6 且 30% 概率 → topic（触发后清零）
        """
        if self.speaking:
            return None
        now = time.time()
        if now - self.last_interaction < self.cfg.PROACTIVE_MIN_IDLE_SECONDS:
            return None
        if self.active_topic is not None:
            return None
        # —— 随机通道（随机+事件混合，不用「定时回复」）——
        # 到随机唤醒点掷骰子：命中则随机开口（情绪/话题），未命中重采样继续等。
        # 情绪发言受冷却限制（话题发言不受限，对齐 PROACTIVE_COOLDOWN 语义）。
        if self._random_enabled and now >= self._next_random_at:
            self._resample_random_at()
            if random.random() < self._random_chance:
                if self.last_proactive_at is not None:
                    since_last = (datetime.now() - self.last_proactive_at).total_seconds()
                    if since_last < self.cfg.PROACTIVE_COOLDOWN_SECONDS:
                        return None
                console.dim("[主动] 随机通道触发，主动开口...")
                return "emotional" if random.random() < 0.4 else "topic"
            return None
        if self.loneliness >= _LONELINESS_THRESHOLD:
            if self.last_proactive_at is not None:
                since_last = (datetime.now() - self.last_proactive_at).total_seconds()
                if since_last < self.cfg.PROACTIVE_COOLDOWN_SECONDS:
                    return None
            return "emotional"
        if self.boredom >= _BOREDOM_THRESHOLD:
            return "topic"
        if self.curiosity_drive > _CURIOSITY_DRIVE_THRESHOLD and random.random() < 0.3:
            self.curiosity_drive = 0.0
            console.dim("[主动] 探索欲驱动，主动聊个话题...")
            return "topic"
        return None

    def _begin_topic(self, topic: dict) -> None:
        """进入话题活跃期并记录使用次数（对标 ActiveTopicState + record_topic_used）。"""
        self.active_topic = {
            "topic_id": topic["id"],
            "started_at": datetime.now(),
            "user_engaged": False,
        }
        self._topic_stats.setdefault(topic["id"], {"use": 0, "engaged": 0})["use"] += 1

    def _pick_topic(self) -> Optional[dict]:
        """按 Muika 权重机制挑选一个话题种子（对标 TopicManager.get_next_topic）。

        1. 类别权重：TOPIC_WEIGHTS 为基础；无聊 > 好奇时 trivia/story 加分、
           philosophy/meta 减分（更想听点轻松的）；
        2. 近期已聊类别 ×0.25 惩罚（窗口 3）；
        3. 每个话题独立冷却 cooldown_minutes（内存记录上次使用时间）；
        4. 类别内按互动率加权（用过 ≥2 次、互动率 <30% → 0.3，<50% → 0.6）；
        5. 全部冷却中返回 None，本次跳过话题管线（对标 Muika：所有话题
           在 cooldown 时 get_next_topic 返回 None，不兜底）。
        """
        weights = dict(_TOPIC_WEIGHTS)
        if self.boredom > self.curiosity:
            for cat, boost in _TOPIC_BOREDOM_BOOST.items():
                weights[cat] = max(0.0, weights.get(cat, 0.0) + boost)
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

    async def _build_prompt(self, kind: str, topic: Optional[dict]) -> str:
        """组装主模型的发言请求：agent（ButlerAgent）优先，无 agent 回退内置。

        agent 负责构造（时段语气 + 类别结尾策略 + 记忆线索注入），
        主模型负责开口生成——即「主动发言交给 agent，agent 催促主模型」。
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
                    kind, topic, memory_context, hour)
            except Exception as e:
                console.error(f"[主动] agent 构造发言请求失败，回退内置：{e}")
        # —— 回退：无 agent 或 agent 失败时的内置简化 prompt ——
        if kind == "emotional":
            return (
                "【安静时刻的自主行动】房间里安静了好一会儿，孤独感仍在蔓延。"
                "请以你的人设自然开口，像自言自语一样说一小段真诚的话，"
                "不必等待对方回应，也尽量不要连珠炮式提问。"
            )
        if topic is None:
            return ""
        return (
            f"【安静时刻的自主行动】你忽然想到了一个念头：「{topic['concept']}」。"
            "请把这个念头自然地表达出来，两三句话即可，结尾留一点余韵，"
            "不要直接向观众提问。"
        )
