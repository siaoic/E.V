/**
 * 认证流程工具：登出与认证状态探测。
 *
 * 走 authApi 实例（携带 Cookie 但 401 不跳转）——
 * 在这两个场景里 401 / 未认证是正常业务结果，不应触发整页跳转。
 *
 * 静默自动登录：登录成功过的 token 会持久化到 localStorage，
 * 之后打开页面若 Cookie 已失效，会用记住的 token 自动重新认证，
 * 不再出现登录页；仅在首次使用或 token 失效时才需要手动输入。
 */
import { authApi } from '@/lib/http'

export interface AuthStatus {
  authenticated: boolean
  token_source?: string
  requires_custom_token?: boolean
}

export interface SetupStatus {
  is_first_setup: boolean
  token_source: string
  requires_custom_token: boolean
  message?: string
}

export interface TokenVerifyResult {
  valid: boolean
  message?: string
}

const REMEMBERED_TOKEN_KEY = 'maibot-remembered-token'

function readRememberedToken(): string | null {
  try {
    return localStorage.getItem(REMEMBERED_TOKEN_KEY)
  } catch {
    return null
  }
}

function saveRememberedToken(token: string): void {
  try {
    localStorage.setItem(REMEMBERED_TOKEN_KEY, token)
  } catch {
    // localStorage 不可用（如隐私模式）时静默跳过，仅失去自动登录能力
  }
}

export { saveRememberedToken }

function clearRememberedToken(): void {
  try {
    localStorage.removeItem(REMEMBERED_TOKEN_KEY)
  } catch {
    // 忽略存储异常
  }
}

/**
 * 调用登出接口、清除记住的 token 并跳转到登录页
 */
export async function logout(): Promise<void> {
  try {
    await authApi.post('/api/webui/auth/logout', { parse: 'response' })
  } catch (error) {
    console.error('登出请求失败:', error)
  }
  clearRememberedToken()
  // 无论成功与否都跳转到登录页
  window.location.href = '/auth'
}

/**
 * 使用 token 向后端验证并写入认证 Cookie
 */
export async function verifyToken(token: string): Promise<TokenVerifyResult> {
  try {
    const data = await authApi.post<{ valid?: boolean; message?: string }>(
      '/api/webui/auth/verify',
      { body: { token } }
    )
    return { valid: data.valid === true, message: data.message }
  } catch (error) {
    console.error('Token 验证请求失败:', error)
    return { valid: false }
  }
}

/**
 * 检查当前认证状态
 */
export async function checkAuthStatus(): Promise<boolean> {
  return (await getAuthStatus()).authenticated
}

/**
 * 确保处于已认证状态：
 * 未认证时用记住的 token 静默重登一次，避免每次打开页面都看到登录页。
 */
export async function ensureAuthenticated(): Promise<boolean> {
  const status = await getAuthStatus()
  if (status.authenticated) {
    return true
  }

  const remembered = readRememberedToken()
  if (!remembered) {
    return false
  }

  const result = await verifyToken(remembered)
  if (result.valid) {
    return true
  }

  // 记住的 token 已失效，清掉避免反复尝试
  clearRememberedToken()
  return false
}

/**
 * 获取当前认证状态和 Token 来源
 */
export async function getAuthStatus(): Promise<AuthStatus> {
  try {
    const data = await authApi.get<AuthStatus>('/api/webui/auth/check')
    return {
      authenticated: data.authenticated === true,
      token_source: data.token_source,
      requires_custom_token: data.requires_custom_token === true,
    }
  } catch {
    return { authenticated: false }
  }
}

/**
 * 手动登录：验证 token，成功后持久化以供后续静默登录
 */
export async function loginWithToken(token: string): Promise<TokenVerifyResult> {
  const result = await verifyToken(token)
  if (result.valid) {
    saveRememberedToken(token)
  }
  return result
}

/**
 * 获取首次配置状态和 Token 来源
 */
export async function getSetupStatus(): Promise<SetupStatus | null> {
  try {
    return await authApi.get<SetupStatus>('/api/webui/setup/status')
  } catch {
    return null
  }
}
