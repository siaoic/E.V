/**
 * World 框架状态 API（「世界状态」页数据源，全部只读）
 *
 * 数据来自主程序世界基座（src/worlds/）与 VTuber 口型桥（src/maisaka/vtuber_bridge/）；
 * 请求样板（认证、解析、错误格式化）由 @/lib/http 请求客户端承担。
 */
import { backendApi } from '@/lib/http'

const API_BASE = '/api/webui/worlds'

export interface WorldDescriptorInfo {
  name: string
  display_name: string
  plugin_id: string
  polls_changes: boolean
  supports_delayed: boolean
  delayed_active: boolean
}

export interface WorldsOverview {
  enabled: boolean
  live_platform: string
  stream_id: string
  inject_state: boolean
  stale_after_seconds: number
  event_throttle_seconds: number
  change_poll_seconds: number
  delayed_sources: string[]
  delayed_seconds: number
  bound_session_id: string
  pending_event_count: number
  worlds: WorldDescriptorInfo[]
}

export interface WorldStateView {
  name: string
  display_name: string
  realtime: string
  delayed: string
}

export interface WorldEventRecord {
  world: string
  event_type: string
  trigger: string
  text: string
  delivered: boolean
  reason: string
  at: string
}

export interface WorldToolRecord {
  tool_name: string
  session_id: string
  timestamp: string
}

export interface VtuberSnapshot {
  mode: string
  enabled: boolean
  connected: boolean
  vts_ws_url: string
  mouth_param: string
  mouth_value: number
  uptime_sec: number
  last_error: string
  recent_emotes: { name: string; vts_name: string; duration_sec: number; at: string }[]
}

export async function getWorldsOverview(): Promise<WorldsOverview> {
  return backendApi.get<WorldsOverview>(`${API_BASE}/overview`)
}

export async function getWorldsState(): Promise<{ worlds: WorldStateView[] }> {
  return backendApi.get<{ worlds: WorldStateView[] }>(`${API_BASE}/state`)
}

export async function getWorldsEvents(): Promise<{ events: WorldEventRecord[]; pending_count: number }> {
  return backendApi.get<{ events: WorldEventRecord[]; pending_count: number }>(`${API_BASE}/events`)
}

export async function getWorldsTools(): Promise<{ tools: WorldToolRecord[]; error?: string }> {
  return backendApi.get<{ tools: WorldToolRecord[]; error?: string }>(`${API_BASE}/tools`)
}

export async function getVtuberStatus(): Promise<VtuberSnapshot> {
  return backendApi.get<VtuberSnapshot>(`${API_BASE}/vtuber`)
}
