"""插件基类与事件对象：所有钩子方法可选，插件只需要实现用到的。

对标 live-2d 的 plugin_sdk.py（钩子命名、事件语义一致），区别是本项目
插件为 Python 同进程 async 运行时：钩子直接以协程被主进程调用，
事件对象直接可读写（无需子进程 JSON-RPC 的 action 列表）。
"""


class Plugin:
    """插件基类：生命周期 / 消息钩子 / 工具钩子，全部可选。"""

    def __init__(self) -> None:
        self.context = None     # PluginContext（加载时由管理器注入）
        self.metadata = None    # metadata.json 内容

    # ---- 生命周期 ----
    async def on_init(self) -> None:
        """插件加载时：读配置、初始化变量。"""

    async def on_start(self) -> None:
        """应用就绪后：启动定时器、连接服务。"""

    async def on_stop(self) -> None:
        """应用关闭前 / 卸载时：清理定时器、保存数据。"""

    async def on_destroy(self) -> None:
        """卸载时：释放资源。"""

    # ---- 消息钩子 ----
    async def on_user_input(self, event) -> None:
        """用户消息发给 AI 之前：注入上下文 / 过滤词 / 改写 / 拦截。"""

    async def on_llm_request(self, request) -> None:
        """即将调用 LLM 时：可修改 request.messages 数组。"""

    async def on_llm_response(self, response) -> None:
        """AI 回复之后、TTS 播放完成前：记录回复、触发副作用。"""

    async def on_tts_text(self, text: str) -> str:
        """TTS 处理文本时：翻译 / 替换词（只影响语音，字幕展示原文）。"""
        return text

    async def on_tts_start(self, text: str) -> None:
        """语音开始播放。"""

    async def on_tts_end(self) -> None:
        """语音播放结束。"""

    # ---- 工具 ----
    def get_tools(self) -> list:
        """返回 OpenAI function calling 格式的工具定义列表。"""
        return []

    async def execute_tool(self, name: str, params: dict) -> str:
        """执行本插件提供的工具；未实现的工具直接抛错（由调用方兜底）。"""
        raise NotImplementedError(f"插件未实现工具：{name}")


class UserInputEvent:
    """onUserInput 钩子事件：插件可注入背景上下文 / 改写文本 / 拦截下发。

    source：'text'（键盘输入）| 'voice'（语音识别）| 'barrage'（弹幕）。
    """

    def __init__(self, text: str, source: str) -> None:
        self.text = text            # 用户输入（插件可改写）
        self.source = source
        self.contexts: list = []    # 注入的背景信息（用户不可见，仅本轮进系统提示）
        self.prevented = False      # 阻止消息发给 AI（插件自己处理）
        self.stopped = False        # 停止后续插件的 onUserInput

    def add_context(self, text: str) -> None:
        """注入背景信息：拼进本轮系统提示，不向用户展示。"""
        self.contexts.append(text)

    def set_text(self, text: str) -> None:
        """改写用户消息内容。"""
        self.text = text

    def prevent_default(self) -> None:
        """阻止消息发给 AI，插件自行处理。"""
        self.prevented = True

    def stop_propagation(self) -> None:
        """阻止后续插件继续处理本条消息。"""
        self.stopped = True


class LLMRequestEvent:
    """onLLMRequest 钩子事件：request.messages 可直接修改（列表原地操作）。"""

    def __init__(self, messages: list) -> None:
        self.messages = messages


class LLMResponseEvent:
    """onLLMResponse 钩子事件：response.text 为 AI 完整回复。"""

    def __init__(self, text: str) -> None:
        self.text = text
