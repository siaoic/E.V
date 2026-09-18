/**
 * use-auth 模块测试：useAuthGuard 认证守卫与 checkAuth / checkFirstSetup 辅助函数。
 *
 * use-auth 内部有模块级的 30 秒认证状态缓存与并发请求去重 Promise，
 * 为避免跨用例污染，每个用例都通过 vi.resetModules + 动态 import 拿到全新模块实例。
 */
import { renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

// vi.hoisted 保证 mock 工厂内引用的是同一批稳定的 vi.fn（模块重置后依然复用）
const { navigateMock, ensureAuthenticatedMock, authApiGetMock } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  ensureAuthenticatedMock: vi.fn(),
  authApiGetMock: vi.fn(),
}))

vi.mock('@tanstack/react-router', () => ({
  // useNavigate 始终返回同一个 navigate 引用，保持 useEffect 依赖稳定
  useNavigate: () => navigateMock,
}))

vi.mock('@/lib/auth', () => ({
  // 守卫只依赖 ensureAuthenticated：内部已封装 check → 静默重登 → 返回最终认证态
  ensureAuthenticated: ensureAuthenticatedMock,
  getAuthStatus: ensureAuthenticatedMock,
}))

vi.mock('@/lib/http', () => ({
  authApi: { get: authApiGetMock },
}))

/** 每次拿到全新的 use-auth 模块实例（清空模块级缓存与去重 Promise） */
async function loadUseAuth() {
  vi.resetModules()
  return await import('../use-auth')
}

describe('useAuthGuard', () => {
  it('静默认证失败（无记住的 token）时跳转到 /auth 并结束检查态', async () => {
    ensureAuthenticatedMock.mockResolvedValue(false)
    const { useAuthGuard } = await loadUseAuth()

    const { result } = renderHook(() => useAuthGuard())
    // 无缓存时初始处于检查态
    expect(result.current.checking).toBe(true)

    await waitFor(() => expect(result.current.checking).toBe(false))
    expect(navigateMock).toHaveBeenCalledWith({ to: '/auth' })
  })

  it('静默认证成功时直接放行，不发生跳转（含要求自定义 Token 的场景）', async () => {
    ensureAuthenticatedMock.mockResolvedValue(true)
    const { useAuthGuard } = await loadUseAuth()

    const { result } = renderHook(() => useAuthGuard())
    await waitFor(() => expect(result.current.checking).toBe(false))
    expect(navigateMock).not.toHaveBeenCalled()
  })

  it('静默认证流程抛错时按未认证处理，跳转到 /auth', async () => {
    ensureAuthenticatedMock.mockRejectedValue(new Error('网络错误'))
    const { useAuthGuard } = await loadUseAuth()

    const { result } = renderHook(() => useAuthGuard())
    await waitFor(() => expect(result.current.checking).toBe(false))
    expect(navigateMock).toHaveBeenCalledWith({ to: '/auth' })
  })

  it('30 秒内命中缓存时初始即为非检查态，且不再触发静默认证', async () => {
    ensureAuthenticatedMock.mockResolvedValue(true)
    const { useAuthGuard } = await loadUseAuth()

    // 第一次挂载：真实请求并写入缓存
    const first = renderHook(() => useAuthGuard())
    await waitFor(() => expect(first.result.current.checking).toBe(false))
    expect(ensureAuthenticatedMock).toHaveBeenCalledTimes(1)
    first.unmount()

    // 第二次挂载：命中缓存，初始就不处于检查态，也不再发请求
    const second = renderHook(() => useAuthGuard())
    expect(second.result.current.checking).toBe(false)
    expect(ensureAuthenticatedMock).toHaveBeenCalledTimes(1)
    expect(navigateMock).not.toHaveBeenCalled()
  })

  it('缓存超过 30 秒后失效，重新触发静默认证', async () => {
    // 用 Date.now 桩控制缓存时间戳（restoreMocks 会在用例结束后还原）
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(1_000)
    ensureAuthenticatedMock.mockResolvedValue(true)
    const { useAuthGuard } = await loadUseAuth()

    const first = renderHook(() => useAuthGuard())
    await waitFor(() => expect(first.result.current.checking).toBe(false))
    expect(ensureAuthenticatedMock).toHaveBeenCalledTimes(1)
    first.unmount()

    // 前进 31 秒：缓存过期，重新进入检查态并再次请求
    nowSpy.mockReturnValue(32_001)
    const second = renderHook(() => useAuthGuard())
    expect(second.result.current.checking).toBe(true)
    await waitFor(() => expect(second.result.current.checking).toBe(false))
    expect(ensureAuthenticatedMock).toHaveBeenCalledTimes(2)
  })

  it('并发挂载共享同一请求，静默认证只执行一次', async () => {
    ensureAuthenticatedMock.mockResolvedValue(true)
    const { useAuthGuard } = await loadUseAuth()

    // 两个 hook 同步挂载：第二个应复用第一个尚未落定的请求 Promise
    const a = renderHook(() => useAuthGuard())
    const b = renderHook(() => useAuthGuard())
    await waitFor(() => expect(a.result.current.checking).toBe(false))
    await waitFor(() => expect(b.result.current.checking).toBe(false))
    expect(ensureAuthenticatedMock).toHaveBeenCalledTimes(1)
  })
})

describe('checkAuth', () => {
  it('返回静默认证结果（结果写入 30 秒模块缓存）', async () => {
    ensureAuthenticatedMock.mockResolvedValue(true)
    let { checkAuth } = await loadUseAuth()
    await expect(checkAuth()).resolves.toBe(true)

    // 缓存命中：即使 mock 改为未认证，30 秒内仍返回缓存结果
    ensureAuthenticatedMock.mockResolvedValue(false)
    await expect(checkAuth()).resolves.toBe(true)

    // 缓存过期后重新走静默认证，拿到新结果
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(32_001)
    checkAuth = (await loadUseAuth()).checkAuth
    await expect(checkAuth()).resolves.toBe(false)
    nowSpy.mockRestore()
  })
})

describe('checkFirstSetup', () => {
  it('请求 setup 状态接口并返回 is_first_setup', async () => {
    authApiGetMock.mockResolvedValue({ is_first_setup: true })
    const { checkFirstSetup } = await loadUseAuth()

    await expect(checkFirstSetup()).resolves.toBe(true)
    expect(authApiGetMock).toHaveBeenCalledWith('/api/webui/setup/status')
  })

  it('接口失败时记录错误并返回 false', async () => {
    // 静默并断言错误日志（该函数吞掉异常并返回 false）
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    authApiGetMock.mockRejectedValue(new Error('请求失败'))
    const { checkFirstSetup } = await loadUseAuth()

    await expect(checkFirstSetup()).resolves.toBe(false)
    expect(consoleErrorSpy).toHaveBeenCalled()
  })
})
