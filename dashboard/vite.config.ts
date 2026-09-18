import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import path from 'path'

import { dashboardVersionDefine } from './app-version'

// https://vite.dev/config/
export default defineConfig({
  plugins: [tailwindcss(), react()],
  define: dashboardVersionDefine,
  server: {
    host: '127.0.0.1',
    port: 7999,
    watch: {
      // 依赖目录的本地备份不应进入 Vite 文件监听，否则会占用大量句柄并导致服务无响应。
      ignored: ['**/node_modules.mixed-backup-*/**'],
    },
    allowedHosts: ['sengokucolad.tail1e46b9.ts.net'],
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8001',  // WebUI 后端服务器
        changeOrigin: true,
        ws: true,
        // 确保 Cookie 正确转发
        cookieDomainRewrite: '',  // 移除域名限制
        cookiePathRewrite: '/',   // 确保路径一致
      },
      '/maibot_statistics.html': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
    // CodeMirror 扩展依赖同一份 state/view 单例；混用包管理器时重复实例会导致扩展解析失败。
    dedupe: ['@codemirror/state', '@codemirror/view'],
  },
  optimizeDeps: {
    include: ['react', 'react-dom'],
  },
  build: {
    rollupOptions: {
      output: {
        // 不再使用 manualChunks 手动分包：它会把 React 拆进多个 chunk，
        // 模块依赖图变化时形成循环初始化（生产环境整页白屏，dev 模式正常）。
        // 交给 Vite 默认策略按依赖图自动切分，保证单例与初始化顺序正确。
        manualChunks: undefined,
      },
    },
    // 合并分包后主 chunk 体积增大，放宽警告阈值避免噪声告警
    chunkSizeWarningLimit: 2000,
  },
})
