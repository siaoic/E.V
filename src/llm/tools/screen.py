"""屏幕视觉工具：截图 + 交给 ButlerAgent 描述画面（look_at_screen）。

LLM 判断需要"看"画面时调用本工具：截取当前屏幕 → 压缩转 base64 →
把图片交给 agent.describe_image（视觉模型，默认智谱 glm-4v-flash）描述 →
返回画面描述文本给 LLM 继续对话。

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
    "这是一张电脑屏幕截图。请用简洁自然的中文描述画面内容，"
    "供 AI 虚拟主播用来回应观众。描述要具体：屏幕上有什么内容、"
    "是否有正在进行的活动、可见的文字或窗口、整体氛围。"
    "如果画面大部分是软件界面，简述主要窗口与状态即可。"
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

    # 图片直接交给 agent 处理（视觉模型调用统一在 agent.py 实现）
    from src.llm.agent import ButlerAgent
    text = await ButlerAgent().describe_image(img_b64, prompt=_VISION_PROMPT)
    return text or "错误：视觉模型未返回描述。"
