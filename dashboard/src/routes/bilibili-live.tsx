import { ArrowLeft, Radio, RotateCcw, Save } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from '@tanstack/react-router'
import { useEffect, useRef, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { useToast } from '@/hooks/use-toast'
import { getBilibiliLiveStatus, updateLiveVoiceConfig, type LiveVoiceConfig } from '@/lib/bilibili-live-api'
import { getTtsAudioDevices } from '@/lib/config-api'

// 下拉里代表「留空」的哨兵值：Radix Select 不接受空字符串作为 item value
const DEFAULT_DEVICE_VALUE = '__mabot_default_device__'

/** 表单形状：追加音色参考在界面上是「一行一个路径」的文本框 */
interface VoiceForm {
  enabled: boolean
  base_url: string
  spk_audio: string
  spk_audio_text: string
  spk_audio_additional: string
  output_device: string
}

function toForm(voice: LiveVoiceConfig): VoiceForm {
  return {
    enabled: voice.enabled,
    base_url: voice.base_url,
    spk_audio: voice.spk_audio,
    spk_audio_text: voice.spk_audio_text,
    spk_audio_additional: voice.spk_audio_additional.join('\n'),
    output_device: voice.output_device,
  }
}

function toPayload(form: VoiceForm): LiveVoiceConfig {
  return {
    enabled: form.enabled,
    base_url: form.base_url.trim(),
    spk_audio: form.spk_audio.trim(),
    spk_audio_text: form.spk_audio_text.trim(),
    spk_audio_additional: form.spk_audio_additional
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean),
    output_device: form.output_device,
  }
}

export function BilibiliLivePage() {
  const { toast } = useToast()
  const navigate = useNavigate()
  const { data, isLoading, refetch } = useQuery({
    queryKey: ['bilibili-live-status'],
    queryFn: () => getBilibiliLiveStatus(),
  })
  // 输出设备下拉：与「聊天 → 语音朗读」共用主程序枚举出来的设备列表
  const { data: devices = [] } = useQuery({
    queryKey: ['tts-audio-devices'],
    queryFn: () => getTtsAudioDevices(),
  })

  const savedVoice = useRef<VoiceForm | null>(null)
  const [form, setForm] = useState<VoiceForm>({
    enabled: false,
    base_url: '',
    spk_audio: '',
    spk_audio_text: '',
    spk_audio_additional: '',
    output_device: '',
  })
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (data?.voice) {
      const next = toForm(data.voice)
      setForm(next)
      savedVoice.current = next
    }
  }, [data])

  const isDirty = savedVoice.current !== null && JSON.stringify(form) !== JSON.stringify(savedVoice.current)

  const bot = data?.bot

  const handleSave = async () => {
    setSaving(true)
    try {
      await updateLiveVoiceConfig(toPayload(form))
      savedVoice.current = form
      toast({ title: '已保存', description: '直播语音配置已写入 config/bilibili_live.toml' })
    } catch (error) {
      toast({
        title: '保存失败',
        description: error instanceof Error ? error.message : '无法保存直播语音配置',
        variant: 'destructive',
      })
    } finally {
      setSaving(false)
    }
  }

  const handleDiscard = () => {
    if (savedVoice.current) {
      setForm(savedVoice.current)
    } else {
      void refetch()
    }
  }

  // 配置里的设备名可能不在这台机器的设备列表里（换机迁移、设备被拔掉），补一个选项避免下拉显示空白
  const deviceNames = devices.map((device) => device.name)
  const showCurrentDevice = !!form.output_device && !deviceNames.includes(form.output_device)

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6 p-6">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="-ml-2 h-8 text-muted-foreground hover:text-foreground"
        onClick={() => navigate({ to: '/plugin-config' })}
      >
        <ArrowLeft className="mr-2 h-4 w-4" />
        返回插件管理
      </Button>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">直播语音</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            把麦麦对直播间弹幕的回复合成语音，播放到主播扬声器 / 直播间采集设备（保存到
            config/bilibili_live.toml）。弹幕接入本身由 world-bilibili 插件负责，请在「插件管理」中配置房间号与启停。
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={handleDiscard} disabled={saving || !isDirty}>
          <RotateCcw className="h-4 w-4" />
          还原
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Radio className="h-4 w-4" />
            直播语音朗读
          </CardTitle>
          <CardDescription>
            走独立朗读路径，不会触发「聊天 → 语音朗读」的通用开关，避免同一句回复播两遍。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between gap-4 rounded-md border p-3">
            <div>
              <div className="font-medium">启用直播语音</div>
              <div className="text-xs text-muted-foreground">
                {form.enabled ? '直播回复会合成语音并播放' : '直播回复不触发任何语音'}
              </div>
            </div>
            <Switch
              checked={form.enabled}
              onCheckedChange={(checked) => setForm((prev) => ({ ...prev, enabled: checked }))}
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="live-voice-base-url">语音服务地址</Label>
            <Input
              id="live-voice-base-url"
              value={form.base_url}
              placeholder="http://127.0.0.1:8095"
              onChange={(event) => setForm((prev) => ({ ...prev, base_url: event.target.value }))}
            />
            <p className="text-xs text-muted-foreground">GSV-TTS-Lite 流式服务地址（tts_service/app.py）。</p>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="live-voice-spk-audio">主参考音频</Label>
              <Input
                id="live-voice-spk-audio"
                value={form.spk_audio}
                placeholder="tts_service/refs/upload_xxx.wav"
                onChange={(event) => setForm((prev) => ({ ...prev, spk_audio: event.target.value }))}
              />
              <p className="text-xs text-muted-foreground">决定音色与说话风格；留空回退通用朗读配置。</p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="live-voice-device">输出设备</Label>
              <Select
                value={form.output_device || DEFAULT_DEVICE_VALUE}
                onValueChange={(value) =>
                  setForm((prev) => ({
                    ...prev,
                    output_device: value === DEFAULT_DEVICE_VALUE ? '' : value,
                  }))
                }
              >
                <SelectTrigger id="live-voice-device" className="w-full">
                  <SelectValue placeholder="系统默认" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={DEFAULT_DEVICE_VALUE}>系统默认</SelectItem>
                  {showCurrentDevice && (
                    <SelectItem value={form.output_device}>{form.output_device}（当前配置）</SelectItem>
                  )}
                  {devices.map((device) => (
                    <SelectItem key={device.name} value={device.name}>
                      {device.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">建议选虚拟扬声器（如 VB-CABLE）；留空回退通用朗读配置。</p>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="live-voice-spk-text">参考音频文本</Label>
            <Textarea
              id="live-voice-spk-text"
              rows={2}
              value={form.spk_audio_text}
              placeholder="主参考音频里朗读的内容，留空则由服务端使用默认"
              onChange={(event) => setForm((prev) => ({ ...prev, spk_audio_text: event.target.value }))}
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="live-voice-spk-additional">追加音色参考</Label>
            <Textarea
              id="live-voice-spk-additional"
              rows={4}
              value={form.spk_audio_additional}
              placeholder={'一行一个音频路径，无需文本\n例如 tts_service/refs/upload_xxx.wav'}
              onChange={(event) => setForm((prev) => ({ ...prev, spk_audio_additional: event.target.value }))}
            />
            <p className="text-xs text-muted-foreground">用于加强音色克隆，可多个；留空回退通用朗读配置。</p>
          </div>

          <div className="flex items-center gap-3">
            <Button onClick={handleSave} disabled={saving || isLoading || !isDirty}>
              <Save className="h-4 w-4" />
              {saving ? '保存中...' : '保存配置'}
            </Button>
            <span className="text-xs text-muted-foreground">
              {isDirty ? '有未保存的修改' : '已与配置一致'}
            </span>
          </div>
          <p className="text-xs text-muted-foreground">
            开关、音色与输出设备保存后立即生效；语音服务地址在下次启动主程序时生效。
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>直播间相关设置（只读）</CardTitle>
          <CardDescription>
            麦麦在直播间的发言频率、专注模式与专属提示词，统一在「麦麦设置」中修改。
          </CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <p className="text-sm text-muted-foreground">加载中...</p>
          ) : bot ? (
            <dl className="grid gap-3 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-muted-foreground">直播间账号</dt>
                <dd className="flex items-center gap-2 font-medium">
                  {bot.bot_account ? `${bot.bot_account}` : '未配置'}
                  {bot.platform_registered && <Badge variant="outline">已启用</Badge>}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">直播间发言频率 (talk_value)</dt>
                <dd className="font-medium">
                  {bot.live_talk_value != null ? bot.live_talk_value : '未设置'}
                  {bot.enable_talk_value_rules ? '' : '（频率规则未开启）'}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Focus 专注模式</dt>
                <dd className="font-medium">{bot.focus_mode ? '开启' : '关闭'}</dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-muted-foreground">直播间专属提示词</dt>
                <dd className="whitespace-pre-wrap rounded-md bg-muted p-3 text-sm">
                  {bot.live_prompt || '未设置'}
                </dd>
              </div>
            </dl>
          ) : (
            <p className="text-sm text-muted-foreground">读取设置失败</p>
          )}

          <div className="mt-4">
            <Button asChild variant="outline" size="sm">
              <Link to="/config/bot">前往「麦麦设置」修改</Link>
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

export default BilibiliLivePage
