# 多 World 与 VTuber 表演层：未实施部分实施计划

> 本文**只写还没做的**。两份源文档里已落地的部分在 §1 作为基线列清（照着不再重做），
> 未落地部分逐项给出形态、文件、协议、配置与验收。
>
> 来源：
> - [边打游戏边聊天-事件驱动多World架构.md](file:///e:/AI/MaiBot-main/.trae/documents/%E8%BE%B9%E6%89%93%E6%B8%B8%E6%88%8F%E8%BE%B9%E8%81%8A%E5%A4%A9-%E4%BA%8B%E4%BB%B6%E9%A9%B1%E5%8A%A8%E5%A4%9AWorld%E6%9E%B6%E6%9E%84.md)（下称「多 World 架构」）—— **P0 / P1 / P2 / P4 已落地**
> - [vtuber-python-port-plan.md](file:///e:/AI/MaiBot-main/.trae/documents/vtuber-python-port-plan.md)（下称「VTuber 移植」）—— **整份未落地**
>
> 工作目录：`e:\AI\直播`（下称「直播实例」）。两份源文档仍存放在 `e:\AI\MaiBot-main\.trae\documents\`；源文档与本文早期版本写的历史路径 `e:\AI\MaiBot-main\直播` 已不存在——实例现已独立为 `e:\AI\直播`，本文全部内链已指向现位置。
>
> 修订记录：**2026-09-18** 复核基线 B1–B11（全部成立，行号无漂移）；修正实例路径与 26 处内链；补齐 W4 / W5 / W6 的落地细节（模块布局、能力通道、刷新时机、端点草案）；§6 实施顺序补入此前遗漏的 W4。
>
> 实施进度：**2026-09-18 当天已按 §6 顺序完成 W1–W6 全部代码**（W3 `plugins/world-asr`、W1 `plugins/world-minecraft` 含 mc-node 桥且已 `npm install`、W5 基座双视图 + CONFIG_VERSION 8.14.47 + 三语 prompt + 单测 9 通过、W4 `player.py` 包络钩子 + `src/maisaka/vtuber_bridge/` + `EmoteToolProvider` + `[vtuber]` 配置节、W2 `plugins/world-pvz` 含标定说明、W6 `/worlds` 路由 + dashboard「世界状态」页已 `npm run build`）。**上述勾选框仍留空**：勾选标准是真实环境验收（MC 服务器 / PvZ 实机标定 / VTS 授权 / 真实开播），代码落地不等于验收通过。V0–V5（VTuber 表演层移植）未动工，仍是独立大轨。

---

## 1. 已落地基线（已核实，不必再做）

以下事实均已逐行核对现有代码（最近一次复核：2026-09-18，全部成立），是后续所有工作的起点。

| # | 基线 | 事实与位置 |
| --- | --- | --- |
| B1 | 世界框架基座在主程序 | [src/worlds/](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds) 共 7 个模块：`types.py` / `bus.py` / `state_cache.py` / `registry.py` / `manager.py` / `tool_provider.py` / `__init__.py` |
| B2 | 具体世界由**插件**实现 | 插件通过能力 `world.register` / `world.unregister` / `world.event` / `world.state` 上报（[registry.py:68-71](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/plugin_runtime/capabilities/registry.py#L68-L71)）；基座通过插件运行时的 `invoke_api` **回呼**世界的 `world_poll` / `world_observe`（[manager.py:33-35](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/manager.py#L33-L35)、[manager.py:342-402](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/manager.py#L342-L402)） |
| B3 | 事件注入入口已就绪 | [runtime.py:946](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/maisaka/runtime.py#L946) `register_world_event(source_name=…, text=…, trigger=…)`，四种投递语义（preempt / flush / debounce / piggyback）已落地；**不走 `register_message()`**，不污染外部消息统计 |
| B4 | 事件总线已支持延迟投递 | [bus.py:48-58](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/bus.py#L48-L58) 用 `heapq` 按 `WorldEvent.delay_seconds` 排队 + [bus.py:106-124](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/bus.py#L106-L124) 逐世界节流 —— **防穿帮的「事件延迟」半边已经做完** |
| B5 | 变化检测轮询已就绪 | [manager.py:404-428](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/manager.py#L404-L428) `_poll_loop` / `_poll_once` 按 `worlds.change_poll_seconds` 轮询声明了 `polls_changes=True` 的世界 |
| B6 | 世界工具 Provider 已注册 | [runtime.py:1504](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/maisaka/runtime.py#L1504) `register_provider(WorldToolProvider())`；当前**只提供 `world_observe`** 一个工具（[tool_provider.py:24-88](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/tool_provider.py#L24-L88)） |
| B7 | prompt 占位符已就位 | `{worlds_section}` 已在 6 个文件中：`prompts/{zh-CN,en-US,ja-JP}/maisaka_chat.prompt` 与 `maisaka_chat_focus.prompt`；zh-CN 含「# 关于世界事件」四条规则 |
| B8 | 基座配置已就位 | `WorldsConfig`（[official_configs.py:5906](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/config/official_configs.py#L5906)）；`bot_config.toml` 已有 `[worlds]`：`enabled=false` / `live_platform="bilibili_live"` / `stream_id=""` / `inject_state=true` / `stale_after_seconds=45.0` / `event_throttle_seconds=3.0` / `change_poll_seconds=2.0`；`CONFIG_VERSION = "8.14.46"` |
| B9 | B 站世界已完成（原 P4） | 插件 [world-bilibili](file:///e:/AI/%E7%9B%B4%E6%92%AD/plugins/world-bilibili/plugin.py) v0.3.0：弹幕/SC 走 `message.inject_inbound`，礼物/上舰/进场走 `world.event`，出站走 `@MessageGateway`；内置适配器已退休 |
| B10 | 插件**原生工具**通道已存在 | `maibot_sdk.Tool` 装饰器 + [component_query.py:283](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/plugin_runtime/component_query.py#L283) 构建 `ToolSpec` + `PluginToolProvider` 已接入 —— **世界插件可以自己声明工具，基座不需要新增工具通道** |
| B11 | 插件**依赖**通道已存在 | manifest `dependencies: [{"type": "python_package", "name": …, "version_spec": …}]`，由 [dependency_pipeline.py](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/plugin_runtime/dependency_pipeline.py) 安装 —— **新世界的第三方依赖进插件 manifest，不进主程序 pyproject.toml** |

---

## 2. 待实施总览

| 编号 | 名称 | 源出处 | 形态 | 前置依赖 | 代价 |
| --- | --- | --- | --- | --- | --- |
| **W1** | Minecraft World | 多 World 架构 §P3 | 插件 `plugins/world-minecraft/` | Node ≥18 + `mineflayer` | 中 |
| **W2** | PvZ World | 多 World 架构 §P5 | 插件 `plugins/world-pvz/` | 4 个新依赖（进插件 manifest） | 高（视觉识别） |
| **W3** | ASR World | 多 World 架构 §P6 | 插件 `plugins/world-asr/` | 无（依赖已具备） | 低 |
| **W4** | Live2D 口型 + 表情 | 多 World 架构 §P7.1 | 主程序钩子 + 注入模块 | VTube Studio 运行中 | 中 |
| **W5** | 防「先知穿帮」延迟双视图 | 多 World 架构 §P7.2 | 主程序基座（配置 + 渲染契约） | W1 / W2 | 中 |
| **W6** | WebUI World 状态页 | 多 World 架构 §P8 后半 | 主程序路由 + `dashboard/` | W1–W5 | 中 |
| **V0–V5** | VTuber 表演层移植 | VTuber 移植 全篇 | 独立子进程 `src/vtuber/` | 无新依赖 | 很高（~15k 行） |

**进度跟踪**（勾选即代表该项已按本文各节「验收」标准验收通过；按 §6 顺序排列）

- [ ] W3 ASR World
- [ ] W1 Minecraft World
- [ ] W4 Live2D 口型 + 表情
- [ ] W5 延迟双视图
- [ ] W2 PvZ World
- [ ] W6 WebUI World 状态页
- [ ] V0–V5 VTuber 表演层（P0 骨架 / P1 排期混音 / P2 overlay / P3 语音 / P4 VTS / P5 接入）

---

## 3. 逐项实施计划

### W1 — Minecraft World（多 World 架构 §P3）

**目标**：AI 知道自己在 MC 里的处境，能对世界下动作，动作结果以事件回传。

**形态**：插件 `plugins/world-minecraft/`（**不是**原 spec 的 `src/worlds/minecraft/`，理由见 §4-D1）。

```
plugins/world-minecraft/
├─ _manifest.json      # capabilities: world.register/unregister/event/state
├─ plugin.py           # WorldWorldPlugin：@API 暴露 world_poll/world_observe；@Tool 声明动作工具
├─ config.toml         # host / port / username / version
├─ bridge.py           # stdio NDJSON JSON-RPC 客户端 + 子进程生命周期
├─ renderer.py         # 结构化 JSON → 中文文本（渲染只在 Python 侧）
└─ node/               # package.json + index.js（mineflayer）
```

**关键实现**

1. **桥接协议**：stdio NDJSON JSON-RPC。无端口、无鉴权、无防火墙弹窗；Node 进程 EOF 即退出，生命周期天然跟随插件进程。**硬约束：stdout 只走协议帧，所有日志走 stderr**（否则污染协议流）。
2. **Node 侧只报结构化 JSON**：`state`（坐标 / 血量 / 饥饿 / 周围方块 / 背包）、`state_changed`、`chat`、`damage`、`death`、`player_join` / `player_leave`、`task_done`。**不做中文渲染**。
3. **变更检测在 Node 侧**：Node 自己按 1s 节拍比对关键字段哈希，只有真变了才推 `state_changed`。插件收到后调 `world.state` 上报快照、并按显著性发 `world.event`：
   - 一般变化（位置移动、背包变化）→ `trigger="debounce"`
   - 显著变化（掉血、死亡、僵尸靠近、玩家进出）→ `trigger="preempt"`
   - 因为变化由 Node **主动推**，注册世界时 `polls_changes=false`，基座轮询不介入（B5 的轮询只留给 W2 这种必须截屏的）。
4. **`world_observe` 用 `@API` 暴露**，返回结构照 [world-bilibili](file:///e:/AI/%E7%9B%B4%E6%92%AD/plugins/world-bilibili/plugin.py#L226-L272) 的写法：`{"success": True, "result": {"snapshot": "<中文状态>"}}`（基座 [manager.py:378-397](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/manager.py#L378-L397) 只认这个形状）。
5. **动作工具**：用 `maibot_sdk.Tool` 声明 `minecraft_do`（自由动作）、`minecraft_scout`（观察周围）、`minecraft_check`（查背包/状态），描述用中文。**「提交即返回」**：工具体只把动作塞进插件内的 `asyncio.Queue` 并立刻返回「已提交，执行结果稍后以世界事件回传」，**不 await 真实执行**；worker 协程串行消费队列，完成后 `world.event(trigger="debounce")` 回传结果。
6. **Node 依赖**：参考已有解压产物 `plugins/mineflayer-4.39.0`（**缺 `node_modules`，需 `npm install`**）。Node 侧自带 `package.json`，不走插件 manifest 的 python 依赖通道（B11 只装 Python 包）。

**配置**（插件 `config.toml`，不是主配置）：`host` / `port` / `username` / `version`（空串=自动协商）。

**验收**：连本地或远程 MC 服务器 → AI 能正确回答「你在哪、周围有什么」→ 能执行 `minecraft_do` 并收到结果事件 → 挖矿时状态**不刷新**（无变化不注入），怪物靠近时**立刻**刷新。

**风险**：连不上 / 中途掉线 / 协议流被 Node 日志污染。按项目规范**明确报错并回传失败事件，不静默兜底、不假装成功**。

---

### W2 — PvZ World（多 World 架构 §P5）

**目标**：AI 能看着植物大战僵尸的画面做决策并操作游戏。

**形态**：插件 `plugins/world-pvz/`。

```
plugins/world-pvz/
├─ _manifest.json      # capabilities: world.* ；dependencies: mss / pywin32 / opencv-python-headless / pydirectinput
├─ plugin.py           # @API world_poll / world_observe；@Tool pvz_plant / pvz_collect / pvz_shovel
├─ config.toml         # window_title / 阈值 / 模板缩放基准分辨率
├─ vision.py           # 帧差 + OpenCV 模板匹配
├─ input.py            # pydirectinput 注入
└─ templates/          # 约 15 张模板 PNG
```

**关键实现**

1. **新增依赖进插件 manifest 的 `dependencies`**（`mss`、`pywin32`、`opencv-python-headless`、`pydirectinput`），由 `dependency_pipeline` 安装 —— **不改主程序 `pyproject.toml` / `requirements.txt`**（偏离 §4-D6）。
2. **只识别 4 类**（PWSR 思路：语义记录交给 AI Memory，机械计算留给 World）：① 场景阶段（主菜单 / 选卡 / 战斗 / 结算）② 阳光数 ③ 每行是否有僵尸 ④ 卡片冷却状态。**不做**僵尸种类、血量、植物识别。
3. **两次检测分级**（§3.2 分离设计的直接受益者）：
   - 注册世界时 `polls_changes=true` → 基座按 `worlds.change_poll_seconds` 每 2s 回呼一次 `world_poll`。
   - `world_poll` 里做**廉价帧差**：`mss` 截窗口 → 灰度 → 缩到 64×64 → 与上一帧算平均绝对差，未超阈值直接返回 `{"changed": False}`。
   - 只有超阈值才跑**昂贵**的 OpenCV 模板匹配，返回 `{"changed": True, "snapshot": "<中文状态>"}`。
4. **状态文本**示例：`现在是第 3 波，阳光 175，第 2 行和第 4 行有僵尸，樱桃炸弹冷却好了。`
5. **工具**（`@Tool`，同样「提交即返回」）：`pvz_plant`（种植）、`pvz_collect`（收阳光）、`pvz_shovel`（铲除）。输入注入用 `pydirectinput`；**游戏窗口未聚焦时不执行，并回传失败事件**。

**验收**：静止画面下帧差不触发渲染；阳光数变化时状态刷新；`pvz_plant` 能真实种植。

**风险**：窗口不在前台、分辨率不匹配导致模板失配、游戏本身暂停。全部走「明确报错 + 失败事件」。

---

### W3 — ASR World（多 World 架构 §P6）

**目标**：麦克风里说的话变成 AI 上下文里的转写文本。

**形态**：插件 `plugins/world-asr/`（最小的一项）。

```
plugins/world-asr/
├─ _manifest.json      # capabilities: world.register/unregister/event
├─ plugin.py           # 世界注册 + 转写→事件
├─ config.toml         # enabled / device / silence_seconds
└─ capture.py          # VAD 分段采集
```

**关键实现**

1. `capture.py` 用 `sounddevice` + `soundfile` 做 VAD 分段采集（**主程序环境已有**，无需新增依赖）。
2. 分段音频 → base64 → 复用 [utils_voice.py](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/common/utils/utils_voice.py) 的 `asr_model.transcribe_audio()`。
3. 转写文本 → `world.event(world_name="asr", trigger="debounce", text="你听到：<转写文本>")`。
4. **三重开关**：主配置 `worlds.enabled` + 插件 `enabled` + 既有 `global_config.voice.enable_asr`。
5. 麦克风设备选择：插件 `config.toml` 的 `device` 留空时用系统默认输入设备，与直播语音的**输出**设备互不干扰（输出侧消歧逻辑见 [player.py:275-303](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/maisaka/tts/player.py#L275-L303)）。

**验收**：对麦克风说话，1~2 秒后 AI 上下文出现转写文本。

> 若插件运行环境与主程序隔离导致缺包，把 `sounddevice` / `soundfile` 写进插件 manifest 的 `dependencies` 即可（B11）。

---

### W4 — Live2D 口型联动 + 表情（多 World 架构 §P7.1）

**目标**：AI 说话时 Live2D 模型的嘴随音量开合、静音闭合；并能主动做表情。

**信号源已在手**：[player.py:250-264](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/maisaka/tts/player.py#L250-L264) 每个 PCM chunk 已解码为 `float32`，取 RMS 就是口型开合度。

**主程序改动（唯一，约 6 行）** —— `src/maisaka/tts/player.py` 加包络回调钩子，**不改变播放行为**：

```python
# TtsPlayer.__init__ 新增
self._envelope_listeners: list[Callable[[float, int], None]] = []

# _collect_and_play 里 stream.write 之前
rms = float(np.sqrt(np.mean(pcm ** 2))) if pcm.size else 0.0
for listener in self._envelope_listeners:
    listener(rms, sample_rate)
```

`rms` 与 `sample_rate` 就足够任何订阅者重建口型曲线。

**订阅对象（明确）**：只订阅 `live_voice_player`（[live_player.py](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/maisaka/tts/live_player.py) 的 `_build_live_player()` 产出的直播朗读专用 `TtsPlayer` 实例）——直播形象只跟直播间回复动嘴；通用本机朗读 `tts_player` **不接**，避免非直播朗读也带动模型嘴型。

**注入模块布局**（D7：放主程序，不进 `src/worlds/`）：

```
src/maisaka/vtuber_bridge/
├─ __init__.py
├─ config.py       # 读 config/bilibili_live.toml 的 [vtuber]（D8）；路径约定复用 live_player.py 的 LIVE_TTS_CONFIG 环境变量 + 仓库根回退
├─ vts_client.py   # aiohttp WS 最小子集：认证 / 参数白名单 / 注入 / 表情 / 保活重连
├─ lipsync.py      # 包络 → MouthOpen 曲线；30Hz 注入节拍
├─ emote.py        # ExpressionActivationRequest 定时关闭 + EmoteToolProvider
└─ state.py        # 连接状态只读快照（供 W6 WebUI 展示与 §5 互斥自检用）
```

- **启动与停止**：随主程序异步任务启停（生命周期参照 `WorldManager` 的 `_poll_task` 模式：`start()` 建任务、`stop()` 取消任务并关闭 WS）。`[vtuber].lipsync_enabled = false` 时整个包不加载，零开销。
- **表情清单来源**：vts-client.ts 没有「列出表情」的协议调用，表情清单由 `[vtuber].expressions` 映射表配置（中文名 → VTS 表情名）。`vtuber_emote(emotion)` 的参数说明由该表生成；`emotion` 不在表内直接返回错误；表内表情在 VTS 侧不存在（`ExpressionActivationRequest` 报错）时按规范明确报错并提示修正配置，不静默吞掉。
- **状态出口**：`state.py` 暴露 `snapshot()`（连接状态 / 当前注入参数 / 最近一次错误），W6 的 VTS 状态卡片读它；未连接时如实返回，不伪造在线。

**vts_client.py 协议最小子集**（照 [vts-client.ts](file:///e:/AI/%E7%9B%B4%E6%92%AD/cortico-world-vtuber-main/src/vts-client.ts)，用 `aiohttp`，已有依赖）：

- 连接 `ws://127.0.0.1:8001`，`apiName: "VTubeStudioPublicAPI"`。
- 认证：`AuthenticationTokenRequest`（超时放宽到 60s，等人工在 VTS 弹窗点「允许」）→ token 落配置 → 之后重连只走 `AuthenticationRequest`。
- **连接时必须先 `InputParameterListRequest` 取实机参数白名单**，注入前过滤掉不存在的参数 —— 否则**整包**被 `APIError 453` 拒绝。
- 逐帧注入：`InjectParameterDataRequest`，`{faceFound: false, mode: "set"|"add", parameterValues: [{id, value, weight}]}`；**口型用 `mode="set"`**（覆盖模型值），头/眼用 `"add"`（叠加在 idle/物理之上）。
- 表情：`ExpressionActivationRequest`。
- 断线重连退避 `[500,1000,2000,4000,5000]ms`；连续 3 次超时熔断重连。

**口型曲线**：订阅 `player.py` 的包络回调 → 上行即时 / 下行 `τ=40ms` 指数衰减（避免嘴突然闭合的生硬感）→ 以固定节拍 **30Hz** 调用 `inject_parameters(mode="set")`；音量归一到 `MouthOpen ∈ [0,1]`，静音时归零。

**「表情」出口**：给 AI 一个表情工具（如 `vtuber_emote(emotion)`）。**建议形态**：作为主程序内的 `ToolProvider` 注册进 `_register_tool_providers()`（与 `WorldToolProvider` 同层），**不注册成「世界」** —— 理由见 §4-D7。

**配置**：与 VTuber 引擎共用同一节（见 §4-D8）。W4 只读这些键：`lipsync_enabled`（W4 口型开关，**与 V 引擎总开关 `enabled` 互斥**，见 §5）、`vts_ws_url` / `vts_plugin_name` / `vts_plugin_developer` / `vts_auth_token` / `mouth_param` / `expressions`。V 引擎专属键（`engine_url` / `pack_dir` / `overlay_port` 等）W4 不读。

**验收**：首次连接 VTS 弹授权窗 → AI 说话时模型嘴随音量开合、静音时闭合。

**风险**：
- 首次连接需人工点「允许」；token 存配置后重连不再弹窗；**token 失效时停止自动重连并明确报错**，不反复弹窗。
- 依赖 VTube Studio 正在运行且已加载 Live2D 模型；未就绪时口型模块**启动失败并明确报错**（不影响聊天与游戏两路输出）。

---

### W5 — 防「先知穿帮」延迟双视图（多 World 架构 §P7.2）

**目标**：AI 不在僵尸出现在**观众**屏幕之前就喊「有僵尸！」。

**已做完的一半**：事件的延迟投递已经落地 —— `WorldEvent.delay_seconds`（[types.py:49](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/types.py#L49)）+ 总线 `heapq` 按到期时间排队（[bus.py:48-58](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/bus.py#L48-L58)）。世界侧只需在上报时带上 `delay_seconds`。

**还缺的部分**

1. **配置**：`[worlds]` 增加 `delayed_sources: List[str] = []`（如 `["minecraft", "pvz"]`）与 `delayed_seconds: int = 8`（OBS 画面延迟秒数）。当前 B8 的 `[worlds]` 里**没有**这两项 → 改 `WorldsConfig`（[official_configs.py:5906](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/config/official_configs.py#L5906)）+ `CONFIG_VERSION` `"8.14.46" → "8.14.47"`。
2. **双视图渲染契约**：世界提供两个渲染方法 —— `render_snapshot()`（实时视图，给 AI 决策动作用）与 `render_delayed_snapshot()`（延迟视图，即观众此刻在 OBS 上看到的画面，给**解说**用）。
   - 插件化下的表达：
     - `WorldDescriptor`（[types.py:53-69](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/types.py#L53-L69)）追加 `supports_delayed: bool = False`；插件注册时经 `world.register` 能力透传 `supports_delayed=True`（world-bilibili 形态是 `ctx.call_capability("world.register", …)` 直接加参，[_cap_world_register](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/plugin_runtime/capabilities/worlds.py#L50) 解析进描述符即可，SDK 无需升级）。
     - 约定可选 API `world_observe_delayed`（返回形状与 `world_observe` 相同：`{"success": True, "result": {"snapshot": …}}`），只由声明了 `supports_delayed` 的世界实现。
3. **事件延迟由基座统一套**（新决策）：`_cap_world_event`（[capabilities/worlds.py:118](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/plugin_runtime/capabilities/worlds.py#L118)）已透传 `delay_seconds`（worlds.py:144，已核实），但 OBS 延迟是**部署参数，只应配一处**——基座对 `world_name ∈ delayed_sources` 的事件把 `delay_seconds` **强制覆写**为 `worlds.delayed_seconds`，世界侧完全不感知 OBS 延迟。不做「世界上报值与配置取大」之类的折中；确有「某类事件不延迟」的例外需求时，将来再加按 `event_type` 的白名单，第一版不做。
4. **延迟快照的缓存与刷新时机**（本项原文缺失，是基座真正要写的新逻辑）：
   - `WorldStateCache` 每世界扩为两个槽位（realtime / delayed），各自独立 `dirty` 与 `last_injected_at`；
   - 刷新时机：`_poll_once` 对 `supports_delayed` 且 `name ∈ delayed_sources` 的世界，在 `world_poll` 报 changed 时**依次**回呼 `world_observe` 与 `world_observe_delayed`；`stale_after_seconds` 对两个视图同样兜底（长时间无变化也要周期性重注入延迟视图，维持 AI 对「观众画面」的时间感知）；
   - `build_state_injection`（[manager.py:270](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/manager.py#L270)）对这类世界输出两段，其余世界维持现状（即 §4-D9）。
5. **注入格式**（明确区分两个视图，外层沿用现状的 `【{display_name}】` 头）：

```
【Minecraft 世界】
【实时】你在 (128,64,-302)，血量 12/20，3 只僵尸正在靠近。
【观众此刻看到的画面】你还在挖矿，血量 18/20，周围没有僵尸。
```

6. **prompt 规则**：解说只能基于「观众此刻看到的画面」。属对现有 `{worlds_section}` 四条规则的补充 → 需同步 zh-CN / en-US / ja-JP 三语。zh-CN 增补句（基准，另两语照译）：

```
- 当某个世界的状态同时给出「实时」与「观众此刻看到的画面」两个视图时：你的操作与决策依据实时视图；说给观众听的解说只能基于观众此刻看到的画面，绝不能提前说出观众画面里还没有的内容。
```

7. **W1 侧配合**：Node 环形快照需保留 ≥ `delayed_seconds + 5s` 余量的历史状态（1s 节拍即 ≥ 13 份），`world_observe_delayed` 返回「now − delayed_seconds」时刻的渲染。

**验收**：设 `delayed_seconds=8` 后，① AI 不会在僵尸出现在观众画面前喊「有僵尸」；② 僵尸出现后 8 秒内问「观众现在能看到什么」，回答**不包含**僵尸；问「你现在看到什么」，回答**包含**僵尸——两个视图不串。

> 延迟视图的内容保真度取决于世界侧能重建「N 秒前的状态」；W1 由 Node 侧保留一份环形快照即可，W2 由截帧环形缓冲即可。**做不到就明确报错/不声明 `supports_delayed`，不要用实时视图冒充延迟视图。**

---

### W6 — WebUI World 状态页（多 World 架构 §P8 后半）

**已完成的一半**：配置侧（`WorldsConfig` + `CONFIG_VERSION 8.14.46` + `[worlds]` 已生成）已完成。**只差 WebUI。**

**后端**：新增 [src/webui/routers/worlds.py](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/webui/routers)，`APIRouter(prefix="/worlds", tags=["worlds"], dependencies=[Depends(require_auth)])`，挂载方式照抄同目录既有路由（[bilibili_live.py:29](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/webui/routers/bilibili_live.py#L29) 同款）。端点草案：

| 端点 | 返回 | 数据源 |
| --- | --- | --- |
| `GET /worlds/overview` | 总开关、`live_platform`、各世界清单与在线状态 | `worlds.*` 配置 + `WorldRegistry.descriptors()`（[registry.py:76](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/registry.py#L76)，含 `name` / `display_name` / `plugin_id` / `polls_changes` / `supports_delayed`）；registry 里有描述符即视为在线 |
| `GET /worlds/state` | 各世界当前状态文本与最后更新时刻 | `WorldStateCache.build_blocks()` |
| `GET /worlds/events` | 最近世界事件流 | `WorldEventBus` **新增环形缓冲**：`collections.deque(maxlen=200)`，在事件出队投递时记录 `world` / `trigger` / `text` / 投递时刻；内存态，不落库 |
| `GET /worlds/tools` | 近期世界工具调用 | 复用现有工具调用落库（`database.query` 里已有 `tool_name` 维度） |
| `GET /worlds/vtuber` | VTS 连接状态 | W4 `state.py` 的 `snapshot()`；W4 未实施时如实返回 `not_configured`，不伪造在线 |

**前端**：`dashboard/`（React 19 + TanStack Router + bun）新增「世界状态」页面 + 导航项（[router.tsx](file:///e:/AI/%E7%9B%B4%E6%92%AD/dashboard/src/router.tsx) 加 `createRoute` 并挂进路由表）。**i18n 要同步四个 locale 文件（zh / en / ja / ko）——比 prompt 的三语多一门韩语，别漏。** 展示聊天流时按项目规范显示实际名称（群名称 / xxx 的私聊），不用 session_id。

**注意**：主程序 WebUI 的 8001 走 `[webui] mode = "production"` 读 `dashboard/dist`，而 **7999 是常驻 vite dev server**。改前端后若要看 8001 的效果，必须重新构建：`bun run build`（即 `tsc -b && vite build`；仓库锁文件是 bun.lock，用 `npm run build` 亦可）；只调样式可先在 7999 看。页面新增属较大改动，收尾要跑一次 build。

---

### V0–V5 — VTuber 表演层移植（VTuber 移植 全篇）

**状态：整份未落地** —— `直播/src/vtuber/` 目录不存在；`config/bilibili_live.toml` 里只有 `[voice]`，没有 `[vtuber]` 节。

**目标**：把 `直播/cortico-world-vtuber-main`（TypeScript，约 15k 行）的能力用 Python 重写进直播实例，让麦麦的直播间回复**直接成为演出台本**（含 `【】` / `<>` 记号）：语音照常出声，Live2D 跟着做动作，OBS 里字幕跟着嘴逐字上屏。

**已确认口径**：Python 原生重写；复用现有 GSV-TTS-Lite；机器上已有 VTube Studio + Live2D 模型，注入层可真实联调。

**核心决策（照原文档不变）**

| 项 | 决定 |
| --- | --- |
| 台本来源 | **麦麦的直播间回复直接当台本** —— 改动集中在 [reply.py:631-636](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/maisaka/builtin_tool/reply.py#L631-L636) 一处 |
| 引擎形态 | **独立子进程** `python -m src.vtuber.service`（60Hz 注入不能被主循环拖尾） |
| 宿主通信 | MaiBot 侧 HTTP POST 台本给引擎；引擎提供 overlay 的 HTTP/SSE/WS |
| 新增依赖 | **无**（`aiohttp` / `sounddevice` / `fastapi` / `numpy` 都已在） |
| 时钟基准 | `time.monotonic()` 为 L2/L3 唯一时钟；音频播放坐标由 sounddevice 写入进度导出 |

**目录与模块映射**（新建包 `直播/src/vtuber/`；TS 目录在验收前保留作参考，不删）

| TS 源 | Python 目标 | 移植要点 |
| --- | --- | --- |
| `pack.ts` | `pack.py` | 三份 JSON（`params`/`vocab`/`clips`）加载与严格校验、`resolveTag`（含 `Reset`/别名归一）、`range` 钳位、`vocab_table_rows` |
| `clips.ts` | `clips.py` | `EASES`、`sample_keys`、`drift_noise`、Pulse/Sustain/Gaze 资产 dataclass |
| `states.ts` | `states.py` | 三通道状态机；`STATE_FADE_IN_MS=300`，超时 Gaze 4–6s / Pose 10–15s / Emotion 20–30s，衰减淡出 2–3s |
| `parser.ts` | `parser.py` | `ScriptParser` 逐字符状态机（`【】` 切分、`<>` 锚点、`[]` 语气词、`INLINE_TAG_MAX=32`、`SENTENCE_FLUSH_MIN_CHARS=40`、半开括号按字面溢出）。**不移植 `JsonScriptStream`** |
| `mixer.ts` | `mixer.py` | 60Hz 逐帧混音：sustain 冻结快照 crossfade、pulse 加性轨迹、prosody 层、gaze controller（保持—突跳、头滞后跟随、眼在头内）、ducking（0.3 / 90ms 压入 / 350ms 恢复）、接管期眨眼、lipsync、`AMBIENT_HEAD` 环境漂移、override 语义 |
| `orchestrator.ts` | `engine.py` | **精简版 Performer**：beat 队列、`gap` 计算（同行 100ms / 换行 400ms / 省略号 300ms、上限 1.2s、±10% 抖动）、speech_onset 让位、同拍 gesture 延迟 300ms、字幕派发与重发、积压闸门（`speechCapSec`）、硬打断（音频 150ms fade、State 保留）、纯动作 beat 不等语音 |
| `subtitle-cues.ts` | `subtitle_cues.py` | cue 切分（句末必断、长句逗号就近断、零标点按单元数硬切）+ 字幕时间**估计档**（`msPerUnit=400` + 停顿先验 `endPunct=570` / `midPunct=200`）+ cue 字段（`atMs`/`durMs`/`speakMs`） |
| `tts.ts` + `tts-server.ts` | `tts.py` | 换成 GSV-TTS-Lite SSE 客户端（收流方式照抄 [player.py:217-272](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/maisaka/tts/player.py#L217-L272)）；每片产出 PCM 与 **RMS/峰值包络**；预取 1–2 片 |
| `device-audio.ts` | `audio.py` | sounddevice 输出；设备名消歧复用 [player.py:275-303](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/maisaka/tts/player.py#L275-L303)；写入进度导出「可听时刻」 |
| `backend.ts` + `vts-client.ts` | `backend.py` + `vts_client.py` | `aiohttp` WS：认证 → token 落配置 → `AuthenticationRequest`；连接时 `InputParameterListRequest` 取白名单（不存在的参数必须丢，否则整包被拒）；每帧两帧在途 + 最新帧候补；FX 走 `ExpressionActivationRequest` 定时关；保活与重连退避 |
| `models/*` | `models/contract.py`·`schema.py`·`registry.py` | `ModelProfile`/`ParamWiring`/`FxEntry`、`to_wire`（`clamp(neutral + value×scale)`、`invert`、`aliasTo` 多路取均值）、`DEFAULT_PROFILE`、档案 JSON 校验、按 id / VTS 模型名定档 |
| `perform-stream.ts` | `perform_stream.py` | `GET /overlay`、SSE `/stream`（`id:` 序号 + `Last-Event-ID` 续传 + 15s 心跳 + **2KB 垫片**防 OBS CEF 攒流）、WS `/danmaku`（只放本机 Origin） |
| `overlay/` | `overlay/` | `overlay.html`/`app.js`/`styles.css` **原样搬运**（只订同源 `/stream`，事件契约不变） |
| `world.ts`/`definition.ts`/`proxy.ts`/`engine-child.ts`/`engine-ipc.ts` | `service.py` + `client.py` | 进程入口（读配置、起 overlay 服务、接 VTS、跑 tick 循环、POST `/act` 与 `/interrupt`）+ MaiBot 侧 thin client |

**分阶段实施**（每阶段都能单独验证，不做「全部写完再跑」）

| 阶段 | 内容 | 验证 |
| --- | --- | --- |
| **P0** 骨架 / 演出包 / 解析器 | `src/vtuber/{__init__,clips,pack,voice_tags,parser}.py` + 演出包数据（直接搬 `cortico-world-vtuber-main/src/examples/vtuber-pack/{params,vocab,clips}.json` 到 `src/vtuber/packs/example/`，`pack_dir` 可指向自定义包） | `uv run pytest src/vtuber/tests -q`（包校验失败路径、别名归一、`【】`/`<>`/`[]`/半开括号/40 字切分/锚点 `charOffset`） |
| **P1** 排期 + 混音 + 字幕 cue（无外部设备） | `states.py`、`mixer.py`、`engine.py`、`subtitle_cues.py`；`mixer` 支持 `ambient=False` 的确定性模式，`engine` 支持「空音频/空后端」 | 单测（状态机超时衰减、gap 与锚点时刻、ducking 曲线、字幕 cue 时刻）；`python -m src.vtuber.service --no-vts --no-audio` 打逐帧 IR 与 cue 的结构化日志 |
| **P2** overlay 服务 + 画面 | `perform_stream.py` + `overlay/` 静态资产搬入（改注释与来源声明，不改契约） | `--no-vts --no-audio` 起服务，浏览器开 `http://127.0.0.1:7792/overlay?bg=dim`，`curl -X POST .../act -d '{"script":"【微笑】大家好【】<看向弹幕>今天聊点啥"}'` → 页面出现字幕与动作气泡 |
| **P3** 语音 | `tts.py`（GSV SSE 分片）+ `audio.py`（声卡输出 + 可听时刻）+ 包络驱动 lipsync | 真机跑一段台本：出声、口型跟动、字幕与语音同步（估计档） |
| **P4** VTS 注入 + 模型档案 | `vts_client.py`、`backend.py`、`models/*`；首次连接弹 VTS 授权窗，token 写进 `config/bilibili_live.toml`；按你的模型写一份 `cortico.profile.json` 放进 `<live2d_dir>/<模型目录>/` | 模型真的转头/换表情/看向弹幕；`--no-vts` 下只打帧日志 |
| **P5** 接入直播链路 | [reply.py:633-636](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/maisaka/builtin_tool/reply.py#L633-L636) 直播分支（`if sent and platform == LIVE_PLATFORM:`，其中 `sent` 靠插件 `world-bilibili` 的出站网关回报成功才成立）：把 `live_voice_player.speak(merged_speak_text)` 换成同一段文本交给 `vtuber_client.act(...)`（**同一条回复只能播一次**；`[vtuber].enabled = false` 时保持现有朗读路径；启用但引擎不可达则**明确报错，不静默回退**）；`prompt.py` 追加演出规则与由演出包生成的词表表；`config/bilibili_live.toml` 新增 `[vtuber]` 节；WebUI 加「引擎连接状态 + overlay 链接」 | 真实直播间开播，发弹幕走完整链路 |

**配置项**（`config/bilibili_live.toml` 新增 `[vtuber]`）：`enabled`、`engine_url`(http://127.0.0.1:8096)、`pack_dir`、`vts_ws_url`(ws://127.0.0.1:8001)、`vts_plugin_name`/`vts_plugin_developer`/`vts_auth_token`、`live2d_dir`、`model_profile`、`overlay_port`(7792)、`stream_enabled`、`speech_cap_sec`(20)、`silence_remind_sec`(10)、按通道的 `decay_gaze_sec`/`decay_pose_sec`/`decay_emotion_sec`；同节还承载 W4 的键（`lipsync_enabled` / `mouth_param` / `expressions`，见 W4 节，与 `enabled` 互斥）；音频设备与音色**复用现有 `[voice]`**，不重复配置。

**明确不移植 / 降级**（照原文档不变）

- **强制对齐**：本机没有对齐器 → 字幕与 `<>` 锚点只走估计档；锚点抢跑重对齐、前缀对齐不做。
- **VoxCPM2 语气词 `[]`**：GSV-TTS-Lite 不认 → 默认整块剥离，提示词里同步不列该表（`voice_tags.py` 留一张默认空的映射表）。
- **韵律重音预排、跑飞预算裁剪、静音扫描精修**：第二阶段；第一版只做句首 `speech_onset` 头动。
- `obsDelaySec` 播出延迟地板、诊断录制导出、Cortico 控制台面板、`runtime/` 权重下载：不做。
- 子进程帧率隔离：由「独立引擎进程」这一层替代，不再另做 fork/IPC。

**验证总览**：① `pytest src/vtuber/tests -q` ② `--no-vts` + `POST /act` + 浏览器 `/overlay?bg=dim` ③ VTS 实机（授权 → 注入 → 动作/表情/注视正确；模型档案缺失时用 `DEFAULT_PROFILE` 并提示）④ WebUI 引擎状态与 overlay 链接可复制给 OBS ⑤ 真机直播确认「回复即台本」整链无重复朗读。

**风险**：60Hz 注入在 Python 下的抖动（引擎独立进程 + 固定节拍补偿）；VTS 首次授权需人工点「允许」；**同一条回复不能既走 `live_voice_player` 又走 VTuber 引擎**（回声/双播）；**许可：cortico-world-vtuber 是 AGPL-3.0-or-later，MaiBot 同为 AGPL-3.0 → 每个移植文件头注明来源与许可，overlay 资产与演出包保留出处说明。**

---

## 4. 相对原 spec 的必要偏离（基座插件化之后）

原文档写于「基座尚未落地」时，部分写法与现状不符。下面是必须按现状调整的地方：

| # | 原 spec 写法 | 现行做法 | 理由 |
| --- | --- | --- | --- |
| **D1** | 各世界建在 `src/worlds/minecraft` / `pvz` / `asr` | 建在 `plugins/world-minecraft` / `world-pvz` / `world-asr` | 基座在主程序、**具体世界一律插件**（原文档 §P4 已自我修正为此形态，其余世界沿用） |
| **D2** | `src/worlds/world.py` 里的 `World` ABC + `WorldHost` | 实际是 `WorldDescriptor`（[types.py:53-69](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/types.py#L53-L69)）+ 插件能力 + 基座回呼 `world_poll`/`world_observe`。**没有 `world.py`** | 世界跑在插件进程里，ABC 无从继承；契约改由「能力 + 约定 API」表达 |
| **D3** | `src/worlds/change_task.py` 挂周期任务 | 已并入 [manager.py:404-428](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/manager.py#L404-L428) 的 `_poll_loop`/`_poll_once` | 无需额外文件；且只轮询 `polls_changes=True` 的世界 |
| **D4** | 世界的工具由 `WorldToolProvider.list_tools()` 汇总 | 插件用 `maibot_sdk.Tool` **原生声明**（B10）；基座只留 `world_observe` | 插件工具通道已存在，重复造一层没有收益 |
| **D5** | 世界配置放主配置 `[worlds.minecraft]` / `[worlds.pvz]` / `[worlds.asr]` | 各世界的连接参数放**插件自己的 `config.toml`**（`PluginConfigBase`）；主配置 `[worlds]` 只留基座字段（B8） | 与 `world-bilibili` 的既有形态一致：世界归插件，基座归主程序 |
| **D6** | PvZ 新依赖写进 `pyproject.toml` 并同步 `requirements.txt` | 写进**插件 manifest** 的 `dependencies`（B11） | 只有这一个插件需要，不该污染主程序依赖 |
| **D7** | 口型挂在 `src/worlds/vtuber/` 并作为 World 挂载 | 建议：包络钩子在主程序 `player.py`（已在原文档 §7.1 明确），VTS 注入 + lipsync 也放**主程序模块**，表情工具用**独立 `ToolProvider`**（与 `WorldToolProvider` 同层）而非「世界」 | ① 包络源在主程序进程内，30Hz 跨进程 IPC 不划算；② 基座 [manager.py:342-402](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/manager.py#L342-L402) 的 `world_poll`/`world_observe` 回呼**只认插件 API**，主程序内的世界无法被回呼，注册成世界反而多一层不可用的契约 |
| **D8** | 多 World 架构 §P8 用 `[worlds.vtuber]`；VTuber 移植用 `config/bilibili_live.toml` 的 `[vtuber]` | **两文档冲突，需统一**。建议只保留 `[vtuber]`（引擎与口型同属表演层输出侧，不是「世界」），口型参数并入该节 | 一次性避免两份配置各自演化 |
| **D9** | 世界状态注入格式 `【实时】/【观众此刻看到的画面】` | 现状注入是 `<world_state>` + `【{display_name}】`（[manager.py:284-291](file:///e:/AI/%E7%9B%B4%E6%92%AD/src/worlds/manager.py#L284-L291)）；双视图需在此之上扩展 | 只在 `supports_delayed` 的世界上有第二段，其余世界维持现状 |

---

## 5. Live2D 侧的交集与硬约束

- **共用一个信号源**：包络回调留在 `player.py`。W4 的 lipsync 订阅它（只订 `live_voice_player` 实例，见 W4 节）；将来 VTuber 移植的 `src/vtuber/tts.py` **直接订阅同一个回调**，不另开音频捕获。
- **唯一注入方（硬约束）**：同一时刻只允许一个 VTS 注入方。
  - 若只实施 W4：W4 是注入方。
  - 若 W4 与 V-P4 都实施：`src/vtuber/` 引擎是**唯一**注入方，W4 的 lipsync **必须停用**。
  - 配置层面体现为互斥：`[vtuber].enabled`（V 引擎）与 `[vtuber].lipsync_enabled`（W4）**不得同时为真**，启动时检测到同真**显式报错**，不静默二选一。W6 的 `/worlds/vtuber` 端点把当前生效的注入方如实显示出来。
- **配置不重复**：音频设备与音色统一复用现有 `[voice]`（[config/bilibili_live.toml](file:///e:/AI/%E7%9B%B4%E6%92%AD/config/bilibili_live.toml)），VTuber 只加 `[vtuber]`。

---

## 6. 建议实施顺序

| 顺序 | 项 | 依赖 | 说明 |
| --- | --- | --- | --- |
| 1 | **W3 ASR World** | 无 | 最便宜，能最快把「世界 → 事件 → 上下文」这条链在真实世界（而非 B 站）上再验证一遍 |
| 2 | **W1 Minecraft World** | Node + mineflayer | 「边玩边聊」的主干；同时产出 W5 需要的延迟视图素材 |
| 3 | **W4 Live2D 口型 + 表情** | 无（VTube Studio 运行中即可） | 独立短链路，能最早看到「形象活了」；与 V 轨互斥（§5）——若决定直接上 V0–V5，可跳过 W4，避免做完即停用 |
| 4 | **W5 延迟双视图** | W1 | 事件延迟半边已就绪，只补配置与渲染契约 |
| 5 | **W2 PvZ World** | 4 个新依赖 | 视觉识别最重，放最后做游戏侧 |
| 6 | **W6 WebUI World 状态页** | W1–W5 | 有内容可展示才有意义 |
| 7 | **V0–V5 VTuber 表演层** | 无（独立轨道） | 体量最大，可与 1–5 并行推进；进入 V-P4 前必须先定 D7/D8（本文已按建议口径写定） |

---

## 7. 风险与「不静默兜底」

按项目规范，以下场景**一律明确报错 / 回传失败事件，不做静默兜底**：

- **W1 / W2 依赖外部程序**（风险最高）：MC 服务器连不上、PvZ 窗口不在前台、模板因分辨率不匹配失配 → 世界明确报错并回传失败事件，不假装成功。
- **Node 侧协议流污染** → 违反「stdout 只走协议帧」时立刻暴露，不写容错解析。
- **W4 首次 VTS 授权**需人工点「允许」；token 失效时**停止自动重连并明确报错**，不反复弹窗。
- **W4 / V-P4 注入方互斥**：`[vtuber].enabled` 与 `[vtuber].lipsync_enabled` 同时为真时启动即报错（见 §5）。
- **V-P5 双播**：启用 VTuber 后必须关闭 `live_voice_player` 路径；引擎不可达时报错，不回退朗读。
- **W5 延迟视图保真**：世界重建不出 N 秒前状态时，不声明 `supports_delayed`，不用实时视图冒充。
- **W6 后端**：`WorldRegistry` / `WorldStateCache` / 总线历史出现不一致时如实展示，不伪造「在线」。

---

## 附录：与本次改动相关的收尾项

1. **i18n 死键** `routes.bilibiliLive`：4 个语言文件里都有（中「直播接入」/ 英「Live Access」/ 日「配信連携」/ 韩「라이브 연동」）但**已无任何代码引用**（适配器退休后的残留），建议清理。（2026-09-18 复核仍成立。注意：`/bilibili-live` **路由本身还在**，是「直播语音」页——死的只是这个 i18n 键，别把路由一起删了。）
2. **两份源 spec 的后续世界仍写作 `src/worlds/minecraft|pvz|asr`**：本文 §4-D1 已按插件化纠正。是否回改原文由你决定。
3. **仓库根 `e:\AI\MaiBot-main\`（重构前副本）未动**：如需与 `直播/` 对齐，需另行处理。
