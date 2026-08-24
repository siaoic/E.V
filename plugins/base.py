"""插件基类与事件对象：所有钩子方法可选，插件只需要实现用到的。

对标 live-2d 的 plugin_sdk.py（钩子命名、事件语义一致），区别是本项目
插件为 Python 同进程 async 运行时：钩子直接以协程被主进程调用，
事件对象直接可读写（无需子进程 JSON-RPC 的 action 列表）。
"""

# 钩子名白名单（3.11）：PluginContext.register_hook 只接受名单内的钩子名，
# 防止插件注册任意回调名导致的事件分发混乱
VALID_HOOKS = frozenset({
    # 原有 10 个
    "on_init", "on_start", "on_stop", "on_destroy",
    "on_user_input", "on_llm_request", "on_llm_response",
    "on_tts_text", "on_tts_start", "on_tts_end",
    # 新增 7 个
    "on_slot_activate",
    "on_session_start", "on_session_end",
    "on_danmaku",
    "on_emotion_decide", "on_proactive_decide",
    "on_config_reload",
})


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

    # ---- 新增钩子 ----
    async def on_slot_activate(self, event) -> None:
        """Slot 切换：旧实现卸载、新实现装载。"""

    async def on_session_start(self) -> None:
        """会话开始：用户建立会话上下文。"""

    async def on_session_end(self) -> None:
        """会话结束：清理会话级资源。"""

    async def on_danmaku(self, event) -> None:
        """弹幕到达：跨平台统一格式。"""

    async def on_emotion_decide(self, event) -> None:
        """情绪分类决策：可修改 decided 覆盖默认结果。"""

    async def on_proactive_decide(self, event) -> None:
        """主动对话决策：插件可干预主动发起的触发。"""

    async def on_config_reload(self, new_config) -> None:
        """配置热更新：接收新配置字典。"""

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


class SlotActivateEvent:
    """Slot 切换事件：插件可用于释放旧资源/建立新连接。"""

    def __init__(self, slot_name: str, old_impl, new_impl) -> None:
        self.slot_name = slot_name    # SlotName.value（字符串）
        self.old_impl = old_impl      # 旧实现实例（可能 None）
        self.new_impl = new_impl      # 新实现实例


class DanmakuEvent:
    """弹幕到达事件：统一格式（跨任何弹幕源一致）。"""

    def __init__(self, item: dict) -> None:
        # item 字段约定: user_name, user_id, content, source(平台名), room_id, timestamp
        self.item = item
        # 便捷属性
        self.user_name = item.get("user_name", "")
        self.content = item.get("content", "")
        self.source = item.get("source", "")


class EmotionDecideEvent:
    """情绪分类决策事件：插件可覆盖默认分类器或干预 decided 结果。"""

    def __init__(self, text: str, emotion_candidates: list[tuple[str, float]], decided: str | None = None) -> None:
        self.text = text
        self.emotion_candidates = emotion_candidates  # [(name, score)] 有序
        self.decided = decided   # 钩子可修改: plugin 钩子赋值 decided 后终止分类
