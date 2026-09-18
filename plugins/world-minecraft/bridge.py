"""mc-node 子进程桥：stdio NDJSON JSON-RPC 客户端 + 进程生命周期管理。

协议硬约束（Node 侧同款）：
- stdout 只允许出现一行一个 JSON 的协议帧；出现任何无法解析的行即视为
  协议违约——关闭连接并抛错，**不做容错解析**；
- 所有日志走 stderr，本模块逐行转投主日志。

选 stdio 而非端口：无鉴权、无防火墙弹窗；Node 进程 EOF 即退出，
生命周期天然跟随插件进程。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# 拉起 mc-node 时注入的环境变量（Node 侧只从环境读配置，不走协议）
_ENV_PREFIX = "MC_"


class MinecraftBridge:
    """一个 mc-node 子进程的完整生命周期。"""

    def __init__(
        self,
        *,
        node_dir: Path,
        node_executable: str,
        host: str,
        port: int,
        username: str,
        version: str,
        threat_radius: float,
        on_frame: Callable[[Dict[str, Any]], None],
        on_closed: Callable[[str], None],
        logger,
    ) -> None:
        """构建桥。

        Args:
            node_dir: ``node/`` 目录（含 ``index.js`` 与 ``package.json``）。
            node_executable: Node 可执行文件路径；空串用 PATH 上的 ``node``。
            version: MC 协议版本；空串表示自动协商。
            threat_radius: 敌对生物警戒半径（格），透传给 Node。
            on_frame: 收到一帧合法协议 JSON 时回调（事件循环线程）。
            on_closed: 进程退出时回调，参数为退出原因摘要。
            logger: 结构化日志器。
        """

        self._node_dir = node_dir
        self._node_executable = node_executable
        self._host = host
        self._port = port
        self._username = username
        self._version = version
        self._threat_radius = threat_radius
        self._on_frame = on_frame
        self._on_closed = on_closed
        self._logger = logger

        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader_tasks: list[asyncio.Task[None]] = []
        self._closed = False

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """预检环境并拉起 mc-node 子进程。

        Raises:
            RuntimeError: Node 不可用、依赖未安装或进程启动失败时抛出。
        """

        executable = self._resolve_node_executable()
        self._ensure_node_dependencies()
        await self._ensure_node_version(executable)

        env = os.environ.copy()
        env.update(
            {
                "MC_HOST": self._host,
                "MC_PORT": str(self._port),
                "MC_USERNAME": self._username,
                "MC_VERSION": self._version,
                "MC_THREAT_RADIUS": str(self._threat_radius),
            }
        )
        try:
            self._process = await asyncio.create_subprocess_exec(
                executable,
                "index.js",
                cwd=str(self._node_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise RuntimeError(f"启动 mc-node 子进程失败: {exc}") from exc

        self._closed = False
        self._reader_tasks = [
            asyncio.create_task(self._read_stdout(), name="mc-bridge.stdout"),
            asyncio.create_task(self._read_stderr(), name="mc-bridge.stderr"),
            asyncio.create_task(self._watch_exit(), name="mc-bridge.exit"),
        ]
        self._logger.info(f"mc-node 已启动: pid={self._process.pid} node={executable}")

    async def stop(self) -> None:
        """关闭 stdin 让 Node 优雅退出；超时后强制杀掉。"""

        process = self._process
        self._process = None
        self._closed = True
        if process is None:
            return

        if process.stdin is not None and not process.stdin.is_closing():
            with contextlib.suppress(Exception):
                await process.stdin.drain()
                process.stdin.close()

        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            self._logger.warning("mc-node 未在 5 秒内退出，强制终止")
            process.kill()
            with contextlib.suppress(Exception):
                await process.wait()

        for task in self._reader_tasks:
            task.cancel()
        self._reader_tasks = []

    @property
    def alive(self) -> bool:
        """子进程是否仍在运行。"""

        return self._process is not None and self._process.returncode is None

    async def send(self, frame: Dict[str, Any]) -> None:
        """向上行写一帧协议 JSON。

        Raises:
            RuntimeError: 进程已退出或写管道失败时抛出。
        """

        process = self._process
        if process is None or process.stdin is None or process.stdin.is_closing():
            raise RuntimeError("mc-node 未在运行，无法发送指令")
        line = json.dumps(frame, ensure_ascii=False) + "\n"
        try:
            process.stdin.write(line.encode("utf-8"))
            await process.stdin.drain()
        except Exception as exc:
            raise RuntimeError(f"向 mc-node 写指令失败: {exc}") from exc

    # ------------------------------------------------------------------ 内部实现

    def _resolve_node_executable(self) -> str:
        """解析 Node 可执行文件并确认存在。"""

        configured = self._node_executable.strip()
        executable = shutil.which(configured or "node")
        if not executable:
            raise RuntimeError(
                f"找不到 Node 可执行文件（{configured or 'node'}）。"
                "Minecraft 世界需要 Node ≥ 18，请安装 Node 或在插件配置 minecraft.node_executable 指定路径"
            )
        return executable

    def _ensure_node_dependencies(self) -> None:
        """确认 mc-node 的依赖已经安装。

        Raises:
            RuntimeError: ``node_modules/mineflayer`` 不存在时抛出——
                依赖缺失必须明确报错并给出安装命令，不能等 Node 崩溃再猜。
        """

        marker = self._node_dir / "node_modules" / "mineflayer" / "package.json"
        if not marker.exists():
            raise RuntimeError(
                f"mc-node 依赖未安装：缺少 {marker}。"
                f"请先执行: cd \"{self._node_dir}\" && npm install"
            )

    async def _ensure_node_version(self, executable: str) -> None:
        """确认 Node 版本 ≥ 18（mineflayer 4.x 的要求）。

        Raises:
            RuntimeError: 无法取得版本或版本过低时抛出。
        """

        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
        except OSError as exc:
            raise RuntimeError(f"执行 {executable} --version 失败: {exc}") from exc

        match = re.search(r"v(\d+)\.", stdout.decode("utf-8", errors="replace"))
        if not match:
            raise RuntimeError(f"无法解析 Node 版本输出: {stdout!r}")
        if int(match.group(1)) < 18:
            raise RuntimeError(
                f"Node 版本过低（{stdout.decode().strip()}），mc-node 需要 Node ≥ 18"
            )

    async def _read_stdout(self) -> None:
        """逐行读协议帧；解析失败视为协议违约，关闭连接。"""

        process = self._process
        if process is None or process.stdout is None:
            return
        while True:
            raw = await process.stdout.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as exc:
                # 协议流被污染（通常是 Node 侧日志误走 stdout）：立刻暴露，不写容错解析
                self._logger.error(f"mc-node 协议违约（stdout 出现非 JSON 行）: {line[:200]!r} error={exc}")
                await self._abort("stdout 协议违约")
                return
            if isinstance(frame, dict):
                self._on_frame(frame)

    async def _read_stderr(self) -> None:
        """逐行读 Node 日志并转投主日志。"""

        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            raw = await process.stderr.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self._logger.info(f"[mc-node] {line}")

    async def _watch_exit(self) -> None:
        """等待进程退出并通知宿主。"""

        process = self._process
        if process is None:
            return
        returncode = await process.wait()
        if self._closed:
            return
        self._closed = True
        self._on_closed(f"mc-node 进程退出（returncode={returncode}）")

    async def _abort(self, reason: str) -> None:
        """协议违约时立刻终止子进程。"""

        self._logger.error(f"正在终止 mc-node: {reason}")
        process = self._process
        if process is not None and process.returncode is None:
            process.kill()
