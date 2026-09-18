import { Link, useRouterState } from '@tanstack/react-router'
import { useCallback, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  BookOpen,
  Check,
  Globe,
  LogOut,
  Moon,
  Search,
  Settings,
  Sun,
} from 'lucide-react'

import { BackgroundLayer } from '@/components/background-layer'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useTheme } from '@/components/use-theme'
import { toggleThemeWithTransition } from '@/components/use-theme'
import { useBackground } from '@/hooks/use-background'
import { logout } from '@/lib/auth'
import { cn } from '@/lib/utils'

import { primaryNavSection } from './constants'
import { LogoArea } from './LogoArea'
import { NavItem } from './NavItem'
import { useMenuSections } from './use-menu-sections'

interface SidebarProps {
  mobileMenuOpen: boolean
  onMobileMenuClose: () => void
  onSearchOpenChange: (open: boolean) => void
}

const LANGUAGE_CODES = ['zh', 'en', 'ja', 'ko'] as const
const LANGUAGE_NAMES: Record<(typeof LANGUAGE_CODES)[number], string> = {
  zh: '中文',
  en: 'English',
  ja: '日本語',
  ko: '한국어',
}

/**
 * Cortico 风格侧栏（rail）：
 * 品牌区 → 主导航组（无标题）→ 分组导航（大写小标题 + hairline 分隔）→ 底部操作区。
 * 底部操作区承接原顶栏的搜索 / 文档 / 语言 / 主题 / 设置 / 登出入口。
 */
export function Sidebar({ mobileMenuOpen, onMobileMenuClose, onSearchOpenChange }: SidebarProps) {
  const { t, i18n: i18nInstance } = useTranslation()
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  const { resolvedTheme, setTheme } = useTheme()
  const menuSections = useMenuSections()
  // 用户自定义侧栏背景（外观设置），未配置或继承页面时使用 Cortico 纸面底色
  const { config: sidebarBg, inheritedFrom } = useBackground('sidebar')
  const hasCustomSidebarBackground = inheritedFrom !== 'page' && sidebarBg.type !== 'none'
  const [languageMenuOpen, setLanguageMenuOpen] = useState(false)
  const currentLang = i18nInstance.language || 'zh'

  const handleLogout = useCallback(async () => {
    await logout()
  }, [])

  const renderNavItem = (
    item: (typeof menuSections)[number]['items'][number],
    keyPrefix: string
  ) => (
    <NavItem
      key={`${keyPrefix}-${item.path}`}
      item={item}
      onMobileMenuClose={onMobileMenuClose}
    />
  )

  return (
    <aside
      data-dashboard-sidebar="true"
      data-dashboard-sidebar-mobile-open={mobileMenuOpen ? 'true' : 'false'}
      className={cn(
        'fixed inset-y-0 left-0 isolate z-50 flex h-full w-[var(--layout-sidebar-width)] flex-col transition-transform duration-300 motion-reduce:transition-none lg:relative lg:z-0 lg:translate-x-0',
        hasCustomSidebarBackground
          ? 'bg-[hsl(var(--color-card))]'
          : 'bg-[hsl(var(--cortico-sidebar-bg))]',
        mobileMenuOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'
      )}
    >
      {/* 自定义侧栏背景层（外观设置中配置） */}
      {hasCustomSidebarBackground && <BackgroundLayer config={sidebarBg} layerId="sidebar" />}
      {/* 品牌区 */}
      <div className="relative z-10">
        <LogoArea sidebarOpen />
      </div>

      {/* 导航区 */}
      <ScrollArea
        scrollbars="vertical"
        className="relative z-10 min-h-0 flex-1 overflow-x-hidden"
        viewportClassName="[&>div]:!block"
      >
        <nav aria-label={t('a11y.sidebarNav')} className="px-[10px] pb-5 pt-4">
          <ul className="flex flex-col">
            {/* 主导航组：终端类入口，无分组标题 */}
            <li>
              <ul
                className="flex flex-col gap-px"
                data-dashboard-nav-group="primary"
              >
                {primaryNavSection.items.map((item) => renderNavItem(item, 'primary'))}
              </ul>
            </li>

            {menuSections.map((section) => (
              <li key={section.title}>
                <div
                  aria-hidden="true"
                  data-dashboard-nav-group-divider="true"
                  className="my-[14px] border-t border-[hsl(var(--color-border))] opacity-78"
                />
                {/* 分组标题：11px 大写字距排印 */}
                <h3
                  data-dashboard-sidebar-section-title="true"
                  className="text-muted-foreground mb-[7px] px-[10px] text-[11px] font-semibold tracking-[0.07em] whitespace-nowrap uppercase"
                >
                  {t(section.title)}
                </h3>
                <ul className="flex flex-col gap-px">
                  {section.items.map((item) => renderNavItem(item, section.title))}
                </ul>
              </li>
            ))}
          </ul>
        </nav>
      </ScrollArea>

      {/* 底部操作区（railfoot） */}
      <div
        data-dashboard-railfoot="true"
        className="relative z-10 flex shrink-0 flex-col gap-[9px] border-t px-4 pt-[10px] pb-3.5"
      >
        {/* 第一行：搜索 / 文档 / 语言 / 主题 */}
        <div className="flex items-center gap-[3px]">
          <RailActionButton
            label={t('header.searchPlaceholder')}
            onClick={() => onSearchOpenChange(true)}
          >
            <Search className="h-[17px] w-[17px]" />
          </RailActionButton>
          <RailActionButton
            label={t('header.viewDocs')}
            onClick={() => window.open('https://docs.mai-mai.org', '_blank')}
          >
            <BookOpen className="h-[17px] w-[17px]" />
          </RailActionButton>
          <div className="hidden sm:block">
            <DropdownMenu open={languageMenuOpen} onOpenChange={setLanguageMenuOpen}>
              <DropdownMenuTrigger asChild>
                <RailActionButton label={t('header.switchLanguage')} asChild>
                  <span>
                    <Globe className="h-[17px] w-[17px]" />
                  </span>
                </RailActionButton>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" side="top">
                {LANGUAGE_CODES.map((code) => (
                  <DropdownMenuItem
                    key={code}
                    onClick={() => i18nInstance.changeLanguage(code)}
                    className={cn(
                      'cursor-pointer',
                      currentLang.split('-')[0] === code && 'text-primary font-semibold'
                    )}
                  >
                    {currentLang.split('-')[0] === code && <Check className="mr-2 h-3 w-3" />}
                    {LANGUAGE_NAMES[code]}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
          <RailActionButton
            label={resolvedTheme === 'dark' ? t('header.switchToLight') : t('header.switchToDark')}
            onClick={(event) => {
              const newTheme = resolvedTheme === 'dark' ? 'light' : 'dark'
              toggleThemeWithTransition(newTheme, setTheme, event)
            }}
          >
            {resolvedTheme === 'dark' ? (
              <Sun className="h-[17px] w-[17px]" />
            ) : (
              <Moon className="h-[17px] w-[17px]" />
            )}
          </RailActionButton>
        </div>

        {/* 第二行：设置 / 登出 */}
        <div className="flex items-center gap-[3px]">
          <RailActionButton asChild label={t('sidebar.menu.settings')}>
            <Link to="/settings" data-active={pathname === '/settings' ? 'true' : 'false'}>
              <Settings className="h-[17px] w-[17px]" />
            </Link>
          </RailActionButton>
          <RailActionButton label={t('header.logout')} variant="danger" onClick={() => void handleLogout()}>
            <LogOut className="h-[17px] w-[17px]" />
          </RailActionButton>
          {/* 语言按钮（移动端始终展示） */}
          <div className="sm:hidden">
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <RailActionButton label={t('header.switchLanguage')} asChild>
                  <span>
                    <Globe className="h-[17px] w-[17px]" />
                  </span>
                </RailActionButton>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" side="top">
                {LANGUAGE_CODES.map((code) => (
                  <DropdownMenuItem
                    key={code}
                    onClick={() => i18nInstance.changeLanguage(code)}
                    className={cn(
                      'cursor-pointer',
                      currentLang.split('-')[0] === code && 'text-primary font-semibold'
                    )}
                  >
                    {currentLang.split('-')[0] === code && <Check className="mr-2 h-3 w-3" />}
                    {LANGUAGE_NAMES[code]}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </div>
    </aside>
  )
}

interface RailActionButtonProps {
  label: string
  variant?: 'default' | 'danger'
  onClick?: (event: React.MouseEvent<HTMLButtonElement>) => void
  asChild?: boolean
  children: React.ReactNode
}

/**
 * railfoot 的 34px 图标按钮：透明底、10px 圆角、悬停纸面染色；
 * danger 变体用于登出（悬停染 danger 色）。
 */
function RailActionButton({
  label,
  variant = 'default',
  onClick,
  asChild,
  children,
}: RailActionButtonProps) {
  const className = cn(
    'relative inline-flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[var(--visual-radius-md)] p-0 transition-colors duration-150 focus-visible:ring-ring focus-visible:ring-2 focus-visible:outline-none',
    variant === 'danger'
      ? 'hover:bg-[hsl(var(--cortico-danger)/0.12)] hover:text-[hsl(var(--cortico-danger))]'
      : 'hover:bg-[hsl(var(--cortico-hover))] hover:text-foreground'
  )

  if (asChild) {
    return (
      <Button
        type="button"
        variant="ghost"
        size="icon"
        asChild
        title={label}
        aria-label={label}
        className={className}
      >
        {children}
      </Button>
    )
  }

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      title={label}
      aria-label={label}
      className={className}
      onClick={onClick}
    >
      {children}
    </Button>
  )
}
