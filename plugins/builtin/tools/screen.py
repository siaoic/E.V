"""屏幕视觉工具：截图 + 交给 ButlerAgent 描述画面（look_at_screen）。

LLM 判断需要"看"画面时调用本工具：截取当前屏幕 → 压缩转 base64 →
把图片交给 agent.describe_image 描述（优先主模型，主模型不支持图片时
回退 BUTLER_MODEL）→ 返回画面描述文本给 LLM 继续对话。

设计：
- 本模块只负责「截图 + 压缩」，图片 → 视觉模型的调用统一在
  src/llm/agent.py 的 ButlerAgent.describe_image 完成（复用其客户端、
  超时、降级机制，不在此重复建 OpenAI client）
- PIL 懒加载：只在真正调用工具时才 import
"""

import asyncio
import base64
import io

# 视觉模型描述画面的提示词：让 AI 主播能自然地把画面内容接进对话
_VISION_PROMPT = (
    "描述我当前屏幕上的一切。"
    "告诉我画面里有什么：是什么软件、什么界面、什么内容，"
    "有没有弹窗、文字、代码、数字，或者任何重要的信息。"
    "我该怎么理解这个画面——它是不是游戏，或者统计后台，或者别的东西。"
    "说清楚就够，不需要多余的话。"
    "如果画面空旷或者无趣，也可以直接告诉我。"
)

# 截图压缩参数：控制请求体大小（多模态接口对图片尺寸/体积有限制）
_MAX_SIDE = 768     # 长边最大像素
_JPEG_QUALITY = 80  # JPEG 压缩质量


def _grab_screen_b64(max_side: int = _MAX_SIDE,
                     quality: int = _JPEG_QUALITY) -> str:
    """同步截屏 → 等比缩放 → JPEG base64（供 asyncio.to_thread 调用）。"""
    import PIL.Image
    import PIL.ImageGrab

    img = PIL.ImageGrab.grab()  # 全屏截图
    img = img.convert("RGB")
    width, height = img.size
    scale = min(1.0, max_side / max(width, height))
    if scale < 1.0:
        img = img.resize((int(width * scale), int(height * scale)),
                         PIL.Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


async def _look_at_screen() -> str:
    """截取当前屏幕，交给 ButlerAgent 描述画面，返回画面描述文本。"""
    try:
        img_b64 = await asyncio.to_thread(_grab_screen_b64)
    except Exception as e:
        return f"错误：截屏失败：{e}"

    # 图片直接交给管家处理（视觉模型调用统一在 butler_agent.py 实现）
    from ev.llm.butler_agent import ButlerAgent
    text = await ButlerAgent().describe_image(img_b64, prompt=_VISION_PROMPT)
    return text or "错误：视觉模型未返回描述。"
