// 视觉验收用轻量 mock 后端：仅支撑 WebUI 登录态与基础页面渲染（127.0.0.1:8001）
// 注意：backendApi 不剥信封，除 config/bot（可带 config 字段）外均返回裸数据
const http = require('http')

const server = http.createServer((req, res) => {
  const url = req.url || ''
  console.log('[mock]', req.method, url)
  const send = (code, body) => {
    res.writeHead(code, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify(body))
  }

  if (url.includes('/auth/check')) return send(200, { authenticated: true, token_source: 'mock' })
  if (url.includes('/setup/status'))
    return send(200, { is_first_setup: false, token_source: 'mock', requires_custom_token: false })
  if (url.includes('/auth/login') || url.includes('/auth/verify'))
    return send(200, { authenticated: true })
  if (url.includes('/auth/logout')) return send(200, { ok: true })
  if (url.includes('/config/bot')) return send(200, { config: { experimental: {}, debug: {} } })
  if (url.includes('/version-compatibility')) return send(200, { compatible: true })
  if (url.includes('/update-notice')) return send(200, { has_notice: false })
  if (url.includes('/reasoning-process/stages'))
    return send(200, { stages: [], stage_infos: [] })
  if (url.includes('/reasoning-process'))
    return send(200, {
      items: [],
      total: 0,
      page: 1,
      page_size: 20,
      stages: [],
      stage_infos: [],
      sessions: [],
      session_infos: [],
      selected_session: '',
    })
  if (url.includes('/local-cache/database'))
    return send(200, { total_size: 0, files: [], tables: [], table_sizes: [] })
  if (url.includes('/local-cache'))
    return send(200, { directories: [], database: { total_size: 0, files: [], tables: [] } })
  if (url.startsWith('/api')) {
    if (req.method === 'GET') return send(200, [])
    return send(200, { ok: true })
  }
  send(404, { error: 'not found' })
})

server.listen(8001, '127.0.0.1', () => console.log('mock backend on 8001'))
