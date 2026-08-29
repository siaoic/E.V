"""look_at_screen 工具注册（L3-C）：截图并让视觉模型描述画面。"""

from plugins.builtin.tools.look_at_screen.screen import _look_at_screen


def register(ctx):
    """注册 look_at_screen：用户问「看到什么」或需了解屏幕内容时用。"""
    ctx.tools.register(
        name="look_at_screen",
        description="截取当前电脑屏幕画面并用视觉模型观察，返回画面内容描述。"
                    "当用户问「你在看什么/你看到什么/我屏幕上有什么/能看到我吗」，"
                    "或需要了解当前屏幕、直播画面内容才能回答时调用。"
                    "视觉模型通常需 10~30 秒返回，耐心等待；不要因超时占位而立刻重复调用。",
        parameters={},
        execute=lambda args: _look_at_screen(),
        timeout=60.0,  # 实测 VLM 远程调用 >10s：默认 10s 会掐死并返回占位（2026-08-29）
        enabled_by="TOOL_LOOK_SCREEN_ENABLED",
    )
