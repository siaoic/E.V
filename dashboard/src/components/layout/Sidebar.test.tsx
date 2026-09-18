import type { ReactNode } from 'react'

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { LogoArea } from './LogoArea'
import { Sidebar } from './Sidebar'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { language: 'zh', changeLanguage: vi.fn() },
  }),
}))

vi.mock('@tanstack/react-router', () => ({
  useRouterState: ({ select }: { select: (state: { location: { pathname: string } }) => unknown }) =>
    select({ location: { pathname: '/' } }),
  // Link 简化为普通锚点
  Link: ({
    to,
    children,
    ...props
  }: { to: string; children?: ReactNode } & Record<string, unknown>) => (
    <a href={to} {...props}>
      {children}
    </a>
  ),
}))

vi.mock('@/components/use-theme', () => ({
  useTheme: () => ({ theme: 'light', resolvedTheme: 'light', setTheme: vi.fn() }),
  toggleThemeWithTransition: vi.fn(),
}))

vi.mock('@/hooks/use-background', () => ({
  useBackground: () => ({
    config: { type: 'none', effects: {}, customCSS: '' },
    inheritedFrom: 'page',
  }),
}))

vi.mock('@/components/background-layer', () => ({
  BackgroundLayer: ({ layerId }: { layerId: string }) => (
    <div data-testid={`background-${layerId}`} />
  ),
}))

vi.mock('@/lib/auth', () => ({
  logout: vi.fn(async () => {}),
}))

vi.mock('@/components/ui/scroll-area', () => ({
  ScrollArea: ({
    children,
    className,
  }: {
    children: ReactNode
    className?: string
  }) => (
    <div className={className}>{children}</div>
  ),
}))

vi.mock('@/components/ui/button', () => ({
  Button: ({
    children,
    onClick,
    asChild,
    title,
    className,
  }: {
    children: ReactNode
    onClick?: (event: unknown) => void
    asChild?: boolean
    title?: string
    className?: string
  }) =>
    asChild ? (
      <span title={title} className={className}>
        {children}
      </span>
    ) : (
      <button type="button" title={title} className={className} onClick={onClick}>
        {children}
      </button>
    ),
}))

vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenu: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DropdownMenuTrigger: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DropdownMenuContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DropdownMenuItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}))

vi.mock('./NavItem', () => ({
  NavItem: ({
    item,
    onMobileMenuClose,
  }: {
    item: { path: string; label: string }
    onMobileMenuClose: () => void
  }) => (
    <button type="button" data-path={item.path} onClick={onMobileMenuClose}>
      {item.label}
    </button>
  ),
}))

vi.mock('./use-menu-sections', () => ({
  useMenuSections: () => [
    {
      title: 'sidebar.groups.botConfig',
      items: [{ path: '/config/bot', label: 'sidebar.menu.botMainConfig' }],
    },
    {
      title: 'sidebar.groups.botResources',
      items: [
        { path: '/resource/emoji', label: 'sidebar.menu.emojiManagement' },
        { path: '/resource/expression', label: 'sidebar.menu.expressionManagement' },
      ],
    },
  ],
}))

describe('LogoArea 与 Sidebar', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('品牌区渲染 wordmark 和光谱彩条（光谱条由皮肤 CSS 控制显隐）', () => {
    render(<LogoArea sidebarOpen />)

    expect(screen.getByText(/maib/)).toBeInTheDocument()
    expect(document.querySelector('[data-dashboard-logo-spectrum="true"]')).toBeInTheDocument()
  })

  it('渲染主导航组、分组标题与导航项，点击导航项触发回调', () => {
    const onMobileMenuClose = vi.fn()
    const { container } = render(
      <Sidebar mobileMenuOpen={false} onMobileMenuClose={onMobileMenuClose} onSearchOpenChange={vi.fn()} />
    )

    const aside = container.querySelector('[data-dashboard-sidebar="true"]')
    expect(aside).toHaveClass('lg:translate-x-0')
    expect(aside).toHaveAttribute('data-dashboard-sidebar-mobile-open', 'false')
    expect(screen.getByRole('navigation')).toHaveAttribute('aria-label', 'a11y.sidebarNav')
    // 分组标题按 Cortico 样式渲染（大写小标题）
    expect(screen.getByText('sidebar.groups.botConfig')).toBeInTheDocument()
    expect(screen.getByText('sidebar.groups.botResources')).toBeInTheDocument()
    // 分组分隔线
    expect(container.querySelector('[data-dashboard-nav-group-divider="true"]')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'sidebar.menu.botMainConfig' }))
    expect(onMobileMenuClose).toHaveBeenCalledOnce()
  })

  it('移动端收起时侧栏平移隐藏，桌面端常驻', () => {
    const { container } = render(
      <Sidebar mobileMenuOpen={false} onMobileMenuClose={vi.fn()} onSearchOpenChange={vi.fn()} />
    )

    const aside = container.querySelector('[data-dashboard-sidebar="true"]')
    expect(aside).toHaveClass('-translate-x-full', 'lg:translate-x-0')
  })

  it('railfoot 渲染搜索 / 文档 / 设置 / 登出操作按钮', () => {
    const onSearchOpenChange = vi.fn()
    render(
      <Sidebar mobileMenuOpen={false} onMobileMenuClose={vi.fn()} onSearchOpenChange={onSearchOpenChange} />
    )

    const railfoot = document.querySelector('[data-dashboard-railfoot="true"]')
    expect(railfoot).toBeInTheDocument()

    fireEvent.click(screen.getByTitle('header.searchPlaceholder'))
    expect(onSearchOpenChange).toHaveBeenCalledWith(true)
    expect(screen.getByTitle('header.viewDocs')).toBeInTheDocument()
    expect(screen.getByTitle('sidebar.menu.settings')).toBeInTheDocument()
    expect(screen.getByTitle('header.logout')).toBeInTheDocument()
  })
})
