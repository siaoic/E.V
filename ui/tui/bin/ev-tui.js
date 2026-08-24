#!/usr/bin/env node
/**
 * ev-tui 启动器（瘦壳，参考 dsh bin/dsh-tui.js 的双态启动器设计）。
 *
 * 职责：
 *   1. spawn `node --import tsx/esm src/index.tsx`（TUI 渲染进程）；
 *   2. 透传 stdio + 退出码（forwardExit）；
 *   3. 零 lib 依赖（启动器迁移契约）。
 *
 * TUI 渲染进程内部 spawn `python main.py`（Python 后端）+ JSON-RPC 通信。
 *
 * 用法：
 *   node bin/ev-tui.js                 # 直接运行
 *   ev-tui                              # npm install -g 后
 *   node bin/ev-tui.js --profile demo   # 透传参数
 */
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
// here = bin/；tuiRoot = ui/tui/（tsx 与 ink 依赖都在 ui/tui/node_modules）
const tuiRoot = join(here, '..')
const entry = join(tuiRoot, 'src', 'index.tsx')

// 强制 production（参考 dsh bin/dsh-tui.js L160）
process.env.NODE_ENV ??= 'production'

// cwd 设为 tuiRoot：node 从 ui/tui/node_modules 解析 tsx 等依赖，
// 这样从项目根或其他目录跑也能正确加载。
const child = spawn(process.execPath,
  ['--import', 'tsx/esm', entry, ...process.argv.slice(2)],
  { stdio: 'inherit', env: process.env, cwd: tuiRoot })

child.on('error', err => {
  console.error(`[ev-tui] 启动失败：${err.message}`)
  process.exit(1)
})
child.on('exit', (code, signal) => {
  if (signal) process.kill(process.pid, signal)
  else process.exit(code ?? 0)
})
