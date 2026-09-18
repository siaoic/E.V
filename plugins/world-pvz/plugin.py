"""植物大战僵尸世界插件（``maibot-live.world-pvz``）。

链路：mss 截屏 → 廉价帧差（``world_poll``，基座按 ``worlds.change_poll_seconds``
每 2 秒回呼一次）→ 有变化才跑 OpenCV 模板匹配 → 中文快照交基座注入。
AI 用 ``pvz_plant`` / ``pvz_collect`` / ``pvz_shovel`` 操作游戏：提交即返回，
worker 串行执行（pydirectinput），真实结果以 ``debounce`` 世界事件回传。

不做静默兜底的场景：模板缺失、找不到游戏窗口、窗口不在前台——
全部拒绝加载 / 回传失败事件并明确说明原因。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from maibot_sdk import API, Field as PluginField, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.components import ToolParameterInfo
from maibot_sdk.types import ToolParamType
from pydantic import Field

from .input import PvzInput, WindowNotFound
from .vision import Analysis, PvzVision, VisionError, missing_templates

# 主程序世界框架能力名（基座实现在 src/worlds/，插件通过能力通道调用）。
WORLD_REGISTER_CAPABILITY = "world.register"
WORLD_UNREGISTER_CAPABILITY = "world.unregister"
WORLD_EVENT_CAPABILITY = "world.event"

PHASE_ZH = {
    "main_menu": "主菜单",
    "seed_select": "选卡",
    "battle": "战斗",
    "level_complete": "结算",
    "unknown": "未知画面",
}

DEFAULT_ENV_PROMPT = (
    "你正在植物大战僵尸的游戏窗口前做直播解说与操作。画面阶段、阳光数、"
    "每行是否有僵尸、卡片冷却会作为世界状态注入；只有画面变化时状态才会刷新。"
    "你可以用 pvz_plant 种植、pvz_collect 收阳光、pvz_shovel 铲除植物，"
    "动作提交后真实结果会以世界事件回传；游戏窗口不在前台时动作会被拒绝。"
)


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #


class PluginSectionConfig(PluginConfigBase):
    """插件通用配置。"""

    enabled: bool = PluginField(default=True, description="是否启用插件")
    config_version: str = PluginField(default="0.1.0", description="配置版本，由宿主用于配置迁移")


class PvzConfig(PluginConfigBase):
    """窗口、识别与操作参数（区域与模板按基准分辨率标定）。"""

    window_title: str = PluginField(
        default="植物大战僵尸",
        description="游戏窗口标题（精确匹配）；找不到窗口插件会拒绝加载",
    )
    base_resolution: List[int] = PluginField(
        default_factory=lambda: [800, 600],
        description="模板与区域坐标的基准分辨率 [宽, 高]，需与 templates/ 截取时的窗口大小一致",
    )
    # 基准坐标系下的区域 [x, y, w, h]；草坪 5 行、卡片槽与阳光数字区
    lawn_rows: List[List[float]] = PluginField(
        default_factory=lambda: [
            [45, 90, 690, 78],
            [45, 168, 690, 78],
            [45, 246, 690, 78],
            [45, 324, 690, 78],
            [45, 402, 690, 78],
        ],
        description="草坪每行区域（基准坐标 [x, y, w, h]，自上而下 5 行），需实机标定",
    )
    card_slots: List[List[float]] = PluginField(
        default_factory=lambda: [
            [60 + i * 53, 8, 50, 70] for i in range(10)
        ],
        description="卡片槽区域（基准坐标 [x, y, w, h]，从左到右），需实机标定",
    )
    sun_region: List[float] = PluginField(
        default_factory=lambda: [10, 90, 40, 30],
        description="阳光数字显示区（基准坐标 [x, y, w, h]），需实机标定",
    )
    grid_origin: List[float] = PluginField(
        default_factory=lambda: [60, 100],
        description="草坪网格左上角格心（基准坐标 [x, y]），用于种植/铲除定位",
    )
    grid_cell: List[float] = PluginField(
        default_factory=lambda: [76, 78],
        description="草坪网格间距（基准坐标 [w, h]）",
    )
    diff_threshold: float = PluginField(
        default=2.5, ge=0.5, le=60.0, description="廉价帧差阈值（64x64 平均绝对差，0~255）"
    )
    row_threshold: float = PluginField(
        default=6.0, ge=0.5, le=60.0, description="判定某行有僵尸的差分强度阈值"
    )
    card_ready_brightness: float = PluginField(
        default=110.0, ge=0.0, le=255.0, description="卡片就绪的亮度阈值（冷却中偏暗），需实机标定"
    )
    action_settle_seconds: float = PluginField(
        default=0.4, ge=0.1, le=5.0, description="动作点击后等待画面响应的时长（秒）"
    )
    name: str = PluginField(default="pvz", description="世界机器名，全局唯一")
    display_name: str = PluginField(default="植物大战僵尸", description="世界显示名，出现在注入文本与事件前缀中")
    env_prompt: str = PluginField(
        default=DEFAULT_ENV_PROMPT, description="注入给模型的静态世界说明，只在世界集合变化时注入一次"
    )


class WorldPvzConfig(PluginConfigBase):
    """插件根配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    pvz: PvzConfig = Field(default_factory=PvzConfig)


# --------------------------------------------------------------------------- #
# 截屏（mss 按窗口客户区抓取）
# --------------------------------------------------------------------------- #


class WindowCapture:
    """按窗口标题截取客户区位图（线程安全，world_poll 与 worker 共用）。"""

    def __init__(self, window_title: str) -> None:
        self._window_title = window_title
        self._hwnd: Optional[int] = None
        self._lock = threading.Lock()

    def hwnd(self) -> int:
        """定位（或重新定位）游戏窗口。"""
        with self._lock:
            self._hwnd = find_window(self._window_title)
            return self._hwnd

    def grab(self) -> Tuple[int, "np.ndarray"]:
        """截取当前客户区，返回 (hwnd, BGR 图像)。

        Raises:
            VisionError: 窗口丢失或截屏失败时抛出。
        """
        import mss

        hwnd = self.hwnd()
        left, top, width, height = _client_rect(hwnd)
        with mss.mss() as sct:
            raw = sct.grab({"left": left, "top": top, "width": width, "height": height})
            image = np.asarray(raw)[:, :, :3][:, :, ::-1].copy()  # BGRA → BGR
        return hwnd, image


def _client_rect(hwnd: int) -> Tuple[int, int, int, int]:
    import win32gui

    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    origin_left, origin_top = win32gui.ClientToScreen(hwnd, (left, top))
    return origin_left, origin_top, right - left, bottom - top


# --------------------------------------------------------------------------- #
# 插件入口
# --------------------------------------------------------------------------- #


class WorldPvzPlugin(MaiBotPlugin):
    """植物大战僵尸世界插件入口。"""

    config_model = WorldPvzConfig

    def __init__(self) -> None:
        super().__init__()
        self._vision: Optional[PvzVision] = None
        self._capture: Optional[WindowCapture] = None
        self._input: Optional[PvzInput] = None
        self._registered_name = ""
        self._action_queue: Optional[asyncio.Queue] = None
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._action_counter = 0
        self._last_analysis: Optional[Analysis] = None
        self._window_lost = False

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self) -> None:
        """校验模板与窗口 → 注册世界（轮询型）→ 启动动作 worker。

        Raises:
            RuntimeError: 模板缺失、窗口找不到或依赖不可用时抛出，并给出
                可执行的补救说明——带着半套识别上线只会产出错误的直播行为。
        """

        settings = self.config.pvz
        templates_dir = Path(__file__).resolve().parent / "templates"
        missing = missing_templates(templates_dir)
        if missing:
            raise RuntimeError(
                f"PvZ 模板缺失 {len(missing)} 张：{'、'.join(missing)}。"
                f"请在游戏窗口为基准分辨率 {tuple(settings.base_resolution)} 时截取对应画面"
                f"保存到 {templates_dir}（截图方法见该目录 README.md），再重新加载插件"
            )

        base_resolution = (int(settings.base_resolution[0]), int(settings.base_resolution[1]))
        self._vision = PvzVision(
            templates_dir=templates_dir,
            base_resolution=base_resolution,
            regions={
                "sun": list(settings.sun_region),
                "lawn_rows": [list(row) for row in settings.lawn_rows],
                "card_slots": [list(slot) for slot in settings.card_slots],
            },
            diff_threshold=settings.diff_threshold,
            row_threshold=settings.row_threshold,
            card_ready_brightness=settings.card_ready_brightness,
            logger=self.ctx.logger,
        )
        self._vision.load_templates()

        self._capture = WindowCapture(settings.window_title)
        self._input = PvzInput(window_title=settings.window_title, base_resolution=base_resolution)
        try:
            hwnd = self._capture.hwnd()
        except WindowNotFound:
            raise
        self.ctx.logger.info(f"PvZ 游戏窗口已定位: hwnd={hwnd}")

        await self._register_to_core()
        self._action_queue = asyncio.Queue()
        self._worker_task = asyncio.create_task(self._action_worker())
        self.ctx.logger.info(
            f"植物大战僵尸世界已接入框架: window={settings.window_title!r} "
            f"base_resolution={base_resolution}"
        )

    async def on_unload(self) -> None:
        """停掉动作 worker 并注销世界。"""

        await self._stop_worker()
        await self._unregister_from_core()
        self.ctx.logger.info("植物大战僵尸世界已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置变更后整体重建（区域坐标与阈值都是构造期参数）。"""

        del config_data
        await self.on_unload()
        await self.on_load()
        self.ctx.logger.info(f"植物大战僵尸世界配置已更新（scope={scope} version={version}）")

    # ------------------------------------------------------------------ 框架约定 API

    @API(
        "world_poll",
        description="框架轮询用：先做廉价帧差，画面真变了才跑完整识别。",
        version="1",
        public=True,
    )
    async def handle_world_poll(self, **_kwargs: Any) -> Dict[str, Any]:
        """两级检测的第一级：静止画面零渲染成本。"""

        try:
            _, image = self._capture.grab()
        except VisionError as exc:
            self._window_lost = True
            return {"changed": True, "snapshot": f"游戏窗口异常：{exc}"}
        self._window_lost = False

        frame = self._vision.grab_window(image)
        changed, _score = self._vision.changed_quickly(frame)
        if not changed:
            return {"changed": False}

        self._last_analysis = self._vision.analyze(frame)
        return {"changed": True, "snapshot": self._render_snapshot()}

    @API(
        "world_observe",
        description="按需拉取 PvZ 画面的即时完整识别（强制重新截屏与识别）。",
        version="1",
        public=True,
    )
    async def handle_world_observe(self, **_kwargs: Any) -> Dict[str, Any]:
        """供 world_observe 工具按需拉取（绕过帧差门控）。"""

        try:
            _, image = self._capture.grab()
            frame = self._vision.grab_window(image)
            self._last_analysis = self._vision.analyze(frame)
        except VisionError as exc:
            return {"snapshot": f"游戏窗口异常：{exc}"}
        return {"snapshot": self._render_snapshot()}

    # ------------------------------------------------------------------ 动作工具

    @Tool(
        "pvz_plant",
        description=(
            "在植物大战僵尸里种一株植物：先点选卡片槽位，再点目标草坪格。"
            "提交即返回，真实结果稍后以世界事件回传；游戏窗口不在前台时会被拒绝。"
        ),
        parameters=[
            ToolParameterInfo(name="card_slot", param_type=ToolParamType.INTEGER,
                              description="卡片槽位号（从 1 开始，对应选卡顺序）"),
            ToolParameterInfo(name="row", param_type=ToolParamType.INTEGER,
                              description="草坪行号（从上往下 1~5）"),
            ToolParameterInfo(name="col", param_type=ToolParamType.INTEGER,
                              description="草坪列号（从左往右 1~9）"),
        ],
    )
    async def handle_pvz_plant(self, card_slot: int, row: int, col: int, **_kwargs: Any) -> Dict[str, Any]:
        return self._submit_action(
            "plant",
            {"card_slot": int(card_slot), "row": int(row), "col": int(col)},
            description=f"在第 {row} 行第 {col} 列种植卡片 {card_slot}",
        )

    @Tool(
        "pvz_collect",
        description="收集屏幕上可见的阳光（点击画面中的阳光），提交即返回，结果稍后以世界事件回传。",
        parameters=[],
    )
    async def handle_pvz_collect(self, **_kwargs: Any) -> Dict[str, Any]:
        return self._submit_action("collect", {}, description="收集阳光")

    @Tool(
        "pvz_shovel",
        description="铲除一株植物（先点铲子再点草坪格），提交即返回，结果稍后以世界事件回传。",
        parameters=[
            ToolParameterInfo(name="row", param_type=ToolParamType.INTEGER, description="草坪行号（1~5）"),
            ToolParameterInfo(name="col", param_type=ToolParamType.INTEGER, description="草坪列号（1~9）"),
        ],
    )
    async def handle_pvz_shovel(self, row: int, col: int, **_kwargs: Any) -> Dict[str, Any]:
        return self._submit_action(
            "shovel",
            {"row": int(row), "col": int(col)},
            description=f"铲除第 {row} 行第 {col} 列的植物",
        )

    # ------------------------------------------------------------------ 动作执行

    def _submit_action(self, verb: str, arguments: Dict[str, Any], *, description: str) -> Dict[str, Any]:
        """动作入队并立刻返回（提交即返回语义）。"""

        queue = self._action_queue
        if queue is None:
            return {"success": False, "message": "错误：动作队列尚未就绪，请稍后重试"}
        self._action_counter += 1
        action_id = f"pvz-{self._action_counter}"
        queue.put_nowait({"id": action_id, "verb": verb, "arguments": arguments})
        return {"success": True, "message": f"已提交：{description}（id={action_id}），执行结果稍后以世界事件回传"}

    async def _action_worker(self) -> None:
        """串行执行动作队列；每条结果（含失败）都以世界事件回传。"""

        queue = self._action_queue
        if queue is None:
            return
        while True:
            action = await queue.get()
            try:
                text = await self._execute_action(action)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 失败必须回传事件
                text = f"动作执行失败：{exc}"
                self.ctx.logger.exception(f"动作执行失败: {action!r} error={exc!r}")
            self._report_event(event_type="task_done", text=text)

    async def _execute_action(self, action: Dict[str, Any]) -> str:
        """在工作线程里执行点击动作（pydirectinput 是阻塞调用）。"""

        verb = action["verb"]
        loop = asyncio.get_running_loop()
        if verb == "plant":
            arguments = action["arguments"]
            message = await loop.run_in_executor(None, self._plant_sync, arguments)
        elif verb == "shovel":
            arguments = action["arguments"]
            message = await loop.run_in_executor(None, self._shovel_sync, arguments)
        elif verb == "collect":
            message = await loop.run_in_executor(None, self._collect_sync)
        else:
            raise RuntimeError(f"未知动作 {verb}")
        return f"动作执行成功：{message}"

    def _grid_center(self, row: int, col: int) -> Tuple[float, float]:
        """草坪格号 → 基准坐标格心。"""
        settings = self.config.pvz
        origin_x, origin_y = settings.grid_origin
        cell_w, cell_h = settings.grid_cell
        return origin_x + (col - 1) * cell_w, origin_y + (row - 1) * cell_h

    def _card_slot_center(self, slot: int) -> Tuple[float, float]:
        settings = self.config.pvz
        slots = settings.card_slots
        if not 1 <= slot <= len(slots):
            raise RuntimeError(f"卡片槽位号 {slot} 超出范围（1~{len(slots)}）")
        x, y, w, h = slots[slot - 1]
        return x + w / 2, y + h / 2

    def _shovel_center(self) -> Tuple[float, float]:
        """铲子固定在卡片槽左侧的牌子位置（基准坐标，标定项）。"""
        settings = self.config.pvz
        x, y, _, _ = settings.card_slots[0] if settings.card_slots else [30, 40, 0, 0]
        return max(x - 30, 5), y + 20

    def _plant_sync(self, arguments: Dict[str, Any]) -> str:
        row, col, slot = int(arguments["row"]), int(arguments["col"]), int(arguments["card_slot"])
        if not 1 <= row <= 5:
            raise RuntimeError(f"行号 {row} 超出范围（1~5）")
        if not 1 <= col <= 9:
            raise RuntimeError(f"列号 {col} 超出范围（1~9）")
        card_x, card_y = self._card_slot_center(slot)
        cell_x, cell_y = self._grid_center(row, col)
        self._input.click_base(card_x, card_y)
        time.sleep(self.config.pvz.action_settle_seconds)
        self._input.click_base(cell_x, cell_y)
        time.sleep(self.config.pvz.action_settle_seconds)
        return f"已在第 {row} 行第 {col} 列种植卡片 {slot}"

    def _shovel_sync(self, arguments: Dict[str, Any]) -> str:
        row, col = int(arguments["row"]), int(arguments["col"])
        if not 1 <= row <= 5 or not 1 <= col <= 9:
            raise RuntimeError(f"格号超出范围：行 {row}（1~5）列 {col}（1~9）")
        shovel_x, shovel_y = self._shovel_center()
        cell_x, cell_y = self._grid_center(row, col)
        self._input.click_base(shovel_x, shovel_y)
        time.sleep(self.config.pvz.action_settle_seconds)
        self._input.click_base(cell_x, cell_y)
        time.sleep(self.config.pvz.action_settle_seconds)
        return f"已铲除第 {row} 行第 {col} 列的植物"

    def _collect_sync(self) -> str:
        """扫描草坪区域的亮黄阳光色块并逐个点击。"""
        import cv2

        hwnd, image = self._capture.grab()
        del hwnd
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        # 阳光是高饱和亮黄色块
        mask = cv2.inRange(hsv, (20, 120, 180), (40, 255, 255))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        height, width = image.shape[:2]
        base_w = float(self.config.pvz.base_resolution[0])
        base_h = float(self.config.pvz.base_resolution[1])
        scale_x, scale_y = base_w / max(width, 1), base_h / max(height, 1)
        collected = 0
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
            if cv2.contourArea(contour) < 300:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            self._input.click_base((x + w / 2) * scale_x, (y + h / 2) * scale_y)
            collected += 1
            time.sleep(0.1)
        if not collected:
            return "画面上没有找到可收集的阳光"
        return f"收集了 {collected} 个阳光"

    # ------------------------------------------------------------------ 内部实现

    def _render_snapshot(self) -> str:
        """把识别结果渲染成中文快照（实施计划 W2 的状态文本格式）。"""

        analysis = self._last_analysis
        if self._window_lost or analysis is None:
            return "游戏窗口异常或尚未完成一次识别。"
        phase = PHASE_ZH.get(analysis.phase, analysis.phase)
        if analysis.phase != "battle":
            return f"现在是{phase}画面。"
        parts = [f"现在是{phase}阶段"]
        if analysis.sun is not None:
            parts.append(f"阳光 {analysis.sun}")
        if analysis.zombie_rows:
            rows_text = "、".join(f"第 {row} 行" for row in analysis.zombie_rows)
            parts.append(f"{rows_text}有僵尸")
        else:
            parts.append("草坪上还没有僵尸")
        if analysis.ready_cards:
            cards_text = "、".join(str(card) for card in analysis.ready_cards)
            parts.append(f"卡片 {cards_text} 就绪")
        return "，".join(parts) + "。"

    def _report_event(self, *, event_type: str, text: str) -> None:
        """上报一条世界事件；失败记日志（worker 链路里不能反噬）。"""
        name = self._registered_name
        if not name:
            return

        async def runner():
            try:
                response = await self.ctx.call_capability(
                    WORLD_EVENT_CAPABILITY,
                    world_name=name,
                    event_type=event_type,
                    text=text,
                    trigger="debounce",
                )
                if not isinstance(response, dict) or not response.get("success"):
                    error = response.get("error") if isinstance(response, dict) else response
                    raise RuntimeError(f"上报世界事件失败：{error}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.exception(f"上报世界事件失败: {exc!r}")

        asyncio.get_running_loop().create_task(runner())

    async def _register_to_core(self) -> None:
        """把本世界注册进主程序的世界框架（轮询型）。"""

        settings = self.config.pvz
        name = settings.name.strip()
        if not name:
            raise RuntimeError("植物大战僵尸世界配置缺少 pvz.name")

        response = await self.ctx.call_capability(
            WORLD_REGISTER_CAPABILITY,
            name=name,
            display_name=settings.display_name,
            env_prompt=settings.env_prompt,
            # 画面状态由基座按 change_poll_seconds 轮询 world_poll 取走
            polls_changes=True,
        )
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else response
            raise RuntimeError(f"向世界框架注册植物大战僵尸世界失败：{error}")

        self._registered_name = name

    async def _unregister_from_core(self) -> None:
        """把本世界从世界框架注销；未注册时直接返回。"""

        registered = self._registered_name
        if not registered:
            return
        self._registered_name = ""

        response = await self.ctx.call_capability(WORLD_UNREGISTER_CAPABILITY, name=registered)
        if not isinstance(response, dict) or not response.get("success"):
            error = response.get("error") if isinstance(response, dict) else response
            raise RuntimeError(f"从世界框架注销植物大战僵尸世界失败：world={registered} error={error}")

    async def _stop_worker(self) -> None:
        """取消动作 worker 任务。"""

        task = self._worker_task
        self._worker_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_plugin() -> WorldPvzPlugin:
    """插件工厂函数。"""

    return WorldPvzPlugin()
