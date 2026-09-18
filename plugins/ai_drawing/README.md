# AI 画画插件

让 Bot 在对话中自主调用 LLM 生成 SVG，并输出一个**自包含的 HTML 页面**，
用描边动画一笔一画地把这幅画绘制出来。

## 效果

1. 用户：「帮我画一只小猫」
2. Bot 自动调用 Action `ai_drawing`，先用 LLM 生成一段合法 SVG
3. Bot 发回一条消息，含一个本地 HTML 文件路径
4. 用浏览器打开该文件，会从第一笔开始、用 `stroke-dasharray` + RAF 动画一笔一画地画出来
5. 动画结束时叠加层淡出，显示完整填色

## 触发方式

由 **Action** 触发（`ActivationType.ALWAYS`）。LLM 看到画图需求时自行决定是否调用，不需要用户输入命令。

适用：「画一只小猫」「帮我画朵花」「画一张风景」「画一个爱心」

不适用：纯文字描述、代码任务；用户只想要成品图而不关心过程

## 目录结构

```
ai_drawing/
├── _manifest.json              # 插件清单
├── config.toml                 # 插件配置
├── plugin.py                   # 插件主类
├── prompts/
│   └── svg_drawing.txt         # LLM prompt 模板
├── renderer/
│   └── drawing_viewer.html     # 查看页模板（含内联动画引擎）
├── _smoke_test.py              # 单元测试
└── _e2e_test.py                # 端到端测试（mock ctx）
```

运行时产物写到 SDK 分配的插件数据目录，**不在插件目录内**：

```
<项目根>/data/plugins/maibot-team.ai-drawing-plugin/drawings/<id>.html
```

## 安装

把整个 `ai_drawing/` 目录放到 MaiBot 实例的 `plugins/` 下，启动 Bot 后自动加载。

## 配置项（`config.toml`）

### `[plugin]`
| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否启用插件 |
| `config_version` | `1.0.0` | 配置版本，SDK 强制要求存在，不要手改 |

### `[generation]`
| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `model` | `""` | 生成 SVG 的模型；留空则读宿主 `model.default_text_model`，两者都空会直接报错 |
| `temperature` | `0.6` | LLM 采样温度 |
| `llm_timeout_seconds` | `60` | 单次 LLM 调用超时 |

### `[message]`
| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `action_intro` | ` 让我来画给你看……` | 触发后立即发送的开场白 |
| `action_outro` | `用浏览器打开上面的文件，就能看到一笔一画的绘制过程。` | 末尾说明 |

### `[animation]`
| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `animation_speed` | `2.0` | 作画节奏倍率，越大画得越快（页面内不提供倍速切换） |

## 查看页

产物是**单个自包含 HTML**：SVG、样式、动画引擎全部内联，不依赖任何 HTTP 服务或外部资源。
打开后**立即开画**，没有播放器式控件，只有「实时作画」这一件事：

- **AI 画笔**：一支小铅笔跟着笔尖走，贴住正在描的那一笔；画完自动抬笔淡出。
  笔的大小会按 `viewBox` 自动缩放，画布尺寸不同也不会显得过大或过小。
- **橡皮**（观众用）：点底部「🧽 橡皮」进入擦除模式，按住画布拖动即可擦掉画错的地方，
  旁边的滑块调橡皮粗细。擦除通过 SVG `mask` 实现，只影响显示，不改动 SVG 本身；
  作画过程中也可以随时擦。

> 为什么是本地文件而不是链接：MaiBot 的 WebUI 只服务 `dashboard/dist`，
> 没有为插件提供 HTTP 静态资源出口，插件无法通过 URL 暴露自己的页面。

## 与 LLM 配合的注意事项

`prompts/svg_drawing.txt` 里写死了几条硬性约束，改 prompt 时请保留：

1. **禁用 `<use>` / `<clipPath>` / `<mask>` / `<filter>` / `<text>` 等标签** —— 动画引擎靠 `getTotalLength()` 测量每条路径，这些标签要么测不到长度，要么根本不被遍历。
2. **路径命令只用 `M / L / H / V / C / Q / Z`** —— `A`（弧线）在部分渲染器下 `getTotalLength()` 不稳定，需要弧线时用 `C` 拟合。
3. **每条独立的 `<path>` 对应一笔** —— 建议 12~30 条，太少就没有逐笔感。
4. **不要在 path 上写死 `fill`** —— 描边阶段需要 `fill="none"`，填色由引擎在最后 22% 进度统一补上。

## 调试

跑测试：

```bash
cd plugins/ai_drawing
python _smoke_test.py   # 35 项：SVG 抽取/规范化、模板渲染、落盘、配置一致性、manifest、插件契约、Action 注册
python _e2e_test.py     # 端到端：mock ctx 跑完整 Action，产出真实 HTML 到 _e2e_out/
```

`_e2e_test.py` 跑完会打印产物路径，直接用浏览器打开就能看动画。

常见问题：

| 现象 | 原因 |
| --- | --- |
| 插件加载失败，日志提示 `必须实现 on_config_update()` | SDK 强制要求实现该生命周期方法 |
| `PluginConfigVersionError: 缺少 plugin.config_version` | `[plugin]` 段漏了 `config_version` |
| 消息只有开场白、没有结果 | LLM 返回的 SVG 不合法，看日志里的 `SVG 校验失败` |
| 打开页面一片空白 | SVG 缺 `viewBox`，或 LLM 把 stroke 写成了 `none` |
| 页面完全不动 | 控制台应有 `getTotalLength` 报错，检查 LLM 是否用了不支持的标签 |

## 限制

- 不做 SVG 安全清理，只用于渲染自家 LLM 的输出，**不要开放给用户上传 SVG**
- 生成的文件长期堆积在 `data/plugins/.../drawings/`，没有自动清理
- 一次只画一幅，不支持多 SVG 拼接的过场动画