"""VTubeStudio 控制：通过官方 WebSocket 插件 API 控制已加载的 Live2D 模型。

直接用 `websocket` 实现官方协议（参考 DenchiSoft/VTubeStudio README）：
- 认证（token 持久化，避免每次弹窗）
- 注入 MouthOpen 参数（口型同步）
- 触发热键（说话 / 待机状态、表情、动作）
- 查询当前模型热键列表

设计要点：
- 后台 reader 协程持续消费 VTS 响应，按 requestID 分发给等待者；
- 高频 inject_mouth 为 fire-and-forget，其响应被 reader 静默丢弃，避免缓冲区堆积；
- 依赖 websocket 默认自动 ping 维持连接，无需手动 keep-alive；
- 所有发送做异常吞咽，VTS 未开启时仅告警，不阻断 LLM/TTS 主流程。
"""

import asyncio
import json
import os
import uuid
from typing import Callable, Dict, List, Optional

import websockets

from ev.utils import config, console
from ev.adapter.avatar import BaseAvatarAdapter

# 口型参数缺失时只提示一次（inject_mouth 每帧调用，避免刷屏）
_mouth_param_warned: bool = False


class VTSController(BaseAvatarAdapter):
    """VTubeStudio WebSocket 客户端封装。"""

    def __init__(self) -> None:
        self.cfg = config.cfg
        self.uri = f"ws://localhost:{self.cfg.VTS_PORT}"
        self.ws: Optional[object] = None
        self.authenticated: bool = False
        self._pending: Dict[str, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        # 事件订阅（官方 Event API）：事件名 → 回调列表
        self._event_handlers: Dict[str, List[Callable[[dict], None]]] = {}

    # ---------- 连接与认证 ----------

    async def connect(self) -> bool:
        """连接 VTS 并完成认证。成功返回 True，失败返回 False。"""
        try:
            # websocket 默认启用自动 ping/pong 维持连接
            self.ws = await websockets.connect(self.uri)
        except Exception as e:
            console.error(f"无法连接 {self.uri}：{e}")
            console.dim("请确认 VTubeStudio 已启动、已在设置勾选 "
                        "'Allow Plugin API access'，且端口正确（默认 8001）。")
            return False

        # 启动后台 reader 消费所有响应
        self._reader_task = asyncio.create_task(self._reader_loop())

        try:
            await self._authenticate()
            console.ok("已连接 VTubeStudio 并认证成功")
            return True
        except Exception as e:
            console.error(f"VTS 认证失败：{e}")
            return False

    async def _reader_loop(self) -> None:
        """持续接收 VTS 消息，按 requestID 分发给等待的 future。

        无匹配 requestID 且以 `Event` 结尾的消息是事件推送（官方 Event API），
        分发给对应事件名注册的回调（回调在独立 task 中执行，不阻塞 reader）。
        """
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                rid = msg.get("requestID")
                if rid and rid in self._pending:
                    fut = self._pending.pop(rid)
                    if not fut.done():
                        fut.set_result(msg)
                    continue
                event = msg.get("messageType", "")
                if event.endswith("Event"):
                    for handler in list(self._event_handlers.get(event, ())):
                        asyncio.get_running_loop().create_task(
                            self._run_handler(handler, msg))
        except asyncio.CancelledError:
            pass
        except Exception:
            # 连接断开等，唤醒所有等待者
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_result(None)
            self._pending.clear()

    @staticmethod
    async def _run_handler(handler: Callable, msg: dict) -> None:
        """执行事件回调（容错：单个回调异常不影响其他回调）。"""
        try:
            result = handler(msg)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            console.error(f"VTS 事件回调异常：{e}")

    def on_event(self, event_name: str, handler: Callable) -> None:
        """注册事件回调（官方 Event API，事件名如 ModelLoadedEvent）。"""
        self._event_handlers.setdefault(event_name, []).append(handler)

    async def subscribe_event(self, event_name: str) -> bool:
        """订阅 VTS 事件。成功后 VTS 会持续推送该事件消息。"""
        if not self.authenticated:
            return False
        resp = await self._request(
            "EventSubscriptionRequest",
            {"eventName": event_name, "subscribe": True},
            timeout=10.0,
        )
        return bool(resp and resp.get("messageType") == "EventSubscriptionResponse")

    async def _authenticate(self) -> None:
        """处理 token 流程：有 token 直接认证；否则申请 token（弹窗）后认证。"""
        token = self._load_token()
        if token:
            resp = await self._request(
                "AuthenticationRequest",
                {
                    "pluginName": self.cfg.VTS_PLUGIN_NAME,
                    "pluginDeveloper": self.cfg.VTS_PLUGIN_DEVELOPER,
                    "authenticationToken": token,
                },
            )
            if resp and resp.get("messageType") == "AuthenticationResponse":
                if resp.get("data", {}).get("authenticated"):
                    self.authenticated = True
                    return
                # token 无效或已过期，清除并重新申请
                console.dim(f"token 已失效：{resp.get('data', {}).get('reason', '未知')}")
                self._clear_token()

        console.info("等待在 VTubeStudio 中授权插件（弹窗点 Allow）...")
        resp = await self._request(
            "AuthenticationTokenRequest",
            {
                "pluginName": self.cfg.VTS_PLUGIN_NAME,
                "pluginDeveloper": self.cfg.VTS_PLUGIN_DEVELOPER,
                "pluginIcon": "",
            },
            timeout=120.0,  # 给用户足够时间在 VTS 里点 Allow
        )
        if not resp or resp.get("messageType") != "AuthenticationTokenResponse":
            raise RuntimeError(f"获取 token 失败：{resp}")
        token = resp["data"]["authenticationToken"]
        self._save_token(token)

        resp = await self._request(
            "AuthenticationRequest",
            {
                "pluginName": self.cfg.VTS_PLUGIN_NAME,
                "pluginDeveloper": self.cfg.VTS_PLUGIN_DEVELOPER,
                "authenticationToken": token,
            },
        )
        if not resp or resp.get("messageType") != "AuthenticationResponse":
            raise RuntimeError(f"认证失败：{resp}")
        if not resp.get("data", {}).get("authenticated"):
            raise RuntimeError(f"认证失败：{resp.get('data', {}).get('reason', '未知原因')}")
        self.authenticated = True

    # ---------- 请求 / 响应 ----------

    async def _send(self, message_type: str,
                    data: Optional[dict] = None,
                    request_id: Optional[str] = None) -> None:
        """发送请求（fire-and-forget）。

        连接已关闭（VTS 断开/插件异常）时静默跳过——由 ensure_connected
        负责重连，避免高频注入循环对死连接反复尝试刷屏。
        """
        ws = self.ws
        if ws is None or getattr(ws, "closed", False):
            return
        payload: Dict[str, object] = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": request_id or message_type,
            "messageType": message_type,
        }
        if data is not None:
            payload["data"] = data
        raw = json.dumps(payload, ensure_ascii=False)
        try:
            await ws.send(raw)
        except Exception as e:
            # 连接失效：标记断开，交由 ensure_connected 自动重连
            self.authenticated = False
            console.dim(f"VTS 连接已断开（{type(e).__name__}），将自动重连...")

    def _ws_open(self) -> bool:
        """当前 WebSocket 是否处于可发送状态。"""
        ws = self.ws
        return ws is not None and not getattr(ws, "closed", False) and self.authenticated

    async def ensure_connected(self) -> bool:
        """确保连接可用；已断开则自动重连并重新认证。返回是否可用。

        认证 token 已持久化，重连无需再次弹窗授权。
        """
        if self._ws_open():
            return True
        # 清理旧连接残留
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        self._reader_task = None
        self.ws = None
        self.authenticated = False
        try:
            self.ws = await websockets.connect(self.uri)
            self._reader_task = asyncio.create_task(self._reader_loop())
            await self._authenticate()
            console.ok("已重新连接 VTubeStudio")
            return True
        except Exception as e:
            self.ws = None
            self.authenticated = False
            console.dim(f"VTS 重连失败（{type(e).__name__}），稍后重试")
            return False

    async def _request(self, message_type: str,
                       data: Optional[dict] = None,
                       timeout: float = 30.0) -> Optional[dict]:
        """发送并等待匹配 requestID 的响应。"""
        if not self.ws:
            return None
        request_id = str(uuid.uuid4())[:16]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        await self._send(message_type, data, request_id)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            console.warn(f"等待 {message_type} 响应超时")
            return None

    # ---------- 高层接口 ----------

    async def inject_mouth(self, value: float) -> None:
        """注入嘴型参数实现口型同步。fire-and-forget。

        注入参数与增益可在 .env 配置（MOUTH_PARAMETER / MOUTH_GAIN），
        默认由启动时模型扫描自动探测并写入 config.cfg。
        默认 VoiceVolumePlusMouthOpen——hiyori 等模型在 VTS「参数映射」里
        把嘴部连到这个语音参数；MouthOpen 是摄像头追踪参数，未必被映射。

        用 mode="set" 覆盖该参数（官方文档：API set 会覆盖追踪值，
        需至少每秒发送一次，否则参数丢失回到默认/追踪值）。
        """
        if not self.authenticated:
            return
        param_id = self.cfg.MOUTH_PARAMETER
        if not param_id:
            # 未经模型扫描/未配置时跳过，避免注入空参数名导致口型完全无效
            global _mouth_param_warned
            if not _mouth_param_warned:
                console.warn("口型参数未配置（MOUTH_PARAMETER 为空），跳过口型注入")
                console.dim("请通过 main.py 启动（会自动扫描模型定位嘴部参数），"
                            "或在 .env 中手动设置 MOUTH_PARAMETER")
                _mouth_param_warned = True
            return
        gain = self.cfg.MOUTH_GAIN
        final_value = float(max(0.0, min(1.0, value * gain)))
        await self._send(
            "InjectParameterDataRequest",
            {
                "faceFound": False,
                "mode": "set",
                "parameterValues": [{"id": param_id, "value": final_value}],
            },
            request_id="mouth",
        )

    async def trigger_hotkey(self, hotkey_id: str,
                             priority: str = "High") -> None:
        """触发热键（动作 / 表情 / 状态切换）。fire-and-forget。

        priority：触发动画的播放优先级（Auto/Low/Normal/High）。
        VTS 中正在播放的动画只能被「同级或更高优先级」打断，同级触发会
        排队等当前动画播完——表现为「点击预览不立即生效」。默认 High
        让新动画立即打断当前动画（非动画热键忽略该字段）。
        """
        if not self.authenticated or not hotkey_id:
            return
        data: dict = {"hotkeyID": hotkey_id}
        if priority in ("Auto", "Low", "Normal", "High"):
            data["triggeringAnimationPriority"] = priority
        await self._send(
            "HotkeyTriggerRequest",
            data,
            request_id=f"hotkey_{hotkey_id}",
        )

    async def trigger_hotkey_by_name(self, name: str) -> bool:
        """按名称查找并触发热键。返回是否找到并触发成功。"""
        hotkeys = await self.get_hotkeys()
        for h in hotkeys:
            if h.get("name", "").strip() == name.strip():
                await self.trigger_hotkey(h["id"])
                return True
        return False

    async def trigger_motion(self, motion_file: str) -> bool:
        """按 .motion3.json 文件名查找并触发热键动画。

        遍历当前模型热键，匹配 ``file`` 字段（如 "待机动作.motion3.json"），
        找到后立即触发对应的动画热键。返回是否找到并触发成功。
        """
        hotkeys = await self.get_hotkeys()
        # 先精确匹配文件名
        for h in hotkeys:
            if h.get("file", "").strip() == motion_file.strip():
                console.ok(f"找到动画热键「{h['name']}」→ 触发 {motion_file}")
                await self.trigger_hotkey(h["id"])
                return True
        # 没有精确匹配时尝试部分匹配文件名
        for h in hotkeys:
            if motion_file.strip() in h.get("file", ""):
                console.ok(f"找到动画热键「{h['name']}」→ 触发 {h['file']}")
                await self.trigger_hotkey(h["id"])
                return True
        console.warn(f"未找到关联「{motion_file}」的热键，请确认 VTS 中已配置该动画热键")
        return False

    async def inject_parameters(self, params: Dict[str, float]) -> None:
        """通用参数注入（程序化动作/口型/眨眼）。fire-and-forget。

        mode="set"：API 值覆盖追踪值；需至少每秒重发，否则参数丢失回默认。

        性能：全部参数合并为**单个请求**注入。VTS 官方协议支持
        parameterValues 数组一次注入多参数；拆分逐个发送会使注入频率随
        动效参数增多而线性暴涨（15 个动效参数 × 30fps ≈ 570 req/s），
        超出 VTS 插件承受能力导致其异常关闭连接（1002 protocol error）。
        """
        if not self.authenticated or not params:
            return
        await self._send(
            "InjectParameterDataRequest",
            {
                "faceFound": False,
                "mode": "set",
                "parameterValues": [
                    {"id": pid, "value": float(max(-1000000.0, min(1000000.0, v)))}
                    for pid, v in params.items()
                ],
            },
            request_id="inject",
        )

    async def get_folder_info(self) -> dict:
        """获取 VTS 内部文件夹名（VTSFolderInfoRequest，官方协议）。

        Returns:
            {models, backgrounds, items, config, logs, backup} ——
            均为相对 StreamingAssets 的文件夹名（如 models="Live2DModels"）。
            失败或无认证时返回空 dict。
        """
        if not self.authenticated:
            return {}
        resp = await self._request("VTSFolderInfoRequest", timeout=10.0)
        return (resp or {}).get("data", {}) or {}

    async def get_current_model(self) -> dict:
        """获取当前模型信息：{modelLoaded, modelName, modelID, ...}。"""
        if not self.authenticated:
            return {}
        resp = await self._request("CurrentModelRequest",
                                   timeout=10.0)
        return (resp or {}).get("data", {}) or {}

    async def get_input_parameters(self) -> tuple:
        """获取 VTS 输入参数列表（tracking 参数 + 插件自定义参数）。

        Returns:
            (default_params: list[dict], custom_params: list[dict])
            每个 dict 含 name / min / max / defaultValue 等字段。
        """
        if not self.authenticated:
            return [], []
        resp = await self._request("InputParameterListRequest",
                                   timeout=15.0)
        d = (resp or {}).get("data", {}) or {}
        return d.get("defaultParameters", []), d.get("customParameters", [])

    async def get_output_parameters(self) -> list:
        """获取当前模型 Live2D 输出参数列表。

        Returns:
            [{name, value, min, max, defaultValue, ...}, ...]
            失败或无认证时返回空列表。
        """
        if not self.authenticated:
            return []
        resp = await self._request("Live2DParameterListRequest",
                                   timeout=15.0)
        return (resp or {}).get("data", {}).get("parameters", []) or []

    async def get_hotkeys(self) -> list:
        """获取当前模型可用热键列表，返回 [{name, id, type, file}, ...]。"""
        if not self.authenticated:
            return []
        resp = await self._request(
            "HotkeysInCurrentModelRequest",
            timeout=10.0,
        )
        if not resp or "data" not in resp:
            return []
        return [
            {"name": h.get("name", ""),
             "id": h.get("hotkeyID", ""),
             "type": h.get("type", -1),
             "file": h.get("file", "")}
            for h in resp["data"].get("availableHotkeys", [])
        ]

    async def get_expressions(self) -> list:
        """获取当前模型可用表情列表，返回 [{name, file, active}, ...]。"""
        if not self.authenticated:
            return []
        resp = await self._request("ExpressionStateRequest", {"details": False})
        if not resp or "data" not in resp:
            return []
        return [
            {"name": e.get("name", ""),
             "file": e.get("file", ""),
             "active": bool(e.get("active", False))}
            for e in resp["data"].get("expressions", [])
        ]

    async def get_available_models(self) -> list:
        """获取 VTS 中所有可用模型列表。
        
        Returns:
            [{modelName, modelID, vtsModelName, modelLoaded, vtsModelIconName}, ...]
        """
        if not self.authenticated:
            return []
        resp = await self._request("AvailableModelsRequest",
                                   timeout=10.0)
        if not resp or "data" not in resp:
            return []
        return resp["data"].get("availableModels", [])

    async def load_model(self, model_id: str) -> bool:
        """按 ID 加载模型。成功返回 True。"""
        if not self.authenticated or not model_id:
            return False
        resp = await self._request(
            "ModelLoadRequest",
            {"modelID": model_id},
            timeout=30.0,  # 加载可能耗时较长
        )
        return bool(resp and resp.get("messageType") == "ModelLoadResponse")

    async def activate_expression(self, expr_file: str, active: bool = True,
                                  fade_time: float = 0.25) -> None:
        """激活/停用表情。fire-and-forget。

        官方文档：fadeTime 0~2 秒，默认 0.25；停用时沿用淡入时长。
        推荐用热键激活表情，但支持直接激活（适用于插件控制）。
        """
        if not self.authenticated or not expr_file:
            return
        await self._send(
            "ExpressionActivationRequest",
            {
                "expressionFile": expr_file,
                "fadeTime": float(max(0.0, min(2.0, fade_time))),
                "active": bool(active),
            },
            request_id="expr",
        )

    # ---------- token 持久化 ----------

    def _load_token(self) -> Optional[str]:
        if not os.path.exists(self.cfg.TOKEN_FILE):
            return None
        try:
            with open(self.cfg.TOKEN_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("token")
        except Exception:
            return None

    def _save_token(self, token: str) -> None:
        try:
            path = self.cfg.TOKEN_FILE
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"token": token}, f)
        except Exception as e:
            console.warn(f"保存 token 失败：{e}")

    def _clear_token(self) -> None:
        try:
            if os.path.exists(self.cfg.TOKEN_FILE):
                os.remove(self.cfg.TOKEN_FILE)
        except Exception:
            pass

    # ---------- 关闭 ----------

    async def close(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.authenticated = False