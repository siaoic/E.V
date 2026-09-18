"""AI 画画插件冒烟测试：覆盖 SVG 抽取/规范化、模板渲染、落盘与插件契约。

运行： python _smoke_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = PLUGIN_DIR.parents[1]  # 直播目录

# 导入项目 logger 会在 CWD 下创建 logs/，切到项目根避免污染插件目录
os.chdir(PROJECT_DIR)
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "src"))

from plugin import (  # noqa: E402
    DrawingStore,
    SvgValidationError,
    ViewerRenderer,
    _extract_svg,
    _normalize_svg,
    create_plugin,
)
from plugin_runtime.runner.plugin_loader import PluginLoader  # noqa: E402
from plugin_runtime.runner.manifest_validator import ManifestValidator  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


SAMPLE = "<svg viewBox='0 0 400 400'><path d='M 10 10 L 100 100'/><path d='M 20 20 C 30 30 40 40 50 50'/></svg>"

print("== 1. SVG 抽取 ==")
check("剥离 markdown 围栏", "```" not in _extract_svg("```svg\n" + SAMPLE + "\n```"))
mixed = "好的，我来画：\n" + SAMPLE + "\n希望你喜欢"
check("剥离前后说明文字", _extract_svg(mixed).startswith("<svg") and _extract_svg(mixed).endswith("</svg>"))
try:
    _extract_svg("hello world")
    check("抽不到 <svg> 抛错", False, "没抛")
except SvgValidationError:
    check("抽不到 <svg> 抛错", True)

print("\n== 2. SVG 规范化 ==")
svg, info = _normalize_svg(_extract_svg(mixed))
check("补 xmlns", 'xmlns="http://www.w3.org/2000/svg"' in svg)
check("补 fill=none", 'fill="none"' in svg)
check("viewBox 提取", info.get("viewBox") == "0 0 400 400", info)
check("只补一次 xmlns", svg.count("xmlns=") == 1, svg[:150])
check("只补一次 fill", svg.count('fill="none"') == 1, svg[:150])

try:
    _normalize_svg("<svg viewBox='0 0 100 100'><rect/></svg>")
    check("无 path 抛错", False, "没抛")
except SvgValidationError:
    check("无 path 抛错", True)

print("\n== 3. 模板渲染 ==")
renderer = ViewerRenderer(PLUGIN_DIR / "renderer" / "drawing_viewer.html")
html = renderer.render(SAMPLE, 2.0, '一只"猫" & <狗>')
check("SVG 已注入", SAMPLE in html)
check("占位符已清空", "__SVG__" not in html and "__DRAWING_SPEED__" not in html and "__PROMPT_HINT__" not in html)
check("速度已注入", "var defaultSpeed = 2.0;" in html)
check("提示文字已转义", "&quot;猫&quot;" in html and "&lt;狗&gt;" in html)
check("引擎内联", "function DrawingEngine" in html and "function parseSegments" in html)
check("无外部依赖", "<script src=" not in html and "<link rel=" not in html)
check("无 fetch 残留", "fetch(" not in html)
check("含 AI 画笔", 'data-pen' in html and "onTool" in html)
check("含 AI 橡皮", 'data-eraser' in html and "ai-erase-mask-" in html and "ensureEraseMask" in html)
check("橡皮不暴露给用户", "eraserBtn" not in html and "pointerdown" not in html)
check("无播放器式控件", 'id="progress"' not in html and 'id="speed"' not in html and "playBtn" not in html)

print("\n== 4. 落盘 ==")
tmp_dir = PLUGIN_DIR / "_tmp_store_test"
if tmp_dir.exists():
    shutil.rmtree(tmp_dir)
store = DrawingStore(tmp_dir)
page = store.save(html, "测试小猫")
check("文件已写入", page.is_file(), str(page))
check("路径为绝对路径", page.is_absolute())
check("内容是完整 HTML", page.read_text(encoding="utf-8").startswith("<!doctype html>"))
shutil.rmtree(tmp_dir)

print("\n== 5. 配置模型与 config.toml 一致性 ==")
cfg = tomllib.loads((PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
inst = create_plugin()
default_cfg = inst.get_default_config()
check("config.toml 顶层键齐全", set(cfg.keys()) == set(default_cfg.keys()), f"{set(cfg)} vs {set(default_cfg)}")
for section in default_cfg:
    check(f"[{section}] 键一致", set(cfg[section].keys()) == set(default_cfg[section].keys()),
          f"{set(cfg[section])} vs {set(default_cfg[section])}")
check("plugin.config_version 存在（SDK 强制要求）", bool(cfg.get("plugin", {}).get("config_version")))
try:
    inst.set_plugin_config(cfg)
    check("set_plugin_config 可正常注入", True)
except Exception as exc:
    check("set_plugin_config 可正常注入", False, f"{type(exc).__name__}: {exc}")

print("\n== 6. Manifest 与插件契约 ==")
validator = ManifestValidator(log_errors=False, log_compat_warnings=False)
manifest = validator.load_from_plugin_path(PLUGIN_DIR)
check("manifest 校验通过", manifest is not None, "; ".join(validator.errors))
if manifest:
    check("capabilities 与实际用法匹配",
          set(manifest.capabilities) == {"llm.generate", "send.text"}, str(manifest.capabilities))
    check("无未使用能力", "send.hybrid" not in manifest.capabilities and "config.get" not in manifest.capabilities)

try:
    PluginLoader._validate_sdk_plugin_contract(manifest.id if manifest else "ai-drawing", inst)
    check("SDK 插件契约通过", True)
except Exception as exc:
    check("SDK 插件契约通过", False, f"{type(exc).__name__}: {exc}")

print("\n== 7. Action 已注册 ==")
components = inst.get_components()
actions = [c for c in components if c.get("metadata", {}).get("legacy_component_type") == "ACTION"]
check("注册了 1 个 Action", len(actions) == 1, str([c.get("name") for c in components]))
if actions:
    meta = actions[0]["metadata"]
    check("Action 名为 ai_drawing", actions[0].get("name") == "ai_drawing", str(actions[0].get("name")))
    check("Action 参数已声明", set(meta.get("action_parameters", {})) == {"user_request", "style_hint"})
    check("ActivationType 为 always", meta.get("activation_type") == "always", str(meta.get("activation_type")))
    check("有触发说明", bool(meta.get("action_require")))

print(f"\n=== 总计: {PASS} pass / {FAIL} fail ===")
sys.exit(0 if FAIL == 0 else 1)