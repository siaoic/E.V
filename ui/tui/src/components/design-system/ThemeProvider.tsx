import React, { createContext, useContext, useEffect, useState } from 'react'
import { setActiveThemeName, type ThemeName } from '../../theme.js'

/**
 * E.V TUI 精简版 ThemeProvider —— 复用 dsh 的 Gentle Mist Blue 调色板，
 * 但不引入 dsh 的 OSC 11 终端背景检测 / customTheme / themePrefs 依赖链。
 *
 * 默认锁定 `dark` 调色板（dsh 截图同款），ThemedBox/ThemedText 通过
 * useTheme() 拿到主题名，再经 getTheme(name) 解析为 darkTheme 调色板，
 * 颜色与 dsh-TUI 截图完全一致。
 *
 * 运行时切换：DSH_TUI_THEME 环境变量（兼容 dsh）可选 dark/light/dark-ansi，
 * 或通过 setTheme() 切换（E.V TUI 暂不暴露 /theme 命令）。
 */

type ThemeContextValue = {
  theme: string
  setTheme: (name: string) => boolean
}

const ThemeContext = createContext<ThemeContextValue>({
  theme: 'dark',
  setTheme: () => false,
})

/** 内置主题名白名单（不解析用户主题，避免 customTheme 依赖） */
const BUILTIN = new Set(['dark', 'light', 'dark-ansi'])

export function ThemeProvider({
  children,
}: {
  children: React.ReactNode
}): React.ReactNode {
  const [theme, setThemeName] = useState<string>(() => {
    const v = process.env.DSH_TUI_THEME
    return v && BUILTIN.has(v) ? v : 'dark'
  })

  useEffect(() => {
    setActiveThemeName(theme as ThemeName)
  }, [theme])

  const setTheme = React.useCallback((name: string): boolean => {
    if (!BUILTIN.has(name)) return false
    setThemeName(name)
    return true
  }, [])

  const value = React.useMemo(() => ({ theme, setTheme }), [theme, setTheme])
  return (
    <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
  )
}

/**
 * 返回 [themeName, setTheme]。ThemedBox/ThemedText 用 themeName 经
 * getTheme(themeName) 解析为具体调色板。
 */
export function useTheme(): [string, (name: string) => boolean] {
  const { theme, setTheme } = useContext(ThemeContext)
  return [theme, setTheme]
}
