import { useId } from 'react'
import { motion, useReducedMotion } from 'motion/react'

import { cn } from '@/lib/utils'

interface LogoAreaProps {
  sidebarOpen?: boolean
}

const SIDEBAR_SPECTRUM_TRANSITION = {
  duration: 0.22,
  ease: [0.22, 1, 0.36, 1] as const,
}

/**
 * Cortico 风格品牌区：
 * 纸面底色上的小写排印 wordmark，"o" 使用 accent 强调色；
 * 彩虹光谱条仅在 future-retro 皮肤下展示（modern 下通过 CSS 隐藏）。
 */
export function LogoArea({ sidebarOpen = true }: LogoAreaProps) {
  const spectrumLayoutId = useId()
  const prefersReducedMotion = useReducedMotion()
  const spectrumTransition = prefersReducedMotion
    ? { duration: 0 }
    : SIDEBAR_SPECTRUM_TRANSITION

  return (
    <div
      data-dashboard-logo-area="true"
      className="flex h-[var(--layout-sidebar-logo-height)] shrink-0 items-center px-[var(--layout-sidebar-logo-padding-x)]"
    >
      <div className="relative flex w-full items-center justify-start overflow-visible">
        <div
          className={cn(
            'flex shrink-0 items-center justify-start transition-opacity duration-[220ms] motion-reduce:transition-none',
            !sidebarOpen && 'lg:pointer-events-none lg:opacity-0'
          )}
        >
          {/* Cortico 式 wordmark：小写排印 + accent 强调 */}
          <span
            data-dashboard-logo-title="true"
            className="whitespace-nowrap leading-[1.3] tracking-[-0.025em]"
          >
            maib<span className="logo-accent">o</span>t
          </span>
          {sidebarOpen && (
            <motion.span
              layoutId={`sidebar-logo-spectrum-${spectrumLayoutId}`}
              aria-hidden="true"
              data-dashboard-logo-spectrum="true"
              className="mt-2 flex h-2 w-28 max-w-full translate-y-1 items-end"
              transition={spectrumTransition}
            >
              {Array.from({ length: 6 }, (_, index) => (
                <motion.span
                  key={index}
                  layoutId={`sidebar-logo-spectrum-band-${spectrumLayoutId}-${index}`}
                  transition={spectrumTransition}
                />
              ))}
            </motion.span>
          )}
        </div>
      </div>
    </div>
  )
}
