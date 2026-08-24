/**
 * E.V TUI public surface —— 复用 dsh 的 ink core + Gentle Mist Blue 主题。
 *
 * 与 dsh src/ui.ts 一致：Box/Text 是 ThemedBox/ThemedText（theme-aware），
 * 颜色用 theme key（如 color="claude" / "success" / "subtle"），由
 * theme.ts 的 darkTheme 调色板解析为 Gentle Mist Blue 真彩色，与 dsh
 * 截图完全一致。ThemeProvider 是精简版（不引入 OSC 11 检测/customTheme）。
 */
export { default as render, renderSync, createRoot } from './ink/root.js'
export type { RenderOptions, Instance, Root } from './ink/root.js'
export { ThemeProvider, useTheme } from './components/design-system/ThemeProvider.js'
export { default as Box } from './components/design-system/ThemedBox.js'
export { default as Text } from './components/design-system/ThemedText.js'
export { default as Spacer } from './ink/components/Spacer.js'
export { default as Newline, type Props as NewlineProps } from './ink/components/Newline.js'
export { NoSelect } from './ink/components/NoSelect.js'
export { AlternateScreen } from './ink/components/AlternateScreen.js'
export {
  default as ScrollBox,
  type ScrollBoxProps,
  type ScrollBoxHandle,
} from './ink/components/ScrollBox.js'
export { default as useInput } from './ink/hooks/use-input.js'
export { useCopyOnSelect } from './ink/hooks/use-copy-on-select.js'
export { default as useStdin } from './ink/hooks/use-stdin.js'
export { default as useApp } from './ink/hooks/use-app.js'
export { useAnimationFrame } from './ink/hooks/use-animation-frame.js'
export { useTerminalSize } from './ink/hooks/use-terminal-size.js'
export { Ansi } from './ink/Ansi.js'
export type { Key } from './ink/events/input-event.js'
// 重新导出 Theme 类型，方便组件用 keyof Theme 做颜色 prop
export type { Theme } from './theme.js'
