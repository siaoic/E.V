# -*- coding: utf-8 -*-
"""VTube Studio WebSocket 客户端（aiohttp 最小子集）。

协议照 ``cortico-world-vtuber-main/src/vts-client.ts`` 重写，只保留
W4 需要的部分：认证（token 申请/复用）、参数白名单、逐帧注入、
表情开关、保活重连。

硬约束（与 TS 版一致）：
- 连接后必须先 ``InputParameterListRequest`` 取实机参数白名单，注入前
  过滤掉不存在的参数——否则**整包**注入会被 ``APIError 453`` 拒绝；
- token 失效（认证被拒）时**停止自动重连**并明确报错，不反复弹授权窗；
- 授权弹窗等待放宽到 60 秒（要等人工在 VTS 里点「允许」）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import aiohttp

from . import state

# 重连退避序列（毫秒）；用尽后固定在最后一档
_RECONNECT_BACKOFF_MS = [500, 1000, 2000, 4000, 5000]
_AUTH_POPUP_TIMEOUT_SEC = 60.0
_REQUEST_TIMEOUT_SEC = 10.0
_AUTH_FAILURE_LIMIT = 3  # 连续认证超时熔断：VTS 大概率没开，别一直弹窗/重试

_API_NAME = "VTubeStudioPublicAPI"


class VtsClient:
    """一个到 VTube Studio API 的长期 WebSocket 连接。"""

    def __init__(
        self,
        *,
        ws_url: str,
        plugin_name: str,
        plugin_developer: str,
        auth_token: str = "",
        on_auth_token: Optional[Callable[[str], None]] = None,
        logger=None,
    ) -> None:
        """构建客户端；不立即连接，连接在 :meth:`start` 的后台任务里进行。

        Args:
            auth_token: 已保存的授权 token；为空时走一次授权弹窗流程。
            on_auth_token: 拿到新 token 后的持久化回调（写回配置）。
        """

        self._ws_url = ws_url
        self._plugin_name = plugin_name
        self._plugin_developer = plugin_developer
        self._auth_token = auth_token
        self._on_auth_token = on_auth_token
        self._logger = logger

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._request_id = 100
        self._pending: Dict[int, asyncio.Future] = {}
        self._recv_task: Optional[asyncio.Task[None]] = None

        self._parameter_whitelist: set[str] = set()
        self._available_expressions: List[str] = []
        self._connected = asyncio.Event()
        self._auth_rejected = asyncio.Event()
        self._auth_failure_count = 0
        self._stop_requested = False

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动连接循环；重复调用无副作用。"""
        if self._task is None or self._task.done():
            self._stop_requested = False
            self._auth_rejected.clear()
            self._task = asyncio.create_task(self._run_loop(), name="vts-client")

    async def stop(self) -> None:
        """停止连接循环并关闭 WebSocket。"""
        self._stop_requested = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._close_ws()

    @property
    def connected(self) -> bool:
        """VTS 是否已完成认证并可用。"""
        return self._connected.is_set()

    async def wait_connected(self, timeout: float) -> bool:
        """等待连接就绪；超时返回 False。"""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------------ 业务接口

    def parameter_available(self, name: str) -> bool:
        """参数是否在实机白名单内。"""
        return name in self._parameter_whitelist

    @property
    def available_expressions(self) -> List[str]:
        """当前模型可用的表情文件名列表（连接后填充）。"""
        return list(self._available_expressions)

    async def inject_parameters(self, mode: str, values: Sequence[Tuple[str, float]]) -> None:
        """注入一帧参数；白名单外的参数直接丢弃（整包会被 VTS 拒绝）。

        注入不等待应答（30Hz 节拍下不能逐帧等 RTT）；应答由接收循环
        自行消化。未连接时静默跳过——口型断流由状态页如实呈现。
        """
        ws = self._ws
        if ws is None or not self.connected or not values:
            return
        payload_values = [
            {"id": name, "value": float(value), "weight": 1.0}
            for name, value in values
            if name in self._parameter_whitelist
        ]
        if not payload_values:
            return
        await self._send(
            "InjectParameterDataRequest",
            {"faceFound": False, "mode": mode, "parameterValues": payload_values},
            wait_response=False,
        )

    async def set_expression(self, expression_file: str, active: bool, fade_time_sec: float = 0.25) -> None:
        """开关一个表情文件（``ExpressionActivationRequest``）。

        Raises:
            RuntimeError: 未连接或 VTS 返回错误时抛出。
        """
        await self.request(
            "ExpressionActivationRequest",
            {"expressionFile": expression_file, "active": bool(active), "fadeTime": fade_time_sec},
        )

    async def request(self, message_type: str, payload: Dict[str, Any], timeout: float = _REQUEST_TIMEOUT_SEC) -> Dict[str, Any]:
        """发送一条请求并等待应答数据。"""
        data = await self._send(message_type, payload, wait_response=True, timeout=timeout)
        return data

    # ------------------------------------------------------------------ 内部实现

    async def _run_loop(self) -> None:
        """连接 → 认证 → 白名单 → 在线，直到停止或 token 被拒。"""
        backoff_index = 0
        try:
            while not self._stop_requested:
                try:
                    await self._connect_once()
                    # 连接成功后从退避序列头开始
                    backoff_index = 0
                except _AuthRejected as exc:
                    # token 失效：按约束停止自动重连并明确报错，不反复弹窗
                    state.mark_error(str(exc))
                    self._log_error(f"{exc} 已停止自动重连。")
                    self._auth_rejected.set()
                    return
                except Exception as exc:  # noqa: BLE001 - 单次连接失败走退避重连
                    state.mark_connected(False)
                    state.mark_error(f"连接失败: {exc!r}")
                    delay_ms = _RECONNECT_BACKOFF_MS[min(backoff_index, len(_RECONNECT_BACKOFF_MS) - 1)]
                    backoff_index += 1
                    if backoff_index <= 1:
                        # 只在第一次失败时打完整错误，后续退避重试不再刷屏
                        self._log_error(f"VTS 连接失败: {exc!r}（每 {delay_ms}ms 退避重试）")
                    await asyncio.sleep(delay_ms / 1000.0)
        except asyncio.CancelledError:
            raise
        finally:
            await self._close_ws()

    async def _connect_once(self) -> None:
        """完成一次完整连接（握手 → 认证 → 白名单 → 就绪）。"""
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        try:
            self._ws = await self._session.ws_connect(self._ws_url)
        except Exception:
            await self._close_ws()
            raise

        try:
            hello = await self._recv_json()
            if hello.get("apiName") != _API_NAME:
                raise RuntimeError(f"对端不是 VTube Studio API: {hello!r}")

            await self._authenticate()
            self._parameter_whitelist = await self._fetch_parameter_whitelist()
            self._available_expressions = await self._fetch_expressions()
        except _AuthRejected:
            await self._close_ws()
            raise
        except Exception:
            await self._close_ws()
            raise

        self._connected.set()
        self._auth_failure_count = 0
        state.mark_connected(True)
        state.mark_error("")
        if self._logger is not None:
            self._logger.info(
                f"VTS 已连接: {self._ws_url} 白名单参数 {len(self._parameter_whitelist)} 个，"
                f"表情 {len(self._available_expressions)} 个"
            )
        self._recv_task = asyncio.create_task(self._recv_loop(), name="vts-client.recv")
        try:
            # 挂在接收循环上：断线时清理现场并返回（外层循环负责重连）
            await self._recv_task
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._recv_task = None
            self._connected.clear()
            state.mark_connected(False)
            await self._close_ws()
            self._fail_pending("连接已断开")

    async def _authenticate(self) -> None:
        """token 申请（需要人工在 VTS 弹窗允许）或复用已有 token 认证。"""

        if not self._auth_token:
            data = await self._send(
                "AuthenticationTokenRequest",
                {"pluginName": self._plugin_name, "pluginDeveloper": self._plugin_developer},
                wait_response=True,
                timeout=_AUTH_POPUP_TIMEOUT_SEC,
            )
            token = str(data.get("authenticationToken") or "")
            if not token:
                raise RuntimeError("VTS 未返回 authenticationToken（请在 VTS 弹窗中点「允许」）")
            self._auth_token = token
            if self._on_auth_token is not None:
                try:
                    self._on_auth_token(token)
                except Exception as exc:  # noqa: BLE001 - token 落盘失败不阻断认证
                    self._log_error(f"保存 VTS token 到配置失败（本次运行仍可用）: {exc!r}")

        auth = await self._send(
            "AuthenticationRequest",
            {
                "pluginName": self._plugin_name,
                "pluginDeveloper": self._plugin_developer,
                "authenticationToken": self._auth_token,
            },
            wait_response=True,
            timeout=_AUTH_POPUP_TIMEOUT_SEC,
        )
        if not auth.get("authenticated"):
            raise _AuthRejected(
                "VTS 认证失败：token 无效。请在 VTS 里清掉本插件授权后重新允许，"
                "或把 [vtuber].vts_auth_token 置空后重启"
            )

    async def _fetch_parameter_whitelist(self) -> set[str]:
        """取实机参数白名单（默认参数 + 自定义参数）。"""
        data = await self.request("InputParameterListRequest", {})
        names: set[str] = set()
        for group in ("defaultParameters", "customParameters"):
            for item in data.get(group) or []:
                name = item.get("name") if isinstance(item, dict) else None
                if name:
                    names.add(str(name))
        return names

    async def _fetch_expressions(self) -> List[str]:
        """取当前模型可用的表情文件列表（供表情配置校验）。"""
        data = await self.request("ExpressionStateRequest", {"details": False})
        files: List[str] = []
        for item in data.get("expressions") or []:
            file_name = item.get("file") if isinstance(item, dict) else None
            if file_name:
                files.append(str(file_name))
        return files

    async def _send(
        self,
        message_type: str,
        payload: Dict[str, Any],
        *,
        wait_response: bool,
        timeout: float = _REQUEST_TIMEOUT_SEC,
    ) -> Dict[str, Any]:
        """发出一条请求；``wait_response`` 时等待同 requestID 的应答。"""
        ws = self._ws
        if ws is None or ws.closed:
            raise RuntimeError("VTS 未连接")
        self._request_id += 1
        request_id = self._request_id
        message = {
            "apiName": _API_NAME,
            "apiVersion": "1.0",
            "requestID": str(request_id),
            "messageType": message_type,
            "data": payload,
        }

        future: Optional[asyncio.Future] = None
        if wait_response:
            future = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
        try:
            await ws.send_str(json.dumps(message, ensure_ascii=False))
            if not wait_response or future is None:
                return {}
            data = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            if wait_response:
                self._auth_failure_count = getattr(self, "_auth_failure_count", 0) + 1
                if self._auth_failure_count >= _AUTH_FAILURE_LIMIT:
                    self._auth_failure_count = 0
                    raise _AuthRejected(
                        f"VTS 请求连续 {self._auth_failure_limit} 次超时（{message_type}），熔断本次连接"
                    )
            raise
        except asyncio.CancelledError:
            raise
        finally:
            self._pending.pop(request_id, None)

        self._auth_failure_count = 0
        if isinstance(data.get("errorID"), int) and data.get("errorID"):
            raise RuntimeError(f"VTS APIError {data.get('errorID')}（{message_type}）: {data.get('data')}")
        return data.get("data") if isinstance(data.get("data"), dict) else {}

    async def _recv_json(self) -> Dict[str, Any]:
        """等待并解析一条 JSON 消息（仅用于握手期的第一条）。"""
        ws = self._ws
        if ws is None:
            raise RuntimeError("VTS 未连接")
        msg = await ws.receive()
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise RuntimeError(f"VTS 握手失败: {msg.type}")
        return json.loads(msg.data)

    async def _recv_loop(self) -> None:
        """接收循环：完成 pending future，忽略未知 requestID（如注入应答）。"""
        ws = self._ws
        if ws is None:
            return
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            request_id = data.get("requestID")
            if request_id is None:
                continue
            try:
                request_id_int = int(request_id)
            except (TypeError, ValueError):
                continue
            future = self._pending.get(request_id_int)
            if future is not None and not future.done():
                future.set_result(data)

    async def _close_ws(self) -> None:
        """关闭 WebSocket 与 HTTP 会话。"""
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            try:
                await ws.close()
            except Exception:
                pass
        session, self._session = self._session, None
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception:
                pass

    def _fail_pending(self, reason: str) -> None:
        """断线时让所有等待中的请求立刻失败。"""
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(RuntimeError(reason))
        self._pending.clear()

    def _log_error(self, message: str) -> None:
        if self._logger is not None:
            self._logger.error(message)


class _AuthRejected(RuntimeError):
    """VTS 认证被拒（token 失效）：停止自动重连。"""
