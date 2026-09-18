import { useEffect, useState } from 'react'
import { useNavigate } from '@tanstack/react-router'

import { ensureAuthenticated, getAuthStatus } from '@/lib/auth'
import { authApi } from '@/lib/http'

const AUTH_STATUS_CACHE_MS = 30_000
let cachedAuthStatus: (Awaited<ReturnType<typeof getAuthStatus>> & { checkedAt: number }) | null =
  null
let authEntryPromise: Promise<boolean> | null = null

function readCachedAuthStatus() {
  if (!cachedAuthStatus) {
    return undefined
  }
  if (Date.now() - cachedAuthStatus.checkedAt > AUTH_STATUS_CACHE_MS) {
    cachedAuthStatus = null
    return undefined
  }
  return cachedAuthStatus
}

/**
 * 确保进入应用前已认证：
 * Cookie 失效时用记住的 token 静默重登，成功则不出现登录页。
 * 返回 true 表示已认证（含静默重登成功）。
 */
function ensureEntryAuthenticated(): Promise<boolean> {
  authEntryPromise ??= ensureAuthenticated().finally(() => {
    authEntryPromise = null
  })
  return authEntryPromise
}

export function useAuthGuard() {
  const navigate = useNavigate()
  const [checking, setChecking] = useState(() => {
    const cached = readCachedAuthStatus()
    return cached?.authenticated !== true
  })

  useEffect(() => {
    let cancelled = false
    const cached = readCachedAuthStatus()
    if (cached?.authenticated === true) {
      setChecking(false)
      return () => {
        cancelled = true
      }
    }

    const verifyAuth = async () => {
      try {
        const authenticated = await ensureEntryAuthenticated()
        cachedAuthStatus = { authenticated, checkedAt: Date.now() }
        if (cancelled) {
          return
        }
        if (!authenticated) {
          navigate({ to: '/auth' })
        }
      } catch {
        // 发生错误时也跳转到登录页
        if (!cancelled) {
          navigate({ to: '/auth' })
        }
      } finally {
        if (!cancelled) {
          setChecking(false)
        }
      }
    }

    verifyAuth()

    return () => {
      cancelled = true
    }
  }, [navigate])

  return { checking }
}

/**
 * 检查是否已认证（异步）；未认证时尝试用记住的 token 静默重登
 */
export async function checkAuth(): Promise<boolean> {
  const cached = readCachedAuthStatus()
  if (cached?.authenticated === true) {
    return true
  }
  const authenticated = await ensureEntryAuthenticated()
  cachedAuthStatus = { authenticated, checkedAt: Date.now() }
  return authenticated
}

/**
 * 检查是否需要首次配置
 */
export async function checkFirstSetup(): Promise<boolean> {
  try {
    const data = await authApi.get<{ is_first_setup: boolean }>('/api/webui/setup/status')
    return data.is_first_setup
  } catch (error) {
    console.error('检查首次配置状态失败:', error)
    return false
  }
}
