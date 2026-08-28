// 桥接层：MCP 服务器 <-> 浏览器虚拟钢琴 的 WebSocket 通道
//
// 设计：
//  - 浏览器页面(Piano.html + mcpBridge.js)连上来后发 {type:"hello"}，被注册为"浏览器目标"。
//  - 其他 WS 客户端（MCP 工具 / 测试脚本）发来的命令(note_on/note_off/...) 会被转发给浏览器目标。
//  - 只有发送了 hello 的浏览器页面才会成为转发目标，避免测试脚本抢占。
//
// 注意：所有日志输出到 stderr，避免污染 MCP 的 stdio 协议通道。

import { WebSocketServer } from 'ws';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const DEFAULT_PORT = 7788;
const log = (...args) => process.stderr.write(args.join(' ') + '\n');

// Piano 静态页面根目录（../Piano/），用于 http://localhost:7788 直接打开页面
const WEB_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'Piano');

const MIME_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.ico': 'image/x-icon',
  '.mid': 'audio/midi',
  '.wav': 'audio/wav',
  '.sf2': 'application/octet-stream',
  '.SF2': 'application/octet-stream',
};

const COMMAND_TYPES = new Set([
  'note_on', 'note_off', 'stop_all', 'stop_play', 'set_volume', 'set_sustain', 'set_options', 'ping',
  'schedule_notes', 'schedule_play',
]);

export class Bridge {
  constructor(port = DEFAULT_PORT) {
    this.port = Number(process.env.MCP_PIANO_PORT) || port;
    this.wss = null;
    this.client = null;          // 当前浏览器桥接目标
    this.meta = null;            // 浏览器 hello 携带的信息
    this.connectedAt = null;
    this.lastMessageAt = null;
    this.lastLogs = [];
  }

  start() {
    return new Promise((resolve, reject) => {
      const httpServer = http.createServer((req, res) => this._serveHttp(req, res));
      const wss = new WebSocketServer({ server: httpServer });
      this.wss = wss;

      httpServer.on('error', (err) => {
        if (!this.wss || !this.wss._listening) reject(err);
        else log('[bridge] http error:', err.message);
      });
      httpServer.listen(this.port, () => wss.emit('listening'));

      wss.on('listening', () => resolve());
      wss.on('error', (err) => {
        if (!this.wss || !this.wss._listening) reject(err);
        else log('[bridge] ws error:', err.message);
      });

      wss.on('connection', (ws) => {
        ws.isBridge = false;
        ws.bridgeMeta = null;

        ws.on('message', (data) => this._onMessage(ws, data));
        ws.on('close', () => {
          if (this.client === ws) {
            this.client = null;
            this.meta = null;
            this.connectedAt = null;
            log('[bridge] 浏览器已断开连接');
          }
        });
        ws.on('error', () => {});
      });
    });
  }

  isConnected() {
    return !!this.client && this.client.readyState === 1;
  }

  /** 向浏览器目标发送命令；未连接返回 false */
  send(obj) {
    if (!this.isConnected()) return false;
    try {
      this.client.send(JSON.stringify(obj));
      return true;
    } catch {
      return false;
    }
  }

  _onMessage(ws, data) {
    let msg;
    try {
      msg = JSON.parse(data.toString());
    } catch {
      return; // 忽略无法解析的消息
    }
    if (!msg || typeof msg !== 'object') return;
    this.lastMessageAt = new Date();

    // 浏览器页面注册
    if (msg.type === 'hello') {
      ws.isBridge = true;
      ws.bridgeMeta = msg;
      this.client = ws;
      this.meta = msg;
      this.connectedAt = new Date();
      log(`[bridge] ✅ 浏览器已连接（${this.port} 端口）${msg.title ? '「' + msg.title + '」' : ''}`);
      return;
    }

    // 页面日志（从浏览器转发回来）
    if (msg.type === 'log') {
      const line = `[页面] ${msg.message}`;
      log(line);
      this.lastLogs.push(line);
      if (this.lastLogs.length > 200) this.lastLogs.shift();
      return;
    }

    if (msg.type === 'audio_state') {
      if (this.meta) this.meta.audioState = msg.state;
      return;
    }

    // 命令转发：任意 WS 客户端发来的命令 -> 浏览器目标
    if (COMMAND_TYPES.has(msg.type)) {
      const ok = this.send(msg);
      if (!ok) log(`[bridge] 收到 ${msg.type} 命令，但没有浏览器连接，已丢弃`);
      return;
    }

    // 未知类型
    log(`[bridge] 忽略未知消息类型: ${msg.type}`);
  }

  /** 托管 Piano 静态页面：http://localhost:7788/ -> Piano.html */
  _serveHttp(req, res) {
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      res.writeHead(405).end();
      return;
    }

    let urlPath;
    try {
      urlPath = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
    } catch {
      res.writeHead(400).end();
      return;
    }
    if (urlPath === '/' || urlPath === '') urlPath = '/Piano.html';
    if (urlPath.endsWith('/')) urlPath += 'Piano.html';

    const filePath = path.normalize(path.join(WEB_ROOT, urlPath));
    if (!filePath.startsWith(WEB_ROOT + path.sep) && filePath !== WEB_ROOT) {
      res.writeHead(403).end('Forbidden');
      return;
    }

    fs.stat(filePath, (err, stat) => {
      if (err || !stat.isFile()) {
        res.writeHead(404).end('Not Found');
        return;
      }
      const ext = path.extname(filePath);
      res.writeHead(200, {
        'Content-Type': MIME_TYPES[ext] || 'application/octet-stream',
        'Content-Length': stat.size,
        'Cache-Control': 'no-cache',
      });
      if (req.method === 'HEAD') { res.end(); return; }
      fs.createReadStream(filePath)
        .on('error', () => res.destroy())
        .pipe(res);
    });
  }
}
