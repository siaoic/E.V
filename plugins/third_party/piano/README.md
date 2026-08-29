# 🎹 Virtual Piano – Version 2.0

A handmade, fully customizable virtual piano coded in HTML, CSS and JavaScript.  
This version introduces advanced audio features, soundfont support, MIDI file management, and fine control over playback behavior — all directly in your browser.

👉 The virtual piano is available online here: [https://maelangallais.github.io/Piano](https://maelangallais.github.io/Piano)

Or, you can download it directly from GitHub — which is where you are right now. Both options have pros and cons:
 - Even though both versions run in the same browser, they do not share the same memory. This means you won’t be able to access presets saved in one version from the other.
 - The main advantage of the local version is that it works offline, which can come in handy if you want to play while you’re without an internet connection.

---

## 🌟 What's New in Version 2.0

- ✅ Rewritten with the **Web Audio API** (no more `Audio()`), allowing:
  - 🎧 Reverb, Echo, Chorus
  - 🎚️ Volume & Pan control
  - 🎛️ ADSR envelope, Dynamic Release
  - ⚡ Distortion & Effects toggling
  - 🔔 Metronome feature
- 📥 Support for `.mid` and `.sf2` files (play, record, export)
- 🧠 SoundFont buffer storage in RAM or IndexedDB (with size limits)
- 🎼 Playback system that handles MIDI timing and velocity
- 🎛️ Custom keyboard mapping for all 88 keys (all explained)
- 📁 Reverb impulse loading via `.wav` import
- 🧩 Simple caching for last-used files (15 MIDI, 5 SF2, 5 WAV)
- 🧠 LocalStorage-based persistent state
---
## 🤖 AI 自己弹钢琴（MCP 扩展）

本项目已加入 **MCP（Model Context Protocol）** 能力：任意支持 MCP 的 AI 客户端（Claude Desktop / Cursor / Cherry Studio / 豆包等）可以通过工具调用，让浏览器里的虚拟钢琴**真的发声、琴键跟着点亮**地弹奏音符、和弦、旋律，甚至整首 `.mid` 文件。

快速开始：

```bash
# 1. 安装依赖（只需一次）
cd mcp-piano && npm install

# 2. 浏览器打开 Piano/Piano.html（右上角出现"MCP 已连接"）

# 3. 先不听 AI，直接试听演示
node mcp-piano/demo.js twinkle   # 小星星

# 4. 接入 AI：把 mcp-piano/server.js 注册为 MCP server（配置示例见 mcp-piano/config/）
```

对 AI 说一句"用钢琴弹《小星星》"即可。详细说明见 **`mcp-piano/README.md`**。

---

## 🚀 More ?

📖 Detailed documentation is available in the [Project Wiki](https://github.com/maelangallais/Piano/wiki).
