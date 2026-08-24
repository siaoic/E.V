# 插件制作指南

插件是一个放进 `plugins/` 的独立目录，**丢进去重启即用**（启动时自动登记进
`plugins/enabled_plugins.json`，无需改任何代码/配置）。

插件能做四件事，可同时做多件：

- **后台运行**：定时器、轮询外部数据、监听事件
- **拦截消息**：用户消息发给 AI 之前插手，AI 回复之后做点什么
- **注册工具**：给 AI 提供可调用的函数（Function Calling）
- **主动说话**：按条件让 AI 主动开口（走完整 TTS 输出管线）

---

## 目录结构

一个插件 = 一个目录，至少两个文件：

```
plugins/my-plugin/
├── index.py            # 插件代码（必需）
├── metadata.json       # 插件信息（必需）
└── plugin_config.json  # 插件自己的配置项（可选，运行期只读）
```

**metadata.json**

```json
{
  "name": "my-plugin",
  "displayName": "我的插件",
  "version": "1.0.0",
  "author": "你的名字",
  "description": "这个插件是干什么的",
  "main": "index.py"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | 插件唯一标识，也是目录名（缺省用目录名） |
| `main` | 否 | 入口文件，缺省 `index.py` |
| `displayName` | 否 | 界面/日志显示名 |
| `version` | 否 | 版本号 |
| `description` | 否 | 一句话描述（控制中心插件页展示） |

**最小可运行插件（index.py）**

```python
from plugins import Plugin


class MyPlugin(Plugin):
    async def on_start(self):
        self.context.log('ok', '我的插件启动啦')
```

这样就能用。下面看一个覆盖主要能力的完整范例。

---

## 完整范例

下面这个插件同时演示：后台定时器、拦截用户消息、注入系统提示、
注册 AI 工具、监听 TTS 生命周期。

```python
import asyncio

from plugins import Plugin, UserInputEvent


class ExamplePlugin(Plugin):
    """示例插件：一句话问候 + 每日定时播报 + 天气工具。"""

    # ---- 生命周期：加载 / 启动 / 停止 ----

    async def on_init(self):
        # 插件加载时：读配置、初始化变量
        self._greeting = self.context.get_plugin_config().get('greeting', '你好呀')
        self._task = None

    async def on_start(self):
        # 应用就绪后：启动后台定时器
        self._task = asyncio.create_task(self._hourly_tick())

    async def on_stop(self):
        # 应用退出前：取消定时器、保存数据
        if self._task is not None:
            self._task.cancel()

    async def _hourly_tick(self):
        try:
            while True:
                await asyncio.sleep(3600)
                await self.context.send_message('该休息一下啦')
        except asyncio.CancelledError:
            pass

    # ---- 消息钩子：用户说话时 / AI 回复后 ----

    async def on_user_input(self, event: UserInputEvent):
        # 偷偷往系统提示里加背景信息（用户不可见）
        event.add_context(f'（示例插件打招呼：{self._greeting}）')
        # 来源区分：event.source == 'text' | 'voice' | 'barrage'
        if event.source == 'barrage' and '午安' in event.text:
            # 拦截弹幕，插件自己处理，不让 AI 回复
            event.prevent_default()
            await self.context.send_message('午安！')
        # 改写用户消息：event.set_text('改写后')
        # 终止后续插件处理：event.stop_propagation()

    async def on_llm_response(self, response):
        # AI 完整回复之后、TTS 播放之前
        self.context.log('dim', f'AI 说：{response.text[:50]}...')

    async def on_tts_start(self, text):
        # 语音开始播放
        pass

    async def on_tts_end(self):
        # 语音播放结束
        pass

    # ---- 工具：给 AI 注册可调用函数 ----

    def get_tools(self):
        return [{
            'type': 'function',
            'function': {
                'name': 'get_weather',
                'description': '查询城市天气',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'city': {'type': 'string', 'description': '城市名'},
                    },
                    'required': ['city'],
                },
            },
        }]

    async def execute_tool(self, name, params):
        if name == 'get_weather':
            return f"{params.get('city')}：晴，25°C"
        raise NotImplementedError(f'未实现的工具：{name}')
```

---

## context 能做什么

`self.context` 是插件与应用之间的桥梁，完整能力如下：

```python
# ---- 日志 / 配置 ----
self.context.log('info', '普通消息')        # info / warn / error / ok / dim
self.context.get_config()                  # 整个 .env 配置
self.context.get_plugin_config()           # 插件自己的 plugin_config.json

# ---- 临时存储（进程内，重启清空）----
self.context.storage.set('key', value)
self.context.storage.get('key', default=None)
self.context.storage.delete('key')

# ---- 说话 / 对话 ----
await self.context.send_message('提示词')  # 让 AI 主动说一句（完整输出锁 + TTS）
await self.context.get_messages()          # 当前对话历史（OpenAI messages 格式）
await self.context.call_llm('帮我总结')    # 偷偷问 AI，不进对话历史、不产生回复

# ---- 系统提示注入 ----
self.context.add_system_prompt_patch('patch-id', '长期记住这件事')
self.context.remove_system_prompt_patch('patch-id')

# ---- 界面 ----
await self.context.show_subtitle('字幕内容', 3000)   # 显示字幕，3 秒后清除
await self.context.trigger_emotion('happy')          # 触发表情（VTS/桌宠）

# ---- 运行时注册（不依赖写死工具）----
self.context.register_tool({'type': 'function', 'function': {...}})
self.context.register_hook('on_tts_end', fn)         # 钩子名必须是白名单内的
self.context.register_memory_provider(provider)      # 自定义记忆后端（预留）

# ---- 插件间通信 ----
other = self.context.get_plugin('other-plugin')      # 拿另一个插件实例
self.context.on('my-event', handler)                 # 订阅事件总线
await self.context.emit('my-event', data)            # 发布事件
self.context.off('my-event', handler)                # 退订
```

---

## 钩子一览

所有钩子都是可选的，不用的不写。异步钩子直接 `async def`，主程序会 `await`。

| 钩子 | 触发时机 | 常见用途 |
|------|---------|---------|
| `on_init()` | 插件加载时 | 读配置、初始化变量 |
| `on_start()` | 应用就绪后 | 启动定时器、连接服务 |
| `on_stop()` | 应用关闭前 | 清理定时器、保存数据 |
| `on_destroy()` | 插件卸载时 | 释放资源 |
| `on_user_input(event)` | 用户消息发给 AI 之前 | 注入上下文、改写、拦截 |
| `on_llm_request(request)` | 即将调用 LLM | 修改 `request.messages` 数组 |
| `on_llm_response(response)` | AI 回复后、TTS 播放前 | 记录回复、触发副作用 |
| `on_tts_text(text) → str` | TTS 处理文本时 | 翻译、替换词（只影响语音，字幕不变） |
| `on_tts_start(text)` | 语音开始播放 | 同步动画、状态标记 |
| `on_tts_end()` | 语音播放结束 | 重置定时器、下一步操作 |

**事件对象**

- `UserInputEvent`：`text` / `source`（`'text'` | `'voice'` | `'barrage'`）/
  `contexts`；方法 `add_context()` / `set_text()` / `prevent_default()` / `stop_propagation()`
- `LLMRequestEvent`：`messages` 列表可直接原地修改
- `LLMResponseEvent`：`text` 为 AI 完整回复

---

## 进阶：register(ctx) 编程式注册

插件包内声明 `register(ctx)` 函数即可编程式注册钩子 / 工具 / 记忆 provider，
与上面「类方法声明」两种方式并存：

```python
def register(ctx):
    # 只注册钩子白名单内的钩子名，未知名直接抛错拒绝
    ctx.register_hook('on_tts_end', my_end_handler)
    ctx.register_tool({'type': 'function', 'function': {...}})
    ctx.register_memory_provider(my_provider)
```

`register` 在类实例化前执行，适合做不依赖实例状态的静态注册；
类方法适合需要实例状态（如定时器、缓存）的场景。

---

## 运行期管理

| 命令 | 作用 |
|------|------|
| `!plugins list` | 查看已加载插件 |
| `!plugins sync` | 重新扫描：登记新插件 / 卸载被禁用的（丢目录后运行中生效） |
| `!plugins enable <相对路径>` | 启用并热加载 |
| `!plugins disable <相对路径>` | 禁用并卸载（写入 `disabled` 清单，重启不复活） |
| `!plugins reload <插件名>` | 热重载 |

控制中心「插件」页也能开关插件并即时生效（走 `!plugins sync`）。

---

## 注意事项

- **丢目录即用**：新插件自动登记进启用清单；想关掉用
  `!plugins disable <路径>` 或在控制中心关闭，禁用项不会因自动登记重新启用
- 目录里没有 `metadata.json` 不会被当作插件（如 `plugins/builtin/tools/` 本地工具包、
  `plugins/third_party/mindcraft/` 等）
- 一个插件出错（加载失败、钩子抛异常）只影响它自己，主程序正常跑
- `metadata.json` / `index.py` 修改后执行 `!plugins reload <插件名>` 即时生效
