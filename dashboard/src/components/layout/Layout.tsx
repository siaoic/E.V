import { lazy, Suspense, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useRouter, useRouterState } from '@tanstack/react-router'
import { AnimatePresence, motion } from 'motion/react'
import { Menu, Moon, Sun } from 'lucide-react'

import { BackgroundLayer } from '@/components/background-layer'
import { BackToTop } from '@/components/back-to-top'
import { HttpWarningBanner } from '@/components/http-warning-banner'
import { SkipNav } from '@/components/ui/skip-nav'
import { useAnnounce } from '@/components/ui/announcer-context'
import { TooltipProvider } from '@/components/ui/tooltip'
import { useTheme, toggleThemeWithTransition } from '@/components/use-theme'
import { useAuthGuard } from '@/hooks/use-auth'
import { useBackground } from '@/hooks/use-background'

import { TitleBar } from '@/components/electron/TitleBar'
import { matchesShortcut } from '@/lib/keyboard'
import { isElectron } from '@/lib/runtime'
import { cn } from '@/lib/utils'
import { Sidebar } from './Sidebar'
import type { LayoutProps, WorkspaceMode } from './types'
import { useMenuSections } from './use-menu-sections'

const LAYOUT_IMMERSIVE_EVENT = 'maibot-layout-immersive-change'
const PAGE_TRANSITION_DURATION_MS = 180
const UpdateNoticeDialog = lazy(() =>
  import('@/components/update-notice-dialog').then((module) => ({
    default: module.UpdateNoticeDialog,
  }))
)

/** 移动端顶栏的主题切换按钮 */
function MobileThemeToggle() {
  const { t } = useTranslation()
  const { resolvedTheme, setTheme } = useTheme()

  return (
    <button
      type="button"
      onClick={(event) => {
        const newTheme = resolvedTheme === 'dark' ? 'light' : 'dark'
        toggleThemeWithTransition(newTheme, setTheme, event)
      }}
      aria-label={resolvedTheme === 'dark' ? t('header.switchToLight') : t('header.switchToDark')}
      className="hover:bg-[hsl(var(--cortico-hover))] rounded-[var(--visual-radius-md)] p-2 transition-colors"
    >
      {resolvedTheme === 'dark' ? (
        <Sun className="h-5 w-5" />
      ) : (
        <Moon className="h-5 w-5" />
      )}
    </button>
  )
}

export function Layout({ children }: LayoutProps) {
  const { t } = useTranslation()
  const { checking } = useAuthGuard() // 检查认证状态
  const router = useRouter()
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  const announce = useAnnounce()
  const isLogsPath =
    pathname === '/logs' || pathname === '/statistics' || pathname.startsWith('/reasoning-process')
  const workspaceMode: WorkspaceMode = pathname === '/chat' ? 'chat' : isLogsPath ? 'logs' : 'settings'
  const isSettingsWorkspace = workspaceMode === 'settings'
  const showBackToTop = isSettingsWorkspace && pathname !== '/planner-monitor'

  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [, setSearchOpen] = useState(false)
  // 沉浸模式（专注陪伴等）下隐藏侧栏，退出时恢复
  const [immersive, setImmersive] = useState(false)
  const menuSections = useMenuSections()

  // 搜索快捷键监听（Cmd/Ctrl + K）
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (matchesShortcut(e, ['mod', 'k'])) {
        e.preventDefault()
        setSearchOpen(true)
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [])

  // 沉浸模式事件：进入时收起侧栏，退出时恢复
  useEffect(() => {
    const handleImmersiveChange = (event: Event) => {
      const detail = (event as CustomEvent<{ immersive?: boolean }>).detail
      setImmersive(detail?.immersive === true)
      if (detail?.immersive === true) {
        setMobileMenuOpen(false)
      }
    }

    window.addEventListener(LAYOUT_IMMERSIVE_EVENT, handleImmersiveChange)
    return () => window.removeEventListener(LAYOUT_IMMERSIVE_EVENT, handleImmersiveChange)
  }, [])

  // 路由变更：焦点管理 + 屏幕阅读器播报 + document.title 更新
  useEffect(() => {
    // 构建 路径 -> 页面标题 的映射表（以当前语言 t() 翻译）
    const pathToLabel: Record<string, string> = {}
    for (const section of menuSections) {
      for (const item of section.items) {
        pathToLabel[item.path] = t(item.label)
      }
    }
    pathToLabel['/chat'] = t('workspace.chat')
    pathToLabel['/planner-monitor'] = t('sidebar.menu.maisakaMonitor')
    pathToLabel['/focus'] = t('sidebar.menu.focusCompanion')
    pathToLabel['/logs'] = t('workspace.logs')
    pathToLabel['/reasoning-process'] = t('sidebar.menu.reasoningProcess')

    return router.subscribe('onResolved', () => {
      const pageTitle = pathToLabel[router.state.location.pathname] ?? 'MaiBot Dashboard'
      const fullTitle =
        pageTitle === 'MaiBot Dashboard' ? 'MaiBot Dashboard' : `${pageTitle} — MaiBot Dashboard`

      // 更新 document.title
      document.title = fullTitle

      // 屏幕阅读器朗读导航结果
      announce(t('a11y.navigatedTo', { page: pageTitle }), 'polite')

      // 将焦点移到主内容区（仅当焦点不在其内部时）
      const mainEl = document.getElementById('main-content')
      if (mainEl && !mainEl.contains(document.activeElement)) {
        // requestAnimationFrame 确保 DOM 已渲染完成
        requestAnimationFrame(() => {
          mainEl.focus({ preventScroll: true })
        })
      }
    })
  }, [router, announce, t, menuSections])

  const { config: pageBg } = useBackground('page')

  // 认证检查中，显示加载状态
  if (checking) {
    return (
      <div className="bg-background flex h-screen items-center justify-center">
        <div className="text-muted-foreground">{t('layout.verifyingLogin')}</div>
      </div>
    )
  }

  return (
    <TooltipProvider delayDuration={300}>
      <SkipNav />
      {isElectron() && <TitleBar />}
      <div
        data-dashboard-shell="true"
        className={cn(
          'relative isolate flex h-[100dvh] overflow-hidden overscroll-none',
          isElectron() && 'pt-8'
        )}
      >
        <BackgroundLayer config={pageBg} layerId="page" />
        <div className="relative z-10 flex h-full min-h-0 w-full overflow-hidden">
          {/* Cortico 式常驻侧栏：桌面端固定宽度，沉浸模式下隐藏 */}
          {!immersive && (
            <div
              data-dashboard-sidebar-layout="true"
              className="hidden shrink-0 lg:block"
              style={{ width: 'var(--layout-sidebar-width)' }}
            >
              <Sidebar
                mobileMenuOpen={mobileMenuOpen}
                onMobileMenuClose={() => setMobileMenuOpen(false)}
                onSearchOpenChange={setSearchOpen}
              />
            </div>
          )}

          {/* 移动端 Sidebar 走自己的 fixed 定位，通过 mobileMenuOpen 控制显隐 */}
          {!immersive && (
            <div className="lg:hidden">
              <Sidebar
                mobileMenuOpen={mobileMenuOpen}
                onMobileMenuClose={() => setMobileMenuOpen(false)}
                onSearchOpenChange={setSearchOpen}
              />
            </div>
          )}

          {/* Mobile overlay */}
          <AnimatePresence>
            {mobileMenuOpen && (
              <motion.div
                aria-hidden="true"
                className="fixed inset-0 z-40 bg-black/50 lg:hidden"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.18 }}
                onClick={() => setMobileMenuOpen(false)}
              />
            )}
          </AnimatePresence>

          {/* Main content */}
          <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
            {/* HTTP 安全警告横幅 */}
            <HttpWarningBanner />

            {/* 移动端顶栏：汉堡入口 + 品牌 + 主题切换（桌面端由侧栏承接） */}
            <div
              data-dashboard-mobile-topbar="true"
              className="bg-[hsl(var(--cortico-sidebar-bg))] flex h-12 shrink-0 items-center justify-between border-b px-3 lg:hidden"
            >
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => setMobileMenuOpen(true)}
                  aria-label={t('a11y.openMenu')}
                  className="hover:bg-[hsl(var(--cortico-hover))] rounded-[var(--visual-radius-md)] p-2 transition-colors"
                >
                  <Menu className="h-5 w-5" />
                </button>
                <span
                  data-dashboard-logo-title="true"
                  className="text-foreground text-lg leading-[1.3] tracking-[-0.025em]"
                >
                  maib<span className="logo-accent">o</span>t
                </span>
              </div>
              <MobileThemeToggle />
            </div>

            {/* Page content */}
            <main
              id="main-content"
              data-dashboard-main="true"
              tabIndex={-1}
              className={cn(
                'relative isolate min-h-0 flex-1 outline-none',
                isSettingsWorkspace
                  ? 'overflow-y-auto overflow-x-hidden overscroll-contain'
                  : 'overflow-hidden',
                workspaceMode === 'chat'
                  ? 'bg-transparent'
                  : pageBg.type === 'none'
                    ? 'bg-background'
                    : 'bg-transparent'
              )}
            >
              <motion.div
                key={workspaceMode}
                data-dashboard-workspace-content="true"
                className="relative z-10 h-full min-h-full min-w-0"
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{
                  duration: PAGE_TRANSITION_DURATION_MS / 1000,
                  ease: [0.22, 1, 0.36, 1],
                }}
              >
                {children}
              </motion.div>
            </main>

            {/* Back to Top Button */}
            {showBackToTop && <BackToTop />}
          </div>
        </div>
      </div>
      <Suspense fallback={null}>
        <UpdateNoticeDialog />
      </Suspense>
    </TooltipProvider>
  )
}
