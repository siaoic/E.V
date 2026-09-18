import { forwardRef, type HTMLAttributes, type ReactNode } from 'react'

import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { Layout } from './Layout'

const routerMocks = vi.hoisted(() => ({
  navigate: vi.fn(() => Promise.resolve()),
  pathname: '/',
  status: 'idle' as 'idle' | 'pending',
  subscribe: vi.fn((_event?: string, _callback?: () => void) => () => {}),
}))

const layoutMocks = vi.hoisted(() => {
  const t = (key: string, options?: { page?: string }) =>
    options?.page ? `${key}:${options.page}` : key

  return {
    t,
    announce: vi.fn(),
    checking: false,
    electron: false,
    theme: 'light' as 'light' | 'dark' | 'system',
    pageBgType: 'none' as string,
    matchesShortcut: vi.fn(() => false),
    menuSections: [] as Array<{
      title: string
      items: Array<{ path: string; label: string }>
    }>,
  }
})

vi.mock('@tanstack/react-router', () => ({
  useRouter: () => ({
    navigate: routerMocks.navigate,
    get state() {
      return { location: { pathname: routerMocks.pathname } }
    },
    subscribe: routerMocks.subscribe,
  }),
  useRouterState: ({
    select,
  }: {
    select: (state: {
      location: { pathname: string }
      status: 'idle' | 'pending'
    }) => unknown
  }) =>
    select({
      location: { pathname: routerMocks.pathname },
      status: routerMocks.status,
    }),
}))
vi.mock('motion/react', () => {
  const MotionDiv = forwardRef<
    HTMLDivElement,
    HTMLAttributes<HTMLDivElement> & {
      animate?: unknown
      initial?: unknown
      layout?: boolean | 'position' | 'size'
      transition?: unknown
      variants?: unknown
    }
  >(({ animate, initial, layout, transition, variants, ...props }, ref) => (
    <div
      ref={ref}
      data-motion-layout={layout === false ? 'false' : layout}
      data-motion-configured={
        [animate, initial, layout, transition, variants].some(Boolean) ? 'true' : undefined
      }
      {...props}
    />
  ))
  MotionDiv.displayName = 'MotionDiv'

  return {
    AnimatePresence: ({ children }: { children: ReactNode }) => children,
    motion: { div: MotionDiv },
  }
})
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: layoutMocks.t }),
}))

vi.mock('@/components/background-layer', () => ({
  BackgroundLayer: () => null,
}))
vi.mock('@/components/back-to-top', () => ({
  BackToTop: () => <div data-testid="back-to-top">BackToTop</div>,
}))
vi.mock('@/components/http-warning-banner', () => ({
  HttpWarningBanner: () => null,
}))
vi.mock('@/components/update-notice-dialog', () => ({
  UpdateNoticeDialog: () => <div data-testid="update-notice-dialog">更新公告入口</div>,
}))
vi.mock('@/components/electron/TitleBar', () => ({
  TitleBar: () => <div data-testid="electron-title-bar">TitleBar</div>,
}))
vi.mock('@/components/ui/announcer-context', () => ({
  useAnnounce: () => layoutMocks.announce,
}))
vi.mock('@/components/ui/skip-nav', () => ({
  SkipNav: () => null,
}))
vi.mock('@/components/ui/tooltip', () => ({
  TooltipProvider: ({ children }: { children: ReactNode }) => children,
}))
vi.mock('@/components/use-theme', () => ({
  useTheme: () => ({ setTheme: vi.fn(), theme: layoutMocks.theme }),
}))
vi.mock('@/hooks/use-auth', () => ({
  useAuthGuard: () => ({ checking: layoutMocks.checking }),
}))
vi.mock('@/hooks/use-background', () => ({
  useBackground: () => ({ config: { type: layoutMocks.pageBgType } }),
}))
vi.mock('@/lib/keyboard', () => ({
  matchesShortcut: layoutMocks.matchesShortcut,
}))
vi.mock('@/lib/runtime', () => ({
  isElectron: () => layoutMocks.electron,
}))
vi.mock('./Sidebar', () => ({
  Sidebar: ({
    mobileMenuOpen,
    onMobileMenuClose,
    onSearchOpenChange,
  }: {
    mobileMenuOpen: boolean
    onMobileMenuClose: () => void
    onSearchOpenChange: (open: boolean) => void
  }) => (
    <div data-testid="sidebar" data-mobile-menu-open={String(mobileMenuOpen)}>
      <button type="button" onClick={onMobileMenuClose}>
        关闭移动菜单
      </button>
      <button type="button" onClick={() => onSearchOpenChange(true)}>
        打开搜索
      </button>
    </div>
  ),
}))
vi.mock('./use-menu-sections', () => ({
  useMenuSections: () => layoutMocks.menuSections,
}))

function getWorkspaceContent(container: HTMLElement) {
  return container.querySelector('[data-dashboard-workspace-content="true"]')
}

function getMain() {
  return document.querySelector('[data-dashboard-main="true"]')
}

function getMobileOverlay() {
  return document.querySelector('.bg-black\\/50')
}

describe('Layout 壳层（Cortico 布局）', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    localStorage.clear()
    routerMocks.pathname = '/'
    routerMocks.status = 'idle'
    routerMocks.navigate.mockImplementation(() => Promise.resolve())
    layoutMocks.checking = false
    layoutMocks.electron = false
    layoutMocks.theme = 'light'
    layoutMocks.pageBgType = 'none'
    layoutMocks.menuSections = []
    layoutMocks.matchesShortcut.mockReturnValue(false)
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      return window.setTimeout(() => callback(0), 0)
    })
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation((frameId) => {
      window.clearTimeout(frameId)
    })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('认证检查中只展示校验文案，不挂载壳层', () => {
    layoutMocks.checking = true
    render(
      <Layout>
        <div>首页内容</div>
      </Layout>
    )

    expect(screen.getByText('layout.verifyingLogin')).toBeInTheDocument()
    expect(document.querySelector('[data-dashboard-shell="true"]')).not.toBeInTheDocument()
    expect(screen.queryByTestId('update-notice-dialog')).not.toBeInTheDocument()
  })

  it('挂载更新公告入口，Electron 下补 TitleBar 与顶栏留白', async () => {
    // lazy 组件的动态导入需要真实微任务队列，fake timers 会让 waitFor 永远等待
    vi.useRealTimers()
    layoutMocks.electron = true
    render(
      <Layout>
        <div>首页内容</div>
      </Layout>
    )

    await waitFor(() => {
      expect(screen.getByTestId('update-notice-dialog')).toBeInTheDocument()
    })
    expect(screen.getByTestId('electron-title-bar')).toBeInTheDocument()
    expect(document.querySelector('[data-dashboard-shell="true"]')).toHaveClass('pt-8')
  })

  it('命令面板快捷键命中后触发默认拦截，未命中则不拦截', () => {
    render(
      <Layout>
        <div>首页内容</div>
      </Layout>
    )

    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }))
    })
    expect(layoutMocks.matchesShortcut).toHaveBeenCalledWith(expect.any(KeyboardEvent), ['mod', 'k'])

    layoutMocks.matchesShortcut.mockReturnValue(true)
    const event = new KeyboardEvent('keydown', { key: 'k', metaKey: true, cancelable: true })
    act(() => {
      window.dispatchEvent(event)
    })
    expect(event.defaultPrevented).toBe(true)
  })

  it('沉浸模式隐藏侧栏与遮罩，退出后恢复侧栏', () => {
    const { container } = render(
      <Layout>
        <div>首页内容</div>
      </Layout>
    )
    expect(container.querySelector('[data-dashboard-sidebar-layout="true"]')).toBeInTheDocument()
    expect(getMobileOverlay()).not.toBeInTheDocument()

    act(() => {
      window.dispatchEvent(
        new CustomEvent('maibot-layout-immersive-change', { detail: { immersive: true } })
      )
    })
    expect(container.querySelector('[data-dashboard-sidebar-layout="true"]')).not.toBeInTheDocument()
    expect(getMobileOverlay()).not.toBeInTheDocument()

    act(() => {
      window.dispatchEvent(
        new CustomEvent('maibot-layout-immersive-change', { detail: { immersive: false } })
      )
    })
    expect(container.querySelector('[data-dashboard-sidebar-layout="true"]')).toBeInTheDocument()
  })

  it('设置工作区主区铺底色且可滚动，自定义背景时透明', () => {
    const settingsView = render(
      <Layout>
        <div>首页内容</div>
      </Layout>
    )
    expect(getMain()).toHaveClass('bg-background', 'overflow-y-auto')
    settingsView.unmount()

    layoutMocks.pageBgType = 'image'
    render(
      <Layout>
        <div>首页内容</div>
      </Layout>
    )
    expect(getMain()).toHaveClass('bg-transparent')
  })

  it('聊天工作区主区透明且不滚动', () => {
    routerMocks.pathname = '/chat'
    render(
      <Layout>
        <div>聊天内容</div>
      </Layout>
    )
    expect(getMain()).toHaveClass('bg-transparent', 'overflow-hidden')
  })

  it('工作区内容渲染在 main 内，返回顶部按钮只在设置工作区出现', () => {
    routerMocks.pathname = '/config/bot'
    const { unmount } = render(
      <Layout>
        <div>首页内容</div>
      </Layout>
    )
    expect(screen.getByTestId('back-to-top')).toBeInTheDocument()
    expect(getWorkspaceContent(document.body as HTMLElement)).not.toBeNull()
    unmount()

    routerMocks.pathname = '/chat'
    render(
      <Layout>
        <div>聊天内容</div>
      </Layout>
    )
    expect(screen.queryByTestId('back-to-top')).not.toBeInTheDocument()
  })

  it('侧栏常驻渲染（桌面端与移动端各一实例）', () => {
    render(
      <Layout>
        <div>首页内容</div>
      </Layout>
    )

    expect(screen.getAllByTestId('sidebar').length).toBe(2)
  })

  it('路由解析后更新标题、播报并在主区外时回收焦点', () => {
    let onResolved: (() => void) | undefined
    const unsubscribe = vi.fn()
    layoutMocks.menuSections = [
      {
        title: 'sidebar.groups.botConfig',
        items: [{ path: '/config/bot', label: 'sidebar.menu.botMainConfig' }],
      },
    ]
    routerMocks.subscribe.mockImplementation((_event?: string, callback?: () => void) => {
      onResolved = callback
      return unsubscribe
    })

    const { unmount } = render(
      <Layout>
        <button type="button">内部按钮</button>
      </Layout>
    )
    expect(routerMocks.subscribe).toHaveBeenCalledWith('onResolved', expect.any(Function))

    const main = document.getElementById('main-content')
    expect(main).not.toBeNull()
    const focusSpy = vi.spyOn(main as HTMLElement, 'focus')
    document.body.tabIndex = -1
    document.body.focus()

    routerMocks.pathname = '/config/bot'
    act(() => {
      onResolved?.()
    })
    act(() => {
      vi.advanceTimersByTime(0)
    })
    expect(document.title).toBe('sidebar.menu.botMainConfig — MaiBot Dashboard')
    expect(layoutMocks.announce).toHaveBeenCalledWith(
      'a11y.navigatedTo:sidebar.menu.botMainConfig',
      'polite'
    )
    expect(focusSpy).toHaveBeenCalledWith({ preventScroll: true })

    focusSpy.mockClear()
    screen.getByRole('button', { name: '内部按钮' }).focus()
    routerMocks.pathname = '/chat'
    act(() => {
      onResolved?.()
    })
    act(() => {
      vi.advanceTimersByTime(0)
    })
    expect(document.title).toBe('workspace.chat — MaiBot Dashboard')
    expect(focusSpy).not.toHaveBeenCalled()

    routerMocks.pathname = '/unknown-page'
    act(() => {
      onResolved?.()
    })
    expect(document.title).toBe('MaiBot Dashboard')
    expect(layoutMocks.announce).toHaveBeenCalledWith(
      'a11y.navigatedTo:MaiBot Dashboard',
      'polite'
    )

    unmount()
    expect(unsubscribe).toHaveBeenCalled()
  })
})
