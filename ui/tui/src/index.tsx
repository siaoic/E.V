/**
 * E.V TUI 入口（严格参考 dsh-TUI Chat.tsx + StatusLine.tsx 视觉结构）。
 *
 * 复用 dsh 的 ink core + Gentle Mist Blue 主题（theme.ts darkTheme 调色板），
 * 颜色用 theme key，与 dsh-TUI 截图完全一致。
 *
 * 视觉结构（从上到下）：
 *   1. 用户消息 / AI 回复（带 briefLabelYou / briefLabelClaude 标签）
 *   2. 思考指示（"思考 · 6s (ctrl+o 展开)" —— 仅 AI 工作时显示）
 *   3. 工具执行区（子代理卡片 + 彩色状态点 ●）
 *   4. 流式回复（AI 正在输出的文本）
 *   5. 输入框（promptBorder 边框 + claude 提示符）
 *   6. 状态栏（模型 · TPS · 缓存 · tokens · ctx 进度条）
 *
 * 与 Python 后端通信协议：
 *   TUI → Python stdin：  {"method":"send","text":"用户输入"}
 *   Python → TUI stdout：
 *     {"type":"log","level":"ok|info|warn|fail|dim","text":"..."}
 *     {"type":"assistant_chunk","text":"..."}
 *     {"type":"status","model":"...","tps":123,"cache":0.95,"tokens_in":1000,"tokens_out":500,"ctx_used":0.02,"ctx_total":1.0}
 */
import React, { useEffect, useRef, useState } from 'react'
import { render, Box, Text, useInput, useApp, ThemeProvider } from './ui.js'
import type { Theme } from './ui.js'
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { EventEmitter } from 'node:events'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import figures from 'figures'

const here = dirname(fileURLToPath(import.meta.url))
const PROJECT_ROOT = join(here, '..', '..', '..')
const PYTHON_ENTRY = join(PROJECT_ROOT, 'main.py')

// ============================================================
// JSON-RPC over stdio
// ============================================================
type RpcEvent =
  | { type: 'log'; level: 'ok' | 'warn' | 'fail' | 'info' | 'dim'; text: string }
  | { type: 'assistant_chunk'; text: string }
  | {
      type: 'status'
      model?: string
      tps?: number
      cache?: number
      tokens_in?: number
      tokens_out?: number
      ctx_used?: number
      ctx_total?: number
      working?: boolean
      thinking_ms?: number
    }

const rpc = new EventEmitter()
let pyProc: ChildProcessWithoutNullStreams | null = null
let pyAlive = false
let pyBuffer = ''

function startPython(args: string[] = []) {
  const env = { ...process.env, RUN_MODE: 'tui' }
  pyProc = spawn('python', [PYTHON_ENTRY, ...args], {
    cwd: PROJECT_ROOT,
    env,
    stdio: ['pipe', 'pipe', 'inherit'],
  })
  pyAlive = true

  pyProc.stdout?.on('data', (chunk: Buffer) => {
    pyBuffer += chunk.toString('utf8')
    let idx: number
    while ((idx = pyBuffer.indexOf('\n')) >= 0) {
      const line = pyBuffer.slice(0, idx).trim()
      pyBuffer = pyBuffer.slice(idx + 1)
      if (!line) continue
      try {
        const evt = JSON.parse(line) as RpcEvent
        rpc.emit('event', evt)
      } catch {
        rpc.emit('event', { type: 'log', level: 'info', text: line })
      }
    }
  })

  pyProc.on('error', err => {
    pyAlive = false
    rpc.emit('event', {
      type: 'log', level: 'fail',
      text: `[ev-tui] Python 启动失败：${err.message}`,
    })
  })
  pyProc.on('exit', (code, signal) => {
    pyAlive = false
    rpc.emit('event', {
      type: 'status', working: false,
    })
    rpc.emit('event', {
      type: 'log', level: 'dim',
      text: `[ev-tui] Python 退出（code=${code}, signal=${signal}）`,
    })
  })
}

function sendToPython(text: string) {
  if (!pyProc?.stdin?.writable || !pyAlive) return
  pyProc.stdin.write(JSON.stringify({ method: 'send', text }) + '\n')
}

function gracefulExit(exit: () => void) {
  try { pyProc?.stdin?.end() } catch { /* noop */ }
  try { pyProc?.kill('SIGTERM') } catch { /* noop */ }
  exit()
}

// ============================================================
// 状态管理
// ============================================================
type Message = { role: 'user' | 'ai' | 'system'; text: string }
type LogLine = { level: string; text: string }
type Status = {
  model: string
  tps: number
  cache: number
  tokens_in: number
  tokens_out: number
  ctx_used: number
  ctx_total: number
  working: boolean
  thinking_ms: number
}

// log level → theme key（darkTheme 调色板）
const LEVEL_COLOR: Record<string, keyof Theme> = {
  ok: 'success',       // #82B89D
  warn: 'warning',     // #D8B270
  fail: 'error',       // #DA8A93
  info: 'claude',      // #7DA1DE
  dim: 'subtle',       // #5E6673
}

// 子代理状态点颜色
const SUBAGENT_DOT_RUNNING: keyof Theme = 'subagentStatusRunning'     // #7DA1DE
const SUBAGENT_DOT_DONE: keyof Theme = 'subagentStatusCompleted'       // #82B89D
const SUBAGENT_DOT_FAILED: keyof Theme = 'subagentStatusFailed'        // #DA8A93
const SUBAGENT_BULLET: keyof Theme = 'subagentBullet'                  // #D194AE

// log level → 子代理状态映射
const LEVEL_TO_SUBAGENT_DOT: Record<string, keyof Theme> = {
  ok: SUBAGENT_DOT_DONE,
  info: SUBAGENT_DOT_RUNNING,
  fail: SUBAGENT_DOT_FAILED,
  warn: SUBAGENT_DOT_FAILED,
  dim: 'inactive',
}

// ============================================================
// 辅助：格式化 tokens / TPS gauge / context bar
// ============================================================
function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

function tpsGauge(tps: number, peak?: number): string {
  // dsh 风格 TPS 仪表：5 格条形图
  const p = peak ?? Math.max(tps, 200)
  const ratio = Math.min(1, tps / p)
  const filled = Math.round(ratio * 5)
  const empty = 5 - filled
  return '█'.repeat(filled) + '░'.repeat(empty)
}

function contextBar(used: number, total: number, width: number): string {
  if (total <= 0) return '░'.repeat(width)
  const ratio = Math.min(1, used / total)
  const filled = Math.round(ratio * width)
  const empty = width - filled
  // 已用部分用蓝色，剩余用灰色
  const bar = '█'.repeat(filled) + '░'.repeat(empty)
  return bar
}

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rs = s % 60
  return `${m}m${rs}s`
}

// ============================================================
// slash 命令表
// ============================================================
type SlashCommand = {
  name: string
  description: string
  tag?: string
  resolve?: (args: string[]) => 'local' | string
}

const SLASH_COMMANDS: SlashCommand[] = [
  { name: 'help', description: '显示快捷键和命令帮助' },
  { name: 'quit', description: '退出 E.V TUI', tag: 'alias /exit' },
  { name: 'exit', description: '退出 E.V TUI' },
  { name: 'q', description: '退出 E.V TUI', tag: 'alias /exit' },
  { name: 'clear', description: '清空消息列表' },
  {
    name: 'plugins', description: '查看插件状态（映射 !plugins）',
    resolve: args => `!plugins ${args.join(' ')}`.trim(),
  },
  {
    name: 'delegation', description: '查看后台委派任务（映射 !delegation）',
    resolve: args => `!delegation ${args.join(' ')}`.trim(),
  },
  {
    name: 'doctor', description: '开播自检（映射 !doctor）',
    resolve: () => '!doctor',
  },
  {
    name: 'perf', description: '辅助调用记账（映射 !perf）',
    resolve: () => '!perf',
  },
  {
    name: 'journey', description: '学习星图（映射 !journey）',
    resolve: () => '!journey',
  },
]

// ============================================================
// 主组件
// ============================================================
function App() {
  const [messages, setMessages] = useState<Message[]>([])
  const [logs, setLogs] = useState<LogLine[]>([])
  const [input, setInput] = useState('')
  const [showHelp, setShowHelp] = useState(false)
  const [showLogs, setShowLogs] = useState(true)
  const [streaming, setStreaming] = useState('')
  const streamingRef = useRef('')
  const [status, setStatus] = useState<Status>({
    model: '', tps: 0, cache: 0, tokens_in: 0, tokens_out: 0,
    ctx_used: 0, ctx_total: 0, working: false, thinking_ms: 0,
  })
  const thinkingStartRef = useRef<number | null>(null)
  const { exit } = useApp()

  // 启动 Python 后端 + 订阅事件
  useEffect(() => {
    startPython(process.argv.slice(2))
    const onEvent = (evt: RpcEvent) => {
      switch (evt.type) {
        case 'log':
          setLogs(l => [...l, { level: evt.level, text: evt.text }])
          break
        case 'assistant_chunk': {
          streamingRef.current += evt.text
          setStreaming(streamingRef.current)
          break
        }
        case 'status': {
          setStatus(s => ({
            model: evt.model ?? s.model,
            tps: evt.tps ?? s.tps,
            cache: evt.cache ?? s.cache,
            tokens_in: evt.tokens_in ?? s.tokens_in,
            tokens_out: evt.tokens_out ?? s.tokens_out,
            ctx_used: evt.ctx_used ?? s.ctx_used,
            ctx_total: evt.ctx_total ?? s.ctx_total,
            working: evt.working ?? s.working,
            thinking_ms: evt.thinking_ms ?? s.thinking_ms,
          }))
          break
        }
      }
    }
    rpc.on('event', onEvent)
    return () => {
      rpc.off('event', onEvent)
      try { pyProc?.stdin?.end() } catch { /* noop */ }
      try { pyProc?.kill('SIGTERM') } catch { /* noop */ }
    }
  }, [])

  const flushStreaming = () => {
    if (streamingRef.current) {
      const text = streamingRef.current
      streamingRef.current = ''
      setStreaming('')
      setMessages(m => [...m, { role: 'ai', text }])
    }
  }

  // 工作状态跟踪（用于思考指示）
  const [thinkingElapsed, setThinkingElapsed] = useState<number | null>(null)
  useEffect(() => {
    if (status.working) {
      if (thinkingStartRef.current === null) thinkingStartRef.current = Date.now()
      const timer = setInterval(() => {
        if (thinkingStartRef.current !== null) {
          setThinkingElapsed(Date.now() - thinkingStartRef.current)
        }
      }, 500)
      return () => clearInterval(timer)
    } else {
      thinkingStartRef.current = null
      setThinkingElapsed(null)
    }
  }, [status.working])

  // 快捷键
  useInput((ch, key) => {
    if (key.ctrl && ch === 'c') { gracefulExit(exit); return }
    if (key.ctrl && ch === 'l') { setMessages([]); setLogs([]); return }
    if (key.escape) {
      if (showHelp) { setShowHelp(false); return }
      if (input) { setInput(''); return }
      return
    }
    if (key.return) {
      const text = input.trim()
      setInput('')
      if (!text) return
      if (text.startsWith('/')) {
        const [name, ...rest] = text.slice(1).split(/\s+/)
        const cmd = SLASH_COMMANDS.find(c => c.name === name)
        if (!cmd) {
          setMessages(m => [...m, { role: 'system', text: `未知命令：/${name}（输入 /help 查看）` }])
          return
        }
        switch (name) {
          case 'help': setShowHelp(s => !s); break
          case 'quit': case 'exit': case 'q': gracefulExit(exit); break
          case 'clear': setMessages([]); break
          default: {
            flushStreaming()
            const pyCmd = cmd.resolve ? cmd.resolve(rest) : `!${name}`
            if (pyCmd === 'local') return
            setMessages(m => [...m, { role: 'user', text }])
            sendToPython(pyCmd)
          }
        }
        return
      }
      flushStreaming()
      setMessages(m => [...m, { role: 'user', text }])
      sendToPython(text)
      return
    }
    if (key.backspace || key.delete) { setInput(s => s.slice(0, -1)); return }
    if (ch && !key.ctrl && !key.meta && !key.escape) { setInput(s => s + ch) }
  })

  // ============================================================
  // 渲染
  // ============================================================
  const ctxPct = status.ctx_total > 0
    ? ((status.ctx_used / status.ctx_total) * 100).toFixed(1)
    : '0.0'
  const ctxLabel = `ctx ${ctxPct}% (${fmtTokens(status.ctx_used)}/${fmtTokens(status.ctx_total)})`

  return (
    <Box flexDirection="column" paddingX={1}>
      {/* ===== 主对话区 ===== */}
      <Box flexDirection="column" marginTop={1}>
        {messages.map((m, i) => (
          <Box key={i} flexDirection="column">
            {m.role === 'user' ? (
              <Text color="text">{m.text}</Text>
            ) : m.role === 'ai' ? (
              <Box flexDirection="row">
                <Text color="briefLabelClaude" bold>{'> '}</Text>
                <Text color="text">{m.text}</Text>
              </Box>
            ) : (
              <Text color="subtle">{m.text}</Text>
            )}
          </Box>
        ))}

        {/* 思考指示（仅工作时显示，参考 dsh SpinnerAnimationRow） */}
        {thinkingElapsed !== null && (
          <Box flexDirection="row" marginTop={1}>
            <Text color="subtle">思考 · {fmtDuration(thinkingElapsed)} (ctrl+o 展开)</Text>
          </Box>
        )}

        {/* 工具执行区：启动日志面板（参考 dsh 工具卡片样式） */}
        {showLogs && logs.length > 0 && (
          <Box flexDirection="column" marginTop={1}>
            {logs.map((l, i) => {
              const dotColor = LEVEL_TO_SUBAGENT_DOT[l.level] ?? 'inactive'
              const icon = l.level === 'fail'
                ? figures.cross
                : l.level === 'ok'
                  ? figures.tick
                  : figures.bullet
              return (
                <Box key={i} flexDirection="row">
                  <Text color={dotColor}>{icon} </Text>
                  <Text color={LEVEL_COLOR[l.level] ?? 'text'}>{l.text}</Text>
                </Box>
              )
            })}
          </Box>
        )}

        {/* 流式回复 */}
        {streaming && (
          <Box flexDirection="row">
            <Text color="briefLabelClaude" bold>{'> '}</Text>
            <Text color="text">{streaming}</Text>
          </Box>
        )}
      </Box>

      {/* ===== 输入框（参考 dsh PromptInput：promptBorder 边框） ===== */}
      <Box
        flexDirection="row"
        marginTop={1}
        borderStyle="single"
        borderColor="promptBorder"
        paddingX={1}
      >
        <Text color="claude" bold>›</Text>
        <Text> </Text>
        <Text color="text">{input}</Text>
        <Text color="promptBorder">▏</Text>
      </Box>

      {/* ===== 状态栏（参考 dsh StatusLine） ===== */}
      <Box flexDirection="column" marginTop={1}>
        {/* ctx 进度条 */}
        {status.ctx_total > 0 && (
          <Box flexDirection="row">
            <Text color="inactiveShimmer">{ctxLabel} </Text>
            <Text>{contextBar(status.ctx_used, status.ctx_total, 40)}</Text>
          </Box>
        )}
        {/* 状态栏行 */}
        <Box flexDirection="row" justifyContent="space-between">
          {/* 左侧：模型 · TPS · 缓存 */}
          <Box flexDirection="row" gap={2}>
            {status.model && (
              <Text color="inactiveShimmer">{status.model}</Text>
            )}
            {status.tps > 0 && (
              <Text color="inactiveShimmer">
                <Text color="claude">{tpsGauge(status.tps, 200)}</Text>{' '}
                <Text dimColor>{Math.round(status.tps)} tps</Text>
              </Text>
            )}
            {status.cache > 0 && (
              <Text color="inactiveShimmer">
                <Text dimColor>缓存</Text>{(status.cache * 100).toFixed(1)}%
              </Text>
            )}
            {status.tokens_in + status.tokens_out > 0 && (
              <Text color="inactiveShimmer">
                {fmtTokens(status.tokens_in)}→{fmtTokens(status.tokens_out)} tok
              </Text>
            )}
          </Box>
          {/* 右侧：会话 ID */}
          <Box flexDirection="row">
            <Text color="subtle">E.V TUI</Text>
          </Box>
        </Box>
      </Box>

      {/* ===== 帮助菜单 ===== */}
      {showHelp && (
        <Box
          flexDirection="column"
          marginTop={1}
          borderStyle="single"
          borderColor="permission"
          paddingX={1}
        >
          <Text bold color="claude">命令</Text>
          {SLASH_COMMANDS.map(cmd => (
            <Box key={cmd.name} flexDirection="row">
              <Text color="briefLabelYou">/{cmd.name}</Text>
              {cmd.tag && <Text color="subtle"> [{cmd.tag}]</Text>}
              <Text color="text"> — {cmd.description}</Text>
            </Box>
          ))}
        </Box>
      )}
    </Box>
  )
}

// ThemeProvider 包裹
render(
  <ThemeProvider>
    <App />
  </ThemeProvider>
)
