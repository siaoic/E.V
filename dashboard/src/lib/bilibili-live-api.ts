/**
 * B站直播（bilibili_live）语音配置 API
 *
 * 弹幕接入（房间号、SESSDATA、启停）已由 world-bilibili 插件负责，在其插件配置页维护；
 * 本文件只覆盖直播语音朗读（config/bilibili_live.toml 的 [voice] 节）。
 * 请求样板（认证、解析、错误格式化）由 @/lib/http 请求客户端承担；
 * 本文件只声明 endpoint 与响应类型。
 */
import { backendApi } from '@/lib/http'

const API_BASE = '/api/webui/bilibili_live'

export interface LiveVoiceConfig {
  enabled: boolean
  base_url: string
  spk_audio: string
  spk_audio_text: string
  spk_audio_additional: string[]
  output_device: string
}

export interface BilibiliLiveBotInfo {
  platform_registered: boolean
  bot_account: string
  focus_mode: boolean
  enable_talk_value_rules: boolean
  rule_item_id: string
  live_talk_value: number | null
  live_prompt: string
}

export interface BilibiliLiveStatusPayload {
  success: boolean
  voice: LiveVoiceConfig
  bot: BilibiliLiveBotInfo
}

export async function getBilibiliLiveStatus(): Promise<BilibiliLiveStatusPayload> {
  return backendApi.get<BilibiliLiveStatusPayload>(API_BASE)
}

export async function updateLiveVoiceConfig(
  payload: LiveVoiceConfig
): Promise<{ success: boolean; message: string }> {
  return backendApi.post<{ success: boolean; message: string }>(`${API_BASE}/voice`, {
    body: payload,
  })
}
