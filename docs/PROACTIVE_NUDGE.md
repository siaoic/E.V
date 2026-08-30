# 主动对话重构：Neuro 风格契机驱动（Nudge Engine）

> 重构参考：`新建文件夹/EV-Anthropomorphic`（SKILL_anthropomorphic_proactive.md /
> NEURO_LIKE.md / reference_code/ev/social/{nudge,proactive}.py）。
> 目标：让 E.V 像 Neuro-sama 一样"看起来随时说话"，实际是**系统持续给 LLM 创造说话契机**，
> 但说不说完全由主模型自主决定。

## 1. 旧版 vs 新版

| | 旧版（定时心跳） | 新版（契机驱动） |
|---|---|---|
| 触发 | 每 5~15s 心跳醒来就问一次 LLM「想不想说」 | 心跳只做**契机检查**（纯本地、零 LLM 调用），契机命中才问 LLM |
| token | 静默期每分钟烧 4~12 次决策调用 | 每个契机只烧 1 次；被拒后同因 60s 内不再打扰 |
| 冷场兜底 | 静默 ≥25s 强制开口（绕过模型意愿） | 保留但收窄：仅冷场/太久没说契机 + `PROACTIVE_FORCE_SPEAK=true` 才兜底 |
| 拒绝权 | 模型可输出 `<SILENT>` | 同样支持，且 **接受率/拒绝率被统计**，供自进化复盘校准 |
| 弹幕感知 | 无（弹幕只走挑选器回复管线） | 未读堆积 / 弹幕爆发 → 产生开口契机 |
| 事件感知 | 仅「互动结束 + 随机超时」 | 5 种契机：氛围切换 / 弹幕爆发 / 未读堆积 / 太久没说 / 冷场 |

## 2. 五种契机（按优先级）

| 契机 | 触发条件 | 给 LLM 的提示 |
|------|---------|--------------|
| `state_change` | 互动回复刚结束（30s 内） | "氛围变化了：从 interactive 切到 idle…" |
| `burst` | 30s 滑动窗口内 ≥10 条弹幕 | "弹幕爆炸了，30 秒内 12 条，想参与吗？" |
| `many_unread` | 累积 ≥5 条未回应弹幕 | "有 7 条弹幕你还没回应，要不要看看？" |
| `silent_too_long` | AI ≥300s 没说话 | "你已经 312 秒没说话了，可能该说点啥了" |
| `long_silence` | 直播间 ≥30s 无任何活动（兜底） | "直播间已经安静 35 秒了，你可以主动说点啥" |

防刷机制：全局冷却 30s + 同因重复抑制 60s + 60s 窗口内最多 3 次。
契机有 20s 保质期：被忙碌门控挡住的陈旧契机自动丢弃。

## 3. 数据流

```
[弹幕到达] ──observe("danmaku")──────┐
[键盘/语音输入] ──observe("user_input")──┤   NudgeEngine（纯内存状态）
[AI 播报完成] ──observe("ai_spoke")────┤   · 未读计数 / 爆发窗口 / 时间戳
[互动回复完成] ──observe("state_change")┘   · 冷却 / 重复抑制 / 统计
                                      │
                     命中契机 → listener → pending_nudge + _wakeup.set()
                                      │
[主循环 wait_input 心跳到点] ──→ engine._take_nudge()
                                      │
                     有契机 → 选话题 → 问 LLM（带契机上下文）
                     无契机 → 本轮直接沉默（零 LLM 调用）
                                      │
              模型接受 → report_act → 入队 → TTS 播报
              模型 [SILENT] → report_reject → 沉默（60s 内同因不打扰）
```

## 4. 新 API

```python
engine = runtime.proactive

# 被动响应式主动发言（LLM/技能可主动举手，不经过契机门控）
result = await engine.request_speak(
    topic_hint="刚才有人说樱花，想接着聊",   # 可选：指定话题
    reason="看到有意思的话题想参与",          # 可选：申请理由（进 prompt）
    nudge_reason="long_silence",             # 可选：若由契机触发
)
# → {"ok": True, "text": ..., "topic": ...}
# → {"ok": False, "reason": "busy" / "output_locked" / "silent" /
#    "duplicate_or_queue_full" / "topic_unavailable"}

# 查询契机状态（调试 / 未来暴露为工具）
engine.nudge_check()
# → {"ok": True, "should_speak": True, "reason": "long_silence",
#     "hint": "直播间已经安静 35 秒了…", "state": {...}}

# 事件埋点（供其他模块）
engine.notify("state_change", {"from": "observe", "to": "active"})

# 运行统计（含契机接受率，供进化复盘）
engine.get_stats()
# → {"trigger": 12, "speak": 8, "silent": 3, "dropped": 1,
#     "nudge_total": 23, "nudge_acted": 15, "nudge_rejected": 8, ...}
```

标记协议（`_parse_decision` 全兼容）：`[SILENT]` / `<SILENT>` / 「沉默」「算了」→ 拒说；
`[END]` / `<END>` → 剥除标记后正常播报前置文本。

## 5. 配置（.env，全部有默认值）

```ini
PROACTIVE_NUDGE_ENABLED=true            # 契机引擎总开关（false=回退旧心跳直问模式）
PROACTIVE_NUDGE_LONG_SILENCE_SEC=30     # 冷场阈值（内敛人设 60~90）
PROACTIVE_NUDGE_SILENT_TOO_LONG_SEC=300 # 太久没说阈值（内敛 600 / 热情 180）
PROACTIVE_NUDGE_MANY_UNREAD=5           # 未读堆积阈值（内敛 8~10 / 热情 3~5）
PROACTIVE_NUDGE_BURST_THRESHOLD=10      # 爆发条数阈值（小直播间 5~8）
PROACTIVE_NUDGE_BURST_WINDOW_SEC=30     # 爆发检测窗口
PROACTIVE_NUDGE_COOLDOWN_SEC=30         # 两次契机最小间隔
PROACTIVE_NUDGE_REPEAT_GAP_SEC=60       # 同因重复抑制
PROACTIVE_FORCE_SPEAK=true              # 冷场兜底强制开口（false=纯自主不强推）
```

## 6. 接线点

| 位置 | 作用 |
|------|------|
| `ev/llm/proactive/nudge.py` | 契机引擎（本体重写，修了参考实现的同因粘滞 bug） |
| `ev/llm/proactive/core.py` | 引擎接线 + `request_speak` + prompt 契机段 + 标记解析 |
| `ev/llm/proactive/executor.py` | heartbeat 契机门控 + 播报后 `ai_spoke` 回报 |
| `ev/danmaku/client.py:_on_danmaku` | 弹幕埋点 `nudge.observe("danmaku")` |
| `ev/kernel/application.py` | 键盘互动回复完成 → `on_ai_spoke()` |
| `ev/kernel/_helpers.py:_chat_danmaku` | 弹幕回复完成 → `on_ai_spoke()` |
| `ev/kernel/handlers/input.py` | **零改动**（wake/heartbeat 接口不变） |

## 7. 反模式（与参考实现一致的红线）

- ❌ 不要让契机强推（绕过 LLM 的 [SILENT]）——`PROACTIVE_FORCE_SPEAK=false` 可彻底关掉兜底
- ❌ 不要把冷却调到 <10s——会变话痨，失去"主动"的拟人感
- ❌ 不要忽略拒绝率统计——接受率 <30% 说明 prompt 调教失败，>90% 说明 [SILENT] 形同虚设
- ✅ 健康指标：契机 ~5-20 次/小时；接受率 50~80%
