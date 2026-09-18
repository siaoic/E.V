import { Link, useMatchRoute } from '@tanstack/react-router'
import { useTranslation } from 'react-i18next'

import { cn } from '@/lib/utils'

import type { MenuItem } from './types'

interface NavItemProps {
  item: MenuItem
  onMobileMenuClose: () => void
}

/**
 * Cortico 风格导航项：
 * 36px 最小高度、10px 圆角、16px 线性图标；
 * 悬停为中性纸面染色，激活为 accent 染色 + accent 图标。
 */
export function NavItem({ item, onMobileMenuClose }: NavItemProps) {
  const { t } = useTranslation()
  const matchRoute = useMatchRoute()
  const isActive = item.external ? false : matchRoute({ to: item.path })
  const Icon = item.icon
  const label = t(item.label)

  const linkClassName = cn(
    'relative flex min-h-[var(--layout-sidebar-nav-item-height)] items-center gap-[9px] rounded-[var(--visual-radius-md)] px-[var(--layout-sidebar-nav-item-padding-x)] py-[7px] transition-colors duration-150',
    'hover:bg-[hsl(var(--cortico-hover))] hover:text-foreground',
    isActive
      ? 'bg-[hsl(var(--cortico-active))] text-foreground'
      : 'text-muted-foreground hover:text-foreground'
  )
  const commonLinkProps = {
    'data-tour': item.tourId,
    'data-dashboard-nav-item': 'true',
    'data-active': isActive ? 'true' : 'false',
    className: linkClassName,
    onClick: onMobileMenuClose,
  } as const

  const menuItemContent = (
    <>
      <Icon
        data-dashboard-nav-icon="true"
        className={cn('h-4 w-4 shrink-0', isActive ? 'text-primary' : 'text-current opacity-80')}
        size={16}
      />
      <span
        data-dashboard-nav-label="true"
        className={cn(
          'min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-sm',
          isActive ? 'font-medium' : 'font-normal'
        )}
        title={label}
      >
        {label}
      </span>
    </>
  )

  const link = item.external ? (
    <a href={item.path} target="_blank" rel="noopener noreferrer" {...commonLinkProps}>
      {menuItemContent}
    </a>
  ) : (
    <Link to={item.path} {...commonLinkProps}>
      {menuItemContent}
    </Link>
  )

  return <li className="relative">{link}</li>
}
