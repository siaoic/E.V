"""端到端测试：mock ctx 跑完整 Action 流程，产出一个真实的绘制页面。

运行： python _e2e_test.py
产物： _e2e_out/ai_drawing_e2e.html
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any, List

PLUGIN_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = PLUGIN_DIR.parents[1]  # 直播目录

# 导入 maibot_sdk 会在 CWD 下创建 logs/，切到项目根避免污染插件目录
os.chdir(PROJECT_DIR)
sys.path.insert(0, str(PLUGIN_DIR))

from plugin import create_plugin  # noqa: E402

OUT_DIR = PLUGIN_DIR / "_e2e_out"

# LLM 会返回的画（带 markdown 围栏 + 前后说明，模拟真实脏输出）
LLM_RESPONSE = """好的，我来画一只小猫：

```svg
<svg viewBox="0 0 400 400" fill="none">
  <defs>
    <linearGradient id="fur" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#f5d6a8"/>
      <stop offset="100%" stop-color="#e0b077"/>
    </linearGradient>
  </defs>
  <g stroke="#2b2b2b" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
    <path d="M 130 210 L 110 130 L 175 175"/>
    <path d="M 270 210 L 290 130 L 225 175"/>
    <path d="M 130 220 Q 200 150 270 220 Q 200 320 130 220 Z" fill="url(#fur)"/>
    <path d="M 165 215 Q 175 205 185 215"/>
    <path d="M 215 215 Q 225 205 235 215"/>
    <path d="M 195 245 Q 200 250 205 245"/>
    <path d="M 175 270 Q 200 285 225 270"/>
  </g>
</svg>
```

希望你喜欢这只小猫～
"""


class StubLogger:
    def info(self, msg: str, *args: Any) -> None:
        print(f"    [INFO] {msg % args if args else msg}")

    def warning(self, msg: str, *args: Any) -> None:
        print(f"    [WARN] {msg % args if args else msg}")

    def error(self, msg: str, *args: Any) -> None:
        print(f"    [ERROR] {msg % args if args else msg}")

    def exception(self, msg: str, *args: Any) -> None:
        print(f"    [EXC] {msg % args if args else msg}")


class StubSend:
    def __init__(self) -> None:
        self.sent: List[str] = []

    async def text(self, text: str, stream_id: str, **kwargs: Any) -> bool:
        self.sent.append(text)
        print(f"    → 发送到 {stream_id}:")
        for line in text.splitlines():
            print(f"        {line}")
        return True


class StubConfig:
    async def get(self, key: str, default: Any = None) -> Any:
        raise AssertionError(f"插件不应读取宿主全局配置，却读了 {key}")


class StubLlm:
    """按宿主能力层的真实 payload 结构返回，而不是裸字符串。"""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: List[dict] = []

    async def generate(
        self,
        prompt: str = "",
        model: str = "",
        temperature: float | None = None,
        *,
        task_name: str = "",
        **kwargs: Any,
    ) -> dict:
        self.calls.append({"prompt": prompt, "model": model, "temperature": temperature, "task_name": task_name})
        print(f"    [LLM] task_name={task_name} model={model!r} temperature={temperature}")
        print(f"    [LLM] prompt 长度 = {len(prompt)} 字符")
        assert "用户的画图请求" in prompt, "prompt 未拼接用户请求"
        assert task_name, "task_name 为空会导致能力层不知道用哪个模型任务"
        if self.fail:
            return {"success": False, "response": "", "reasoning": "", "model_name": "", "error": "stub 上游 502"}
        return {
            "success": True,
            "response": LLM_RESPONSE,
            "reasoning": "",
            "model_name": "stub-model",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }


class StubPaths:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class StubCtx:
    def __init__(self, data_dir: Path, llm_fail: bool = False) -> None:
        self.logger = StubLogger()
        self.send = StubSend()
        self.config = StubConfig()
        self.llm = StubLlm(fail=llm_fail)
        self.paths = StubPaths(data_dir)


async def run_action(data_dir: Path, llm_fail: bool = False) -> tuple[Any, Any, Any]:
    """跑一遍完整 Action，返回 (插件实例, 上下文, Action 结果)。"""

    inst = create_plugin()
    ctx = StubCtx(data_dir, llm_fail=llm_fail)
    inst._set_context(ctx)  # noqa: SLF001 - 测试中直接注入 mock 上下文
    inst.set_plugin_config({})
    await inst.on_load()
    result = await inst.handle_ai_drawing(
        stream_id="bilibili_live:22625027",
        user_request="画一只小猫",
        style_hint="可爱、简约",
    )
    return inst, ctx, result


async def check_llm_failure() -> int:
    """LLM 失败时必须显式报错，且不产出任何页面。"""

    print("\n== LLM 失败路径 ==")
    fail_dir = OUT_DIR / "fail_case"
    inst, ctx, (ok, message) = await run_action(fail_dir, llm_fail=True)
    await inst.on_unload()

    checks = [
        ("Action 返回失败", not ok),
        ("失败原因透传上游错误", "stub 上游 502" in message, message),
        ("向用户发了报错消息", any("出错了" in text for text in ctx.send.sent)),
        ("未产出页面", not list(fail_dir.rglob("*.html"))),
    ]
    failed = 0
    for name, ok_flag, *detail in checks:
        print(f"    {'PASS' if ok_flag else 'FAIL'}  {name}  {detail[0] if detail else ''}")
        failed += 0 if ok_flag else 1
    return failed


async def main() -> int:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    print("== 调用 Action ai_drawing ==")
    inst, ctx, (ok, message) = await run_action(OUT_DIR)
    print(f"\n    Action 返回: ok={ok}, message={message}")

    print("\n== on_config_update ==")
    await inst.on_config_update("self", {"plugin": {"enabled": True}}, "1.0.0")

    print("\n== on_unload ==")
    await inst.on_unload()

    if not ok:
        print("\n!! Action 失败")
        return 1

    # 校验产物
    pages = list(OUT_DIR.glob("drawings/*.html"))
    if len(pages) != 1:
        print(f"\n!! 期望 1 个产物页面，实际 {len(pages)}")
        return 1

    page = pages[0]
    html = page.read_text(encoding="utf-8")
    print(f"\n== 产物校验 ==\n    路径: {page}\n    大小: {len(html)} 字节")

    checks = [
        ("是完整 HTML 文档", html.startswith("<!doctype html>")),
        ("含注入的 SVG", "<svg" in html and "viewBox=\"0 0 400 400\"" in html),
        ("SVG 带 xmlns", 'xmlns="http://www.w3.org/2000/svg"' in html),
        ("含描边引擎", "function parseSegments" in html and "DrawingEngine" in html),
        ("速度已注入", "var defaultSpeed = 2.0;" in html),
        ("提示文字已注入", "画一只小猫" in html),
        ("无未替换占位符", "__SVG__" not in html and "__DRAWING_SPEED__" not in html),
        ("无外部资源引用", "<script src=" not in html and "<link rel=" not in html),
    ]
    failed = 0
    for name, ok_flag in checks:
        print(f"    {'PASS' if ok_flag else 'FAIL'}  {name}")
        failed += 0 if ok_flag else 1

    failed += await check_llm_failure()

    print(f"\n    Action 共发送 {len(ctx.send.sent)} 条消息")
    print(f"\n=== 端到端结果: {'通过' if failed == 0 else f'{failed} 项失败'} ===")
    print(f"\n用浏览器打开查看动画效果：\n    {page}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))