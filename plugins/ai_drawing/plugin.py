"""AI 画画插件

让 LLM 在对话中调用此插件生成 SVG，并输出一个自包含的 HTML 页面，
用描边动画引擎一笔一画地把这幅画绘制出来。

触发链路：
    1. LLM 自主判断需要画画 → 调用 Action `ai_drawing`
    2. 插件构造 prompt → 调用 ctx.llm.generate → 取回 SVG
    3. 校验 SVG（必须有 viewBox + 至少一个 path）
    4. 把 SVG 注入查看页模板，写成自包含 HTML 落到插件数据目录
    5. 用 ctx.send.text 发送"提示文字 + 本地文件路径"

为什么用本地 HTML 文件而不是链接：
    MaiBot 的 WebUI 只服务 ``dashboard/dist``，没有为插件提供 HTTP 静态
    资源出口，因此插件无法通过 URL 暴露自己的页面。
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Tuple

from pydantic import Field

from maibot_sdk import Action, Field as PluginField, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ActivationType


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #


class PluginSectionConfig(PluginConfigBase):
    """插件通用配置。"""

    enabled: bool = PluginField(default=True, description="是否启用插件")
    config_version: str = PluginField(default="1.0.0", description="配置版本，由宿主用于配置迁移")


class GenerationConfig(PluginConfigBase):
    """LLM 生成相关配置。"""

    task_name: str = PluginField(default="utils", description="宿主模型任务名，决定使用哪一组模型与超时策略")
    model: str = PluginField(default="", description="强制指定具体模型名，留空则按任务名选择模型")
    temperature: float = PluginField(default=0.6, ge=0.0, le=2.0, description="LLM 采样温度")
    llm_timeout_seconds: int = PluginField(default=60, ge=10, le=300, description="LLM 单次调用超时（秒）")


class MessageConfig(PluginConfigBase):
    """消息文案配置。"""

    action_intro: str = PluginField(default="🎨 让我来画给你看……", description="触发 Action 后发送的开场白")
    action_outro: str = PluginField(
        default="用浏览器打开上面的文件，就能看到一笔一画的绘制过程。",
        description="完成时附加在末尾的说明",
    )


class AnimationConfig(PluginConfigBase):
    """动画播放参数。"""

    animation_speed: float = PluginField(default=2.0, ge=0.25, le=16.0, description="查看页的默认播放倍率")


class AiDrawingConfig(PluginConfigBase):
    """插件根配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    message: MessageConfig = Field(default_factory=MessageConfig)
    animation: AnimationConfig = Field(default_factory=AnimationConfig)


# --------------------------------------------------------------------------- #
# SVG 校验与解析
# --------------------------------------------------------------------------- #


# 只接受一个根 <svg> 标签，避免 LLM 输出 Markdown 围栏或额外说明文字
_SVG_TAG_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)
_SVG_CLOSE_RE = re.compile(r"</svg\s*>", re.IGNORECASE)
_PATH_RE = re.compile(r"<path\b", re.IGNORECASE)
_VIEWBOX_RE = re.compile(r'viewBox\s*=\s*"([^"]+)"', re.IGNORECASE)
_VIEWBOX_SINGLE_QUOTE_RE = re.compile(r"viewBox\s*=\s*'([^']+)'", re.IGNORECASE)
_XMLNS_RE = re.compile(r"xmlns\s*=", re.IGNORECASE)
_FILL_ATTR_RE = re.compile(r"\bfill\s*=", re.IGNORECASE)

# 查看页模板中的占位符
_TEMPLATE_SVG_TOKEN = "__SVG__"
_TEMPLATE_SPEED_TOKEN = "__DRAWING_SPEED__"
_TEMPLATE_HINT_TOKEN = "__PROMPT_HINT__"


class SvgValidationError(ValueError):
    """SVG 校验失败。"""


def _extract_svg(raw: str) -> str:
    """从 LLM 输出中抽取首段 <svg>…</svg>，并剥离 markdown 围栏。"""

    if not raw:
        raise SvgValidationError("LLM 返回为空")

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)

    start = _SVG_TAG_RE.search(cleaned)
    end = _SVG_CLOSE_RE.search(cleaned)
    if not start or not end or end.start() <= start.start():
        raise SvgValidationError("未找到完整的 <svg>…</svg> 片段")

    return cleaned[start.start() : end.end()]


def _read_root_attrs(svg: str) -> str:
    """截取根 <svg ...> 标签内的属性区，用于判断 xmlns / fill 是否存在。"""

    tag_end = svg.find(">")
    return svg[:tag_end] if tag_end >= 0 else svg


def _normalize_svg(svg: str) -> Tuple[str, Dict[str, str]]:
    """轻量校验 + 规范化：补 xmlns / fill，抽取 viewBox。"""

    if not _PATH_RE.search(svg):
        raise SvgValidationError("SVG 内未发现任何 <path> 元素，无法绘制")

    root_attrs = _read_root_attrs(svg)

    # 注入命名空间（如果 LLM 漏了）
    if not _XMLNS_RE.search(root_attrs):
        svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)

    # 根节点强制 fill="none"：描边动画需要先只画线，最后才由引擎补填色
    if not _FILL_ATTR_RE.search(_read_root_attrs(svg)):
        svg = svg.replace("<svg", '<svg fill="none"', 1)

    viewbox_match = _VIEWBOX_RE.search(svg) or _VIEWBOX_SINGLE_QUOTE_RE.search(svg)
    viewbox = viewbox_match.group(1).strip() if viewbox_match else "0 0 400 400"

    return svg, {"viewBox": viewbox}


# --------------------------------------------------------------------------- #
# 渲染：把 SVG 注入查看页模板，写成自包含 HTML
# --------------------------------------------------------------------------- #


class ViewerRenderer:
    """把 SVG 注入模板，生成可直接用浏览器打开的自包含 HTML。"""

    def __init__(self, template_path: Path) -> None:
        self._template_path = template_path
        self._template: str | None = None

    def _load_template(self) -> str:
        """惰性读取模板，并校验占位符齐全。"""

        if self._template is not None:
            return self._template

        if not self._template_path.is_file():
            raise FileNotFoundError(f"查看页模板缺失: {self._template_path}")

        template = self._template_path.read_text(encoding="utf-8")
        for token in (_TEMPLATE_SVG_TOKEN, _TEMPLATE_SPEED_TOKEN, _TEMPLATE_HINT_TOKEN):
            if token not in template:
                raise ValueError(f"查看页模板缺少占位符 {token}")

        self._template = template
        return template

    def render(self, svg: str, speed: float, prompt_hint: str) -> str:
        """返回注入完成的完整 HTML 文本。"""

        template = self._load_template()
        # 提示文字会进入 HTML 属性与文本节点，需转义避免破坏结构
        safe_hint = (
            prompt_hint.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        )
        return (
            template.replace(_TEMPLATE_SVG_TOKEN, svg)
            .replace(_TEMPLATE_SPEED_TOKEN, repr(float(speed)))
            .replace(_TEMPLATE_HINT_TOKEN, safe_hint)
        )


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #


class DrawingStore:
    """把生成的查看页写入插件数据目录。"""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._base_dir.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def save(self, html: str, prompt_hint: str) -> Path:
        """写入 HTML，返回绝对路径。"""

        file_id = f"{int(time.time())}-{abs(hash(prompt_hint)) % 0xFFFFFF:06x}"
        target = self._base_dir / f"{file_id}.html"
        target.write_text(html, encoding="utf-8")
        return target.resolve()


# --------------------------------------------------------------------------- #
# 插件主类
# --------------------------------------------------------------------------- #


class AiDrawingPlugin(MaiBotPlugin):
    """AI 画画插件入口。"""

    config_model = AiDrawingConfig

    # Action 参数的元信息
    _ACTION_DESCRIPTION: ClassVar[str] = (
        "当用户想要看到一幅由代码一笔一画绘制出来的 SVG 插画时调用此 Action。"
        "适用于：'画一只小猫'、'帮我画朵花'、'用 SVG 画个爱心'、'画一张风景'等请求。"
        "不要用于纯文字描述或代码任务。"
    )
    _ACTION_REQUIREMENTS: ClassVar[List[str]] = [
        "用户明确希望看到一幅画、一只动物、一个图标等视觉内容时使用",
        "用户说'画'、'画一个'、'帮我画'、'画张图'时使用",
        "用户要求看到代码逐笔绘制过程时优先使用",
    ]
    _ACTION_ASSOCIATED_TYPES: ClassVar[List[str]] = ["text"]

    def __init__(self) -> None:
        super().__init__()
        self._renderer: ViewerRenderer | None = None
        self._store: DrawingStore | None = None
        self._system_prompt = ""

    # --------------------------------------------------------------------------- #
    # 生命周期
    # --------------------------------------------------------------------------- #

    async def on_load(self) -> None:
        plugin_root = Path(__file__).resolve().parent

        self._renderer = ViewerRenderer(plugin_root / "renderer" / "drawing_viewer.html")
        # 运行时产物写入 SDK 分配的插件数据目录，不污染插件自身目录
        self._store = DrawingStore(self.ctx.paths.data_dir / "drawings")

        prompt_path = plugin_root / "prompts" / "svg_drawing.txt"
        if not prompt_path.is_file():
            raise FileNotFoundError(f"prompt 模板缺失: {prompt_path}")
        self._system_prompt = prompt_path.read_text(encoding="utf-8")

        self.ctx.logger.info("AI 画画插件已加载，输出目录：%s", self._store.base_dir)

    async def on_unload(self) -> None:
        self._store = None
        self._renderer = None
        self.ctx.logger.info("AI 画画插件已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新。

        宿主在调用本方法前已完成配置注入，因此 ``self.config`` 已是最新值；
        prompt、动画速度等都在使用时实时读取，无需重建任何资源。
        """

        del config_data, version
        if scope == "self":
            self.ctx.logger.info(
                "AI 画画插件配置已更新：enabled=%s, 速度=%s×",
                self.config.plugin.enabled,
                self.config.animation.animation_speed,
            )

    # --------------------------------------------------------------------------- #
    # Action：ai_drawing
    # --------------------------------------------------------------------------- #

    @Action(
        "ai_drawing",
        description=_ACTION_DESCRIPTION,
        activation_type=ActivationType.ALWAYS,
        action_parameters={
            "user_request": "用户希望绘制的内容描述（中文）",
            "style_hint": "可选的风格提示，例如'简约'、'卡通'、'像素风'",
        },
        action_require=_ACTION_REQUIREMENTS,
        associated_types=_ACTION_ASSOCIATED_TYPES,
    )
    async def handle_ai_drawing(
        self,
        stream_id: str = "",
        user_request: str = "",
        style_hint: str = "",
        **_kwargs: Any,
    ) -> Tuple[bool, str]:
        """接收用户的画图请求 → 调用 LLM → 渲染自包含页面 → 发送文件路径。"""

        if not self.config.plugin.enabled:
            return False, "插件未启用"

        if not user_request:
            return False, "缺少画图请求内容"

        if not stream_id:
            return False, "stream_id 缺失"

        try:
            await self.ctx.send.text(
                f"{self.config.message.action_intro}\n> {user_request[:60]}",
                stream_id,
            )

            svg = await self._generate_svg(user_request, style_hint)
            clean_svg, info = _normalize_svg(svg)

            page_path = self._render_page(clean_svg, user_request)
            await self.ctx.send.text(self._build_message(page_path, user_request, info), stream_id)

            return True, f"已生成绘制页面：{page_path}"

        except SvgValidationError as exc:
            self.ctx.logger.warning("SVG 校验失败：%s", exc)
            await self.ctx.send.text(f"😢 这张画好像没画好：{exc}", stream_id)
            return False, f"SVG 校验失败：{exc}"

        except asyncio.TimeoutError:
            self.ctx.logger.error("LLM 调用超时")
            await self.ctx.send.text("⏱ 画图超时啦，稍后再试试～", stream_id)
            return False, "LLM 超时"

        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.exception("AI 画画 Action 异常")
            await self.ctx.send.text(f"💥 画画的时候出错了：{exc}", stream_id)
            return False, f"异常：{exc}"

    # --------------------------------------------------------------------------- #
    # 内部辅助
    # --------------------------------------------------------------------------- #

    async def _generate_svg(self, user_request: str, style_hint: str) -> str:
        """调用 LLM 生成 SVG。

        能力层返回的是统一 payload，正文在 ``response`` 字段；
        失败时直接抛错暴露给上层，不做静默兜底。
        """

        composed_prompt = self._system_prompt
        if style_hint:
            composed_prompt += f"\n\n风格要求：{style_hint}"
        composed_prompt += f"\n\n用户的画图请求：{user_request}"

        generation = self.config.generation
        result = await asyncio.wait_for(
            self.ctx.llm.generate(
                prompt=composed_prompt,
                task_name=generation.task_name,
                model=generation.model,
                temperature=generation.temperature,
            ),
            timeout=generation.llm_timeout_seconds,
        )

        if not result.get("success"):
            raise RuntimeError(f"LLM 调用失败：{result.get('error') or '未返回失败详情'}")

        return _extract_svg(str(result.get("response") or ""))

    def _render_page(self, svg: str, prompt_hint: str) -> Path:
        """把 SVG 注入模板并落盘，返回绝对路径。"""

        if self._renderer is None or self._store is None:
            raise RuntimeError("插件尚未完成 on_load 初始化")

        html = self._renderer.render(
            svg=svg,
            speed=self.config.animation.animation_speed,
            prompt_hint=prompt_hint,
        )
        return self._store.save(html, prompt_hint)

    def _build_message(self, page_path: Path, user_request: str, info: Dict[str, str]) -> str:
        """构造发送给用户的文本：简介 + 本地页面路径。"""

        return (
            f"✅ 画好啦～「{user_request[:30]}」\n"
            f"画布 {info.get('viewBox', '0 0 400 400')}\n"
            f"📄 {page_path}\n"
            f"{self.config.message.action_outro}"
        )


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #


def create_plugin() -> AiDrawingPlugin:
    return AiDrawingPlugin()