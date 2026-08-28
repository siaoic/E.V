# 🎹 MCP Piano —— 让 AI 自己弹钢琴

给这个虚拟钢琴项目加上的 **MCP（Model Context Protocol）** 能力：AI 助手（Claude Desktop / Cursor / Cherry Studio / 豆包等任意支持 MCP 的客户端）通过调用工具，就能让浏览器里的虚拟钢琴**真的发出声音、琴键跟着点亮**地弹奏音符、和弦、旋律，甚至整首 MIDI 曲子。

## 架构

```
AI(MCP 客户端)  ──stdio──▶  mcp-piano/server.js  ──WebSocket(7788)──▶  浏览器虚拟钢琴
                              │                                          Piano.html
                              └─ 调度音符时机                                 └─ mcpBridge.js 驱动琴键
```

- **`server.js`**：MCP 服务器，暴露弹琴工具；同时内置 WebSocket 服务（默认端口 `7788`），作为通向浏览器的"指挥通道"。
- **`Piano/Scripts/js/mcpBridge.js`**：已注入 `Piano.html` 的浏览器桥接脚本，收到命令后调用钢琴引擎 `playSound()` 发声并点亮琴键。

## 快速开始

### 1. 安装依赖（只需一次）

```bash
cd mcp-piano
npm install
```

### 2. 打开钢琴页面

在浏览器打开 `Piano/Piano.html`（推荐用本机服务器或直接双击打开都行）。
页面右上角会出现 **「MCP 连接中…」→「MCP 已连接」** 的小徽标，表示桥接层已连上。

### 3. 用 AI 弹琴（MCP 客户端方式）

把你使用的 MCP 客户端指向本服务器，例如：

- **Claude Desktop**：把 `config/claude_desktop_config.json` 里的内容合并进
  `~/Library/Application Support/Claude/claude_desktop_config.json`（Windows 为 `%APPDATA%\Claude\claude_desktop_config.json`），把 `/绝对路径/mcp-piano/server.js` 改成你的实际绝对路径，重启客户端。
- **Cursor**：把 `config/cursor-mcp.json` 内容放到项目根目录 `.cursor/mcp.json`，同样改路径。
- **其他 MCP 客户端**：注册一个 stdio 型 MCP 服务器，命令为 `node`，参数为 `server.js` 的绝对路径。

然后就可以对 AI 说：

> "用钢琴弹一首《小星星》"
> "弹个 C 大三和弦"
> "播放 /Users/me/music/天空之城.mid"

### 4. 不想用 AI？先自己试听（Demo 模式）

不经过 MCP 客户端，直接让钢琴弹演示曲目：

```bash
cd mcp-piano
node demo.js twinkle    # 小星星（默认）
node demo.js ode        # 欢乐颂
node demo.js jingle     # 铃儿响叮当
```

## 暴露给 AI 的工具

| 工具 | 说明 |
|---|---|
| `get_status` | 查看浏览器是否已连接、端口、标题、音频状态 |
| `play_note(note, duration?, velocity?)` | 弹单个音符，如 `"C4"`、`"C#4"`、`"Db4"` 或 MIDI 编号 |
| `play_chord(notes[], duration?, velocity?)` | 同时弹和弦，如 `["C4","E4","G4"]` |
| `play_melody(melody, tempo?)` | 弹旋律：字符串 `"C4 E4 G4"` 或 `{note,beat,duration}` 数组 |
| `play_score(score)` | 弹多声部乐谱（左右手），`{tempo, tracks:[{notes:[...]}]}` |
| `play_midi_file(path)` | 读取并弹奏本地 `.mid` 文件 |
| `stop_all()` | 立即停止所有音符 |
| `set_volume(volume)` | 设主音量 0~200 |
| `set_sustain(on)` | 开关延音踏板 |

### 旋律字符串语法

- 空格分隔音符，每拍一个音：`"C4 E4 G4 C5"`
- `:n` 指定持续拍数：`"C4:2 E4:1"`（前一个音 2 拍）
- `@v` 指定力度：`"C4@0.8"`
- `R` 表示休止符：`"C4 R C4"`
- 支持升降号：`"C#4"` / `"Db4"` / `"Bb3"`
- 可选 `|` 作为小节分隔（会被忽略）

### 音符范围

A0（MIDI 21）～ C8（MIDI 108），超出范围会提示错误。

## 常见问题

- **页面显示「MCP 未连接」**：`server.js` 没在运行，或端口被占用（可设环境变量 `MCP_PIANO_PORT` 换端口，需同时改页面里的 `window.MCP_PIANO_WS_URL`）。
- **AI 弹了但没声音，琴键有亮**：浏览器自动播放策略限制。第一次弹琴前**点击一下页面任意位置**解锁音频即可（右上角也会出现提示）。
- **AI 弹的旋律没看到琴键**：桥接层会自动滚动钢琴到正在弹的八度；若不想自动滚动，可发送 `set_options {autoScroll:false}`。
- **如何换端口**：启动前 `export MCP_PIANO_PORT=8888`；并在打开页面前于浏览器控制台执行 `window.MCP_PIANO_WS_URL='ws://localhost:8888'`。

## 目录结构

```
mcp-piano/
├── server.js            # MCP 服务器入口（stdio + WebSocket）
├── demo.js              # 无需 AI 的演示脚本
├── src/
│   ├── bridge.js        # WebSocket 桥接层（服务端）
│   ├── scheduler.js     # 音符时序调度器
│   ├── melody.js        # 旋律字符串 / 乐谱解析
│   ├── midiPlayer.js    # .mid 文件解析
│   └── notes.js         # 音符名 <-> MIDI 编号
├── config/              # Claude / Cursor 等 MCP 客户端配置示例
└── test/test-client.js  # 桥接层连通性测试
```
