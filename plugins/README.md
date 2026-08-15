# 插件开发指南

插件直接放在 `plugins/` 下一个目录一个插件，应用启动时自动加载。

插件能做三件事：
- **后台运行**：定时器、轮询外部数据、监听事件
- **拦截消息**：在用户消息发给 AI 之前插手，或者在 AI 回复之后做点什么
- **注册工具**：给 AI 提供可以调用的函数（Function Calling）

一个插件可以同时做这三件事。

---

## 五分钟上手

新建一个目录：

```
plugins/my-plugin/
├── index.py
├── metadata.json
└── plugin_config.json   # 可选，插件自己的配置项
```

**metadata.json**

```json
{
  "name": "my-plugin",
  "displayName": "我的插件",
  "version": "1.0.0",
  "author": "你的名字",
  "description": "做什么的",
  "main": "index.py"
}
```

**启用插件**，在 `plugins/enabled_plugins.json` 里加上插件路径：

```json
{
  "plugins": [
    "my-plugin"
  ]
}
```

**index.py**

```python
from plugins import Plugin

class MyPlugin(Plugin):
    async def on_start(self):
        self.context.log('info', '我启动了')
```

重启应用，终端出现 `✅ [插件] 已加载: 我的插件` 就成了。

---

## 三种玩法

### 1. 后台定时器

```python
import asyncio

class MyPlugin(Plugin):
    async def on_start(self):
        self._task = asyncio.create_task(self._loop())

    async def on_stop(self):
        self._task.cancel()

    async def _loop(self):
        try:
            while True:
                await asyncio.sleep(60)
                # 每分钟做一次
        except asyncio.CancelledError:
            pass
```

也可以让 AI 主动说一句话：

```python
async def _loop(self):
    try:
        while True:
            await asyncio.sleep(60)
            await self.context.send_message('提示词，让 AI 说点什么')
    except asyncio.CancelledError:
        pass
```

### 2. 拦截消息

消息在发给 AI 之前经过 `on_user_input`，AI 回复之后经过 `on_llm_response`：

```python
class MyPlugin(Plugin):
    async def on_user_input(self, event):
        # 给这次请求偷偷加点背景信息（用户看不到）
        event.add_context('（现在是下午3点，用户在工作）')

        # 修改用户说的话
        # event.set_text('改写后的消息')

        # 阻止消息发给 AI，插件自己处理
        # event.prevent_default()

    async def on_llm_response(self, response):
        # AI 刚说完话，response.text 是回复内容
        # 可以在这里记录日志、触发其他操作
        pass

    async def on_tts_end(self):
        # AI 说完话了（语音播放结束）
        pass
```

`event.source` 可以区分来源：`'text'`（文字）、`'voice'`（语音）、`'barrage'`（弹幕）

### 3. 给 AI 注册工具

AI 可以在对话中主动调用这些工具：

```python
class MyPlugin(Plugin):
    def get_tools(self):
        return [{
            'type': 'function',
            'function': {
                'name': 'get_weather',
                'description': '查询城市天气',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'city': {'type': 'string', 'description': '城市名'}
                    },
                    'required': ['city']
                }
            }
        }]

    async def execute_tool(self, name, params):
        if name == 'get_weather':
            return f"{params['city']}：晴，25°C"
```

---

## context 能做什么

`self.context` 是插件和应用之间的桥梁：

```python
# 打日志（显示在终端）
self.context.log('info', '消息')
self.context.log('warn', '警告')

# 读配置
self.context.get_config()           # 整个 .env 配置
self.context.get_plugin_config()    # 插件自己的 plugin_config.json

# 临时存数据（重启清空）
self.context.storage.set('key', value)
self.context.storage.get('key')

# 让 AI 主动说一句话（走完整输出锁 + TTS 流程）
await self.context.send_message('提示词')

# 往系统提示词里注入内容（每次 AI 请求都会带着，直到 remove）
self.context.add_system_prompt_patch('patch-id', '你记住这件事')
self.context.remove_system_prompt_patch('patch-id')

# 获取当前对话历史
messages = await self.context.get_messages()

# 插件自己偷偷问 AI（不进入对话历史）
result = await self.context.call_llm('帮我总结一下')

# UI 操作
await self.context.show_subtitle('在屏幕上显示字幕', 3000)  # 持续3秒
await self.context.trigger_emotion('happy')                  # 触发表情

# 运行时动态注册工具
self.context.register_tool({'name': 'my_tool', ...})

# 获取另一个插件的实例（插件间通信）
other = self.context.get_plugin('other-plugin-name')

# 事件总线（跨插件松耦合通信）
self.context.on('my-event', handler)
await self.context.emit('my-event', data)
self.context.off('my-event', handler)
```

---

## 完整钩子列表

| 钩子 | 什么时候触发 | 常见用途 |
|------|-------------|---------|
| `on_init()` | 插件加载时 | 读配置、初始化变量 |
| `on_start()` | 应用就绪后 | 启动定时器、连接服务 |
| `on_stop()` | 应用关闭前 | 清理定时器、保存数据 |
| `on_destroy()` | 插件卸载时 | 释放资源 |
| `on_user_input(event)` | 用户消息发给 AI 之前 | 注入上下文、过滤词、修改消息 |
| `on_llm_request(request)` | 即将调用 LLM 时 | 修改 request.messages 数组 |
| `on_llm_response(response)` | AI 回复之后、TTS 之前 | 记录回复、触发副作用 |
| `on_tts_text(text) → str` | TTS 处理文本时 | 翻译、替换词（只影响语音，字幕不变）|
| `on_tts_start(text)` | 语音开始播放 | 同步动画、状态标记 |
| `on_tts_end()` | 语音播放结束 | 重置定时器、下一步操作 |

所有方法都是可选的，不需要的不用写。异步方法直接 `async def` 即可，
主程序会 await 每个钩子。

---

## 运行期管理

控制台输入 `!plugins list` 查看已加载插件；`!plugins reload <name>` 热重载；
`!plugins enable/disable <相对路径>` 启停（同步写 enabled_plugins.json）。
控制中心「插件」页也能开关插件并即时生效。
